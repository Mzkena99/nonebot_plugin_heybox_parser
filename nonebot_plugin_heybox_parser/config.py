from typing import Literal

from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """小黑盒解析插件配置类

    使用 Pydantic 管理从 Environment 或 .env 读取的插件配置参数
    """

    heybox_auto_parse: bool = Field(default=True, description="是否自动监听并解析消息中的小黑盒链接")
    heybox_max_video_size: int = Field(default=50, description="允许下载并发送的最大视频文件大小，单位为 MB")
    heybox_proxy: str | None = Field(default=None, description="HTTP/HTTPS 请求代理地址")
    heybox_cookie: str = Field(default="", description="小黑盒合法 Session Cookie，配置后可完全绕过 show_captcha 验证码风控")
    heybox_clean_url: bool = Field(default=True, description="是否去除分享链接中的多余跟踪参数")
    heybox_render_mode: Literal["auto", "forward", "image", "direct"] = Field(
        default="auto",
        description="消息呈现模式：auto（智能选择）、forward（合并转发）、image（HTML长图）、direct（普通直接发送）",
    )
    heybox_forward_threshold_images: int = Field(default=3, description="触发合并转发的图片张数阈值（超过此数量触发，即 >3 张）")
    heybox_forward_threshold_length: int = Field(default=100, description="触发合并转发的正文字数阈值（超过此字数触发，即 >100 字）")
    heybox_max_images_forward: int = Field(default=30, description="合并转发模式下最多允许发送的图片数量")
    heybox_forward_image_type: Literal["path", "base64"] = Field(
        default="path",
        description="合并转发中图片消息段的传输格式：path（本地文件路径，默认，极低内存消耗）、base64（Base64内联数据流，强力图床转存）",
    )
    heybox_cache_expire_days: int = Field(default=3, description="本地缓存文件保留天数，0 表示不按时间自动清理")
    heybox_max_cache_mb: int = Field(default=1024, description="本地缓存目录最大允许占用的磁盘空间，单位为 MB")
    heybox_download_concurrency: int = Field(default=8, description="多图并发异步下载的最大并发协程数")


# 实例化配置对象，自动读取全局配置
plugin_config = get_plugin_config(Config)

