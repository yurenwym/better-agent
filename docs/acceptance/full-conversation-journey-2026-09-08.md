# 全对话流程验收：2026-09-08

> 最新状态（16:30）：01–08交互与持久化验收通过；09经验观察、自动候选、真实评测通过，停在人工批准门禁。未启动Canary或发布，不能宣布完整9关通过。服务运行于8000。

## 用例与执行约定

- 编号：ACCEPT-FULL-JOURNEY-20260908-001。
- 场景：在同一对话中完成一份 7 天 PostgreSQL 索引与查询性能学习计划，从咨询、澄清、研究、专家协作，到计划持久化、执行反馈、复盘和受控自进化。初始 pgvector 场景受 GitHub 网络影响，经用户同意改用可访问的 PostgreSQL 官方资料；不把该调整记为原 pgvector 研究通过。
- 使用真实运行服务、真实模型和真实持久化结果；模拟响应、预置结果、已有单元测试通过均不能代替本次验收。
- 所有新增测试会话、计划和反馈带用例编号；不修改或删除已有用户数据。执行反馈明确标注为验收模拟，不冒充真实学习成果。
- 每关记录输入、预期、实际、资源 ID、时间和证据。按用户最新授权，非架构性问题自行判断、修复并复验后推进；架构调整、明显扩权或不可逆数据变更暂停确认。不跳过未通过关卡，不盲目重复业务写入。
- 用户确认恢复后，先复验阻塞关卡；通过后继续，不盲目重跑已完成的写操作。
- 运行中的任务在停止时不得继续提交后续动作；如已有异步任务，记录其 ID 和状态，不擅自取消无关任务。

## 前置检查

1. 确认目标应用 URL，首页与 `/api/health` 可访问。
2. 确认 PostgreSQL、worker 和所需模型路由可用；不输出密钥或完整凭据。
3. 复用应用的 CSRF、幂等与版本控制机制，使用独立验收会话。
4. 自进化以受控候选、真实评测、人工审批和可回滚为边界；不得绕过安全门禁，也不得自动将候选发布为全局稳定配置。

## 顺序关卡

| 关卡 | 操作与输入 | 通过标准 | 必留证据 |
| --- | --- | --- | --- |
| 01 对话 | 新建独立会话，发送“[ACCEPT-FULL-JOURNEY-20260908-001] 请简要说明 PostgreSQL 普通索引和 pgvector 向量索引的区别。” | turn 为 COMPLETED；assistant 正文非空且回答切题；排队、上下文、首 token、流式耗时均有非负值；刷新后消息仍存在。 | thread/turn/message ID、正文、耗时、刷新结果。 |
| 02 询问 | 同一会话发送“我想学 PostgreSQL 和 pgvector。请先通过结构化询问确认我的水平、每天可用时间和学习目标，暂时不要创建计划。”；收到问题后回答“会基础 SQL、每天 60 分钟、7 天内完成可复现的向量检索实验”。 | 进入 AWAITING_INPUT，出现 1–4 个有效问题；提交答案后生成 continuation turn 并完成；回答体现用户补充条件，待回答状态解除。 | ask ID、问题与答案、父子 turn ID、状态变化。 |
| 03 深度研究 | 在同一会话发起 Web 深度研究：依据 PostgreSQL 官方文档研究索引选择、EXPLAIN ANALYZE 与可复现实验。 | job 为 COMPLETED；报告非空；至少一个可访问来源和一条支撑结论的证据；报告回写会话；无伪造引用。 | job ID、报告、来源 URL、证据与会话消息关联。 |
| 04 多 Agent 协作 | 请求数据库、向量检索、测试策略专家共同审查研究结论，为后续学习计划提出建议。 | 实际启动至少两个不同专家任务；run 为 SUCCEEDED；所有任务成功终结；有可读产物；汇总体现各专家输入并回写原会话。仅主模型自称多 Agent 不算通过。 | run/task ID、专家身份、任务状态、产物与汇总消息。 |
| 05 创建计划 | 明确要求根据研究、专家建议及已确认条件创建 7 天学习计划文档；每天包含任务、预估时间、可验证产物。 | 对话完成；关联实际 plan document；初版可读取；Markdown 有标题和可执行步骤，满足每天 60 分钟等约束。仅在聊天输出计划正文不算持久化通过。 | plan ID、来源 thread、版本、Markdown、页面展示。 |
| 06 保存计划 | 在计划编辑入口追加“验收要求：保留 SQL、参数、检索结果与实验记录”，保存后刷新重开。 | 新版本递增、内容哈希变化；旧版仍可读；当前版本与持久化内容一致；文件投影最终为 READY 且内容一致。 | 保存请求、前后版本与哈希、刷新结果、投影状态。 |
| 07 计划进度 | 编译为当天开始的 7 天执行项目，预览后激活；完成一项当天动作，提交“验收模拟：实际耗时 45 分钟，难度适中”的反馈。 | program 为 ACTIVE；动作状态与版本正确变化；Today 和计划页面进度一致；刷新后状态及反馈不丢失。 | program/action ID、编译预览、前后状态、反馈和进度。 |
| 08 复盘 | 仅关闭本用例当天的其余动作，逐项给出明确标注的验收反馈，等待后台复盘。 | daily review 为 COMPLETED；summary 非空且与实际提交的反馈一致；可回读；若有调整建议，应保持待确认，未经同意不得自动应用。 | review ID、关联日期与动作、总结、风险与调整建议状态。 |
| 09 自进化 | 检查本流程终态事件对应的 evolution experience，核对证据来源；满足独立证据要求时生成候选并运行真实评测。 | 事件可追溯到经验；候选证据满足门禁且无新增权限；评测有真实结果；人工审批前不能生效。经用户明确批准后再验审批、Canary 与回滚，确认稳定配置恢复。 | experience/candidate/evaluation ID、来源与评测结果、审批绑定、Canary 样本与回滚前后版本。 |

### 自进化特殊边界

成功完成一次对话流程不保证满足自动候选生成所需的多条独立证据。证据不足须记录为“阻塞/未验证”，停下请用户决定是否增加隔离验收场景；不得编造经验、故意损坏现有会话或直接写数据库伪造通过。涉及真实人工审批时暂停，由用户明确确认；全部门禁完成前不得将关卡 09 标记为通过。

## 本次实际执行记录

- 日期：2026-09-08，时区 Asia/Shanghai。
- 总体结果：BLOCKED，停在前置检查，业务关卡通过数 0/9。
- 首次检查：GET `http://127.0.0.1:8000/api/health`，连接失败。
- 排除沙箱限制：获得授权后，在沙箱外分别只读访问以下健康接口，均返回 PowerShell `无法连接到远程服务器`：
  - `http://127.0.0.1:8000/api/health`，README 中的默认运行端口。
  - `http://127.0.0.1:8100/api/health`，既有验收文档记录的端口。
- HTTP 状态：无，未取得 HTTP 响应。该证据只能证明这两个地址当前不可达，不能断言其他端口无服务，也不能判断数据库或模型是否故障。
- 业务资源 ID：无。没有创建测试会话、发送消息或发起研究、专家任务、计划、复盘与进化操作。
- 01–09：全部 NOT_RUN，不沿用其他日期的通过记录。
- 现场保护：未启动或重启服务，未修改业务代码，未覆盖既有验收文档或用户未提交修改。
- 恢复条件：用户确认实际服务地址并使其可访问，且明确同意继续；先复验健康检查，再从关卡 01 开始。

## 后续逐关记录格式

每次恢复在本文追加：执行时间、关卡、输入摘要、期望结果、实际结果、资源 ID、HTTP/业务状态、证据路径、PASS/FAIL/BLOCKED。首个失败后立即结束当次执行，后续关卡保持 NOT_RUN。

### 2026-09-08 用户确认端口后的复查

- 用户确认目标端口为 `8000`。
- 操作：GET `http://127.0.0.1:8000/api/health`，请求超时上限 10 秒。
- 预期：取得成功的健康响应。
- 实际：仍返回 `无法连接到远程服务器`，未取得 HTTP 响应。
- 结果：BLOCKED，继续停在前置检查；关卡 01–09 仍为 NOT_RUN。
- 未启动、重启服务或修改业务代码，等待用户处理服务或明确授权排查启动。

### 2026-09-08 12:24 用户授权排查与启动后的结果

- 已获用户授权排查并启动 `8000` 服务。
- 启动前检查：PostgreSQL 容器 `better-postgres-1` 处于 healthy；5432 正在监听；8000 无监听进程；配置文件 `LLM_AP_PATH` 存在，未输出凭据。
- 复用 `scripts/start_host.ps1 -Port 8000`，以隐藏后台进程启动，未修改启动脚本或业务代码。
- PostgreSQL 容器正常运行；Alembic 命令未报告失败；前端 `tsc -b && vite build` 成功；应用报告 `Application startup complete`。
- 随后发生首个新阻塞：`error while attempting to bind on address ('127.0.0.1', 8000)`，错误为 `[Errno 13] / [WinError 10013]`，即操作系统禁止套接字访问。
- 应用随后报告 `Application shutdown complete`，健康接口仍无法连接。
- 当前证据不能确定是端口排除/保留范围、安全软件还是其他系统策略导致；未将其认定为普通端口占用。
- 启动日志：`data/logs/acceptance-20260908-122441.stdout.log`、`data/logs/acceptance-20260908-122441.stderr.log`。
- 结果：BLOCKED，停在前置检查。01–09 全部 NOT_RUN，未创建本用例业务资源。
- 已停止后续动作，未换端口、未调整防火墙/端口保留配置、未重启系统服务。等待用户确认下一步处理方式后再继续。

### 2026-09-08 12:27–12:28 启动阻塞处理完成

- 用户授权：直到解决启动问题，开始对话前无需再次询问。
- 根因证据：IPv4/IPv6 TCP 排除范围均包含 `7997–8096`；8000 无监听进程；非管理员直接删除被拒，管理员直接删除亦失败。
- 最小处理：通过 `scripts/acceptance-port-8000.ps1` 短暂停止 WinNAT，添加 IPv4 TCP 8000 单端口持久化管理保留，再在 finally 中恢复 WinNAT。未改变全局动态端口范围、未修改防火墙、未重启 Docker、未修改业务代码。
- 结果：WinNAT Running，PostgreSQL healthy；非管理员 TcpListener 绑定 `127.0.0.1:8000` 成功。
- 系统变更可撤销：不再需要时，可在管理员终端执行 `netsh interface ipv4 delete excludedportrange protocol=tcp startport=8000 numberofports=1 store=persistent`；本次未撤销，避免再次动态占用。
- 复用 `scripts/start_host.ps1 -Port 8000` 启动，应用进程 PID 48732；健康接口 HTTP 成功，返回 `status=ok`、`api_key_configured=true`；真实首页可加载。
- 日志：`data/logs/acceptance-port-8000.log`、`data/logs/acceptance-20260908-122816.stdout.log`、`data/logs/acceptance-20260908-122816.stderr.log`。

### 2026-09-08 12:29–12:30 关卡 01：FAIL，已停止

- 方式：Playwright 驱动本机 Edge，打开真实服务页面，通过输入框与“发送”按钮提交用例中的首轮问题。没有 mock、没有预置模型回复。
- 提交 HTTP 202，thread：`thread_d614f2a483594f3688dd5a37faa3cdbf`。
- turn：`turn_b1281985bb36489993f4e6d8c2f6842d`，状态 `COMPLETED`，reason_code 为 `protocol_fallback`。
- assistant message：`message_5bc7f228cd164c1c993c670c3ec0a49f`，正文长度 1099；回复非空、可回读，刷新页面后仍存在；浏览器未捕获 pageerror。
- 首个确定故障：持久化消息和页面正文均以 `v=1\npolicy=answer\n` 开头，内部协议字段泄漏到用户回复。事件 seq=22 的 message.delta 也包含同样字段，因此不是单纯页面显示问题。
- 耗时观察更正：首次读到 COMPLETED 时 metrics 全为 null，但事件 seq=32 在 `04:29:33.354274Z` 已补齐；完成事件为 `04:29:31.230621Z`，两者相差约 2.12 秒。不将其记为永久指标缺失。
- 最终耗时：queue_wait_ms=33，context_ms=1040，model_ttft_ms=3139，stream_ms=28，answer_wait_ms=11792，total_ms=11820，model_attempt_count=3。
- 页面证据：`docs/acceptance/full-conversation-journey-2026-09-08-step01.png`，刷新并等待回复实际显示后截取。
- 边界：只读保留本轮消息与事件，未修复协议解析，未发送第二轮消息；02–09 均未执行。应用保留运行，便于查看现场。
- 继续条件：用户同意处理关卡 01 的协议字段泄漏，修复并复验通过后再进入询问关卡。

### 2026-09-08 12:35–12:39 协议泄漏修复与复验

- 用户授权修复协议泄漏。修改 `backend/app/live_model.py` 的 `_strip_incomplete_control_head`：识别回复开头的合法版本与策略组成的 key=value 控制头，去除已知控制字段后再返回正文；仅有控制头而没有正文时拒绝。未修改前端，未改写历史消息。
- 回归测试覆盖实际故障格式、CRLF/空格/可选控制字段、普通正文和代码示例不误删、无正文拒绝，以及 worker 消息/事件持久化不含协议字段。
- 修复前新增复现测试出现预期失败；修复后隔离真实模型环境变量运行 `test_live_model.py`、`test_conversation_worker.py`、`test_conversation_protocol_v2.py`，结果 101 passed。首次扩大测试有 4 项因继承本机模型环境而在测试初始化失败，隔离环境后全部通过，未修改运行服务模型配置。
- 重启已核实无活动对话/研究任务的应用加载修复，PID 26872，健康正常；日志前缀 `data/logs/acceptance-20260908-123453`。
- 在原验收会话通过真实浏览器重新发送同题，turn `turn_eb35a8617da04b749f1c8e062350b89a`，HTTP 202，最终 COMPLETED / protocol_fallback。
- assistant `message_c0d11a51164a406994193ab354f6b7f3` 的正文从 Markdown 标题开始，不含泄漏控制头。刷新后页面显示正常，截图 `docs/acceptance/full-conversation-journey-2026-09-08-step01-retest.png`。
- 最终耗时：queue_wait_ms=25，context_ms=1018，model_ttft_ms=1495，stream_ms=25，total_ms=9490，model_attempt_count=4。
- 协议字段泄漏子项：PASS。
- 新问题：回复“普通索引做预过滤，向量索引做排序，两者协同”未说明执行计划边界，把它写成通用行为。通过浏览器读取 pgvector 官方 README（https://github.com/pgvector/pgvector#filtering），其说明近似索引过滤发生在索引扫描之后，不能保证上述执行顺序。
- 另一个需纠正的泛化：回复“向量索引应在数据灌入后再创建”没有区分 HNSW 和 IVFFlat；官方 HNSW 说明允许在空表创建，因为没有 IVFFlat 那样的训练步骤（https://github.com/pgvector/pgvector#hnsw）。
- 原回复末尾还自行声称“已复验确认……无需进一步修复”，这只是模型生成内容，不作为验收证据。
- 当前结论：本次代码修复完成；完整关卡 01 因新发现的内容准确性问题仍暂停，不标整体通过。未发送关卡 02 消息，也未执行 03–09；等待用户授权处理新问题。

### 2026-09-08 12:44 继续验收

- 用户授权非架构性问题自行修复和推进。加入通用事实边界、历史回答非证据、禁止无证据声称验收成功的提示词约束；33 项 live_model 测试通过。
- turn `turn_99876d60bb66435e9a07b7202d2a3e07` COMPLETED，已纠正 HNSW 可空表创建，以及向量扫描后过滤与普通索引筛选后精确排序是不同执行路径。协议无泄漏，消息可回读。
- 耗时 queue/context/TTFT/stream 为 41/1055/35296/27 ms，total=47493 ms。
- 01 核心交互及已报问题复验通过；不将模型全部知识性扩展视作已核验事实。参数建议与其他泛化在 03 中要求官方来源核对。

### 关卡 02 初次执行与归档修复

- turn `turn_9b81787faccf47cbb54024f05b695a64` 正常进入 AWAITING_INPUT；ask `ask_838b996398664756b4d8131583363c38` 含 4 个相关问题，页面填写并提交成功，截图 `step02-ask.png`。
- continuation `turn_96107d2d1b44426fb63239c2b34d1e5e` 在回答前失败：历史归档 `archive_1cd8c15d8c0843a58fd4338c4411fb96` 三次 invalid_summary 后 DEAD_LETTER。
- 复现：1600-token 生成预算出现空正文；提高预算后模型把 synopsis 返回为字符串，违反列表结构。不是询问接口错误。
- 修复 LiveEpisodeSummarizer 的完整 JSON 示例和数组约束；生成预算调整为 4096，最终摘要 1600-token 上限、字段/来源/敏感性校验保持不变。摘要版本 episode-v3 保留旧死信并生成新任务。
- 56 项归档与 worker 测试通过；真实模型用本验收前两条消息生成摘要，通过完整结构及来源校验。重启加载后重验询问续接。
- 完整批次复验进一步发现摘要过长，episode-v4 增加总条目/单条长度/引用数量约束，最终上限未放宽。
- 随后发现 PostgreSQL 不支持双参数 MAX：归档提交 `MAX(archived_through_seq, ?)` 改为可移植 CASE，21 项归档测试通过，PostgreSQL CASE 前进/不回退实测通过。
- 仅重排本验收 episode-v4 且因 UndefinedFunction 失败的任务 `archive_0b40074cfc83402d99a0f5d4959a53d1`，没有改写历史消息；任务随后真实 COMPLETED。旧 v2/v3 死信保留。
- 重验 ask `ask_e7c1afe91e2d4270b7dd4b3a56deb93a`，父 turn `turn_8e81bc914d6449ff99e86e8faf6a927f`；页面确认四项信息，续接 turn `turn_3e406db34c7f4175825002096bee9497`。
- 续接最终 COMPLETED，四项条件正确回带，待回答卡片解除，plan API 仍为 null，02 交互闭环 PASS。总耗时 138650 ms（含归档上下文 33589 ms、主模型首 token 86885 ms），速度风险记录但不伪报失败。
- 助手虽未保存计划，但展开了较长的建议安排；其中技术细节未当作已验证脚本运行，03 要求官方来源研究核对后才创建最终计划。
- 03 请求 turn `turn_5a3eb38b66b941dfb28b484ae1ba296d`，同一会话，明确官方来源、索引条件/过滤/HNSW 参数与实验建议。
- 首次研究 `research_87b7b6957d014d6190dee310f6c6e486` 检索到 3 个博客来源，违背仅官方约束，已取消而未判通过。
- 修复 WebSearchRetriever：压缩查询保留 site: 操作符；返回网址与重定向后的网址按域名及路径边界复核。不接受伪域名或同前缀仓库。61 项搜索与研究测试通过。
- 复验请求明确 `site:github.com/pgvector/pgvector OR site:postgresql.org/docs`，仍在同一会话，经原任务 retry API 建立关联重试。自然语言“官方”的范围解析仍依赖规划模型；此处以显式可核验范围验收。
- 限定站点重试 `research_2beaa7d88aa64669bfcbe873c7d7d59b` 无合格结果，明确 FAILED/search_results_irrelevant。未引入越界来源。
- 增加用户显式来源 URL 直读，复用公网与重定向校验、站点边界、来源评分，不依赖搜索引擎索引命中。66 项测试通过，含隔离 Docker PostgreSQL 的归档提交集成测试。未在应用数据库上执行集成测试清表。

### 关卡 03 最终阻塞与安全边界

- 官方 URL 直读重试 `research_45409bad149144beb098f821ce007693` 最终 FAILED/search_results_irrelevant，无报告通过。
- 只读诊断：`github.com`、`raw.githubusercontent.com` 在系统 DNS 解析为 127.0.0.1；`validate_public_url` 返回 `private or local URL is not allowed`。PostgreSQL 官方文档同一路径 HTTP 200。DNS 1.1.1.1 不使用 hosts 时 github.com 解析为公网地址，定位到本机 hosts。
- hosts 的 Steam++ 区块含这两个域名的活动 127.0.0.1 映射，属于本地加速代理配置。不是官方站点不存在，也不是公网地址校验应当删除。
- 曾备份 hosts 到 `data/logs/hosts-before-acceptance-github`，仅临时注释这两个映射，但 Steam++ 立即追加写回活动映射。未停用 Steam++ 或其整体代理。
- 已通过 `scripts/acceptance-github-hosts.ps1 -Restore` 恢复本次临时修改，确认无 ACCEPTANCE-TEMP 标记，原两个域名本地映射仍存在。备份不覆盖当前其他 hosts 修改。
- 继续方式需要选择：临时停用 Steam++ 的 GitHub 加速以恢复系统公网解析；或设计并审批可信代理通路。后者改变应用安全边界，属于需确认的架构性问题。未关闭 SSRF 校验、未信任任意 localhost、未硬编码公网 IP，也未把博客报告标为官方研究成功。
- 当前错误归因局限：研究 API 将没有合格来源统一显示为 search_results_irrelevant，公网校验的具体拒绝原因通过诊断取得；这项错误提示仍可改进。
- 最终回归：test_live_model、test_memory_archive、test_conversation_worker、test_web_search、test_research_engine 合计 151 passed；此前同一版本搜索/研究与隔离 PostgreSQL 兼容测试组合 66 passed（有重叠，不相加冒充独立测试数）。git diff --check 通过。
- 服务健康：`http://127.0.0.1:8000/api/health` 返回 ok/model configured；最后启动日志前缀 `data/logs/acceptance-20260908-130634`。
- 04 多 Agent、05 创建计划、06 保存计划、07 计划进度、08 复盘、09 自进化均 NOT_RUN，未跳过 03。原用户要求的全流程尚未整体通过。

### 2026-09-08 14:39 获准停用 GitHub 加速后的检查

- 用户允许临时停用 Steam++ GitHub 加速。通过管理员只读进程/UIAutomation 检查定位 Watt Toolkit 商店版，打开应用后尝试选择其网络加速入口；未执行 Stop-Process 或终止加速器命令。
- 随后 Steam++、Steam++.Accelerator 进程均已退出，退出原因未确定，不把它表述为已验证的“单项开关操作成功”。未擅自重启可能由用户关闭的应用。
- hosts 中两条 GitHub 活动 localhost 映射已消失，也没有 ACCEPTANCE-TEMP 标记；系统解析 github.com=20.205.243.166，raw.githubusercontent.com 为 185.199.* 公网地址。
- 公网校验阻塞已消除，但真实请求仍失败：Python httpx 访问 github.com 为 ReadTimeout、raw.githubusercontent.com 为 ConnectTimeout；Edge 访问 github.com 为 ERR_CONNECTION_RESET；curl IPv4 原文域名请求连接超时。www.github.com、pgvector.org、raw.github.com 也无可用直连结果。
- TCP 443 连通检查为 True，但不等于 TLS/HTTP 页面可读。系统 WinHTTP 为直连；用户代理配置处于关闭状态，配置的 127.0.0.1:7897 无监听；常见本地代理端口亦未检测到监听。
- 结论：外部网络连通性阻塞，不能仅凭恢复 DNS 宣称官方研究可用。需要能访问 GitHub 官方原文的网络或显式 HTTP/SOCKS 代理；不关闭证书或 SSRF 校验，不注入本地假来源、不跳过 03。
- 本轮没有发起新研究写请求、没有新模型调用；04–09 未执行。应用仍运行，保留此前任务与证据。
- 新增诊断辅助脚本 `scripts/acceptance-steam-inspect.ps1`，记录在 `data/logs/acceptance-steam-processes.json` 与 `acceptance-steam-ui.json`；未改业务代码，未重跑此前已通过的 151 项代码测试。

### 2026-09-08 14:53 更换研究资料并继续

- 用户确认 GitHub 非必需后要求继续修复，场景调整为 PostgreSQL 索引与查询性能，保持同一会话和 7 天/每天 60 分钟约束。两份 PostgreSQL 官方文档直接读取 HTTP 200。
- 服务原先未监听，复用 start_host.ps1 恢复；未继续改系统代理/hosts。
- 研究 `research_4d89d6aab5f947c696803f964b21f26f` 取得 2 个官方来源、12 条证据，但报告覆盖失败。
- 修复 HTML 提取丢段落、相关摘录截断导致事实丢失；模型结构化/章节生成预算提升为 4096，空白或截断章节拒绝，进入既有证据回退和最终审查。65 项相关测试通过。
- 重试请求去掉“七天”研究交付歧义，明确仅研究三项主题、不创建逐日计划；原失败记录保留。
- 重试 `research_cad06b380f7041fbbf73f943006f2680` 的回退证据被导航文本污染，已取消；标准库 HTMLParser 提取 main/article/docContent 主内容，去掉导航、页脚、目录，保留段落。回退仅接收完整事实句，过滤版权/版本模板。
- 官方 EXPLAIN 页正文超过 4 万字符，来源上限由 2 万增至 6 万；模型提炼/阶段超时由 20/45 秒调为 90 秒，均保持有界。66 项测试通过，真实官方页提取验证无导航污染。
- `research_ff3593c7fc304bf38ff412b4699a0b53` 发现规划将完整答案塞入章节标题；添加短单行标题校验，拒绝正文冒充标题。取消后异常曾落为 FAILED，worker 改为取消优先；新增回归测试。85 项研究相关测试通过。
- 聚焦官方索引类型页与 sql-explain 命令页，研究 B-tree/Hash 与 ANALYZE 副作用/事务回滚，保持来源证据及报告回写标准；不冒充此前广泛报告通过。
- `research_d68c63f3ab294d63a1a7af695fef7ef5` 被最终校验拒绝：提炼模型将 B-tree/Hash 事实归入 EXPLAIN 来源，导致引用错配。改为原文逐字证据提炼，标准化空白后必须匹配实际摘录，否则拒绝。86 项相关测试通过。
- 当前真实重试 `research_f7ac599667794ef4a67a6837dcd8185f`；这些失败均保留，未跳关。
- `research_f7ac599667794ef4a67a6837dcd8185f` 在章节覆盖失败，回退规划还把格式要求拆成主题；用明确分隔的两项研究主题重试 `research_59f374817f06405a87f0881eb958ded9`。
- 最后一次重试多个模型阶段立即 FAILED/request，最终回退报告仍未通过覆盖校验。只读查询 model_attempts 确认并非纯内容问题。
- 独立最小模型连接检查：主模型 HTTP 402，error.message=`Insufficient Balance`，type=`unknown_error`，code=`invalid_request_error`。未输出密钥；当前余额不足需用户补充额度或明确选择已有备用模型路由。
- 未充值、未更换计费账户、未绕过 402，也未放宽研究验收门槛。暂停新的真实模型调用。研究服务仍将上游模型故障折叠为覆盖失败，这是后续可改进的错误归因点。
- 04–09 未执行；本轮没有创建计划、模拟学习进度或批准进化候选。系统代理/hosts 未改动。
- 本轮最终回归 175 passed（live_model、memory_archive、conversation_worker、research_engine、research_service、web_search）；git diff --check 通过。8000 健康接口 ok 仅说明应用存活，不代表模型额度可用。

### 2026-09-08 15:38 额度恢复后继续

- 主模型最小检查 HTTP 200，未切换账号或路由。研究 `research_8e613eaf20db4513922944e6019ea548` 取得 10 条证据，但关键原文仍被模型遗漏，已取消。
- 修复证据补充：从完整有界原文按章节优先选择句子，剔除 URL/site/验收标记的排序噪声；接受来源中真实 SQL 示例。模型证据与确定性摘录去重合并，单来源最多18条，保留总证据上限及引用校验。
- 86 项研究回归通过；新增长正文后部事实与 SQL 示例回归后，51 项 engine 测试通过。真实官方原文验证可取到 B-tree 等值范围和 ROLLBACK 示例。
- 03 `research_dd53be92cd4e43aa9c66275b89b46e28` COMPLETED，2份官方来源、20条证据，报告Markdown与对话assistant消息完全一致，刷新可读，截图 `step03-passed.png`。
- 核心研究机制通过；Hash/IN绝对表述和回滚副作用边界作为04专家审查输入，不把模型报告全部扩展结论当作已验证事实。
- 04 页面开关使用标签可正常操作（原生输入被装饰层覆盖，非业务提交）；`agent_run_2ea355dfa87d49e4b9752962a53d6e0c` 启动三个真实专家，但三者生成均达到1400-token上限而失败。
- 专家输出预算调整为4096并约束简洁JSON；拒绝截断结构，协调汇总预算同步调整。10项专家/worker测试通过，重启复验。
- 独立安全判定器8-token预算不足，调整为1024，仍严格safe/unsafe标签校验；专家预算4096仍截断，提升到配置允许的8192，精简重复研究全文输入。
- `agent_run_3c96f137cef84b21b640710136c1dc03` 两位专家成功，planner到8190-token边界失败；协调汇总SUCCEEDED但明确incomplete=true，未据此判全关通过。继续缩小planner任务为每日时间预算审查，而非提前生成七天日程。
- 04 `agent_run_0e0829d48f0249deaaf833e4d1f3adf9` 三位专家与coordinator全部SUCCEEDED，3份专家产物+汇总，incomplete=false，PASS。进入05创建计划。
- 05 turn `turn_31fc0f71be2a4c23858b3a09312da6d6` COMPLETED，创建计划 `plan_008e58fe6a774596a2f1002974d40dc8` v1，7天各60分钟、模拟标记、安全边界，file_status=ready，PASS。
- 06 通过页面编辑器修正“必须走索引”的过强条件，追加验收记录要求；保存v2，哈希 `e00ee5fcada19cf98ba86887ed1d2315bf26f5fc4816bb877f1e729bf7ad75ab`，v1仍在，数据库/文件接口正文一致，刷新可读。截图step06-saved-plan.png，PASS。
- 07 `program_d409426e8c8d4601bd38f328bc85fa70` 编译READY并通过页面激活ACTIVE；2026-09-08至14日共7行动，各60分钟。当天action `action_5a01cdbddc1447a8a46d864b28536a2d` COMPLETED/v1，进度1/7；feedback `feedback_86ea32ad666644d994ead55df61981b8` 保存45分钟/难度3。模拟声明在计划与执行假设中保留，未实际执行学习SQL。截图step07-progress.png，PASS。
- 08 review `review_bb2e1922aaa34133b8c1f5e4a55b8917` COMPLETED，summary正确对应45分钟/1项完成，signals为空、无调整建议。独立API误用SQLite私有连接已修复为数据库公共连接；重启后独立API与Today完全一致，PASS。PostgreSQL回归检查禁止访问SQLite。
- 复盘补跑测试固定调整服务时钟，消除过去日期导致ACTION_COUNT的夹具问题；14项复盘/观察测试通过（隔离本机模型配置）。
- 09 观察器UPSERT列歧义修复为表限定last_row_id，PostgreSQL两次观察集成检查通过。研究/专家/行动/复盘成功事件均已归集，重复观察created=0。
- 自动生成3个READY_FOR_EVAL候选，均无新增权限；选专家候选 `candidate_61bb49f039dd478688795f8765aded76`（4条真实失败经验）执行真实对照与安全评测。尚未批准/灰度/发布。
- 评测首次失败暴露800/20-token生成上限与API异常类未导入；分别调整为4096/1024，补齐EvolutionConflict/EvolutionGateError导入与错误路径回归。8项相关测试通过。
- 09真实评测 `evaluation_fb1df68195c842f5a3f32f37296ebc03` COMPLETED、deterministic_pass=true；12项确定性检查通过，基线2/2、候选2/2，quality_delta=0，safety_violations=0。不能宣称已证明质量提升。
- 候选状态EVALUATED，permission_diff无新增权限；尚未批准、尚未Canary、更未全量发布。人工审批是原验收用例的明确门禁，不以自动修复授权代替批准具体运行策略。
- 待用户决定是否批准该候选，仅进行10% Canary启动与立即回滚的受控验收，不做全量晋升。若不批准，09只记录至评测阶段通过，发布链路未验证。
- 最终回归211 passed，包含本轮相关单元/服务测试与隔离Docker PostgreSQL集成测试；git diff --check通过。应用8000健康，测试浏览器关闭。页面证据step08-review.png与step09-evaluated.png已保存。
