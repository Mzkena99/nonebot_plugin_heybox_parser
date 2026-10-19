# nonebot-plugin-heybox-parser

适用于 NoneBot2 的小黑盒（Heybox）链接解析插件。自动解析群聊或私聊中的分享链接，支持图文长图渲染、视频下载与合并转发。

## 功能

- **链接去参**：去除分享链接中的多余跟踪参数，还原干净短链
- **长图渲染**：生成排版清晰的卡片长图（含正文与热门评论）
- **合并转发**：图文内容较多时自动以合并转发形式发送
- **视频解析**：自动解析并下载发送视频动态（超限时降级为直链）
- **缓存管理**：支持自动清理本地下载的媒体临时文件

## 安装

> **注意**：本项目暂未提交至 NoneBot 官方插件商店及 PyPI，无法通过 `nb plugin install` 或直接 `pip install nonebot-plugin-heybox-parser` 安装。请选用以下任意一种方式进行安装：

### 方式一：通过 Git 安装（推荐）

在 NoneBot 项目的虚拟环境终端中执行以下命令：

```bash
# 从 GitHub 仓库地址直接拉取并安装插件
pip install git+https://github.com/Mzkena99/nonebot_plugin_heybox_parser.git

# （可选）如需启用 HTML 卡片长图渲染功能，请安装渲染扩展依赖
pip install nonebot-plugin-htmlrender jinja2
```

安装完成后，在 NoneBot 项目中启用插件：

- **方式 A：使用 `pyproject.toml`（推荐）**

```toml
# 在 pyproject.toml 的 NoneBot 配置项中添加插件
[tool.nonebot]
# 声明加载本插件
plugins = ["nonebot_plugin_heybox_parser"]
```

- **方式 B：使用 `bot.py`**

```python
# 导入 nonebot 核心模块
import nonebot

# 加载插件
nonebot.load_plugin("nonebot_plugin_heybox_parser")
```

### 方式二：手动放置为本地插件

1. 克隆或下载本仓库代码，将 `nonebot_plugin_heybox_parser` 文件夹复制到 NoneBot 项目的 `src/plugins/` 目录下（或你的自定义插件目录）。
2. 在机器人环境内安装所需的运行依赖：

```bash
# 安装插件运行所需的基础依赖库
pip install httpx aiofiles pydantic

# （可选）如需启用 HTML 卡片长图渲染功能，请安装渲染扩展依赖
pip install nonebot-plugin-htmlrender jinja2
```

3. 确保你的机器人已加载本地插件目录（例如在 `bot.py` 中）：

```python
# 导入 nonebot 核心模块
import nonebot

# 加载本地插件目录（请根据实际目录路径调整）
nonebot.load_plugins("src/plugins")
```

## 配置项

在 NoneBot 项目的 `.env` 中按需添加配置（均为可选）：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `HEYBOX_AUTO_PARSE` | `bool` | `True` | 是否自动解析小黑盒链接 |
| `HEYBOX_CLEAN_URL` | `bool` | `True` | 是否去除多余跟踪参数 |
| `HEYBOX_RENDER_MODE` | `str` | `"auto"` | 呈现模式：`auto` / `forward` / `image` / `direct` |
| `HEYBOX_FORWARD_THRESHOLD_IMAGES` | `int` | `3` | 触发合并转发的图片张数阈值 |
| `HEYBOX_FORWARD_THRESHOLD_LENGTH` | `int` | `100` | 触发合并转发的正文字数阈值 |
| `HEYBOX_COOKIE` | `str` | `""` | 小黑盒 Cookie（防风控） |
| `HEYBOX_PROXY` | `str` | `None` | 请求代理地址 |
| `HEYBOX_MAX_VIDEO_SIZE` | `int` | `50` | 允许发送的最大视频大小（MB） |

## 使用

在群聊或私聊中直接发送小黑盒链接即可自动触发解析。
