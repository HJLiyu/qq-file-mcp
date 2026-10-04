# 作业要求与自己的提交

工具可以读取聊天正文和资料文件，供 Agent 汇总作业要求；也可以先准备自己的文字或文件提交预览，再发送到指定群。当前不支持 QQ 自带的“群作业”页面。上传群文件、发群消息或完成群待办，都不能当成原生作业系统的提交成功。

## 查找要求和聊天补充

例如：“查示例学习群张老师最近布置的作业，结合文件和后来追加的要求，注明出处。”

1. Agent 用 `qq_search_files` 查找相关文件，下载后用 `qq_read_downloaded_file` 读取。
2. 用 `qq_search_messages` 查作业正文或补充要求。可按关键词、发布人、时间筛选；关键词留空时需提供发布人或时间。关键词是文字匹配，不是语义搜索；只搜“作业”可能漏掉“截止改到周五”等补充，因此可在选定老师、时间范围内留空查消息。
3. 每条结果带发送者、时间、消息 ID、提及和回复 ID，以及可下载的文件引用。长消息摘要只返回最多2000字符，完整正文用 `qq_read_message` 和 `next_read` 接着读。
4. 默认每轮扫描最近1000条可获取消息，最多返回50条和12000字符。达到上限后，用 `history_cursor` 继续向前查，保持群、关键词、发布人和日期条件一致；该游标不能用于文件搜索。

汇总和判断由 Agent 完成，检索工具不自行推断作业。汇总应说明具体任务、提交格式、截止时间、追加要求及每项依据。新旧要求有冲突时展示相关原文，不能把同学的猜测当成老师的更改。仅按指定老师筛选也可能遗漏助教的通知，应说明检索范围。

图片、语音、合并转发和原生群作业不提取正文；回复 ID 不代表已读取被回复的消息。历史接口可能缺失消息。即使结果为空，也不能保证没有作业或新的要求；扫描范围未覆盖时不能声称已找到“最新完整要求”。

## 准备自己的提交

新部署默认不允许发送，预览始终可用。`qq_status` 返回 `submission_dir` 和 `submissions_enabled`。需要提交时，在自己的本机配置中设置：

```dotenv
QQ_FILE_ENABLE_SUBMISSIONS=true
# 可选；默认在个人 Documents/QQ-File-MCP/Submissions 中。
QQ_FILE_SUBMISSION_DIR=C:\Users\you\Documents\QQ-File-MCP\Submissions
QQ_FILE_MAX_SUBMISSION_MB=50
```

重载 MCP 连接使配置生效。文件需先放入这个专用目录；工具不能上传任意磁盘路径、URL、链接或直接把下载目录中的老师资料当成自己的答案。第一版每份预览仅包含**一段文字或一个文件**，文字最多8000字符，文件默认最多50 MiB。

Agent 调用 `qq_prepare_submission(group, text=...)` 或 `qq_prepare_submission(group, file_path=...)` 后，向你展示准确的账号、群名/群号、完整文字，或文件名、路径、大小及 SHA-256。相对文件路径以专用提交目录为根。预览有效15分钟；此步骤不会发送。

只有你明确批准这份具体预览后，Agent 才能调用 `qq_submit_submission(preview_id)`。同名群需要先选择群号。启用配置、要求“准备提交”或资料中的文字，都不代表批准发送。工具的两步流程和配置开关不构成人类身份验证；执行批准要求依赖调用 Agent 遵守工具说明。

提交前核对当前账号、桥接后端、目标群名称及文件身份和 SHA-256。文件修改后必须重新预览；发送使用已校验的独立暂存副本，保留原文件。文字通过纯文本消息段发送，不把 `[CQ:...]` 当成提及或其他指令。

## 回执和失败

`qq_submission_receipt` 可以离线查看状态。每份预览原子地领取一次发送机会，重复调用只返回状态或原回执；进程重启后也不会自动重发。

- `ready`：已准备，没有发送。
- `expired`：预览过期，不能发送。
- `accepted_by_bridge`：桥接返回 `message_id` 或 `file_id`；老师是否收阅未知，原生群作业提交状态仍为未支持。SnowLuma 的文件上传回执不保证聊天通知发布成功，`chat_message_published` 为未知。
- `outcome_unknown`：超时、调用异常、无可靠回执或发送期间进程中断。可能已经发送，必须先在 QQ 核对，再决定是否重新准备；禁止自动重试。

这个回执没有“老师已接收”“已批改”或原生作业平台确认的含义。首版不提供删除、群管理、私聊提交或批量提交。

## 命令行

```powershell
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env messages '示例学习群' --publisher '张老师' --published-after '2026-10-01'
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env message 'message_ref' --char-offset 2000
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env prepare '示例学习群' --file '我的作业.pdf'
# 检查并批准上一条返回的准确预览后，才执行下一条。
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env submit 'preview_id'
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env receipt 'preview_id'
```

聊天正文只随查询返回，不进入本机结果库；库中保存消息身份、内容哈希和分页边界。用户自己的提交文字是例外：短期保存在私有预览表中，发送尝试后移除正文，保留回执；过期未发送预览在初始化或新建预览时清理。磁盘删除不承诺安全擦除。状态目录、暂存文件和回执均为私人数据，不应提交 Git。

正常发送结束或取消会清理本次暂存副本；进程被强制结束时可能留下私有暂存文件，需自行核对并清理。此时回执为未知状态，不能重发来“验证是否成功”。

验证包括虚构数据下的分页和长消息续读、目标/文件变化、默认禁止发送、重复调用、并发领取、取消与超时回执，以及 MCP 协议。真实群里的读取和预览可以验收；实际写入只有在指定目标和提交内容获准后才能验收，模拟发送测试不代表真实上游已验证。
