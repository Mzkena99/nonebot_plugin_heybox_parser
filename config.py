from pydantic import BaseModel, Field
from nonebot import get_plugin_config


class Config(BaseModel):
    """小黑盒解析插件配置类

    使用 Pydantic 管理从 Environment 或 .env 读取的插件配置参数
    """

    heybox_auto_parse: bool = Field(default=True, description="是否自动监听并解析消息中的小黑盒链接")
    heybox_max_video_size: int = Field(default=50, description="允许下载并发送的最大视频文件大小，单位为 MB")
    heybox_proxy: str | None = Field(default=None, description="HTTP/HTTPS 请求代理地址")
    heybox_cookie: str = Field(default="", description="小黑盒合法 Session Cookie，配置后可完全绕过 show_captcha 验证码风控")


# 实例化配置对象，自动读取全局配置
plugin_config = get_plugin_config(Config)
