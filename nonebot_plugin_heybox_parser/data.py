from dataclasses import dataclass, field


@dataclass
class VideoContent:
    """小黑盒视频内容数据模型

    包含提取到的视频直链地址
    """

    video_url: str


@dataclass
class ImageContent:
    """小黑盒图集内容数据模型

    包含提取到的图片 URL 列表
    """

    pic_urls: list[str] = field(default_factory=list)


@dataclass
class HeyboxParseResult:
    """小黑盒统一解析结果模型

    封装了解析出来的帖子标题、作者名称、头像、等级、发布时间、话题标签、正文内容、互动统计、纯净链接以及具体媒体对象
    """

    title: str
    author: str | None = None
    author_avatar: str | None = None
    user_level: int | str | None = None
    created_at: str | None = None
    description: str | None = None
    cover_url: str | None = None
    link_id: str | None = None
    clean_url: str | None = None
    topics: list[str] = field(default_factory=list)
    stats: dict[str, int | str] = field(default_factory=dict)
    blocks: list[dict] = field(default_factory=list)
    hot_comments: list[dict] = field(default_factory=list)
    content: VideoContent | ImageContent | None = None

    @property
    def video_url(self) -> str | None:
        """获取视频直链 URL，若内容不是视频则返回 None"""
        return self.content.video_url if isinstance(self.content, VideoContent) else None

    @property
    def pic_urls(self) -> list[str] | None:
        """获取图集 URL 列表，若内容不是图集则返回 None"""
        return self.content.pic_urls if isinstance(self.content, ImageContent) else None

    def __str__(self) -> str:
        """返回格式化的解析结果字符串表示"""
        return (
            f"Title: {self.title}\n"
            f"Author: {self.author}\n"
            f"Cover: {self.cover_url}\n"
            f"Content: {self.content}"
        )
