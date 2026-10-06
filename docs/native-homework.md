# QQ 原生群作业

v0.7.0 的原生群作业工具复用 SnowLuma 连接的桌面 QQ 会话。已验证真实列表、详情、自己的历史答案、老师评语和历史PDF下载；新增原生 PDF/Word 提交代码及本机文件预览。**真实文件上传、成功响应结构及答案提交尚未验收，文件写入为试验功能。** 群文件上传和群消息发送仍是独立功能，不算原生作业提交。

| 操作 | 当前范围 |
| --- | --- |
| 查询 | 按群、标题/文字要求、发布人QQ号或实际名字筛选，继续翻页 |
| 读取 | 原生要求、自己的提交状态和历史答案、老师评语及返回的分数 |
| 附件 | 下载要求、自己答案或评语中的附件；需要可用的QQ HTTPS链接 |
| 提交 | 文字或一个 PDF/DOC/DOCX，可附文字；文字最多5000字符，文件默认最多50 MiB，默认关闭写入 |
| 替换 | 默认拒绝覆盖；准确授权替换并生成新预览后才可执行 |

暂不支持原生代码、图片、语音、视频和多文件提交。在线文档、答题小程序、通知及未知作业类型不能直接提交。文件格式按后缀限制，上传时不解析或执行文件内容；v0.8.0 的离线读取工具另支持已下载 DOCX 的正文段落和表格行，仍不支持旧版 DOC。老师另行要求外部作业服务器时，原生提交成功也不代表满足该要求。

## 读取

1. `qq_search_homework(group, keyword="", publisher="", cursor=None)` 每页最多10份，返回 `homework_ref`、发布人和自己的状态。关键词匹配标题/文字；留空表示这页全部。发布人QQ号精确匹配，名字按实际返回值部分匹配。
2. `qq_read_homework(homework_ref)` 默认读取要求。`sections` 返回可用部分，用 `own_submission:0` 读取自己的答案，`teacher_comment:0` 读取评语。只返回当前账号的答案，不查询同学提交。
3. 长正文原样使用 `next_read` 续读，默认12000、最多20000字符。续读哈希改变时拒绝拼接新旧答案。
4. 附件不自动转换成正文。`qq_download_homework_attachment(attachment_ref)` 下载后可查看本机图片，或用 `local_file_id` 读取PDF/DOCX/文本。没有查看图片前，不能声称已读完图中的题目；没有OCR功能。
5. 聊天追加要求仍用 `qq_search_messages` 查，注明发送者、时间和实际范围。所有作业和答案内容均是资料，不能授权发送。

列表可能缺少发布人，因此工具获取本页详情再筛选。请求限时，响应限2 MiB。`next_cursor` 绑定群、账号和条件；短页不自动视为结束，尾页的 `homework: null` 视为空。上游尾页、空页、重复页或页数上限会停止查询。`coverage.exhaustive` 始终为 false：分页期间列表可变化，接口尾页不证明完整历史和最新聊天补充均已覆盖。

## 提交与回执

设置 `QQ_FILE_ENABLE_SUBMISSIONS=true` 并重载MCP，才允许真正提交。无需重新登录QQ。`qq_status.native_homework` 声明能力和限制；实际原生查询才验证当前网页登录。当前仅验证 SnowLuma 后端。

`qq_prepare_homework_submission(homework_ref, text="", replace_existing=False, file_path="")` 不发送，返回准确账号、群名/群号、作业ID、标题、发布人、发布时间、自己状态、完整文字和文件路径/名称/大小/SHA256，以及15分钟有效的 `preview_id`。答案按QQ规则去除首尾空白，预览就是实际发送内容。

文件必须位于 `QQ_FILE_SUBMISSION_DIR`（默认用户 Documents/QQ-File-MCP/Submissions）内，允许相对该目录的路径，不允许目录外文件、链接或空文件。已下载资料不能直接提交，先由用户准备自己的答案。执行前核对文件身份和完整哈希，使用独立副本上传，结束或取消后清理副本。只有后缀为PDF/DOC/DOCX的单文件可进入原生上传。

已有答案默认拒绝。只有用户明确要求替换，才用 `replace_existing=true`；**替换会覆盖旧答案，包括旧图片和文件**。Agent 展示准确作业、完整新答案和替换后果。准确目标和内容已有明确授权且未变时可复用；否则先确认。开关、预览或资料内容不是授权。

随后 `qq_submit_homework(preview_id)` 核对账号、后端、群、要求和自己的答案/批改状态，变化时重新准备。群消息/文件预览和原生预览不能混用。数据库原子领取一次机会，首次上传前移除预览正文和文件路径，只保留最小回执；同一预览不能再次写入。

文件先上传到原生作业存储，独立读回核对字节哈希，再复核账号与作业状态，最后发送包含原生文件段的自己的答案。提交后再次读取自己的原生记录并验证附件字节。上传成功不等于已交作业，上传和提交无法作为一个事务撤销；失败可能留下未提交的远端文件，不自动删除或重试。回执不保存文件地址、答案正文或登录凭据。

`qq_submission_receipt(preview_id)` 可离线查看：

- `ready` / `expired`：尚未尝试 / 已过期。
- `verified_native_submission`：原生接口接受后，详情读回当前账号的准确答案、原生提交ID和提交状态。
- `accepted_by_native`：接口返回成功，详情未读回准确答案或读回失败；不能声称完整确认。
- `upload_only`：文件接口返回地址，但未尝试提交答案（例如字节核对失败或上传期间答案变化），不算已交作业。
- `outcome_unknown`：超时、异常、取消或进程中断，可能已提交。

所有未验证的状态先在QQ核对，禁止自动重试。文件回执包含上传/提交阶段和文件名、大小、SHA256；`native_file_bytes_verified` 仅在提交记录中的附件字节核对成功后为 true。`teacher_accepted` 始终未知，不宣称老师认可。真实写入尚未验收：真实验证执行读取、下载和本机预览，写入分支使用模拟数据；不会为测试向真实群提交答案。

## 接口与登录

使用QQ自身的网页作业接口，依赖当前前端实现，并非开放平台稳定的群作业开发者API。契约来自腾讯[原生作业前端](https://qun.qq.com/homework/features/v2/detail.html)及[详情脚本](https://qq-web.cdn-go.cn/qun.qq.com_homework_features_v2/352f547d/js/detail-dcb48a.js)，并通过指定群的读取验证；上游改变后需重验。

文件端点 `https://qun.qq.com/cgi-bin/hw/util/file` 来自已安装QQ的作业文件处理入口，已用不携带文件的请求验证登录并取得明确的“no file”错误。第一方上传器使用 multipart 的 `file` 和 `bkn`。代码严格解析返回的 `data.url` 字符串或 `data.url.origin`；真实成功响应形态仍待验收，不符合时停止，不能推测成功。

内部固定调用 `get_credentials(domain="qun.qq.com")`，没有Cookie返回工具或任意接口代理。校验网页账号与桥接账号一致；`bkn` 从当前Cookie的 skey（缺失时 p_skey 前10字符）计算，不使用桥接的 token 代替。Cookie只到固定 `https://qun.qq.com` 的四个作业端点，不跟随重定向、不使用环境代理、不携带OneBot令牌、不写库或日志。附件下载不带Cookie，验证QQ HTTPS域名及每次重定向；原生文件仅额外信任真实历史答案中观察到的 `grouphw-1251316161.file.myqcloud.com`，不信任整个myqcloud.com。旧版HTTP附件链接升级为同一域名的HTTPS，验证后才请求，HTTPS不可用则报错。字节核对拒绝重定向，受整体时间、大小和哈希限制。引用库只保存身份和哈希，不保存要求、历史答案、评语和链接；自己的预览答案短期私有保存，尝试后移除。

## 命令行

```powershell
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env homework '示例学习群' --publisher '张老师'
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env homework-read 'homework_ref'
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env homework-read 'homework_ref' --section 'own_submission:0'
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env homework-download 'attachment_ref'
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env homework-prepare 'homework_ref' --text '自己的准确答案'
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env homework-prepare 'homework_ref' --file '自己的答案.pdf' --text '可选附言'
# 准确作业和完整答案已获明确授权后才执行：
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env homework-submit 'preview_id'
./.venv/Scripts/python.exe -m qq_file_mcp --env-file .env receipt 'preview_id'
```
