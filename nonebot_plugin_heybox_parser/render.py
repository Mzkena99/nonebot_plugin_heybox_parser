import base64
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from nonebot import logger
from nonebot.plugin import require

from .data import HeyboxParseResult

# 模板目录与 Jinja2 环境初始化
TEMPLATE_DIR = Path(__file__).parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=True,
    enable_async=False,
)

# 动态加载 nonebot_plugin_htmlrender 依赖
try:
    require("nonebot_plugin_htmlrender")
    from nonebot_plugin_htmlrender import html_to_pic

    HAS_HTMLRENDER = True
except Exception:
    HAS_HTMLRENDER = False


def _image_to_base64(image_path: Path) -> str:
    """将本地图片文件转换为 Base64 Data URI，方便无头浏览器直接内嵌加载

    Args:
        image_path (Path): 本地图片文件路径

    Returns:
        str: Base64 编码的 Data URI 字符串
    """
    try:
        suffix = image_path.suffix.lower().lstrip(".")
        mime_type = f"image/{suffix}" if suffix in ("png", "gif", "webp") else "image/jpeg"
        with open(image_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"
    except Exception as err:
        logger.warning(f"[小黑盒渲染] 本地图片转 Base64 失败 ({image_path}): {err}")
        return ""


async def render_post_to_image(
    result: HeyboxParseResult,
    local_images: list[Path] | None = None,
) -> bytes | None:
    """将小黑盒图文解析结果渲染为长图字节流

    Args:
        result (HeyboxParseResult): 小黑盒解析结果数据模型
        local_images (list[Path] | None): 本地已缓存的图片路径列表

    Returns:
        bytes | None: 渲染生成的图片字节数据，若未安装 htmlrender 或渲染异常则返回 None
    """
    if not HAS_HTMLRENDER:
        logger.debug("[小黑盒渲染] 未检测到 nonebot_plugin_htmlrender，跳过长图渲染")
        return None

    try:
        # 将本地图片路径批量转换为 Base64 内嵌格式
        image_sources: list[str] = []
        if local_images:
            for img_path in local_images:
                if img_path.exists():
                    if b64_str := _image_to_base64(img_path):
                        image_sources.append(b64_str)
        elif result.pic_urls:
            image_sources = result.pic_urls

        # 处理富文本分块中的图片内嵌
        processed_blocks: list[dict] = []
        img_idx = 0
        if result.blocks:
            for block in result.blocks:
                if not isinstance(block, dict):
                    continue
                b_type = block.get("type")
                if b_type == "text":
                    processed_blocks.append({
                        "type": "text",
                        "text": block.get("text", "").strip(),
                    })
                elif b_type == "img":
                    # 优先使用本地 Base64 缓存图片
                    img_src = image_sources[img_idx] if img_idx < len(image_sources) else block.get("url") or block.get("src")
                    img_idx += 1
                    processed_blocks.append({
                        "type": "img",
                        "src": img_src,
                    })
                elif b_type == "game":
                    processed_blocks.append({
                        "type": "game",
                        "name": block.get("name") or block.get("title", ""),
                        "score": block.get("score") or block.get("rating"),
                    })

        # 准备模板上下文变量
        template = jinja_env.get_template("post.html")
        rendered_html = template.render(
            title=result.title,
            author=result.author,
            author_avatar=result.author_avatar,
            user_level=result.user_level,
            created_at=result.created_at,
            description=result.description,
            topics=result.topics,
            stats=result.stats,
            blocks=processed_blocks,
            hot_comments=result.hot_comments,
            images=image_sources,
            clean_url=result.clean_url or (f"https://www.xiaoheihe.cn/bbs/post_share?link_id={result.link_id}" if result.link_id else None),
        )

        # 调用 html_to_pic 生成长图（优化为高质量 JPEG 压缩，大幅减小体积并保证清晰度）
        pic_bytes = await html_to_pic(
            html=rendered_html,
            wait=0,
            type="jpeg",
            quality=85,
            viewport={"width": 680, "height": 100},
            device_scale_factor=1.5,  # 适度缩放兼顾高清排版与体积控制
        )
        return pic_bytes

    except Exception as render_err:
        logger.error(f"[小黑盒渲染] 渲染长图时发生异常: {render_err}")
        return None
