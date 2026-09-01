import hashlib
import html
import json
import re
import secrets
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from nonebot import logger

from .config import plugin_config
from .data import HeyboxParseResult, ImageContent, VideoContent

# 小黑盒 API 动态签名所需的加密字符集
HKEY_ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _hkey_mix(v: int) -> int:
    return ((v << 1) ^ 27) & 255 if v & 128 else v << 1


def _hkey_qm(v: int) -> int:
    return _hkey_mix(v) ^ v


def _hkey_dollar(v: int) -> int:
    return _hkey_qm(_hkey_mix(v))


def _hkey_ym(v: int) -> int:
    return _hkey_dollar(_hkey_qm(_hkey_mix(v)))


def _hkey_gm(v: int) -> int:
    return _hkey_ym(v) ^ _hkey_dollar(v) ^ _hkey_qm(v)


def _hkey_km(values: list[int]) -> list[int]:
    if len(values) < 4:
        values = values + [0] * (4 - len(values))
    mixed = [
        _hkey_gm(values[0]) ^ _hkey_ym(values[1]) ^ _hkey_dollar(values[2]) ^ _hkey_qm(values[3]),
        _hkey_qm(values[0]) ^ _hkey_gm(values[1]) ^ _hkey_ym(values[2]) ^ _hkey_dollar(values[3]),
        _hkey_dollar(values[0]) ^ _hkey_qm(values[1]) ^ _hkey_gm(values[2]) ^ _hkey_ym(values[3]),
        _hkey_ym(values[0]) ^ _hkey_dollar(values[1]) ^ _hkey_qm(values[2]) ^ _hkey_gm(values[3]),
    ]
    return mixed + values[4:]


def _hkey_av(value: str, end_offset: int) -> str:
    alphabet = HKEY_ALPHABET[:end_offset]
    return "".join(alphabet[ord(char) % len(alphabet)] for char in value)


def _hkey_sv(value: str) -> str:
    return "".join(HKEY_ALPHABET[ord(char) % len(HKEY_ALPHABET)] for char in value)


def _interleave(values: list[str]) -> str:
    output = []
    for index in range(max(len(v) for v in values)):
        for v in values:
            if index < len(v):
                output.append(v[index])
    return "".join(output)


def _hkey_ov(path: str, timestamp: int, nonce: str) -> str:
    normalized_path = "/" + "/".join(part for part in path.split("/") if part) + "/"
    seed = _interleave([_hkey_av(str(timestamp), -2), _hkey_sv(normalized_path), _hkey_sv(nonce)])[:20]
    digest = hashlib.md5(seed.encode()).hexdigest()
    check = str(sum(_hkey_km([ord(char) for char in digest[-6:]])) % 100).zfill(2)
    prefix = _hkey_av(digest[:5], -4)
    return f"{prefix}{check}"


def get_web_signature(path: str) -> dict[str, str]:
    """生成小黑盒官方 API 接口动态签名参数

    Args:
        path (str): API 请求的路由路径

    Returns:
        dict[str, str]: 包含 hkey, _time, nonce 键值的字典
    """
    timestamp = int(time.time())
    nonce_seed = f"{timestamp}{secrets.token_hex(16)}"
    nonce = hashlib.md5(nonce_seed.encode()).hexdigest().upper()
    return {
        "hkey": _hkey_ov(path, timestamp + 1, nonce),
        "_time": str(timestamp),
        "nonce": nonce,
    }

# 伪装客户端请求头，模拟 iOS/Android 浏览环境
COMMON_HEADER = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://heybox.xiaoheihe.cn/",
}

# 请求超时时间，单位秒
COMMON_TIMEOUT = 10.0


class HeyboxParser:
    """小黑盒核心解析器

    负责链接识别、重定向追踪、数据抽取及数据模型构建
    """

    def __init__(self):
        # 初始化 HTTP 客户端选项与会话级别的持久设备标识
        self.headers = COMMON_HEADER.copy()
        self.proxy = plugin_config.heybox_proxy
        self.heybox_id = secrets.token_hex(12)
        self.device_id = secrets.token_hex(16)

    async def get_redirect_url(self, url: str) -> str:
        """获取重定向后的真实 URL 地址

        Args:
            url (str): 原始分享链接或短链

        Returns:
            str: 重定向后的目标链接
        """
        async with httpx.AsyncClient(
            headers=self.headers,
            proxy=self.proxy,
            timeout=COMMON_TIMEOUT,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            return str(response.url)

    def extract_link_id(self, url: str) -> str | None:
        """从小黑盒 URL 地址中提取 link_id

        Args:
            url (str): 目标链接

        Returns:
            str | None: 提取到的 link_id，若未能提取到则返回 None
        """
        if not url:
            return None

        # 清理 HTML 实体转义字符（如 &amp; -> &）
        clean_url = html.unescape(url).strip()

        # 从 URL 查询参数中解析 link_id
        parsed_url = urlparse(clean_url)
        params = parse_qs(parsed_url.query)
        for key in ("link_id", "linkid", "amp;link_id", "amp;linkid"):
            if key in params and params[key]:
                return params[key][0]

        # 尝试通过正则表达式提取十六进制或字母+数字构成的 link_id
        matched = re.search(r"(?:link_id=|linkid=|/link/|/share/|/detail/|/post/|/wiki/)([a-zA-Z0-9_]+)", clean_url, re.IGNORECASE)
        if matched:
            return matched.group(1)

        # 若 URL 经过二次编码，URL 解码后再试一次
        decoded_url = unquote(clean_url)
        matched = re.search(r"(?:link_id=|linkid=|/link/|/share/|/detail/|/post/|/wiki/)([a-zA-Z0-9_]+)", decoded_url, re.IGNORECASE)
        if matched:
            return matched.group(1)

        return None

    async def parse_url(self, url: str) -> HeyboxParseResult:
        """解析小黑盒分享链接，获取详细音视频与图集数据

        支持 302 重定向 redirect_data 提取 + API 请求 + HTML 页面解析的多级退避机制

        Args:
            url (str): 小黑盒分享 URL 或文本中的短链

        Returns:
            HeyboxParseResult: 结构化解析结果对象

        Raises:
            ValueError: 无法提取有效 link_id 或解析数据异常
        """
        # 清理实体字符（如 &amp; -> &）与斜杠转义
        url = html.unescape(url).replace("\\/", "/").strip()

        # 步骤 1：不跟随重定向发起请求，优先检查 302 重定向中的 Location 头与 redirect_data
        redirect_result = None
        async with httpx.AsyncClient(
            headers=self.headers,
            proxy=self.proxy,
            timeout=COMMON_TIMEOUT,
            follow_redirects=False,
        ) as client:
            try:
                resp = await client.get(url)
                if resp.status_code in (301, 302) and "location" in resp.headers:
                    location_url = resp.headers["location"]
                    parsed_loc = urlparse(location_url)
                    loc_params = parse_qs(parsed_loc.query)
                    if "redirect_data" in loc_params:
                        raw_json_str = loc_params["redirect_data"][0]
                        redirect_json = json.loads(raw_json_str)
                        logger.debug(f"[小黑盒解析] 从重定向 Location 成功解包 redirect_data: {redirect_json}")
                        redirect_result = self._parse_from_redirect_dict(redirect_json)
                        # 如果 redirect_data 中已经包含图集或视频内容，直接返回
                        if redirect_result.content is not None:
                            return redirect_result
            except Exception as loc_err:
                logger.warning(f"[小黑盒解析] 解包 302 location 重定向数据异常: {loc_err}")

        # 步骤 2：获取重定向后的最终地址，提取 link_id
        try:
            final_url = await self.get_redirect_url(url)
            link_id = self.extract_link_id(final_url) or self.extract_link_id(url)
        except Exception as redir_err:
            logger.warning(f"[小黑盒解析] 获取最终重定向 URL 失败: {redir_err}")
            link_id = self.extract_link_id(url)

        if not link_id and not redirect_result:
            raise ValueError("无法从小黑盒链接中提取有效的 link_id")

        # 步骤 3：尝试请求小黑盒分享 API 接口获取数据
        if link_id:
            try:
                return await self._parse_from_api(link_id)
            except Exception as api_err:
                logger.warning(f"[小黑盒解析] 接口请求失败 ({api_err})，自动降级切换至 H5 网页源码解析模式")

            # 步骤 4：API 失败降级处理——请求小黑盒真实 H5 分享页面源码
            h5_url = f"https://www.xiaoheihe.cn/bbs/post_share?link_id={link_id}"
            try:
                async with httpx.AsyncClient(
                    headers=self.headers,
                    proxy=self.proxy,
                    timeout=COMMON_TIMEOUT,
                    follow_redirects=True,
                ) as client:
                    response = await client.get(h5_url)
                    if response.status_code == 200:
                        parsed_h5 = urlparse(str(response.url))
                        h5_params = parse_qs(parsed_h5.query)
                        if "redirect_data" in h5_params:
                            raw_json_str = h5_params["redirect_data"][0]
                            redirect_json = json.loads(raw_json_str)
                            return self._parse_from_redirect_dict(redirect_json)
                        return self._parse_html_data(response.text, link_id)
            except Exception as h5_err:
                logger.warning(f"[小黑盒解析] 请求 H5 页面超时或失败: {h5_err}")

        # 步骤 5：若网络退避降级仍失败，且优先步骤已获取到 redirect_result，则降级返回
        if redirect_result:
            logger.info("[小黑盒解析] 深度媒体抓取超时，降级返回 302 redirect_data 基本元数据")
            return redirect_result

        raise ValueError("小黑盒页面解析失败，网络超时或资源已被删除")

    def _parse_from_redirect_dict(self, data: dict) -> HeyboxParseResult:
        """从重定向 location 提取的 redirect_data 字典构建解析结果对象

        Args:
            data (dict): 解码后的 redirect_data 字典数据

        Returns:
            HeyboxParseResult: 结构化解析结果
        """
        link_info = data.get("link") or data.get("post") or data
        user_info = data.get("user") or link_info.get("user") or {}

        title = link_info.get("title") or link_info.get("description") or "小黑盒分享"
        author_name = user_info.get("username") or user_info.get("nickname") or "小黑盒用户"
        description = link_info.get("description") or link_info.get("summary") or link_info.get("desc")

        # 提取图片资源
        pic_urls = []
        if images := (link_info.get("images") or link_info.get("pics") or link_info.get("preview") or link_info.get("thumb") or link_info.get("cover")):
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict) and (u := img.get("url") or img.get("src") or img.get("origin_url")):
                        pic_urls.append(u)
                    elif isinstance(img, str):
                        pic_urls.append(img)
            elif isinstance(images, str):
                pic_urls.append(images)

        # 提取视频资源
        video_url = None
        if video_data := link_info.get("video"):
            if isinstance(video_data, dict):
                video_url = video_data.get("url") or video_data.get("mp4_url")
            elif isinstance(video_data, str):
                video_url = video_data

        content = None
        if video_url:
            content = VideoContent(video_url=video_url)
        elif pic_urls:
            content = ImageContent(pic_urls=pic_urls)

        cover_url = pic_urls[0] if pic_urls else None

        return HeyboxParseResult(
            title=title,
            author=author_name,
            description=description,
            cover_url=cover_url,
            content=content,
        )

    async def _parse_from_api(self, link_id: str) -> HeyboxParseResult:
        """从小黑盒官方 Web API 接口获取并解析帖子与图集数据

        支持自定义 Session Cookie、持久化设备伪造及多 API 路由容错退避

        Args:
            link_id (str): 小黑盒帖子的 link_id

        Returns:
            HeyboxParseResult: 结构化数据结果
        """
        # 优先使用配置中用户指定的真实 Cookie，未配置则使用会话保持的设备 Cookie
        custom_cookie = plugin_config.heybox_cookie
        cookie_str = (
            custom_cookie
            if custom_cookie
            else f"heybox_id={self.heybox_id}; device_id={self.device_id}"
        )

        headers = self.headers.copy()
        headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://www.xiaoheihe.cn/",
                "Origin": "https://www.xiaoheihe.cn",
                "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "Cookie": cookie_str,
            }
        )

        # 候选 API 路由列表，支持主路由触发风控时自动退避重试备用路由
        endpoints = [
            ("/bbs/app/link/tree", "https://api.xiaoheihe.cn/bbs/app/link/tree"),
            ("/bbs/app/api/share/data/", "https://api.xiaoheihe.cn/bbs/app/api/share/data/"),
            ("/bbs/app/feeds/single", "https://api.xiaoheihe.cn/bbs/app/feeds/single"),
        ]

        last_error = "未获得响应"
        res_json = None

        async with httpx.AsyncClient(
            headers=headers,
            proxy=self.proxy,
            timeout=COMMON_TIMEOUT,
        ) as client:
            for path, api_url in endpoints:
                sig = get_web_signature(path)
                params = {
                    "os_type": "web",
                    "app": "heybox",
                    "client_type": "web",
                    "version": "999.0.4",
                    "web_version": "2.5",
                    "x_client_type": "web",
                    "x_app": "heybox_website",
                    "heybox_id": self.heybox_id,
                    "x_os_type": "Windows",
                    "device_info": "Chrome/125.0.0.0",
                    "device_id": self.device_id,
                    "link_id": link_id,
                    **sig,
                }
                try:
                    response = await client.get(api_url, params=params)
                    if response.status_code != 200:
                        last_error = f"HTTP {response.status_code}"
                        continue

                    data = response.json()
                    status_val = data.get("status")
                    if status_val == "show_captcha":
                        last_error = "触发小黑盒验证码风控 (show_captcha)"
                        logger.warning(f"[小黑盒解析] 接口 {path} 触发 show_captcha 风控，尝试轮询备用路由...")
                        continue

                    if status_val == "ok" or data.get("code") == 0:
                        res_json = data
                        break

                    last_error = data.get("msg") or str(status_val)
                except Exception as req_err:
                    last_error = str(req_err)
                    continue

        if not res_json:
            if "show_captcha" in last_error:
                logger.info("[小黑盒解析] 提示：若频繁遇到 show_captcha 风控，可在 .env 配置文件中设置 HEYBOX_COOKIE 以完全绕过")
            raise ValueError(f"API 业务状态异常: {last_error}")

        result_data = res_json.get("result") or res_json.get("data") or {}
        post_detail = result_data.get("link") or result_data.get("post") or result_data.get("detail") or result_data

        title = (
            post_detail.get("title")
            or post_detail.get("caption")
            or "小黑盒帖子"
        )
        user_info = post_detail.get("user") or post_detail.get("author") or {}
        author_name = user_info.get("username") or user_info.get("nickname") or "小黑盒用户"

        # 提取帖子正文/描述内容
        description = (
            post_detail.get("description")
            or post_detail.get("summary")
            or post_detail.get("desc")
        )
        if not description and (text_content := post_detail.get("text")):
            if isinstance(text_content, str):
                if text_content.strip().startswith("["):
                    try:
                        blocks = json.loads(text_content)
                        text_parts = [b.get("text", "").strip() for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                        description = "\n".join(filter(None, text_parts)).strip()
                    except Exception:
                        description = text_content
                else:
                    description = text_content

        # 视频链接抽取
        video_url = None
        if video_data := post_detail.get("video"):
            video_url = (
                video_data.get("url")
                or video_data.get("mp4_url")
                or video_data.get("main_url")
            )

        # 优先从 post_detail 中提取图集链接
        pic_urls = []

        # 方式 1: 从 images / pics 字段提取
        if image_list := (post_detail.get("images") or post_detail.get("pics")):
            for img_item in image_list:
                if isinstance(img_item, dict):
                    img_url = img_item.get("url") or img_item.get("origin_url") or img_item.get("src")
                    if img_url and img_url not in pic_urls:
                        pic_urls.append(img_url)
                elif isinstance(img_item, str) and img_item not in pic_urls:
                    pic_urls.append(img_item)

        # 方式 2: 小黑盒新版结构，正文图集保存在 text (JSON 字符串) 中
        if not pic_urls and (text_content := post_detail.get("text")):
            if isinstance(text_content, str) and text_content.strip().startswith("["):
                try:
                    blocks = json.loads(text_content)
                    for block in blocks:
                        if isinstance(block, dict) and block.get("type") == "img":
                            img_url = block.get("url") or block.get("src") or block.get("origin_url")
                            if img_url and img_url not in pic_urls:
                                pic_urls.append(img_url)
                except Exception as json_err:
                    logger.warning(f"[小黑盒解析] 解析正文 JSON 图集列表失败: {json_err}")

        # 方式 3: 若上述均未找到，退避在 post_detail 内部（排除用户头像与勋章图标）递归查找图片
        if not pic_urls:
            def find_post_images(obj):
                imgs = []
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in ("user", "medals", "topics", "feedback", "comments"):
                            continue
                        if k in ("images", "pics", "img_url", "url", "origin_url", "src") and isinstance(v, (list, str)):
                            if isinstance(v, list):
                                for item in v:
                                    if isinstance(item, str) and ("http" in item):
                                        imgs.append(item)
                                    elif isinstance(item, dict):
                                        u = item.get("url") or item.get("origin_url") or item.get("src")
                                        if u:
                                            imgs.append(u)
                            elif isinstance(v, str) and ("http" in v) and any(ext in v.lower() for ext in [".jpg", ".png", ".webp", ".jpeg", "maxjia", "heybox"]):
                                imgs.append(v)
                        else:
                            imgs.extend(find_post_images(v))
                elif isinstance(obj, list):
                    for item in obj:
                        imgs.extend(find_post_images(item))
                return imgs

            for img in find_post_images(post_detail):
                if img not in pic_urls:
                    pic_urls.append(img)

        content = None
        if video_url:
            content = VideoContent(video_url=video_url)
        elif pic_urls:
            content = ImageContent(pic_urls=pic_urls)

        cover_url = pic_urls[0] if pic_urls else None

        return HeyboxParseResult(
            title=title,
            author=author_name,
            description=description,
            cover_url=cover_url,
            content=content,
        )

    def _parse_html_data(self, html_content: str, link_id: str) -> HeyboxParseResult:
        """从 H5 页面 HTML 文本中解析提取状态数据

        Args:
            html_content (str): H5 页面响应的 HTML 字符串
            link_id (str): 当前帖子的 link_id

        Returns:
            HeyboxParseResult: 解析得到的数据结果
        """
        # 正则表达式匹配 window.__INITIAL_STATE__ 或类似注入全局变量
        pattern = r"window\.__INITIAL_STATE__\s*=\s*(.*?);?\s*</script>"
        matched = re.search(pattern, html_content, flags=re.DOTALL)

        if not matched:
            # 备用匹配模式
            pattern_backup = r"window\.__HEYBOX_DATA__\s*=\s*(.*?);?\s*</script>"
            matched = re.search(pattern_backup, html_content, flags=re.DOTALL)

        if not matched or not matched.group(1):
            raise ValueError("未能從小黑盒 HTML 页面中匹配到有效的 JSON 状态数据")

        try:
            json_text = matched.group(1).strip()
            # 替换常见的未加引号 JavaScript 特殊字面量
            json_text = json_text.replace("undefined", "null")
            raw_data = json.loads(json_text)
        except Exception as err:
            raise ValueError(f"小黑盒 JSON 数据反序列化失败: {err}")

        # 深入寻找帖子及动态的主体内容数据结构
        post_detail = {}
        if isinstance(raw_data, dict):
            # 兼容不同的数据节点分布
            post_detail = (
                raw_data.get("post", {}).get("detail", {})
                or raw_data.get("bbs", {}).get("post_detail", {})
                or raw_data.get("detail", {})
                or raw_data
            )

        # 提取标题与作者
        title = (
            post_detail.get("title")
            or post_detail.get("caption")
            or "小黑盒帖子"
        )
        user_info = post_detail.get("user") or post_detail.get("author") or {}
        author_name = user_info.get("username") or user_info.get("nickname") or "小黑盒用户"
        description = (
            post_detail.get("description")
            or post_detail.get("summary")
            or post_detail.get("desc")
        )

        # 提取视频直链
        video_url = None
        if video_data := post_detail.get("video"):
            video_url = (
                video_data.get("url")
                or video_data.get("mp4_url")
                or video_data.get("main_url")
            )

        # 提取图集链接列表
        pic_urls = []
        if image_list := (post_detail.get("images") or post_detail.get("pics")):
            for img_item in image_list:
                if isinstance(img_item, dict):
                    # 获取原图或高质量图片 URL
                    img_url = img_item.get("url") or img_item.get("origin_url") or img_item.get("src")
                    if img_url:
                        pic_urls.append(img_url)
                elif isinstance(img_item, str):
                    pic_urls.append(img_item)

        # 判断并构造 Content 包含的对象类型
        content = None
        if video_url:
            content = VideoContent(video_url=video_url)
        elif pic_urls:
            content = ImageContent(pic_urls=pic_urls)

        # 封面图提取
        cover_url = None
        if pic_urls:
            cover_url = pic_urls[0]
        elif video_data and isinstance(video_data, dict):
            cover_url = video_data.get("cover") or video_data.get("thumb")

        return HeyboxParseResult(
            title=title,
            author=author_name,
            description=description,
            cover_url=cover_url,
            content=content,
        )
