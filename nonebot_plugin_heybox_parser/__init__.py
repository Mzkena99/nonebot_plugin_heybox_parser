import asyncio
import base64
import html
import re
from pathlib import Path

from nonebot import get_driver, logger, on_regex
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.plugin import PluginMetadata

from .config import Config, plugin_config
from .data import HeyboxParseResult, ImageContent, VideoContent
from .download import Downloader
from .parser import HeyboxParser
from .render import HAS_HTMLRENDER, render_post_to_image

# 定义 NoneBot2 插件元数据信息
__plugin_meta__ = PluginMetadata(
    name="小黑盒解析器",
    description="适用于 NoneBot2 的小黑盒（Heybox）帖子与视频自动解析插件（支持原链去参、合并转发与长图渲染）",
    usage="在群聊或私聊中发送包含小黑盒分享链接的消息即可自动解析",
    config=Config,
    type="feature",
    homepage="https://github.com/Mzkena99/nonebot_plugin_heybox_parser",
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

# 注册 Bot 启动时的缓存清理钩子
try:
    driver = get_driver()

    @driver.on_startup
    async def _on_bot_startup_cache_clean():
        """Bot 启动时后台异步执行一次缓存文件清理"""
        try:
            asyncio.create_task(downloader.clean_cache())
        except Exception as e:
            logger.debug(f"[小黑盒解析] 启动缓存清理任务创建失败: {e}")
except Exception:
    pass


async def send_forward_post(
    bot: Bot,
    event: MessageEvent,
    result: HeyboxParseResult,
    local_images: list[Path],
    display_url: str,
    render_image: bytes | None = None,
) -> bool:
    """将小黑盒解析内容打包为 OneBot V11 合并转发消息节点并发送

    Args:
        bot (Bot): OneBot V11 Bot 实例
        event (MessageEvent): 触发的消息事件
        result (HeyboxParseResult): 小黑盒解析结果数据模型
        local_images (list[Path]): 本地已下载缓存的图片路径列表
        display_url (str): 格式化展示的小黑盒链接
        render_image (bytes | None): 预渲染生成的 HTML 长图二进制字节流，若提供则作为节点打包进聊天记录

    Returns:
        bool: 发送是否成功（若协议端不支持或发送异常返回 False，便于自动降级）
    """
    try:
        sender_id = int(bot.self_id) if str(bot.self_id).isdigit() else 0
        sender_name = "小黑盒解析"
        nodes: list[MessageSegment] = []

        # 节点 1：帖子概览与基本信息（包含标题、作者、等级、时间、话题、规范链接）
        header_lines = [f"【小黑盒】{result.title}"]
        if result.author:
            level_str = f" (Lv.{result.user_level})" if result.user_level else ""
            header_lines.append(f"作者: {result.author}{level_str}")
        if result.created_at:
            header_lines.append(f"时间: {result.created_at}")
        if result.topics:
            header_lines.append(f"话题: {' '.join(['#' + t for t in result.topics[:3]])}")
        header_lines.append(f"链接: {display_url}")

        header_text = "\n".join(header_lines)
        nodes.append(
            MessageSegment.node_custom(
                user_id=sender_id,
                nickname=sender_name,
                content=Message(header_text),
            )
        )

        # 节点 2：排版精美的 HTML 长图（若成功渲染，以 Base64 方式打包进聊天记录）
        if render_image:
            try:
                # 将长图二进制数据转换为 Base64 编码字符串
                b64_render = base64.b64encode(render_image).decode("utf-8")
                nodes.append(
                    MessageSegment.node_custom(
                        user_id=sender_id,
                        nickname="小黑盒长图",
                        content=Message(MessageSegment.image(f"base64://{b64_render}")),
                    )
                )
            except Exception as render_node_err:
                # 长图节点打包失败时记录警告，不影响后续正文与原图发送
                logger.warning(f"[小黑盒解析] 打包长图节点失败: {render_node_err}")

        # 节点 3：正文完整描述文本（方便用户长按复制文本内容）
        if result.description:
            nodes.append(
                MessageSegment.node_custom(
                    user_id=sender_id,
                    nickname=result.author or "小黑盒用户",
                    content=Message(result.description),
                )
            )

        # 节点 3...N：图集图片（突破 9 张限制，按上限配置完整发送）
        # 将本地图片转换为 Base64 编码以 base64:// 协议发送，
        # 确保 OneBot 协议端能直接上传图片流至腾讯持久化图床，避免本地路径导致图片即时过期
        max_images = plugin_config.heybox_max_images_forward
        for img_path in local_images[:max_images]:
            if img_path.exists():
                try:
                    # 读取本地图片文件的二进制数据并进行 Base64 编码
                    with open(img_path, "rb") as f:
                        img_base64 = base64.b64encode(f.read()).decode("utf-8")
                    # 使用 base64:// 协议构建图片消息段并挂载到转发节点
                    nodes.append(
                        MessageSegment.node_custom(
                            user_id=sender_id,
                            nickname=result.author or "小黑盒用户",
                            content=Message(MessageSegment.image(f"base64://{img_base64}")),
                        )
                    )
                except Exception as img_err:
                    # 若单张图片转换失败则记录警告日志并继续处理下一张图片
                    logger.warning(f"[小黑盒解析] 合并转发图片编码 Base64 失败 ({img_path}): {img_err}")

        # 根据消息事件类型调用对应的合并转发 API
        if isinstance(event, GroupMessageEvent):
            await bot.send_group_forward_msg(group_id=event.group_id, messages=nodes)
        elif isinstance(event, PrivateMessageEvent):
            await bot.send_private_forward_msg(user_id=event.user_id, messages=nodes)
        else:
            await bot.call_api("send_forward_msg", messages=nodes)

        logger.info(f"[小黑盒解析] 成功以合并转发方式发送小黑盒帖子: {result.title}")
        return True

    except Exception as fwd_err:
        logger.warning(f"[小黑盒解析] 合并转发消息发送失败 ({fwd_err})，准备降级为普通消息")
        return False


@heybox_matcher.handle()
async def handle_heybox_share(bot: Bot, event: MessageEvent):
    """处理小黑盒分享链接消息的核心事件回调函数

    提取消息中的小黑盒 URL，解析并智能分发为视频、HTML长图、合并转发或普通图文
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

        # 决定展示的链接（是否启用参数精简）
        display_url = (
            result.clean_url
            if (plugin_config.heybox_clean_url and result.clean_url)
            else share_url
        )

        # 情况 1：处理视频类型内容
        if isinstance(result.content, VideoContent):
            video_url = result.content.video_url

            # 构造基础信息文本（包含标题、作者、原链及视频摘要）
            msg_header = f"【小黑盒】{result.title}\n作者: {result.author or '匿名'}\n链接: {display_url}"
            if result.description:
                desc = (
                    result.description
                    if len(result.description) <= 300
                    else f"{result.description[:300]}..."
                )
                msg_header += f"\n\n内容:\n{desc}"
            await heybox_matcher.send(Message(msg_header))

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
            return

        # 情况 2：处理图文/多图内容（异步并发下载所有图片至本地缓存，严格保序）
        local_images: list[Path] = []
        pic_urls = result.content.pic_urls if isinstance(result.content, ImageContent) else []

        if pic_urls:
            logger.info(f"[小黑盒解析] 开始并发下载图集资源，共 {len(pic_urls)} 张图片")
            max_download = plugin_config.heybox_max_images_forward
            target_urls = pic_urls[:max_download]
            download_tasks = [downloader.download_image(u) for u in target_urls]
            download_results = await asyncio.gather(*download_tasks, return_exceptions=True)

            for idx, dl_res in enumerate(download_results):
                if isinstance(dl_res, Path) and dl_res.exists():
                    local_images.append(dl_res)
                elif isinstance(dl_res, Exception):
                    logger.warning(f"[小黑盒解析] 图片下载失败 ({target_urls[idx]}): {dl_res}")

        # 判断是否触发聊天记录打包条件（图片数量 > 3 或 文字 > 100 字）
        desc_length = len(result.description or "")
        num_images = len(local_images)
        is_heavy_content = (
            num_images > plugin_config.heybox_forward_threshold_images
            or desc_length > plugin_config.heybox_forward_threshold_length
        )

        # 分支 1：显式配置为长图模式（image），仅渲染并发送长图，不在外部追加合并转发
        if plugin_config.heybox_render_mode == "image" and HAS_HTMLRENDER:
            try:
                # 渲染 HTML 长图字节流
                pic_bytes = await render_post_to_image(result, local_images)
                if pic_bytes:
                    render_msg = Message([
                        MessageSegment.text(f"【小黑盒】{result.title}\n链接: {display_url}\n"),
                        MessageSegment.image(pic_bytes),
                    ])
                    await heybox_matcher.send(render_msg)
                    return
            except Exception as render_err:
                logger.warning(f"[小黑盒解析] 长图生成失败 ({render_err})，降级为后续逻辑处理")

        # 分支 2：触发打包聊天记录分支（auto 模式下满足阈值或显式配置为 forward 模式）
        # 满足条件时直接发送合并转发聊天记录，并将 HTML 长图作为其中一个节点打包发送，外部不重复发送
        if (is_heavy_content or plugin_config.heybox_render_mode == "forward") and plugin_config.heybox_render_mode != "direct":
            # 若支持 HTML 渲染，先异步渲染出高清长图数据，以便打包进聊天记录中
            render_bytes = None
            if HAS_HTMLRENDER:
                try:
                    render_bytes = await render_post_to_image(result, local_images)
                except Exception as render_err:
                    logger.warning(f"[小黑盒解析] 嵌入聊天记录的长图生成失败 ({render_err})，仅打包文本与图集")

            # 发送打包好的合并转发消息（包含基本信息节点、长图节点、正文节点、所有原图图集节点）
            forward_success = await send_forward_post(
                bot=bot,
                event=event,
                result=result,
                local_images=local_images,
                display_url=display_url,
                render_image=render_bytes,
            )
            # 发送成功则直接退出，确保外部不会再多发一条消息
            if forward_success:
                return

            # 合并转发失败时的降级方案：若支持 HTML 渲染则优先以单张长图降级发送
            if HAS_HTMLRENDER:
                try:
                    pic_bytes = await render_post_to_image(result, local_images)
                    if pic_bytes:
                        render_msg = Message([
                            MessageSegment.text(f"【小黑盒】{result.title}\n链接: {display_url}\n"),
                            MessageSegment.image(pic_bytes),
                        ])
                        await heybox_matcher.send(render_msg)
                        return
                except Exception as render_err:
                    logger.warning(f"[小黑盒解析] 降级长图生成失败 ({render_err})，继续降级为普通图文")

        # 普通直接发送分支（未触发打包阈值的少图/短文轻量动态，或合并转发异常降级）
        msg_header = f"【小黑盒】{result.title}\n作者: {result.author or '匿名'}\n链接: {display_url}"
        if result.description:
            desc = (
                result.description
                if len(result.description) <= 300
                else f"{result.description[:300]}..."
            )
            msg_header += f"\n\n内容:\n{desc}"
        await heybox_matcher.send(Message(msg_header))

        if local_images:
            max_direct_images = 9
            direct_images = local_images[:max_direct_images]
            image_segments = [MessageSegment.image(p) for p in direct_images]
            if image_segments:
                await heybox_matcher.send(Message(image_segments))

            if len(local_images) > max_direct_images:
                await heybox_matcher.send(
                    f"📷 剩余 {len(local_images) - max_direct_images} 张图片已自动折叠。"
                )

    except Exception as err:
        logger.error(f"[小黑盒解析] 解析小黑盒链接失败: {err}")
        return
