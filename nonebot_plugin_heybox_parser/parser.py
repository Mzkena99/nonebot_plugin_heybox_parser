from datetime import datetime
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


def clean_heybox_url(url: str = "", link_id: str | None = None) -> str:
    """清洗小黑盒分享链接，去除多余跟踪参数并生成标准干净链接

    Args:
        url (str): 原始小黑盒链接
        link_id (str | None): 帖子的 link_id

    Returns:
        str: 清洗后的规范 URL
    """
    # 若存在 link_id，优先构造官方标准分享短链
    if link_id:
        return f"https://www.xiaoheihe.cn/bbs/post_share?link_id={link_id}"
    if not url:
        return ""

    # 解析 URL 并剥离常见跟踪与设备参数
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    tracker_keys = {
        "hkey", "_time", "nonce", "share_time", "src", "invitation_code",
        "is_redirect", "client_type", "version", "web_version", "x_client_type",
        "x_app", "heybox_id", "x_os_type", "device_info", "device_id", "os_type",
        "app", "userid", "user_id", "spm", "from", "timestamp", "amp;hkey",
        "amp;_time", "amp;nonce", "amp;link_id", "amp;linkid",
    }
    cleaned_params = {k: v for k, v in params.items() if k.lower() not in tracker_keys}
    query_str = "&".join(f"{k}={v[0]}" for k, v in cleaned_params.items())

    clean_path = parsed.path or "/"
    if query_str:
        return f"{parsed.scheme}://{parsed.netloc}{clean_path}?{query_str}"
    return f"{parsed.scheme}://{parsed.netloc}{clean_path}"


def clean_original_image_url(url: str) -> str:
    """清理小黑盒图片链接中的 CDN 有损裁剪与强制转码参数

    小黑盒通常在图片链接尾部追加七牛云参数（如 ?imageMogr2/.../thumbnail/...），
    剥离这些裁剪参数能够直接向 CDN 请求未压缩的高分辨率原始图片文件，
    同时能使动态 GIF 图片保留完整的原版动画帧而不是被转为静态图

    Args:
        url (str): 带有 CDN 处理参数的图片链接

    Returns:
        str: 清理后的纯净无损原图直链
    """
    if not url:
        return ""
    # 剥离七牛云 imageMogr2 等处理参数
    if "?imageMogr2" in url:
        return url.split("?")[0]
    return url


def _format_created_at(val: int | float | str | None) -> str | None:
    """格式化时间戳或时间字符串为可读时间（如 2024-05-20 18:30）

    Args:
        val: 原始时间字段（秒/毫秒时间戳或字符串）

    Returns:
        str | None: 格式化后的时间字符串
    """
    if val is None or val == "":
        return None
    try:
        if isinstance(val, (int, float)):
            ts = val / 1000.0 if val > 1e11 else val
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        if isinstance(val, str) and val.strip().isdigit():
            ts = int(val.strip())
            ts = ts / 1000.0 if ts > 1e11 else ts
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        return str(val).strip()
    except Exception:
        return str(val)


def _extract_hot_comments(data: dict) -> list[dict]:
    """从小黑盒 API 或页面原始数据中提取结构化的热门评论列表

    Args:
        data (dict): 接口响应或页面 JSON 字典

    Returns:
        list[dict]: 清洗提取后的热门评论列表
    """
    if not isinstance(data, dict):
        return []

    # 兼容多种热门评论字段层级
    comments_raw = (
        data.get("hot_comments")
        or data.get("hot_comment")
        or data.get("comments")
        or data.get("comment_list")
        or []
    )
    if not isinstance(comments_raw, list):
        return []

    # 扁平化评论条目（兼容楼层结构与单层结构）
    flat_comments: list[dict] = []
    for item in comments_raw:
        if not isinstance(item, dict):
            continue
        if "comment" in item and isinstance(item["comment"], list):
            for sub_c in item["comment"]:
                if isinstance(sub_c, dict):
                    flat_comments.append(sub_c)
        else:
            flat_comments.append(item)

    hot_list: list[dict] = []
    for item in flat_comments:
        user = item.get("user") or item.get("author") or {}
        username = user.get("username") or user.get("nickname") or "小黑盒用户"
        avatar = user.get("avatar") or user.get("avatar_url") or user.get("avartar") or user.get("head_url")
        level_info = user.get("level_info") if isinstance(user.get("level_info"), dict) else {}
        level = user.get("level") or user.get("user_level") or level_info.get("level")
        text = item.get("text") or item.get("content") or ""
        
        if text and isinstance(text, str):
            # 过滤 HTML 标签（如表情卡片与超链接文本）
            clean_text = re.sub(r"<[^>]+>", "", text).strip()
            if clean_text:
                likes = item.get("like_num") or item.get("up") or item.get("up_num") or item.get("praise_num") or 0
                comment_time = _format_created_at(
                    item.get("create_at") or item.get("created_at") or item.get("time")
                )
                hot_list.append({
                    "username": username,
                    "avatar": avatar,
                    "level": level,
                    "content": clean_text,
                    "likes": likes,
                    "created_at": comment_time,
                })
    return hot_list


def _extract_video_url(data: dict, raw_blocks: list | None = None) -> str | None:
    """从字典、分块数据及嵌套结构中提取有效的视频直链 URL

    Args:
        data (dict): 帖子详情或根数据字典
        raw_blocks (list | None): 富文本 JSON 分块列表

    Returns:
        str | None: 提取到的有效视频播放直链地址
    """
    if not isinstance(data, dict):
        return None

    # 1. 检查顶层直接给出的视频链接字段
    for field_name in ("video_url", "mp4_url", "play_url", "main_url", "stream_url", "hls_url", "m3u8_url"):
        if u := data.get(field_name):
            if isinstance(u, str) and ("http" in u):
                return u.strip()

    # 2. 检查嵌套的 video / video_info / video_data / media 字典或列表
    for key in ("video", "video_info", "video_data", "media", "media_info"):
        v_obj = data.get(key)
        if isinstance(v_obj, dict):
            for sub_key in ("url", "mp4_url", "main_url", "play_url", "video_url", "src", "stream", "hls_url", "m3u8_url"):
                if u := v_obj.get(sub_key):
                    if isinstance(u, str) and ("http" in u):
                        return u.strip()
        elif isinstance(v_obj, str) and ("http" in v_obj):
            return v_obj.strip()
        elif isinstance(v_obj, list):
            for item in v_obj:
                if isinstance(item, dict):
                    for sub_key in ("url", "mp4_url", "main_url", "play_url", "video_url", "src"):
                        if u := item.get(sub_key):
                            if isinstance(u, str) and ("http" in u):
                                return u.strip()
                elif isinstance(item, str) and ("http" in item):
                    return item.strip()

    # 3. 检查 videos 列表字段
    if v_list := data.get("videos"):
        if isinstance(v_list, list):
            for item in v_list:
                if isinstance(item, dict):
                    for sub_key in ("url", "mp4_url", "main_url", "play_url", "video_url", "src"):
                        if u := item.get(sub_key):
                            if isinstance(u, str) and ("http" in u):
                                return u.strip()
                elif isinstance(item, str) and ("http" in item):
                    return item.strip()

    # 4. 检查富文本 JSON 分块中的 video 节点
    if raw_blocks:
        for block in raw_blocks:
            if isinstance(block, dict) and block.get("type") in ("video", "media"):
                for sub_key in ("url", "mp4_url", "video_url", "play_url", "src", "main_url"):
                    if u := block.get(sub_key):
                        if isinstance(u, str) and ("http" in u):
                            return u.strip()

    # 5. 兜底策略：在整个数据结构中深度查找带有 .mp4 或视频流特征的直链
    def find_mp4(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("user", "avatar", "author", "medals", "topics", "feedback", "comments", "hot_comments"):
                    continue
                if res := find_mp4(v):
                    return res
        elif isinstance(obj, list):
            for item in obj:
                if res := find_mp4(item):
                    return res
        elif isinstance(obj, str) and ("http" in obj) and (".mp4" in obj.lower() or "video.xiaoheihe.cn" in obj.lower()):
            return obj.strip()
        return None

    return find_mp4(data)


def _extract_video_cover(data: dict) -> str | None:
    """提取视频封面图 URL

    Args:
        data (dict): 帖子详情或根数据字典

    Returns:
        str | None: 提取到的封面图地址
    """
    if not isinstance(data, dict):
        return None
    for key in ("video", "video_info", "video_data", "media"):
        v_obj = data.get(key)
        if isinstance(v_obj, dict):
            for c_key in ("cover", "thumb", "poster", "preview", "cover_url", "thumb_url"):
                if u := v_obj.get(c_key):
                    if isinstance(u, str) and ("http" in u):
                        return u.strip()
    return None


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

    def _parse_from_redirect_dict(self, data: dict, link_id: str | None = None) -> HeyboxParseResult:
        """从重定向 location 提取的 redirect_data 字典构建解析结果对象

        Args:
            data (dict): 解码后的 redirect_data 字典数据
            link_id (str | None): 可选的 link_id

        Returns:
            HeyboxParseResult: 结构化解析结果
        """
        link_info = data.get("link") or data.get("post") or data
        user_info = data.get("user") or link_info.get("user") or {}

        title = link_info.get("title") or link_info.get("description") or "小黑盒分享"
        author_name = user_info.get("username") or user_info.get("nickname") or "小黑盒用户"
        author_avatar = (
            user_info.get("avatar")
            or user_info.get("avatar_url")
            or user_info.get("head_url")
            or user_info.get("head")
        )
        user_level = user_info.get("level") or user_info.get("user_level")
        created_at = _format_created_at(
            link_info.get("create_at")
            or link_info.get("created_at")
            or link_info.get("post_time")
        )
        description = link_info.get("description") or link_info.get("summary") or link_info.get("desc")

        effective_link_id = link_id or link_info.get("link_id") or link_info.get("linkid") or data.get("link_id")
        effective_link_id = str(effective_link_id) if effective_link_id else None
        clean_url = clean_heybox_url(link_id=effective_link_id) if effective_link_id else None

        # 提取话题与版块标签
        topics: list[str] = []
        if raw_topics := (link_info.get("topics") or link_info.get("tags") or link_info.get("tag_name")):
            if isinstance(raw_topics, list):
                for t in raw_topics:
                    if isinstance(t, dict) and (tn := t.get("name") or t.get("topic_name")):
                        topics.append(str(tn))
                    elif isinstance(t, str):
                        topics.append(t)
            elif isinstance(raw_topics, str):
                topics.append(raw_topics)

        # 提取统计互动数据
        stats = {
            "likes": link_info.get("like_num") or link_info.get("up_num") or link_info.get("score") or 0,
            "comments": link_info.get("reply_num") or link_info.get("comment_num") or 0,
            "views": link_info.get("view_num") or link_info.get("read_num") or 0,
            "shares": link_info.get("share_num") or 0,
        }

        # 提取图片资源并清洗为无损原图链接
        pic_urls = []
        if images := (link_info.get("images") or link_info.get("pics") or link_info.get("preview") or link_info.get("thumb") or link_info.get("cover")):
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict) and (u := img.get("origin_url") or img.get("url") or img.get("src")):
                        clean_u = clean_original_image_url(u)
                        if clean_u and clean_u not in pic_urls:
                            pic_urls.append(clean_u)
                    elif isinstance(img, str):
                        clean_u = clean_original_image_url(img)
                        if clean_u and clean_u not in pic_urls:
                            pic_urls.append(clean_u)
            elif isinstance(images, str):
                clean_u = clean_original_image_url(images)
                if clean_u and clean_u not in pic_urls:
                    pic_urls.append(clean_u)

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
        hot_comments = _extract_hot_comments(data) or _extract_hot_comments(link_info)

        return HeyboxParseResult(
            title=title,
            author=author_name,
            author_avatar=author_avatar,
            user_level=user_level,
            created_at=created_at,
            description=description,
            cover_url=cover_url,
            link_id=effective_link_id,
            clean_url=clean_url,
            topics=topics,
            stats=stats,
            blocks=[],
            hot_comments=hot_comments,
            content=content,
        )

    async def _parse_from_api(self, link_id: str) -> HeyboxParseResult:
        """从小黑盒官方 Web API 接口获取并解析帖子与图集数据

        支持自定义 Session Cookie、持久化设备伪造及多 API 路由容错退避

        Args:
            link_id (str): 小黑盒帖子的 link_id

        Returns:
            HeyboxParseResult: 结构化解析结果
        """
        custom_cookie = plugin_config.heybox_cookie
        cookie_str = (
            custom_cookie
            if custom_cookie
            else f"heybox_id={self.heybox_id}; device_id={self.device_id}"
        )

        headers = self.headers.copy()
        headers["Cookie"] = cookie_str

        # 候选 API 路由列表，支持主路由触发风控时自动退避重试备用路由
        endpoints = [
            ("/bbs/app/link/tree", "https://api.xiaoheihe.cn/bbs/app/link/tree"),
            ("/bbs/app/api/share/data/", "https://api.xiaoheihe.cn/bbs/app/api/share/data/"),
            ("/bbs/app/feeds/single", "https://api.xiaoheihe.cn/bbs/app/feeds/single"),
        ]

        res_json = None
        last_error = ""

        async with httpx.AsyncClient(
            headers=headers,
            proxy=self.proxy,
            timeout=COMMON_TIMEOUT,
            follow_redirects=True,
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
                        last_error = f"{path} HTTP {response.status_code}"
                        continue

                    data = response.json()
                    status_val = data.get("status")
                    if status_val == "show_captcha":
                        last_error = "触发小黑盒验证码风控 (show_captcha)"
                        logger.warning(f"[小黑盒解析] 接口 {path} 触发 show_captcha 风控，尝试轮询备用路由...")
                        continue

                    if status_val == "ok" or data.get("code") == 0 or data.get("result") or data.get("data"):
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
        author_avatar = (
            user_info.get("avatar")
            or user_info.get("avatar_url")
            or user_info.get("avartar")
            or user_info.get("head_url")
            or user_info.get("head")
        )
        level_info = user_info.get("level_info") if isinstance(user_info.get("level_info"), dict) else {}
        user_level = user_info.get("level") or user_info.get("user_level") or level_info.get("level")
        created_at = _format_created_at(
            post_detail.get("create_at")
            or post_detail.get("created_at")
            or post_detail.get("post_time")
            or post_detail.get("publish_time")
        )
        clean_url = clean_heybox_url(link_id=link_id)

        # 提取话题与社区版块标签
        topics: list[str] = []
        if raw_topics := (post_detail.get("topics") or post_detail.get("tags") or post_detail.get("topic")):
            if isinstance(raw_topics, list):
                for t in raw_topics:
                    if isinstance(t, dict) and (tn := t.get("name") or t.get("topic_name") or t.get("title")):
                        topics.append(str(tn))
                    elif isinstance(t, str):
                        topics.append(t)
            elif isinstance(raw_topics, str):
                topics.append(raw_topics)
        elif tag_name := (post_detail.get("tag_name") or post_detail.get("forum_name")):
            topics.append(str(tag_name))

        # 提取统计互动数据
        stats = {
            "likes": post_detail.get("like_num") or post_detail.get("up_num") or post_detail.get("score") or 0,
            "comments": post_detail.get("reply_num") or post_detail.get("comment_num") or 0,
            "views": post_detail.get("view_num") or post_detail.get("read_num") or post_detail.get("click_num") or 0,
            "shares": post_detail.get("share_num") or 0,
        }

        # 提取帖子正文/描述内容与富文本分块（确保不被摘要截断）
        description = (
            post_detail.get("description")
            or post_detail.get("summary")
            or post_detail.get("desc")
            or ""
        )
        raw_blocks = []
        if text_content := post_detail.get("text"):
            if isinstance(text_content, str):
                if text_content.strip().startswith("["):
                    try:
                        raw_blocks = json.loads(text_content)
                        # 提取常规文本分块
                        text_parts = [
                            b.get("text", "").strip()
                            for b in raw_blocks
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                        ]
                        full_text = "\n".join(filter(None, text_parts)).strip()

                        # 针对专栏长文：从 type == 'html' 分块中提取格式化纯文本段落
                        html_parts = []
                        for b in raw_blocks:
                            if isinstance(b, dict) and b.get("type") == "html" and (raw_ht := b.get("text")):
                                # 将段落闭合标签与换行标签转换为自然换行，去除其它标签得到纯净段落
                                text_clean = re.sub(r"<\s*/?p[^>]*>", "\n", raw_ht)
                                text_clean = re.sub(r"<\s*br\s*/?>", "\n", text_clean)
                                text_clean = re.sub(r"<[^>]+>", "", text_clean)
                                text_clean = html.unescape(text_clean).strip()
                                if text_clean:
                                    html_parts.append(text_clean)

                        if html_parts:
                            full_html_text = "\n\n".join(html_parts).strip()
                            if len(full_html_text) > len(full_text):
                                full_text = full_html_text

                        # 若分块文本比短描述更完整，则以完整分块文本为准
                        if full_text and len(full_text) > len(description):
                            description = full_text
                    except Exception:
                        if not description:
                            description = text_content
                elif not description:
                    description = text_content

        # 提取热门评论列表
        hot_comments = _extract_hot_comments(result_data) or _extract_hot_comments(post_detail)

        # 提取视频直链与封面
        video_url = (
            _extract_video_url(post_detail, raw_blocks)
            or _extract_video_url(result_data, raw_blocks)
        )
        video_cover = _extract_video_cover(post_detail) or _extract_video_cover(result_data)

        # 优先从 post_detail 中提取图集链接
        pic_urls = []

        # 方式 1: 从 images / pics 字段提取并清洗无损原图
        if image_list := (post_detail.get("images") or post_detail.get("pics")):
            for img_item in image_list:
                if isinstance(img_item, dict):
                    img_url = img_item.get("origin_url") or img_item.get("url") or img_item.get("src")
                    if img_url:
                        clean_url_item = clean_original_image_url(img_url)
                        if clean_url_item and clean_url_item not in pic_urls:
                            pic_urls.append(clean_url_item)
                elif isinstance(img_item, str):
                    clean_url_item = clean_original_image_url(img_item)
                    if clean_url_item and clean_url_item not in pic_urls:
                        pic_urls.append(clean_url_item)

        # 方式 2: 小黑盒新版结构，正文图集保存在 text (JSON 字符串) 中
        if not pic_urls and raw_blocks:
            for block in raw_blocks:
                if isinstance(block, dict) and block.get("type") == "img":
                    img_url = block.get("origin_url") or block.get("url") or block.get("src")
                    if img_url:
                        clean_url_item = clean_original_image_url(img_url)
                        if clean_url_item and clean_url_item not in pic_urls:
                            pic_urls.append(clean_url_item)

        # 方式 3: 若上述均未找到，退避在 post_detail 内部（排除用户头像与勋章图标）递归查找图片
        if not pic_urls:
            def find_post_images(obj):
                imgs = []
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in ("user", "medals", "topics", "feedback", "comments", "hot_comments"):
                            continue
                        if k in ("images", "pics", "img_url", "url", "origin_url", "src") and isinstance(v, (list, str)):
                            if isinstance(v, list):
                                for item in v:
                                    if isinstance(item, str) and ("http" in item):
                                        imgs.append(clean_original_image_url(item))
                                    elif isinstance(item, dict):
                                        u = item.get("origin_url") or item.get("url") or item.get("src")
                                        if u:
                                            imgs.append(clean_original_image_url(u))
                            elif isinstance(v, str) and ("http" in v) and any(ext in v.lower() for ext in [".jpg", ".png", ".webp", ".jpeg", "maxjia", "heybox"]):
                                imgs.append(clean_original_image_url(v))
                        else:
                            imgs.extend(find_post_images(v))
                elif isinstance(obj, list):
                    for item in obj:
                        imgs.extend(find_post_images(item))
                return imgs

            for img in find_post_images(post_detail):
                if img and img not in pic_urls:
                    pic_urls.append(img)

        content = None
        if video_url:
            content = VideoContent(video_url=video_url)
        elif pic_urls:
            content = ImageContent(pic_urls=pic_urls)

        cover_url = pic_urls[0] if pic_urls else video_cover

        return HeyboxParseResult(
            title=title,
            author=author_name,
            author_avatar=author_avatar,
            user_level=user_level,
            created_at=created_at,
            description=description,
            cover_url=cover_url,
            link_id=link_id,
            clean_url=clean_url,
            topics=topics,
            stats=stats,
            blocks=raw_blocks,
            hot_comments=hot_comments,
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
        author_avatar = (
            user_info.get("avatar")
            or user_info.get("avatar_url")
            or user_info.get("head_url")
            or user_info.get("head")
        )
        user_level = user_info.get("level") or user_info.get("user_level")
        created_at = _format_created_at(
            post_detail.get("create_at")
            or post_detail.get("created_at")
            or post_detail.get("post_time")
            or post_detail.get("publish_time")
        )
        clean_url = clean_heybox_url(link_id=link_id)

        # 提取话题与社区版块标签
        topics: list[str] = []
        if raw_topics := (post_detail.get("topics") or post_detail.get("tags") or post_detail.get("topic")):
            if isinstance(raw_topics, list):
                for t in raw_topics:
                    if isinstance(t, dict) and (tn := t.get("name") or t.get("topic_name") or t.get("title")):
                        topics.append(str(tn))
                    elif isinstance(t, str):
                        topics.append(t)
            elif isinstance(raw_topics, str):
                topics.append(raw_topics)
        elif tag_name := (post_detail.get("tag_name") or post_detail.get("forum_name")):
            topics.append(str(tag_name))

        # 提取统计互动数据
        stats = {
            "likes": post_detail.get("like_num") or post_detail.get("up_num") or post_detail.get("score") or 0,
            "comments": post_detail.get("reply_num") or post_detail.get("comment_num") or 0,
            "views": post_detail.get("view_num") or post_detail.get("read_num") or post_detail.get("click_num") or 0,
            "shares": post_detail.get("share_num") or 0,
        }

        description = (
            post_detail.get("description")
            or post_detail.get("summary")
            or post_detail.get("desc")
            or ""
        )
        raw_blocks = []
        if text_content := post_detail.get("text"):
            if isinstance(text_content, str):
                if text_content.strip().startswith("["):
                    try:
                        raw_blocks = json.loads(text_content)
                        text_parts = [
                            b.get("text", "").strip()
                            for b in raw_blocks
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                        ]
                        full_text = "\n".join(filter(None, text_parts)).strip()
                        if full_text and len(full_text) > len(description):
                            description = full_text
                    except Exception:
                        if not description:
                            description = text_content
                elif not description:
                    description = text_content

        # 提取热门评论
        hot_comments = _extract_hot_comments(post_detail) or _extract_hot_comments(raw_data)

        # 提取视频直链与视频封面
        video_url = (
            _extract_video_url(post_detail, raw_blocks)
            or _extract_video_url(raw_data, raw_blocks)
        )
        video_cover = _extract_video_cover(post_detail) or _extract_video_cover(raw_data)

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
        cover_url = pic_urls[0] if pic_urls else video_cover

        return HeyboxParseResult(
            title=title,
            author=author_name,
            author_avatar=author_avatar,
            user_level=user_level,
            created_at=created_at,
            description=description,
            cover_url=cover_url,
            link_id=link_id,
            clean_url=clean_url,
            topics=topics,
            stats=stats,
            blocks=raw_blocks,
            hot_comments=hot_comments,
            content=content,
        )
