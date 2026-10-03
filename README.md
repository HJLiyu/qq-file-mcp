# QQ File MCP

**在 Codex 中按群名和文件名查找 QQ 群资料，并下载到电脑。**

无需让 Agent 逐步识图、点击 QQ 窗口。工具通过本机 OneBot 接口实时查询资料，再通过 MCP 返回候选文件和搜索范围。Windows 可使用 SnowLuma 加载到正在运行的桌面 QQ，在同一账号下聊天、查群文件和下载。

```text
你：找「示例学习群」里文件名包含「课件」的文件。
Codex → qq_search_files → 群文件目录 + 可获取的聊天附件
你：下载第二个。
Codex → qq_download_file → 本机路径、文件大小、SHA-256
```

这是单用户、自行部署的项目。QQ、桥接工具、MCP 工具和 Codex 运行在同一台电脑；电脑关闭时服务离线。无需租服务器，也不调用额外的模型 API。Codex 自身的账号与使用额度照常适用。

**需要同时聊天和查文件时，使用桌面会话接入方式。** 原有 NapCat Shell 安装仍可使用，但它独立登录 QQ，同账号与桌面 QQ 可能发生登录冲突；两种方式不要一起启动。

## 能做什么

- 用群名、群名关键词或群号定位已加入的群；同名群先供选择。
- 实时查询根目录及接口返回的一级文件夹，不预同步。
- 文件名支持完整名称、部分名称、大小写与全半角归一化；明确要求全部文件时可传 `*`。
- 聊天附件默认扫描最近 **1,000 条可获取消息**，可分段继续向前查询。
- 返回实际扫描条数、时间范围、截断或错误原因，避免将“未扫描到”当成“不存在”。
- 下载前重新确认文件，保留同名文件，返回 SHA-256；不会执行下载内容。
- 只暴露状态、群查询、文件检索和下载工具。

## 快速开始：Windows 桌面 QQ 共享会话

需要 Python 3.11+、官方 QQNT、Codex。保留电脑 QQ 的正常登录。

```powershell
git clone https://github.com/HJLiyu/qq-file-mcp.git
cd qq-file-mcp
./scripts/setup.ps1
Copy-Item .env.example .env
```

按 [桌面 QQ 接入说明](docs/desktop-qq.md) 手动准备官方 SnowLuma 发行包，启动控制台并加载已有 QQ 进程。核对 OneBot HTTP 服务绑定 `127.0.0.1`、令牌非空、消息格式 `array`，关闭桥接的自动状态回复命令。在 `.env` 中填写：

```dotenv
QQ_FILE_BACKEND=snowluma
ONEBOT_URL=http://127.0.0.1:3000
ONEBOT_TOKEN=你的本地OneBot访问令牌
```

检查连接并注册工具：

```powershell
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env doctor
./scripts/register-codex.ps1
```

重新加载 Codex 的 MCP 连接或开启新对话，让 Codex 调用 `qq_status`、`qq_search_files`。电脑 QQ 继续聊天，工具复用这个登录会话。退出 QQ 或桥接工具后，实时检索暂不可用。

**聊天历史限制：** SnowLuma v1.14.20 的原版接口需要已观察到该群的消息，才能从这个起点向前取历史；首次加载后安静的群可能返回空。打开群窗口不保证能补齐起点。本工具返回 `HISTORY_ANCHOR_UNAVAILABLE` 和实际覆盖范围；群文件目录不受此限制。本仓库没有提供修改版桥接程序，详见接入说明。

## 原有方式：Windows NapCat Shell

需要 Python 3.11+、已安装的官方 QQNT、Codex，以及可以访问目标群的 QQ 账号。

```powershell
git clone https://github.com/HJLiyu/qq-file-mcp.git
cd qq-file-mcp
./scripts/setup.ps1 -WithNapCat
./scripts/start-napcat.ps1 -OpenWebUI
```

先关闭桌面 QQ，再在本地 NapCat 页面完成 QQ 登录。安装脚本将：

1. 创建 `.venv`，安装本项目依赖。
2. 从 NapCat 项目发布页下载固定版本 **v4.18.28**，核对 SHA-256 后解压到 `.local/`。
3. 从已安装 QQ 的运行目录复制该发布包缺少的 `crypto.dll`、`ssl.dll`。不修改 QQ 安装文件。
4. 生成本地令牌、`.env` 和仅绑定 `127.0.0.1` 的配置。

QQ 安装在其他位置时：

```powershell
./scripts/setup.ps1 -WithNapCat -QQInstallDirectory 'D:\Apps\QQNT'
```

检查连接、接入 Codex：

```powershell
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env doctor
./scripts/register-codex.ps1
```

重新加载 Codex 的 MCP 连接或开启新对话。让 Codex 调用 `qq_status`，再要求它搜索群文件。注册命令中只有配置文件路径，访问令牌保留在本机 `.env` 中。

查完后返回日常电脑 QQ：

```powershell
./scripts/stop-napcat.ps1
# 然后自行打开桌面 QQ 并登录。
```

停止脚本只终止本项目 `.local/napcat-node/node.exe` 对应的进程，保留 QQ 登录数据和已下载的文件。停止期间 MCP 的实时检索与下载不可用，下次需要时再次关闭桌面 QQ、运行 `start-napcat.ps1 -OpenWebUI` 并登录。

> NapCat 是第三方接入，非腾讯官方开放 API，可能出现掉线、登录验证和账号风控。请阅读 [NapCat 安全说明](https://napneko.github.io/other/security)，再决定使用哪个账号。程序没有发消息、删文件或管理群的 MCP 工具，但 NapCat 本身有更广的能力，因此其接口必须保持本机绑定与令牌鉴权。

## 已有 OneBot 服务 / 其他系统

本项目支持同一台机器上的 NapCat 或 SnowLuma HTTP 服务。Windows 安装脚本只提供 Python 工具和可选 NapCat Shell 环境；其他系统的 QQ 运行环境需自行准备。

```bash
python -m venv .venv
# 激活对应系统的虚拟环境后：
python -m pip install -e .
cp .env.example .env
```

启用 HTTP 服务：`127.0.0.1:3000`、设置非空 token、消息格式 `array`（NapCat 名称为 `messagePostFormat`，SnowLuma 为 `messageFormat`）。把相同 token 写入 `.env` 的 `ONEBOT_TOKEN`，并设置 `QQ_FILE_BACKEND=napcat` 或 `snowluma`。连接地址只允许本机回环。

旧配置中的 `NAPCAT_URL` / `NAPCAT_TOKEN` 仍有效；没有设置后端时默认 `napcat`。新的 `ONEBOT_*` 配置优先。文件和分页引用绑定接入方式，切换后会拒绝旧引用并要求重新搜索；推荐使用独立 `QQ_FILE_STATE_DIR`。

优先使用 QQ HTTPS 文件链接下载。如果当前 NapCat 不支持链接接口，将尝试 QQ 本地缓存；此时需要通过 `QQ_FILE_ALLOWED_ROOTS` 指定允许读取的 QQ 缓存目录。不要将整个磁盘或个人主目录加入允许列表。SnowLuma 的 `get_file` 只适用于图片/语音缓存，群文件下载链接不可用时会报错，不会尝试该后备路径。

## 命令行

```powershell
# 搜群
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env groups '示例学习群'

# 搜群文件与最近可获取的聊天附件
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env search '示例学习群' '课件'

# 列出群文件（此命令不自动批量下载）
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env search '示例学习群' '*' --source group_files

# 继续向前搜索聊天附件，保持同一群和关键词
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env search '示例学习群' '课件' --history-cursor '上次返回的标识'

# 下载选定文件；使用搜索返回的 result_id
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env download '选定结果的标识'
```

默认下载目录：`~/Downloads/QQ-File-MCP`。可用 `.env` 中的 `QQ_FILE_DOWNLOAD_DIR` 修改。

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `qq_status` | 检查连接和登录 |
| `qq_find_groups` | 按群名或群号定位群 |
| `qq_search_files` | 文件名查询；`source=both/group_files/history` |
| `qq_more_results` | 读取同一轮搜索的其余候选，每页 50 条 |
| `qq_download_file` | 下载选定 `result_id` 对应的文件 |

## 范围与限制

- 同账号聊天与群文件工具已在 Windows QQNT + SnowLuma v1.14.20 中验证。上游版本、QQ 版本和登录状态改变后仍需重新验证；独立 NapCat Shell 不属于共享会话方式。
- 聊天历史仅覆盖当前 QQ 会话实际能取回的内容，无法保证任意年份的消息都可用。
- 合并转发中的文件、在线文件和多层嵌套目录尚未支持；普通聊天 `file` 附件已实现。
- 群文件默认每个目录最多处理 10,000 项、最多查 100 个一级目录；聊天每轮最多 5,000 条。即使上游忽略请求数量，也在客户端限制目录处理。达到上限会在 coverage/warnings 中说明。
- 两种来源独立限时，默认每种最多 40 秒；一个来源失败不会丢弃另一来源的结果。
- 超时或到达范围上限后，以返回的范围为准。空结果不等于完整历史中不存在。
- 文件结果及继续查询标识默认保留一小时。QQ 会话重启后，聊天消息标识可能失效，需要重新搜索。
- 同一文件可能分别出现在群目录和聊天附件中，本版保留来源，不通过名称猜测它们是同一文件。
- 默认单文件大小上限 512 MiB；过期文件、权限不足或内容大小变化会明确报错。
- 原生 QQ 下载在某些运行环境中可能超时；本版优先使用受限 QQ HTTPS 下载，NapCat 的原生缓存作为后备路径。

## 开发与验证

```powershell
./.venv/Scripts/python.exe -m pip install -e '.[dev]'
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m ruff check src tests scripts
./.venv/Scripts/python.exe -m build
```

测试覆盖两种后端的历史参数差异、同秒消息排序、超过 50 个文件、目录检索、群名歧义、历史翻页重复边界、非连续消息 ID、账号切换、结果过期、同名文件、下载路径、链接重定向、令牌隔离，以及真实 stdio MCP 握手。

测试和示例不包含真实 QQ 消息。`.env`、`.local/`、会话配置、检索结果与下载文件均不应提交到 Git。

架构与验证边界见 [docs/architecture.md](docs/architecture.md)。

## 上游与许可

本项目独立编写的客户端代码使用 MIT 许可。QQ、NapCat、SnowLuma 是独立运行依赖，遵循各自的许可和使用规则；仓库不包含其源代码、修改版或二进制文件。SnowLuma 使用[源码可见非商业许可](https://github.com/SnowLuma/SnowLuma/blob/v1.14.20/LICENSE)，还需阅读其 [EULA](https://github.com/SnowLuma/SnowLuma/blob/v1.14.20/EULA.md)。MCP 使用 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)。
