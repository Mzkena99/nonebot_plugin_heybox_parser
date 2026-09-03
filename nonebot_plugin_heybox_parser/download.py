import asyncio
import hashlib
from pathlib import Path
import re
import time
from urllib.parse import urlparse, urlunparse

import aiofiles
import httpx
from nonebot import logger

from .config import plugin_config
from .parser import COMMON_HEADER

# 插件缓存文件临时目录
CACHE_DIR = Path("data/heybox_parser_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 小黑盒官方静态资源 CDN 镜像节点列表（用于节点故障转移）
OFFICIAL_CDN_HOSTS = [
    "imgheybox1.max-c.com",
    "imgheybox2.max-c.com",
    "imgheybox.max-c.com",
    "cdn.max-c.com",
]


class Downloader:
    """小黑盒资源异步下载器

    负责将解析出的网络视频和图片异步并发下载至本地临时缓存目录，
    内置官方 CDN 镜像多线路容错与磁盘缓存生命周期管理
    """

    def __init__(self):
        self.headers = COMMON_HEADER.copy()
        self.proxy = plugin_config.heybox_proxy
        self.max_video_size = plugin_config.heybox_max_video_size * 1024 * 1024  # 转换为字节
        self._semaphore = asyncio.Semaphore(plugin_config.heybox_download_concurrency)

    def _get_cache_path(self, url: str, extension: str) -> Path:
        """根据 URL 的 MD5 哈希值生成唯一的本地缓存文件名

        Args:
            url (str): 目标下载资源 URL
            extension (str): 文件扩展名 (如 .mp4 或 .jpg)

        Returns:
            Path: 本地文件缓存绝对路径
        """
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        return CACHE_DIR / f"{url_hash}{extension}"

    async def clean_cache(
        self,
        expire_days: int | None = None,
        max_size_mb: int | None = None,
    ) -> int:
        """自动扫描并清理本地缓存目录中的过期与超出容量配额的临时媒体文件

        Args:
            expire_days (int | None): 过期天数阈值，默认为配置项的值
            max_size_mb (int | None): 允许的最大缓存体积 (MB)，默认为配置项的值

        Returns:
            int: 本次清理删除的文件数量
        """
        expire_days = (
            expire_days
            if expire_days is not None
            else plugin_config.heybox_cache_expire_days
        )
        max_size_mb = (
            max_size_mb
            if max_size_mb is not None
            else plugin_config.heybox_max_cache_mb
        )

        if not CACHE_DIR.exists():
            return 0

        deleted_count = 0
        now = time.time()
        file_entries: list[tuple[float, int, Path]] = []

        try:
            # 步骤 1：基于修改时间清理过期文件
            expire_seconds = expire_days * 86400 if expire_days > 0 else None
            for file_path in CACHE_DIR.iterdir():
                if not file_path.is_file():
                    continue
                try:
                    stat = file_path.stat()
                    if expire_seconds and (now - stat.st_mtime > expire_seconds):
                        file_path.unlink(missing_ok=True)
                        deleted_count += 1
                    else:
                        file_entries.append((stat.st_mtime, stat.st_size, file_path))
                except Exception as file_err:
                    logger.debug(f"[小黑盒下载器] 检查文件异常 ({file_path}): {file_err}")

            # 步骤 2：基于最大容量配额清理（LRU 策略，优先删除最旧文件）
            if max_size_mb > 0:
                max_bytes = max_size_mb * 1024 * 1024
                total_size = sum(item[1] for item in file_entries)
                if total_size > max_bytes:
                    # 按时间升序排序（最旧的在前）
                    file_entries.sort(key=lambda x: x[0])
                    target_size = int(max_bytes * 0.8)  # 清理至 80% 安全水位
                    for _, f_size, f_path in file_entries:
                        if total_size <= target_size:
                            break
                        try:
                            f_path.unlink(missing_ok=True)
                            total_size -= f_size
                            deleted_count += 1
                        except Exception as del_err:
                            logger.debug(f"[小黑盒下载器] 删除超额文件失败 ({f_path}): {del_err}")

            if deleted_count > 0:
                logger.info(f"[小黑盒下载器] 本地缓存自动清理完成，共删除 {deleted_count} 个过期/超额媒体文件")

        except Exception as clean_err:
            logger.warning(f"[小黑盒下载器] 缓存清理过程出现异常: {clean_err}")

        return deleted_count

    async def download_video(self, video_url: str) -> Path:
        """异步下载视频文件并返回本地路径

        Args:
            video_url (str): 视频直链地址

        Returns:
            Path: 本地已下载视频文件的路径

        Raises:
            ValueError: 下载失败或文件超过允许的最大限制
        """
        save_path = self._get_cache_path(video_url, ".mp4")
        if save_path.exists():
            logger.debug(f"[小黑盒下载器] 命中本地视频缓存: {save_path}")
            return save_path

        video_headers = self.headers.copy()
        video_headers["Referer"] = "https://www.xiaoheihe.cn/"

        async with self._semaphore:
            async with httpx.AsyncClient(
                headers=video_headers,
                proxy=self.proxy,
                timeout=30.0,
                follow_redirects=True,
            ) as client:
                # 检查 Response Header 中的文件大小
                async with client.stream("GET", video_url) as response:
                    if response.status_code != 200:
                        raise ValueError(f"下载视频失败，HTTP 状态码: {response.status_code}")

                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > self.max_video_size:
                        raise ValueError(
                            f"视频文件大小为 {int(content_length) / 1024 / 1024:.2f} MB，"
                            f"超过允许的最大发送限制 ({plugin_config.heybox_max_video_size} MB)"
                        )

                    downloaded_size = 0
                    async with aiofiles.open(save_path, "wb") as file:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            downloaded_size += len(chunk)
                            if downloaded_size > self.max_video_size:
                                # 超过限制立即终止下载并删除残余文件
                                await file.close()
                                save_path.unlink(missing_ok=True)
                                raise ValueError(
                                    f"下载过程中视频大小超过最大允许限制 ({plugin_config.heybox_max_video_size} MB)"
                                )
                            await file.write(chunk)

        logger.info(f"[小黑盒下载器] 视频成功下载至: {save_path}")
        return save_path

    def _get_candidate_image_urls(self, original_url: str) -> list[str]:
        """为图片 URL 生成高优先级真实无损原图候选与备选官方 CDN 镜像列表

        小黑盒图片存储机制：
        创作者上传的无损原图保存在 /<hash>.<ext>，而客户端默认请求的是位于其子路径下的
        缩略图 /<hash>/thumb.<ext>。通过正则逆向还原无损原图路径，可直接下载与小黑盒客户端
        点击“查看原图”完全一致的高清大图和完整动图

        Args:
            original_url (str): 原始图片 URL

        Returns:
            list[str]: 按优先级排序的高清真实原图与备选 CDN 镜像 URL 列表
        """
        clean_url = original_url.split("?")[0]
        base_candidates: list[str] = []

        # 匹配小黑盒缩略图特征：/<32位hash>/thumb.<ext>
        match = re.search(r'/([a-f0-9]{32})/thumb\.([a-zA-Z0-9]+)', clean_url)
        if match:
            h, ext = match.group(1), match.group(2)
            prefix = clean_url[:match.start()]
            # 候选 1: 同名扩展名的无损原图（小黑盒官方默认原图命名规则）
            base_candidates.append(f"{prefix}/{h}.{ext}")
            # 候选 2: 尝试常见扩展名候选（包含动图与常见静图）
            for alt_ext in ("png", "gif", "jpg", "jpeg", "webp"):
                c = f"{prefix}/{h}.{alt_ext}"
                if c not in base_candidates:
                    base_candidates.append(c)

        # 兜底：原始传入链接（去参数后）
        if clean_url not in base_candidates:
            base_candidates.append(clean_url)
        if original_url not in base_candidates:
            base_candidates.append(original_url)

        # 为每个候选 URL 扩展官方 CDN 镜像
        final_candidates: list[str] = []
        for cand in base_candidates:
            if cand not in final_candidates:
                final_candidates.append(cand)
            try:
                parsed = urlparse(cand)
                host = parsed.netloc.lower()
                if any(key in host for key in ("max-c.com", "xiaoheihe.cn", "heybox")):
                    for alt_host in OFFICIAL_CDN_HOSTS:
                        if alt_host != host:
                            alt_url = urlunparse((
                                parsed.scheme,
                                alt_host,
                                parsed.path,
                                parsed.params,
                                parsed.query,
                                parsed.fragment,
                            ))
                            if alt_url not in final_candidates:
                                final_candidates.append(alt_url)
            except Exception:
                pass
        return final_candidates

    async def download_image(self, image_url: str) -> Path:
        """异步并发下载图片文件并返回本地路径，优先下载 100% 原始无损大图

        Args:
            image_url (str): 图片直链地址

        Returns:
            Path: 本地已下载原图文件的路径
        """
        # 清理图片 URL 查询参数以准确推断初始扩展名
        clean_url = image_url.split("?")[0]
        if clean_url.lower().endswith(".gif"):
            extension = ".gif"
        elif clean_url.lower().endswith(".png"):
            extension = ".png"
        elif clean_url.lower().endswith(".webp"):
            extension = ".webp"
        else:
            extension = ".jpg"

        save_path = self._get_cache_path(image_url, extension)

        # 若已存在本地缓存且非空，直接复用
        if save_path.exists() and save_path.stat().st_size > 0:
            return save_path

        candidate_urls = self._get_candidate_image_urls(image_url)
        referers = ["https://www.xiaoheihe.cn/", "https://heybox.xiaoheihe.cn/", ""]
        last_err = ""

        async with self._semaphore:
            for cand_url in candidate_urls:
                for ref in referers:
                    image_headers = self.headers.copy()
                    if ref:
                        image_headers["Referer"] = ref
                    else:
                        image_headers.pop("Referer", None)

                    try:
                        async with httpx.AsyncClient(
                            headers=image_headers,
                            proxy=self.proxy,
                            timeout=15.0,
                            follow_redirects=True,
                        ) as client:
                            response = await client.get(cand_url)
                            if response.status_code == 200 and len(response.content) > 0:
                                # 根据服务器实际响应的 Content-Type 动态纠正扩展名
                                ctype = response.headers.get("content-type", "").lower()
                                if "gif" in ctype:
                                    correct_ext = ".gif"
                                elif "png" in ctype:
                                    correct_ext = ".png"
                                elif "webp" in ctype:
                                    correct_ext = ".webp"
                                else:
                                    correct_ext = ".jpg"

                                # 若推断扩展名与实际类型不一致，更新为真实扩展名
                                if correct_ext != extension:
                                    save_path = self._get_cache_path(image_url, correct_ext)

                                async with aiofiles.open(save_path, "wb") as file:
                                    await file.write(response.content)

                                logger.debug(f"[小黑盒下载器] 成功下载真实原图 ({len(response.content) / 1024:.1f} KB): {cand_url}")
                                return save_path
                            last_err = f"HTTP {response.status_code}"
                    except Exception as req_err:
                        last_err = str(req_err)
                        continue

        raise ValueError(f"下载原图失败 (尝试了 {len(candidate_urls)} 个原图与 CDN 节点): {last_err}")
