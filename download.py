import hashlib
from pathlib import Path

import aiofiles
import httpx
from nonebot import logger

from .config import plugin_config
from .parser import COMMON_HEADER

# 插件缓存文件临时目录
CACHE_DIR = Path("data/heybox_parser_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class Downloader:
    """小黑盒资源异步下载器

    负责将解析出的网络视频和图片下载至本地临时缓存目录，便于 Bot 发送本地媒体文件
    """

    def __init__(self):
        self.headers = COMMON_HEADER.copy()
        self.proxy = plugin_config.heybox_proxy
        self.max_video_size = plugin_config.heybox_max_video_size * 1024 * 1024  # 转换为字节

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

        async with httpx.AsyncClient(
            headers=self.headers,
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
                            if save_path.exists():
                                save_path.unlink()
                            raise ValueError(
                                f"下载过程中视频大小超过最大允许限制 ({plugin_config.heybox_max_video_size} MB)"
                            )
                        await file.write(chunk)

        logger.info(f"[小黑盒下载器] 视频成功下载至: {save_path}")
        return save_path

    async def download_image(self, image_url: str) -> Path:
        """异步下载图片文件并返回本地路径

        Args:
            image_url (str): 图片直链地址

        Returns:
            Path: 本地已下载图片文件的路径
        """
        # 清理图片 URL 查询参数以准确推断扩展名
        clean_url = image_url.split("?")[0]
        if clean_url.lower().endswith(".png"):
            extension = ".png"
        elif clean_url.lower().endswith(".gif"):
            extension = ".gif"
        elif clean_url.lower().endswith(".webp"):
            extension = ".webp"
        else:
            extension = ".jpg"

        save_path = self._get_cache_path(image_url, extension)

        if save_path.exists():
            return save_path

        # 设置明确的防盗链 Referer 请求头，防止小黑盒 CDN 拦截
        image_headers = self.headers.copy()
        image_headers["Referer"] = "https://www.xiaoheihe.cn/"

        async with httpx.AsyncClient(
            headers=image_headers,
            proxy=self.proxy,
            timeout=15.0,
            follow_redirects=True,
        ) as client:
            response = await client.get(image_url)
            if response.status_code == 200:
                async with aiofiles.open(save_path, "wb") as file:
                    await file.write(response.content)
                return save_path
            else:
                raise ValueError(f"下载图片失败，状态码: {response.status_code}")
