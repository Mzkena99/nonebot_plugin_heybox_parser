import html
import re

from nonebot import logger, on_regex
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.plugin import PluginMetadata

from .config import Config, plugin_config
from .data import ImageContent, VideoContent
from .download import Downloader
from .parser import HeyboxParser

# 定义 NoneBot2 插件元数据信息
__plugin_meta__ = PluginMetadata(
    name="小黑盒解析器",
    description="适用于 NoneBot2 的小黑盒（Heybox）帖子与视频自动解析插件",
    usage="在群聊或私聊中发送包含小黑盒分享链接的消息即可自动解析",
    config=Config,
    type="feature",
    homepage="https://github.com/AkiyamaMizuki/nonebot-plugin-heybox-parser",
)

# 正则表达式：匹配小黑盒分享域名及短链（兼容纯文本、JSON 卡片、XML 小程序中的链接）
HEYBOX_URL_REGEX = r"(https?://(?:[a-zA-Z0-9-]+\.)*xiaoheihe\.cn/[^\s\"\'\\]+)"

# 实例化事件响应器
heybox_matcher = on_regex(
    pattern=HEYBOX_URL_REGEX,
    priority=5,
    block=False,
)

# 实例化解析器与下载器
parser = HeyboxParser()
downloader = Downloader()


@heybox_matcher.handle()
async def handle_heybox_share(event):
    """处理小黑盒分享链接消息的事件回调函数

    提取消息中的小黑盒 URL，解析并下载相关音视频/图集，格式化发送给用户
    """
    if not plugin_config.heybox_auto_parse:
        return

    # 提取整条消息文本（解转义 HTML 实体字符 &amp;，兼容纯文本、JSON 卡片、XML 小程序）
    raw_message = html.unescape(str(event.get_message())).replace("\\/", "/")
    matched = re.search(HEYBOX_URL_REGEX, raw_message)
    if not matched:
        return

    # 提取并清理链接尾部的引号或转义字符
    share_url = matched.group(1).rstrip("\"'\\")
    logger.info(f"[小黑盒解析] 捕获到小黑盒分享链接: {share_url}")

    try:
        # 执行链接解析
        result = await parser.parse_url(share_url)
        logger.info(f"[小黑盒解析] 解析成功: Title='{result.title}', Author='{result.author}'")

        # 构造基础信息文本（包含标题、作者、原链及帖子描述正文）
        msg_header = f"【小黑盒】{result.title}\n作者: {result.author or '匿名'}\n链接: {share_url}"
        if result.description:
            # 限制文字预览长度，防止文本过长造成刷屏
            desc = result.description if len(result.description) <= 300 else f"{result.description[:300]}..."
            msg_header += f"\n\n内容:\n{desc}"
        await heybox_matcher.send(Message(msg_header))

        # 处理不同内容类型
        if isinstance(result.content, VideoContent):
            # 处理视频类型内容
            video_url = result.content.video_url
            try:
                # 下载视频至本地缓存
                local_video_path = await downloader.download_video(video_url)
                # 使用 OneBot V11 视频消息段发送本地文件
                video_segment = MessageSegment.video(local_video_path)
                await heybox_matcher.send(video_segment)
            except Exception as vid_err:
                logger.error(f"[小黑盒解析] 视频下载或发送失败: {vid_err}")
                # 降级处理：若下载失败则发送视频直链
                await heybox_matcher.send(f"🎥 视频直链:\n{video_url}")

        elif isinstance(result.content, ImageContent):
            # 处理图集类型内容
            pic_urls = result.content.pic_urls
            if not pic_urls:
                return

            logger.info(f"[小黑盒解析] 开始处理图集，共 {len(pic_urls)} 张图片")
            # 限制单次最多发送图片张数，防止爆屏
            max_images = 9
            images_to_send = pic_urls[:max_images]

            image_segments = []
            for img_url in images_to_send:
                try:
                    # 下载图片并生成消息段
                    local_img_path = await downloader.download_image(img_url)
                    image_segments.append(MessageSegment.image(local_img_path))
                except Exception as img_err:
                    logger.warning(f"[小黑盒解析] 图片下载失败，尝试发送 URL: {img_err}")
                    image_segments.append(MessageSegment.image(img_url))

            # 批量发送图片消息
            if image_segments:
                await heybox_matcher.send(Message(image_segments))

            if len(pic_urls) > max_images:
                await heybox_matcher.send(f"📷 剩余 {len(pic_urls) - max_images} 张图片已自动折叠。")

    except Exception as err:
        logger.error(f"[小黑盒解析] 解析小黑盒链接失败: {err}")
        # 如果解析异常不阻断后续其他插件
        return
