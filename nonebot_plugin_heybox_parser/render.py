import base64
import io
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from nonebot import logger
from nonebot.plugin import require

from .data import HeyboxParseResult

# 模板目录与 Jinja2 渲染环境初始化
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


def _image_to_base64_fallback(image_path: Path) -> str:
    """回退降级方案：直接读取本地图片文件并转换为 Base64 编码

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
        logger.warning(f"[小黑盒渲染] 本地图片降级转 Base64 失败 ({image_path}): {err}")
        return ""


def _compress_image_for_render(
    image_path: Path,
    max_width: int = 640,
    quality: int = 75,
) -> str:
    """对本地图片进行长图渲染专用的轻量化压缩与等比缩放

    通过将图片宽度等比限制在长图视口物理尺寸（默认 640px）以内，并统一转为高压缩比 JPEG，
    将单张图片的 Base64 体积由数兆暴降至数十 KB，彻底消除 Chromium 解码 4K 大图导致的显存溢出

    Args:
        image_path (Path): 本地原图文件绝对路径
        max_width (int): 渲染允许的最大物理像素宽度（默认 640px，匹配手机截屏视口）
        quality (int): JPEG 压缩质量（默认 75，兼顾文字图像清晰度与极低体积）

    Returns:
        str: 优化后的 Base64 Data URI 字符串（若处理失败则安全降级）
    """
    try:
        from PIL import Image, ImageOps

        # 使用 PIL 读取本地图片
        with Image.open(image_path) as img:
            # 自动处理手机拍摄等场景下的 EXIF 旋转朝向，确保图片方向正确
            img = ImageOps.exif_transpose(img)

            # 获取原始像素宽高
            orig_w, orig_h = img.size

            # 若图片宽度超过长图视口最大宽度，按比例平滑缩小
            if orig_w > max_width:
                scale_ratio = max_width / float(orig_w)
                new_h = max(1, int(orig_h * scale_ratio))
                # 使用高质量 Lanczos 重采样算法平滑缩放图片
                img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)

            # 统一转换为 RGB 模式（若原图包含透明通道则使用纯黑背景填充以贴合深色主题）
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                background = Image.new("RGB", img.size, (30, 32, 37))
                alpha_channel = img.convert("RGBA").split()[3]
                background.paste(img.convert("RGB"), mask=alpha_channel)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # 保存压缩数据至内存字节流
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            buf.seek(0)
            encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
            return f"data:image/jpeg;base64,{encoded}"

    except Exception as err:
        logger.warning(f"[小黑盒渲染] 图片轻量等比压缩失败，尝试回退原始读取 ({image_path}): {err}")
        return _image_to_base64_fallback(image_path)


async def render_post_to_image(
    result: HeyboxParseResult,
    local_images: list[Path] | None = None,
) -> bytes | None:
    """将小黑盒图文解析结果渲染为手机长截屏级别的精美长图字节流

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
        # 将本地图片路径批量等比缩放并转换为轻量 Base64 内嵌格式（固定匹配 640px 视口）
        image_sources: list[str] = []
        if local_images:
            for img_path in local_images:
                if img_path.exists():
                    if b64_str := _compress_image_for_render(img_path, max_width=640, quality=75):
                        image_sources.append(b64_str)
        elif result.pic_urls:
            image_sources = result.pic_urls

        # 处理富文本分块中的图片内嵌（完整保留所有正文段落与全部插图）
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
                    # 优先使用本地等比压缩后的轻量 Base64 缓存图片
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

        # 调用 html_to_pic 生成类似手机长截屏的高清长图
        # 将 viewport 宽度调整为 640px，device_scale_factor 设为 1.0（消除超采样导致的显存几何级暴涨）
        pic_bytes = await html_to_pic(
            html=rendered_html,
            wait=0,
            type="jpeg",
            quality=80,
            viewport={"width": 640, "height": 100},
            device_scale_factor=1.0,
        )
        return pic_bytes

    except Exception as render_err:
        logger.error(f"[小黑盒渲染] 渲染长图时发生异常: {render_err}")
        return None
