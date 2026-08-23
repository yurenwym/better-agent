# Better Agent 三层记忆、深度研究与真人模式实施计划

> 日期：2026-08-23
>
> 依据：`2026-08-23-memory-system-final-design.md` 与 `2026-08-23-deep-research-and-human-mode-design.md`
>
> 原则：SQLite 是权威源；复用现有 Thread Event/SSE；不引入 ORM、队列、RAG、MCP 或新基础设施。

## 1. 交付目标

1. 把现有混合 memory candidate/file 模型收敛为短期消息、可追溯 Episode、版本化长期 Entry 三层，并让 Conversation 与 Agent Runtime 使用同一个 `MemoryContextProvider`。
2. 新增独立、持久、可取消和可恢复的深度研究流水线，支持显式入口与自然语言 V3 路由、研究历史、报告和来源查看。
3. 新增每日、每周、固定小时三种研究定时任务，并以 occurrence key 保证幂等。
4. 新增四种通知渠道配置和投递记录；报告提交与通知副作用分离。
5. 新增全局真人模式；只影响普通聊天正文，并按消息生成时的 presentation 持久化。
6. 完成 Memory、Research、Schedules 和聊天内研究进度/真人气泡 UI。

## 2. 实施顺序

### 阶段 A：数据库与迁移基础

- 新增带 checksum 的 `schema_migrations` 执行器。
- 为 Thread、Message 增加 owner/project、稳定 message seq、presentation 和 research job 关联。
- 创建 memory entry/revision/proposal/episode/archive/pin/projection/audit 表和索引。
- 创建 research job/attempt/report/source/evidence/section/schedule、notification、settings 表及约束。
- 用迁移测试覆盖已有数据库升级、约束、外键和重复初始化。

### 阶段 B：三层记忆

- 先测试并修复旧系统的 scope 聚合、整文件 rollback/manual sync 风险。
- 实现长期 Entry + 不可变 Revision、Proposal CAS、direct remember 幂等、Archive/Forget/Purge。
- 实现确定性 UTF-8 Markdown 只读投影和启动恢复。
- 实现短期完整消息窗口、Turn bundle 归档、lease/source hash/RAW_REFERENCE。
- 实现 owner/project 先过滤、FTS/精确/LIKE fallback、稳定排序和整条目 Token 预算。
- 实现 invocation context pin；Conversation 与 Agent Runtime 统一接入 Provider。
- 扩展 Memory API 和三段式页面：当前对话、最近经历、长期记忆。

### 阶段 C：研究内核

- 实现 typed models、规划 fallback、来源规范化/过滤、逐源 Evidence 绑定、单轮反思、大纲校验、分节写作与引用拼装。
- 使用 fake model/retriever 建立离线确定性闭环。
- 测试零证据失败、未知引用阻断、来源局部失败、取消和有界并发/数量。

### 阶段 D：Research Job、Worker、API 与 SSE

- 实现 `ResearchService` 的幂等建 Job、claim/heartbeat/lease CAS、阶段持久化、取消、重试和原子 finalizing。
- 实现单并发 `ManagedResearchWorker`，应用启动恢复，写作从首个未完成 section 继续。
- 复用 Thread append-only Event、消息 generation/offset；SSE 在研究活动时保持连接。
- 增加研究创建、列表、详情、报告、来源、取消和重试 API。
- 增加对话内 ResearchProgressCard 与 ResearchPage。

### 阶段 E：安全 Web 检索

- 实现 Chat Completions 兼容研究模型适配器。
- 实现搜索 API/网页抓取和允许目录 LocalNoteRetriever。
- 在 DNS 前后及重定向后阻止私网/环回/链路本地地址；限制 scheme、MIME、字节、字符、超时、并发和重试。
- HTML 仅转纯文本；任何来源都作为不可信数据进入研究 prompt。

### 阶段 F：定时任务与通知

- 使用 `zoneinfo` 实现 daily/weekly/interval_hours 的 UTC next-run 计算和 DST 测试。
- 实现单协程 Scheduler、启动最多补跑一次、活动 Job 时跳过、run-now 幂等。
- 实现 Server酱、企微、钉钉、Webhook 四种普通适配函数与业务码校验。
- secret 只保存环境变量名；API/事件/日志只暴露 configured 状态。
- 增加 SchedulesPage 和设置中的通知配置。

### 阶段 G：真人模式与自然语言路由

- 增加 settings API；在 `LiveConversationModel` system prompt 的 visible-body 规则处固定快照。
- 扩展 ControlHead V3 `start_research`，与显式入口统一到 `ResearchService.create_job()`。
- 实现 `splitHumanBubbles`、最多三段合并、history/live 区分、流式稳定、reduced-motion。
- 回归确保 Ask、Plan artifact、Research report、Runtime JSON 不受真人模式影响。

## 3. 验证门槛

- 每阶段先写失败测试，再实现到聚焦测试通过。
- 后端：全量 `pytest`。
- 前端：全量 Vitest、`tsc -b` 与 Vite production build。
- 真实模型：仅从用户指定 `LLM_API.txt` 在进程内读取配置，验证普通流式对话、记忆选择、研究结构化阶段和真人模式；不打印密钥或提交结果。
- 真实 UI：启动 FastAPI/Vite，验证创建研究、刷新恢复、取消/重试、报告/来源、schedule run-now、记忆增删改确认及真人气泡。
- 安全：检查 git diff/status，确保未提交 `data/`、密钥、日志、抓取正文、eval 结果或本地 memory 投影。

## 4. 提交边界

建议形成以下清晰提交：

1. `feat: replace legacy memory with three-layer authority`
2. `feat: add durable evidence-first research`
3. `feat: add research schedules and notifications`
4. `feat: add human conversation presentation`
5. `test: verify live model memory and research flows`

