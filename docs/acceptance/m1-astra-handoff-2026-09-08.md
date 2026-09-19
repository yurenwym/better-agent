# Astra 高：M1开发交接

更新时间：2026-09-08 18:15，Asia/Shanghai。
项目：D:\RAG\better。
用户要求在该项目新建Codex会话，选择Astra、高推理强度继续开发。当前工具不支持创建会话/设置会话模型，因此只保存交接，未宣称新会话已创建。不要用应用自身的Better Agent thread或子Agent替代Codex项目新会话。

## 必读与授权

1. docs/architecture/reliability-development-plan-2026-09-08.md：grilling三轮Q1–Q14均采用推荐，已批准实施。
2. docs/acceptance/m1-memory-development.md：M0/M1当前进度与冻结门槛。
3. docs/acceptance/full-conversation-journey-2026-09-08.md：旧全流程验收与修复历史，只作背景，不要重跑。

用户批准按记忆→对话与上下文→多Agent→深度研究→自进化顺序实施。M1尚未完成，不能提前做M2–M5。非架构问题可自行修复；架构/权限/不可逆操作需确认。真实模型批次必须另行确认调用上限、费用估计和硬上限；不因“继续开发”自动发起付费模型、embedding、后台候选生成或归档调用。现有进化候选未批准，禁止Canary/发布。

技能：开发用ponytail，UI工作用ui-ux-pro-max。按需读取SKILL.md，不继承未读取的技能细则。当前没有AGENTS.md。不要无授权创建额外Agent。

## 工作树与数据

- HEAD：c7221cf34ae065ddb7d2de86b14e9dbb90889af4；大量未提交修改包含之前的验收修复和用户改动，禁止reset/checkout或假定所有diff属于M1。
- M0数据库一致性备份已完成：data/backups/m1-20260908/better-agent.dump。
- SHA256：16399FF87F34481DE10B704B96ED614931C2B2CE56F94C1B0C6F88C04A706CF7。
- 已成功恢复到better_agent_m1_restore_20260908专用库，102表，迁移20260905_0001，7个thread/83消息/0长期记忆/4Episode/1计划。验证库与备份均保留，不能对应用库执行测试清表。
- PostgreSQL容器better-postgres-1运行健康。M1本轮未执行schema迁移，也未修改应用业务数据。

## M1已实现（待继续审查）

- backend/app/memory_v2.py：renderer memory-v4；过滤非置顶无关条目；中文bigram、英文词边界；语义和关键词合并hybrid；部分embedding覆盖仍用语义；语义SQL过滤过期和hash失配；Episode渲染版本/消息范围、归因摘要、decisions和open_loops来源。
- backend/app/memory_archive.py：后台enqueue(proactive=True)以热预算80%提前归档；status及retry方法；重试比较updated_at、原文hash和PROMPT_VERSION；ArchiveUnavailable提供公开恢复提示。原文和游标不因失败丢失。
- backend/app/conversation.py：归档不可用时，未关联计划/行动/Skill且模型支持受限路径，记录context.incomplete，只允许answer且无artifact；传空history且不检索memory；副作用和ask被代码拒绝。外层记忆提示区分已确认长期记忆与有损摘要、当前用户条件优先。
- backend/app/live_model.py：answer_without_history用无工具分类判断是否独立问题，严格JSON true才继续；否则阻断。回答无历史/无工具并标注上下文不完整。此行为仅mock测试过，未真实校准分类准确率。
- backend/app/api.py：GET /api/threads/{id}/archive；POST /api/threads/{id}/archive/{job}/retry（CSRF、owner、expected_updated_at）。
- frontend/src/components/ArchiveStatus.tsx：每5秒读取归档状态，死信显示恢复按钮，重试后忙状态，隐藏已被覆盖的历史失败。
- frontend/src/pages/ChatPage.tsx接入ArchiveStatus；frontend/src/styles.css添加局部样式，当前布局修复尚不合格。

## 测试与结果

最新后端命令（仅隔离测试，不设置应用DATABASE_URL）：

```powershell
$env:LLM_AP_PATH=''
$env:AGENT_MODEL_BASE_URL=''
$env:AGENT_FALLBACK_MODEL_BASE_URL=''
$env:EMBEDDING_API_KEY_ENV='M1_UNUSED_KEY'
python -m pytest tests/test_memory_v2.py tests/test_memory_archive.py tests/test_memory_archive_api.py tests/test_memory_conversation_flow.py tests/test_memory_api_isolation.py tests/test_live_model.py tests/test_conversation_worker.py tests/test_context.py tests/test_memory.py tests/integration/test_postgres_queue_and_embeddings.py tests/integration/test_a17_postgres_conversation.py -q
```

在backend目录执行，结果164 passed。集成测试自建隔离Docker数据库，绝不可将TEST_DATABASE_URL指向应用库。

frontend目录：npm test -- --run，203 passed；npm run build成功。git diff --check成功。

新增/修改测试涉及：test_memory_v2、test_memory_archive、test_memory_archive_api（新）、test_memory_conversation_flow、test_live_model、test_conversation_worker、integration/test_postgres_queue_and_embeddings、integration/test_a17_postgres_conversation；ArchiveStatus.test.tsx（新）、ChatPage.test.tsx。

旧偏好测试的后续请求改为显式相关请求，因为新契约不允许无条件注入非置顶偏好；混合召回测试与A17由semantic改为hybrid。不要把测试调整误认为真实召回质量已获证明。

## 当前阻塞：UI重叠

375×812 viewport，归档提示bbox={x:18,y:339,width:339,height:98}，空对话BA标识bbox={x:163.5,y:412.515625,width:48,height:48}，仍重叠24.5px。新增margin-bottom:24px未解决，须查父级flex/grid/min-height/empty布局，不能继续叠加margin或z-index遮挡。

截图：docs/acceptance/m1-archive-mobile.png、m1-archive-desktop.png、m1-archive-landscape.png。横屏812×375无横向溢出。重试按钮由mock返回DEAD_LETTER→QUEUED已验证。

重现方式：frontend/dist用本地静态服务8123，Playwright拦截所有/api响应；桌面1440×1000进入mock thread m1，再切375×812。手机会话侧栏隐藏，不要直接在375px查找历史会话按钮导致超时。不要访问真实8000创建会话来完成mock UI检查。

## 运行进程与工具注意

- 8123静态服务PID9416：python -m http.server 8123 --bind 127.0.0.1 --directory D:\RAG\better\frontend\dist；可只读检查后停止本测试进程。
- 8000监听PID46888（交接时），旧后端服务未重启加载M1。dist已更新，存在新UI/旧后端临时版本不一致，交付前需处理；重启有付费后台worker风险，不能未经批次授权直接使用start_host.ps1。
- 最近统一exec测试进程均已完成，没有需要继续轮询的测试session。
- 最近Playwright browser已close。新会话不继承node_repl变量，需初始化。
- Node可用playwright：createRequire('D:/RAG/better/frontend/package.json')('playwright')；Edge路径C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe。
- 使用tools.mcp__node_repl__js可运行Playwright。避免15秒超时导致kernel reset，等待页面状态要给合适上限。函数内var可能不留顶层，重要资源ID写文档。

## 建议接续顺序

1. 只读确认git状态和文件，读取方案与技能。
2. 修复ArchiveStatus与空对话布局重叠；桌面/手机/横屏、键盘操作、重试忙态、无横向溢出截图复验。
3. 审查M1新增边界，重点检查归档重试并发/旧版本恢复、降级路径真正无副作用、旧pin变更、词项误召回、结构化Episode来源及预算。发现问题自行修复，不把现有绿测试当作充分审查。
4. 完善离线冻结案例覆盖和阶段交付记录。当前metadata/API/UI变更无需schema迁移，但须记录回滚和旧pin不兼容行为。
5. 更新m1-memory-development进度，只在真实审批批次完成连续3次验收后标M1完成。不要进入M2。
6. 准备真实模型预算单：固定案例、模型/提示词版本、调用上限、最坏费用、超限停止。先核价且征得用户批准；无法可靠计价就不启动。

本交接没有创建Codex新会话或设置模型。由用户在项目中新建会话并选择Astra/高后，发送“读取docs/acceptance/m1-astra-handoff-2026-09-08.md，继续M1开发，严格遵守其中授权边界”。
