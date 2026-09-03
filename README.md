# nonebot-plugin-heybox-parser

适用于 NoneBot2 的小黑盒（Heybox）链接解析插件。自动解析群聊或私聊中的分享链接，支持图文长图渲染、视频下载与合并转发。

## 功能

- **链接去参**：去除分享链接中的多余跟踪参数，还原干净短链
- **长图渲染**：生成排版清晰的卡片长图（含正文与热门评论）
- **合并转发**：图文内容较多时自动以合并转发形式发送
- **视频解析**：自动解析并下载发送视频动态（超限时降级为直链）
- **缓存管理**：支持自动清理本地下载的媒体临时文件

## 安装

### 方式 1：使用 nb-cli 或 pip 安装（推荐）

```bash
nb plugin install nonebot-plugin-heybox-parser
# 或
pip install nonebot-plugin-heybox-parser
```

### 方式 2：从源码本地安装

将本项目克隆到本地后，在 NoneBot 环境中执行可编辑安装：

```bash
pip install -e /path/to/nonebot-plugin-heybox-parser
```

> **提示**：如需支持 HTML 长图渲染，请确保安装了渲染依赖：
> ```bash
> pip install nonebot-plugin-htmlrender jinja2
> playwright install chromium
> ```

## 配置项

在 NoneBot 项目的 `.env` 中按需添加配置（均为可选）：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `HEYBOX_AUTO_PARSE` | `bool` | `True` | 是否自动解析消息中的小黑盒链接 |
| `HEYBOX_CLEAN_URL` | `bool` | `True` | 是否自动精简分享链接多余参数 |
| `HEYBOX_RENDER_MODE` | `str` | `"auto"` | 呈现模式：`auto` / `forward` / `image` / `direct` |
| `HEYBOX_FORWARD_THRESHOLD_IMAGES` | `int` | `3` | 触发合并转发的图片张数阈值（>3张触发） |
| `HEYBOX_FORWARD_THRESHOLD_LENGTH` | `int` | `100` | 触发合并转发的正文字数阈值（>100字触发） |
| `HEYBOX_MAX_IMAGES_FORWARD` | `int` | `30` | 合并转发最多允许发送的图片数量 |
| `HEYBOX_COOKIE` | `str` | `""` | 小黑盒有效 Session Cookie（遇验证码风控时配置） |
| `HEYBOX_PROXY` | `str` | `None` | 请求代理地址（如 `http://127.0.0.1:7890`） |
| `HEYBOX_MAX_VIDEO_SIZE` | `int` | `50` | 允许下载发送的最大视频体积（MB） |

## 使用

在群聊或私聊中直接发送包含小黑盒分享链接的消息，机器人将自动捕获并解析分发。

## 免责声明

1. 本项目仅供 Python 编程学习、网络技术研究以及个人交流使用，严禁用于任何商业营利、非法数据采集或侵害第三方权益的活动。
2. 本项目解析所获取的所有小黑盒帖子、图文、视频及用户相关数据，其全部知识产权和著作权均归属于原平台（小黑盒 / 清水县爱黑盒信息科技有限公司）及其合法创作者所有。
3. 请合理合法地使用本项目。因使用者不当调用、滥用请求频次、用于侵权传播等行为所导致的任何风控、封禁、侵权纠纷或法律责任，均由使用者本人全权承担，本项目开发者不承担任何连带与法律责任。
4. 若小黑盒官方认为本项目侵犯了相关合法权益，请通过 GitHub Issue 联系维护者，我们将第一时间响应并配合处理。

## 许可证

本项目遵循 [MIT License](LICENSE) 开源许可协议。
