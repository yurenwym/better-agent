# Better Agent 最终记忆系统设计：短期、经历与长期记忆

> 状态：最终设计提案，待批准后制定实现计划
>
> 日期：2026-08-23
>
> 适用项目：Better Agent
>
> 冻结决策：保留原生 SQLite；不使用 SQLAlchemy、AgentScope、向量数据库或独立 RAG 基础设施

## 1. 一句话结论

Better Agent 的记忆系统只包含三层：

```text
短期记忆：当前对话正在发生什么
    ↓ 超出窗口后自动压缩
经历记忆：过去发生过什么
    ↓ 判断是否值得长期保留
长期记忆：以后仍然应该知道什么
```

计划文档、工具事件、Checkpoint、System Prompt 和 Skills 不属于记忆。

SQLite 是三层记忆的唯一权威数据源；Markdown 只是用户可查看的只读投影。普通聊天和 Agent 执行通过同一个记忆上下文入口读取记忆。

## 2. 已冻结的技术选择

### 2.1 继续使用原生 SQLite

保留当前技术栈：

```text
Python 标准库 sqlite3
+ 手写 SQL
+ 同步 transaction context manager
+ WAL
+ FastAPI / asyncio 业务层
```

本设计不引入：

- PostgreSQL；
- SQLAlchemy 2.0 Async；
- `aiosqlite`；
- MongoDB、Redis、Neo4j；
- 独立向量数据库。

原因：当前产品是本地单用户 Personal Agent。SQLite 已经满足本地事务、恢复、审计和可移植性要求。当前问题来自记忆边界和数据模型，而不是数据库能力不足。

数据库操作仍然同步。长时间模型调用、文件投影和摘要生成在 transaction 外执行；SQLite transaction 只做短小、确定的状态提交，避免长时间阻塞事件循环。

### 2.2 不使用 AgentScope

Better Agent 继续保留自己的：

- Conversation Worker；
- Agent Runtime；
- PlanDocument；
- Event / Checkpoint；
- Tool Approval；
- ContextAssembler。

不引入 AgentScope 的 ReAct、Memory、Event 或 Session Runtime，避免形成双主循环、双状态机、双事件系统和双恢复机制。

可以参考开源项目的设计思想，但不依赖其运行框架。

### 2.3 第一版不做向量检索

长期记忆首先通过以下规则选择：

1. owner 和 project 硬隔离；
2. 用户 pinned 记忆优先；
3. SQLite FTS5 词法检索；
4. importance、更新时间和稳定 ID 排序；
5. 以完整条目为单位执行 Token 预算。

中文优先评测 FTS5 `trigram`；1–2 字查询使用规范化精确匹配和受 scope 限制的 `LIKE` fallback。

只有真实评测证明词法召回不足后，才讨论向量检索。即使未来加入向量召回，SQLite 仍然是权威数据源。

## 3. 用户应该理解的三层记忆

### 3.1 短期记忆 Short-term Memory

短期记忆是当前 Thread 最近的完整对话，用来回答：

> 我们现在正在讨论什么？

内容包括：

- 当前用户消息；
- 最近若干次用户与助手消息；
- 当前未完成的 Ask 问答；
- 当前正在讨论的计划修改；
- 当前轮必要的工具结果。

数据来源是现有 `thread_messages`，不新建另一份短期记忆表。

默认窗口：

- 最近最多 50 条可用消息；
- 历史最多约 8,000 Token；
- 当前用户消息永远保留；
- pending Ask pair 永远完整保留；
- 活动 PlanDocument 由 PlanContextProvider 单独提供，不占用短期记忆语义。

超出窗口的旧消息不会删除，而是先形成经历记忆，再退出默认 Prompt 窗口。

短期记忆不需要用户确认，也不会直接成为用户画像。

### 3.2 经历记忆 Episodic Memory

经历记忆是旧对话的可追溯摘要，用来回答：

> 之前发生过什么？为什么会变成现在这样？

示例：

> 用户正在规划桂林七天行程；第三天从阳朔调整为龙脊梯田，并希望当天九点后出发。

特点：

- 从完整 Turn bundle 自动生成；
- 保留来源 Thread、消息范围和 source hash；
- 是模型生成的派生叙述，不是权威事实；
- 可以自动保存，不要求逐条确认；
- 用户可以查看、修正和删除；
- 默认只在原 Thread 或已绑定 Project 内检索；
- 敏感经历默认只在原 Thread 使用，不跨 Thread；
- 不允许成为工具授权、安全规则或计划审批依据。

经历摘要失败不会丢失原文：原始 `thread_messages` 仍保存在 SQLite。多次失败后写入 `RAW_REFERENCE`，只保存消息范围、状态和 Hash，使归档游标能够继续前进，后续可以重新生成摘要。

### 3.3 长期记忆 Long-term Memory

长期记忆是跨对话仍然有效的稳定信息，用来回答：

> 以后仍然应该知道什么？

长期记忆类型只保留五种：

| 类型 | 含义 | 示例 |
|---|---|---|
| preference | 用户偏好 | 喜欢节奏舒缓的旅行 |
| constraint | 用户约束 | 不吃辣、每天九点后出发 |
| fact | 稳定事实 | 用户常住上海 |
| decision | 长期决定 | 旅行优先选择高铁 |
| lesson | 可复用经验 | 复杂计划先确定硬约束 |

作用域只有两种：

- `user`：当前用户跨项目有效；
- `project`：只在明确绑定的 Project 内有效。

Skill 不再是一种长期记忆 scope。Skill 是能力和指令；与 Skill 使用相关的用户事实仍然属于 user 或 project memory。

长期记忆的保存规则：

- 用户明确说“记住我不吃辣”：视为直接授权，检查后立即保存，并提供撤销；
- 用户在 MemoryPage 手工新增/编辑：直接保存新版本；
- 模型从普通对话中推断“用户可能不喜欢早起”：只能生成待确认建议；
- 更新、替代和归档都生成待确认建议，旧记忆在确认前继续有效；
- rejected 建议不会基于相同内容和相同证据反复出现。

每条长期记忆是一个独立 Entry，并拥有不可变 Revision。编辑和回滚都创建新 Revision，不覆盖历史正文。

## 4. 明确排除：哪些内容不是记忆

| 内容 | 正确归属 | 为什么不是记忆 |
|---|---|---|
| System Prompt、AGENTS.md、Skills | Instructions / Policy | 决定系统行为和权限 |
| 当前调用拼装出的 Prompt | Working Context | 一次性的模型输入 |
| 七天旅游攻略正文 | PlanDocument | 用户维护的计划资产 |
| PlanVersion / PlanStep | Execution Plan | 一次执行的结构化快照 |
| Event / ThreadEvent | Execution Ledger | 轨迹和审计 |
| Checkpoint | Recovery State | 中断恢复状态 |
| Tool Result / Approval | Tool Ledger | 工具事实和授权 |
| Markdown 文件 | Read-only Projection | SQLite 记忆的可读视图 |

禁止增加可由模型自动修改的 `SOUL.md`。用户对回答风格的偏好可以是长期记忆，但 Agent 身份、工具权限和审批规则只能由指令层修改。

## 5. 三层记忆如何流转

### 5.1 对话进入短期记忆

```text
用户消息提交
  → thread_messages(user)
  → 模型回答完成
  → thread_messages(assistant, ready)
  → 下一次调用读取受限短期窗口
```

只有最终可读 generation 进入正常窗口。running、失败后被替代的 generation 和未完成 Ask 不作为普通历史正文。

### 5.2 短期记忆压缩为经历记忆

触发条件：

- 消息数量超过阈值；
- Token 超过阈值；
- 一个计划或自然任务完成；
- 用户主动要求总结对话。

归档单位是完整 Turn：

```text
READY                → 摘要正文
TERMINAL_EXCLUDED    → 只记录失败/取消等确定性元数据
PENDING              → 暂停归档，不能越过
```

提交过程：

```text
1. SQLite 短 transaction 固定 Turn 范围、lease 和 source hash
2. transaction 外调用摘要模型
3. 校验 source hash 和输出结构
4. SQLite 单 transaction 提交 Episode 并推进 cursor
```

重复触发、Worker 抢占和崩溃恢复由 UNIQUE + lease CAS 保证只生成一个 Episode。

### 5.3 经历记忆提出长期记忆建议

MemoryMaintainer 低频读取：

- 新产生的 Episode；
- 必要的来源消息；
- 当前长期记忆 head。

它只能输出：

```text
ADD       新增长期记忆
UPDATE    更新一个明确 Entry
ARCHIVE   建议停止使用一个 Entry
NO_CHANGE 不值得长期保存
```

MemoryMaintainer 没有直接写长期记忆、计划、指令或工具状态的权限。它只能创建 Proposal。

第一版只在任务完成、Episode 累积达到阈值或用户手动点击“整理建议”时运行，不增加常驻 Dream daemon 或定时任务。

### 5.4 用户明确要求记住

```text
“记住我不吃辣”
  → 识别 explicit remember
  → 校验 owner / scope / secret / duplicate / conflict
  → 使用来源 message ID 作为幂等键
  → 同 transaction 创建 Entry + Revision + audit
  → 回答“已记住，可撤销”
```

如果作用域或被替代的旧记忆不明确，只提出一个最小澄清问题。

## 6. 统一的记忆读取

### 6.1 一个 Provider 服务两个 Runtime

普通 Conversation 与 Agent Runtime 都调用：

```text
MemoryContextProvider.select(request) -> MemoryContextBundle
```

输入：

- owner ID；
- Thread / Run；
- 明确绑定的 project ID；
- 当前用户请求；
- 当前计划标题或步骤；
- 调用目的；
- Semantic 和 Episode Token 预算。

输出：

- 完整渲染的记忆数据块；
- ordered Revision IDs；
- ordered Episode IDs；
- 每条选择原因；
- 被丢弃数量和原因；
- tokenizer/estimator version；
- Token count 和 bundle hash。

Conversation 和 Agent 不再各自查询 memory 表，也不再调用全量 `all_records()` 后二次筛选。

### 6.2 Scope 先于检索

Project scope 只能来自持久化关系：

- Conversation：`threads.project_id`；
- Agent：`goals.project_id`；
- 未绑定 Thread：只能使用 user memory 和原 Thread Episode。

模型不能根据“桂林”“Better Agent”等词自行猜测 Project。

Episode 的硬过滤：

```text
episode.thread_id == current_thread
OR
(episode.project_id IS NOT NULL AND episode.project_id == current_project)
```

`project_id IS NULL` 的 Episode 不会跨 Thread 变成所谓全局经历。

### 6.3 排序规则

按以下稳定顺序选择：

1. scope 匹配的 pinned constraint；
2. scope 匹配的 pinned preference；
3. 当前 Project 的 FTS 命中 Entry；
4. user scope 的 FTS 命中 Entry；
5. 当前 Thread / Project 的相关 Episode；
6. importance、更新时间、稳定 ID 作为 tie-breaker。

Pinned 表示最高候选优先级，不表示无限注入。默认最多 20 条，并最多占 Semantic 预算的 60%。继续 pin 会超限时，UI 要求用户先整理。

### 6.4 Token 预算

默认：

```text
Semantic Memory ≤ 1,500 Token，且不超过可用输入的 15%
Episodic Memory ≤ 1,000 Token，且不超过可用输入的 10%
全部记忆 ≤ 可用输入的 25%
```

当前用户消息、System Policy、pending Ask、活动计划和工具审批是 protected blocks，记忆不能把它们挤掉。

记忆以完整 Entry/Episode 为单位接纳或丢弃，永远不对正文进行字符串折半截断。

### 6.5 调用级 Pin 与审计

一次逻辑模型调用对应一个 `model_invocation_id` 和一个 Context Pin。

- HTTP/供应商重试和流式重连复用同一 pin；
- 同一 Turn 中的新逻辑调用创建新 pin；
- pin 记录最终 Revision/Episode IDs、query/scope/budget、renderer/tokenizer version 和 rendered hash；
- `ContextAssembler` 完成最终裁剪后才保存 pin；
- Purge 会使引用被删正文的 active pin 失效，隐私优先于重放。

SQLite 无法与外部模型 HTTP 请求原子提交，因此事件语义是：

```text
memory.context_prepared  已为 attempt 构造最终记忆上下文
model.attempt_started     已开始尝试模型调用
model.attempt_finished    调用在本地观察到完成/失败
provider_acknowledged     只有供应商返回 request ID 时才记录
```

系统不声称仅凭 SQLite Event 就能证明供应商一定收到请求。

## 7. 最小数据模型

### 7.1 短期与经历记忆

```text
threads
  + owner_id
  + project_id nullable

thread_messages
  + message_seq
  UNIQUE(thread_id, message_seq)

conversation_archive_state
  owner_id, thread_id PRIMARY KEY
  archived_through_seq
  reserved turn range / source_hash
  state, lease_owner, lease_until, attempts, error

memory_episodes
  id, owner_id, thread_id, project_id nullable
  start_message_seq, end_message_seq, source_hash
  summary, topics, decisions, open_loops
  sensitivity, retrieval_policy, retain_until
  status, supersedes_episode_id
  model_id, prompt_version, created_at
  UNIQUE(owner_id, thread_id, start_seq, end_seq, source_hash)
```

### 7.2 长期记忆

```text
memory_entries
  id, owner_id
  kind, scope_type, scope_id
  status, current_revision_id
  canonical_fingerprint
  pinned, importance, sensitivity, valid_until
  created_at, updated_at

memory_revisions
  id, entry_id, revision_no
  operation, content, content_hash
  base_revision_id
  actor, source_refs, reason
  created_at
  UNIQUE(entry_id, revision_no)

memory_proposals
  id, owner_id
  operation, target_entry_id, base_revision_id
  proposed kind/scope/content
  fingerprint, evidence refs/hash
  origin, confidence, reason, status
  request_idempotency_key
  decision_idempotency_key
  accepted_revision_id
```

数据库约束：

- 同 owner/scope 的 active exact fingerprint 唯一；
- Proposal accept 使用 `PENDING -> ACCEPTED` CAS；
- accept 重试返回同一个 `accepted_revision_id`；
- direct remember 使用来源 message/client request ID 幂等；
- 所有查询首先带 owner，再带 scope。

### 7.3 Pin、投影和审计

```text
memory_context_pins
  model_invocation_id UNIQUE
  owner / parent / purpose
  query_hash / scope_hash
  revision_ids / episode_ids
  tokenizer / renderer / budget / token_count
  rendered_hash / invocation_state

memory_projection_intents
  owner_id / path
  expected_hash / target_hash
  state / attempts / error

memory_audit_events
  owner_id / seq
  event_id UNIQUE
  aggregate_type / aggregate_id
  idempotency_key UNIQUE
  operation / actor / occurred_at / safe metadata
```

生命周期 audit 不保存记忆正文或内容 Hash。调用级 prepared event 的 Prompt Hash 随 pin retention 清理。

## 8. SQLite 一致性策略

### 8.1 SQLite 是唯一权威源

只有 committed Entry Revision 和 active Episode 可以进入上下文。Markdown 文件删除、陈旧或写入失败不影响 SQLite 中记忆的有效性。

SQLite transaction 内完成：

- Entry + Revision + head 更新；
- Proposal 决策；
- Episode + archive cursor；
- Context Pin + prepared audit；
- lifecycle state + memory audit。

模型调用和文件写入不放进 SQLite transaction。

### 8.2 Markdown 是只读投影

```text
data/memory/
├── USER.md
├── MEMORY.md
├── projects/<safe-project-id>.md
└── exports/history.jsonl
```

- `USER.md`：user scope preference/constraint；
- `MEMORY.md`：其他 user scope 长期记忆；
- project 文件：project scope 长期记忆；
- `history.jsonl`：可选经历导出。

首版不支持从 Markdown 反向写回 SQLite。用户通过 MemoryPage 编辑。文件被外部修改时保留 conflict 副本，再从 SQLite 重建，不做三路合并。

渲染固定为 UTF-8 无 BOM、LF、稳定排序、单个末尾换行，并记录 renderer version。Project 文件名使用安全编码，不直接拼用户输入。

### 8.3 投影恢复

权威 transaction 提交 Revision 时同时写入 projection intent。随后：

1. 从当前 committed heads 确定性渲染；
2. 临时文件写入、flush/fsync、原子替换；
3. 标记 intent committed；
4. 启动时重试 pending/failed intents；
5. 检测第三方文件 Hash 时保存 conflict 副本，不覆盖。

投影失败只产生 lag/conflict，不回滚已确认记忆。

## 9. 生命周期与用户控制

### 9.1 长期记忆状态

```text
Proposal: PENDING → ACCEPTED | REJECTED | SUPERSEDED
Entry:    ACTIVE ↔ ARCHIVED → PURGED
```

- Edit：创建新 Revision；
- Update：基于明确 target/base 创建新 Revision；
- Rollback：用旧内容创建新的 ROLLBACK Revision；
- Archive：新的模型调用立即停止使用，可恢复；
- Forget：Archive + 可选 HMAC suppression，防止相同建议反复出现；
- Purge：删除正文、索引和 suppression，不可恢复。

### 9.2 冲突

以下情况不做 last-write-wins：

- 用户确认 Proposal 时 Entry head 已变化；
- 两个 Worker 并发 accept；
- 外部文件与投影竞争；
- 新偏好可能替代旧偏好但目标不明确。

UI 显示 old/current/proposed diff，让用户选择。

### 9.3 隐私

- API key、密码、token、private key 等不进入 Episode、Proposal、Revision、Event 或投影；
- 普通敏感信息标记 sensitivity；
- 健康、财务、身份、精确位置经历默认 Thread-only；
- Forget suppression 使用数据库外 OS secret 的 HMAC；不可用时宁可不抑制，也不使用普通 SHA；
- Purge 使 active pin 失效，并清理 FTS、投影、临时文件、WAL/freelist；
- 明确说明外部备份和文件系统快照不受应用控制，不承诺取证级擦除。

## 10. 用户界面

MemoryPage 只展示三个与用户心智一致的区域。

### 10.1 当前对话

默认不重复展示聊天页全部消息，只显示：

- 当前短期窗口使用量；
- 已归档到哪个 Turn；
- 是否存在归档失败或 pending Ask。

### 10.2 最近经历

展示：

- 摘要；
- 来源 Thread/Project；
- 消息范围和时间；
- sensitivity/retrieval policy；
- 是否被长期建议引用。

操作：查看来源、修正摘要、限制作用域、删除。

### 10.3 长期记忆

展示：

- 待确认建议；
- 已生效记忆；
- 已停用记忆；
- 当前 Revision 和来源；
- scope、pinned、importance、validity；
- 最近在哪次调用中被 prepared。

操作：新增、确认、拒绝、编辑、撤销、回滚、Archive、Forget、Purge。

用户不需要阅读 Proposal/Revision/Pin 这些内部名词；UI 使用“待确认变更、当前版本、调用记录”等自然语言。

## 11. 桂林七天攻略示例

用户：

> 帮我制定桂林七天旅游攻略。我不吃辣，行程不要太赶。

系统处理：

1. 原始请求进入短期记忆；
2. 七天具体行程写入 PlanDocument，不写入长期记忆；
3. 本轮直接使用“不吃辣、不要太赶”生成计划；
4. 系统可以询问：“以后旅行也默认不吃辣、节奏舒缓吗？”；
5. 用户确认后，它们成为长期 constraint/preference；
6. 如果用户原话是“记住我不吃辣”，直接保存并提供撤销。

用户后来修改：

> 第三天改成龙脊梯田，九点以后出发。

系统处理：

- 修改内容形成新的 PlanDocument Revision；
- 旧对话离开短期窗口后形成“第三天改为龙脊梯田”的经历记忆；
- “九点以后出发”只出现一次时仍是本计划内容，不自动成为长期偏好；
- 多次稳定表达后，MemoryMaintainer 可以提出长期建议，等待用户确认。

第三天用户询问：

> 今天下雨，第三天怎么调整？

本轮上下文为：

```text
当前问题
+ 当前 PlanDocument 的固定版本
+ 当前 Thread 最近对话
+ 桂林 Project 的相关经历
+ 已确认的不吃辣/舒缓节奏长期记忆
```

不会加载全部历史对话、其他 Project 记忆或所有长期文件。

## 12. 对当前代码的关键修复

实施新功能前必须先修复：

1. `MemoryService._rewrite_path()` 按具体 `project_id/skill_name` 过滤，停止跨 scope 聚合；
2. 禁用旧 path-level rollback 把整份文件写入单条 memory 的行为；
3. 禁用 `sync_manual_edits()` 把整份聚合文件复制给每条 record；
4. Conversation 接入与 Agent 相同的 MemoryContextProvider；
5. 删除 Agent step 开始阶段提前发出的 `memory.applied`；
6. `ContextAssembler` 停止按字符串折半截断记忆；
7. 新记忆模型采用条目 Revision，不再把文件 generation 当作条目版本。

## 13. 实施阶段

### Phase 0：止血

- 修复跨 project/skill 污染；
- 封锁错误回滚与 manual sync；
- 备份 DB 和 memory 文件并扫描已有污染；
- 内容未变化时不再创建重复文件版本。

### Phase 1：长期记忆正确化

- 新建 Entry、Revision、Proposal、audit 和 projection intent；
- 实现 direct remember、确认、拒绝、编辑、回滚、Archive/Forget/Purge；
- Markdown 改为 SQLite 的只读投影；
- 迁移旧数据并处理 duplicate/corrupt/unverified/skill unresolved。

### Phase 2：统一读取

- 实现 MemoryContextProvider；
- Conversation 和 Agent 共用 scope、FTS、排序和预算；
- 实现 invocation-level pin 和 prepared/attempt audit；
- ContextAssembler 返回最终 included/dropped IDs。

### Phase 3：经历记忆

- 为 Thread message 增加稳定 sequence；
- 实现完整 Turn bundle 归档、lease、source hash 和 cursor；
- `_history()` 改为未归档短窗口，避免原文与 Episode 重复注入；
- 增加经历查看、修正、删除和敏感策略。

### Phase 4：长期整理建议

- MemoryMaintainer 从 Episode 生成结构化 Proposal；
- 增加 targeted update/archive、拒绝抑制和证据校验；
- 用真实模型评测准确性、接受率、重复率和成本；
- 数据证明需要前不增加定时 daemon 或向量检索。

## 14. 旧数据迁移

迁移前生成只读 inventory：

```text
clean
duplicate
invalid_evidence
aggregate_shaped_or_corrupt
skill_unresolved
```

- clean confirmed：迁为 ACTIVE Entry + 初始 Revision；
- clean proposed/rejected：迁为相应 Proposal；
- disabled：迁为 ARCHIVED Entry；
- `habit`：映射建议为 preference，但需用户确认；
- 旧 evidence：标记 `legacy_unverified_ref`，不冒充有效证据；
- 疑似整文件正文：进入 quarantine，不自动激活；
- skill scope：转换为 user/project Proposal，无法判断时等待用户决定。

建立 `legacy_memory_mappings` 保存每个旧 ID 的去向。切换前 unresolved 必须为 0，或用户明确弃置。

SQLite schema 迁移采用显式 `schema_migrations(version, checksum, applied_at)`，不继续在 `db.py` 中堆积条件 `ALTER TABLE`。迁移期间持有 maintenance lock，不启动 Worker。

## 15. 必须通过的验收

### 分层

- 当前消息只进入短期窗口；
- 离开窗口的完整 Turn 可形成 Episode；
- Episode 不会自动成为长期事实；
- 长期记忆有 scope、来源、版本和用户控制；
- Plan、Event、Checkpoint、Instructions 不进入记忆表。

### 正确性

- Conversation 与 Agent 对相同 scope/query 选择相同长期记忆；
- 两个 Project、两个 owner 严格隔离；
- 未绑定 Thread 的 Episode 不跨 Thread；
- 并发 remember/accept/archive 只提交一次；
- 旧 lease Worker 无法晚提交；
- 一条记忆完整进入或完整丢弃，不截半句；
- retry 使用同一 invocation pin，新调用读取新 head。

### 归档

- READY Turn 正常摘要；
- failed/interrupted/cancelled 不会永久卡住 cursor；
- pending Ask 会阻止越过；
- 可重试失败不推进 cursor；
- 达到上限后提交 RAW_REFERENCE 才推进；
- 原始消息始终可以作为来源查看。

### 隐私

- Secret 不进入任何记忆正文、投影、事件或导出；
- 敏感 Episode 默认 Thread-only；
- Archive 后新调用立即停止使用；
- Purge 后正文、FTS、投影、suppression 和 active pin 均被清理；
- UI 不承诺无法实现的取证级擦除。

### 审计

- Context Pin IDs 与最终 prepared Prompt 一致；
- prepared、attempt、provider acknowledgment 语义不混淆；
- 生命周期事件不保存正文或低熵内容 Hash；
- MemoryPage、启动恢复和无 Run 操作都有合法 memory audit。

## 16. 非目标

本版本不做：

- 替换 SQLite；
- SQLAlchemy / async ORM 重构；
- AgentScope；
- 向量数据库和通用 RAG；
- Redis/Celery；
- 多 Agent Dream 系统；
- Markdown 双向自动同步；
- 自动修改 System Prompt、Skill 或工具权限；
- 一次性摘要全部历史 Thread。

## 17. 最终架构

```text
                       Better Agent 记忆系统

┌────────────────────────────────────────────────────────┐
│ 短期记忆                                               │
│ 当前 Thread 的最近完整消息                             │
│ 权威源：SQLite thread_messages                         │
└───────────────────────┬────────────────────────────────┘
                        │ 完整 Turn 超出窗口
                        ▼
┌────────────────────────────────────────────────────────┐
│ 经历记忆                                               │
│ 带来源范围的旧对话摘要                                 │
│ 权威源：SQLite memory_episodes                         │
└───────────────────────┬────────────────────────────────┘
                        │ MemoryMaintainer 提出建议
                        ▼
┌────────────────────────────────────────────────────────┐
│ 长期记忆                                               │
│ 用户确认的偏好、约束、事实、决定和经验                 │
│ 权威源：SQLite entries + revisions + proposals         │
└───────────────────────┬────────────────────────────────┘
                        │ scope + FTS5 + budget
                        ▼
              MemoryContextProvider
                        │
                        ▼
             Conversation / Agent Runtime

非记忆：Instructions、PlanDocument、Event、Checkpoint、Tool Ledger
可读投影：USER.md、MEMORY.md、projects/*.md、history.jsonl
```

这套设计的核心不是增加更多组件，而是建立一条不会混淆的链路：

```text
现在聊什么 → 以前发生什么 → 以后记住什么
```

SQLite 负责可信状态，Markdown 负责可读，用户负责长期知识的最终决定，两个 Runtime 负责一致地使用记忆。

## 18. 参考依据

- [Nanobot memory design](https://github.com/HKUDS/nanobot/blob/main/docs/memory.md)：当前 Session、压缩 History、长期文件的分层思想；
- [LangGraph memory concepts](https://docs.langchain.com/oss/python/concepts/memory)：Thread short-term 与跨 Thread long-term，以及 episodic/semantic 分类；
- [LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)：hot-path remember、后台整理和 collection 型记忆；
- [Letta memory](https://docs.letta.com/concepts/memfs/index.md)：小型常驻内容与按需详情；
- [Claude Code CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)：auto-memory、scope、小型索引和用户管理产品契约。Claude Code 核心实现并非开放源码，本设计不推断其内部实现。

