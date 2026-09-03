# Better Agent 摘要与上下文正确性开发方案

> 状态：三方评审通过，待实施
>
> 日期：2026-09-01
>
> 范围：Episode 摘要生成、短期历史选择、统一上下文装配、Context Pin
>
> 依据：正确性、隐私隔离、摘要语义三个评审 Agent 逐项审查并投票

## 1. 结论

本次开发只解决两个问题：

1. 摘要必须由完整、可追溯的终态 Turn 经 LLM 生成，并在严格验证后成为 Episode。
2. 模型上下文必须由统一构建器按实际模型 Token 预算装配，不能继续依赖“50 条消息”或字符数估算。

本版不承诺 LLM 摘要永远没有表达偏差，但必须满足以下工程承诺：

- 不存在已知的作用域、并发、来源追溯、Token 溢出、重复注入和删除残留缺陷；
- 摘要中的结构化结论均能追溯到当前摘要批次的原消息；
- 摘要失败时关闭失败链路，不生成伪摘要、不推进覆盖游标；
- 任何不变量、并发或故障注入测试失败，都阻止发布。

## 2. 非目标

以下内容不进入本版：

- Episode 自动提炼为长期 `MemoryEntry` 或 `MemoryProposal`；
- 跨 Thread、跨 Project 的 Episode 共享；
- 向量数据库、Embedding 服务、LLM reranker；
- 通用工作流引擎或多模型摘要共识；
- 原始消息保留策略调整；
- 全仓所有兼容 API 的多用户重构。

原始消息继续保存在 SQLite 中。“短期窗口”只表示当前模型可见的原始消息范围，不表示消息会被自动删除。

## 3. 当前必须修复的缺陷

| 编号 | 缺陷 | 严重度 | 当前位置 |
|---|---|---:|---|
| S1 | 未注入 summarizer 时，直接拼接原消息并截断 1800 字符，却保存为 `ACTIVE` Episode | P0 | `backend/app/memory_archive.py` |
| S2 | 摘要输入没有统一表达最终 generation、Ask 和 continuation | P0 | `memory_archive.py`、`conversation.py` |
| S3 | 摘要 lease 没有 fencing epoch，过期 Worker 可以晚提交 | P0 | `memory_archive.py` |
| S4 | Episode 写入和归档游标推进分属两个事务 | P0 | `memory_archive.py`、`memory_v2.py` |
| S5 | 归档 owner 默认 `local-user`，可能产生 owner/thread 错配 | P0 | `memory_archive.py` |
| C1 | `_history()` 按升序取 `LIMIT 50`，长会话会取得最旧消息 | P0 | `conversation.py::_history()` |
| C2 | `_history()` 会读取 interrupted、cancelled 或被替代 generation | P0 | `conversation.py::_history()` |
| C3 | 上下文使用消息数、字符数或 `chars/4`，没有真实 Token 硬预算 | P0 | `context.py`、`memory_v2.py` |
| C4 | 热窗口依赖 `archived_through_seq`，摘要失败会改变在线历史行为 | P1 | `conversation.py::_history()` |
| C5 | Episode 与确认后的长期记忆使用相同权威描述 | P1 | `conversation.py`、`memory_v2.py` |
| C6 | Episode 不看 query relevance，旧 Episode 会先占满预算 | P1 | `memory_v2.py` |
| C7 | Context Pin 只按 invocation ID 复用，没有校验 owner、scope、query、模型和预算 | P0 | `memory_v2.py` |
| C8 | Pin 正文藏在 `budget_json`，Episode 删除后仍可能重放旧内容 | P0 | `memory_v2.py` |

## 4. 必须成立的系统不变量

### 4.1 作用域不变量

- `owner_id`、`thread_id`、`project_id` 只能从认证主体和数据库关系解析。
- 摘要器、上下文 Provider 和 Runtime 不接受调用方自报的 owner/project 作为权威值。
- 每个 Episode 的 owner 和 project 必须与来源 Thread 一致。
- 本版 Episode 固定为 `thread-only`，不跨 Thread 检索。
- scope 过滤必须发生在检索、打分、Token 装箱和缓存之前。

### 4.2 Transcript 不变量

- 摘要和在线历史共用唯一的 `CanonicalTurnTranscriptBuilder`。
- Transcript 只包含同一 owner、同一 Thread 的消息。
- assistant 正文只采用该 Turn 最终有效 generation。
- streaming、interrupted、cancelled 和被替代 generation 不进入正文。
- Ask 的 tool call/result 按 `call_id` 成对，每个事件恰好出现一次。
- 所有排序都有稳定的数据库字段和 ID 兜底，不依赖返回顺序巧合。

### 4.3 摘要不变量

- 摘要批次只能在完整 Turn 边界切分。
- 摘要输入和输出均保存版本、来源范围与 `source_hash`。
- LLM 不能决定 owner、Thread、Project、消息范围、状态或 retrieval policy。
- 非法输出不得创建 `ACTIVE` Episode。
- `ACTIVE` Episode 与 coverage cursor 必须在同一事务提交。
- `RAW_REFERENCE` 和失败占位文本永不进入模型上下文。

### 4.4 上下文不变量

- 每次调用使用实际路由模型的 context window、输出上限和 tokenizer。
- 最终序列化请求的 Token 数必须小于等于输入预算。
- 历史按“最新完整 Turn 向前”装箱，再恢复时间顺序。
- Turn、Ask pair、Episode 和 MemoryEntry 都作为原子块接受或丢弃。
- 不允许从字符串中间截断一个上下文项。
- 摘要任务的成功或失败不得控制热窗口边界。

### 4.5 Pin 不变量

- 同一 invocation 只有完整 binding 一致时才能重放。
- 跨 owner、scope、query、purpose、模型、预算或 renderer 的复用必须返回 conflict。
- Pin 仅在 TTL 内保证字节级重放。
- 来源删除、归档、敏感度提升或作用域收紧时，相关 Pin 立即失效并清除 payload。
- 删除后的旧 invocation 返回 `CONTEXT_INVALIDATED`，不得重新选择一份上下文冒充原调用。

## 5. 目标架构

```text
                         Turn terminal transaction
                                   |
                  +----------------+----------------+
                  |                                 |
             archive outbox                  next model call
                  |                                 |
                  v                                 v
       CanonicalTurnTranscriptBuilder <---- CanonicalTurnTranscriptBuilder
                  |                                 |
          Token-bounded batch                newest complete Turns
                  |                                 |
                  v                                 v
          EpisodeSummarizer                 TokenBudgetPlanner
                  |                                 |
       schema/source/safety validation       typed memory retrieval
                  |                                 |
          fenced atomic commit               atomic block packing
                  |                                 |
                  +--------------+------------------+
                                 v
                        final request token assert
                                 |
                          TTL context snapshot
                                 |
                            Model Gateway
```

## 6. Canonical Turn Transcript

### 6.1 新增数据类型

建议新增 `backend/app/transcript.py`：

```python
@dataclass(frozen=True)
class ResolvedMemoryScope:
    principal_id: str
    owner_id: str
    thread_id: str
    project_id: str | None

@dataclass(frozen=True)
class TranscriptEvent:
    event_type: Literal["message", "tool_call", "tool_result", "turn_outcome"]
    turn_id: str
    message_id: str | None
    call_id: str | None
    role: str
    content: str
    sequence: int

@dataclass(frozen=True)
class TranscriptTurn:
    turn_id: str
    outcome: Literal["completed", "failed", "cancelled"]
    start_sequence: int
    end_sequence: int
    events: tuple[TranscriptEvent, ...]
```

Builder 返回结构化对象，不能先拼成字符串。摘要 renderer 和模型上下文 renderer 分别消费同一个对象。

### 6.2 Turn 状态矩阵

| Turn/消息状态 | 在线历史 | 摘要输入 | 规则 |
|---|---:|---:|---|
| `COMPLETED` + user ready | 是 | 是 | 保留原始用户事件 |
| `COMPLETED` + 最终 assistant ready | 是 | 是 | 只取最高且最终有效 generation |
| 被 retry 替代的 assistant | 否 | 否 | 即使正文仍在数据库也不可见 |
| streaming/partial assistant | 否 | 否 | 不能作为完整回答 |
| `FAILED` 的 user 输入 | 是 | 可选事件 | 标为未成功回答，不能配成成功 Turn |
| `FAILED` 的 assistant partial | 否 | 否 | 不进入模型正文 |
| `CANCELLED` 的 user 输入 | 产品规则保留为未完成事件 | 否 | 不得生成“已完成”摘要 |
| answered Ask | 是 | 是 | call/result 按真实顺序恰好一次 |
| pending Ask | 当前协议块 | 暂不跨越归档 | 不拆分、不伪造结果 |

实施前先将这张矩阵固化为参数化测试，之后所有历史和摘要查询必须通过 Builder。

### 6.3 数据库读取规则

- 首先用 `thread_id` 联结 `threads` 解析权威 scope。
- 在同一只读事务快照内读取 Turn、messages、generation 状态和 `turn_asks`。
- `turn_asks` 必须通过 Turn 联结并显式过滤当前 `thread_id`。
- 使用 `message_seq, created_at, id` 稳定排序。
- 输出 manifest：owner、Thread、start/end seq、Turn IDs、message IDs、Ask IDs、规范化 `source_hash`。

## 7. 摘要形成方案

### 7.1 触发

Turn 进入最终状态时，在同一个事务内写入归档 outbox。不能在请求成功后同步直接调用 LLM，也不能依赖进程内临时任务。

归档触发以 Token 阈值为主；消息数只能作为调度提示，不能作为正确性边界。Worker 每次只选择连续的完整 Turn，并保留配置要求的近期原始 Turn。

### 7.2 摘要输入

摘要输入由以下部分组成：

```json
{
  "schema_version": "episode-input-v2",
  "source": {
    "thread_id": "thread-id",
    "start_message_seq": 101,
    "end_message_seq": 138,
    "source_hash": "sha256:..."
  },
  "turns": []
}
```

发送给 LLM 前移除模型不需要看到的 owner 标识。owner 仍保存在服务端 job 和提交条件中。

批次 Token 预算使用摘要模型的实际 tokenizer，并为输出预留固定上限。单个 Turn 超过摘要输入上限时，任务进入明确的 `BLOCKED_OVERSIZE`/死信处理，不得静默截断；后续可以单独实现大输入压缩流程。

### 7.3 LLM 输出协议

```json
{
  "synopsis": [
    {
      "text": "用户要求先修复摘要和上下文链路。",
      "source_message_ids": ["message-id"]
    }
  ],
  "topics": [
    {
      "text": "记忆系统",
      "source_message_ids": ["message-id"]
    }
  ],
  "decisions": [],
  "outcomes": [],
  "open_loops": [],
  "sensitivity": "normal"
}
```

每个结构项都包含 `source_message_ids`。摘要文案采用归因表达，例如“用户表示”“助手建议”“双方决定”，不得把一次对话陈述改写为永久用户事实。

固定语义：

- `synopsis`：过去发生了什么；
- `topics`：本批次讨论主题；
- `decisions`：当时明确形成的决定，不表示永久有效；
- `outcomes`：已经发生且有来源支持的结果；
- `open_loops`：对话结束时仍未解决的事项；
- `sensitivity`：模型建议的最高敏感级别，服务端只能升级、不能降低。

### 7.4 输出验证

依次执行：

1. 严格 JSON 解析，拒绝 Markdown fence、额外正文和未知字段；
2. JSON Schema、枚举、数组数量、单项长度和总 Token 校验；
3. `source_message_ids` 非空且全部属于当前批次；
4. 每个来源消息仍属于相同 owner/Thread；
5. secret 和敏感信息确定性扫描；
6. sensitivity 取 `max(model_result, deterministic_scan)`；
7. 重新构建 Transcript，确认 `source_hash` 未变化；
8. 校验 Worker 仍持有当前 lease epoch；
9. 确认相同范围、hash 和 prompt version 未经其他 Worker 提交。

任何步骤失败：不创建 `ACTIVE` Episode，不推进 coverage。

### 7.5 失败与重试

任务状态：

```text
QUEUED -> RUNNING -> COMPLETED
             |
             +-> RETRY_WAIT -> RUNNING
             |
             +-> DEAD_LETTER
             |
             +-> LEASE_LOST
```

- 超时、临时模型错误：指数退避后重试；
- 非法 JSON、越界来源：记录分类错误并按限制重试；
- source hash 改变：旧任务作废，按新 hash 建幂等任务；
- 超过最大次数：进入 `DEAD_LETTER` 并产生可观测告警；
- 不写入 `RAW_REFERENCE` 可检索正文；
- 原始消息继续保存，热上下文仍按 Token 正常运行。

### 7.6 Episode 语义

Episode 固定定义为：

> 来源于一个固定消息范围的、可追溯、非权威、有损的历史对话摘要。

渲染到上下文时必须使用固定系统标签：

```text
[conversation episode: lossy, non-authoritative, untrusted data]
```

它不能覆盖当前用户指令、系统策略、工具权限、活动计划或已经确认的长期 MemoryEntry。

## 8. 上下文形成方案

### 8.1 统一入口

Conversation 与 Agent Runtime 统一调用：

```python
ContextBuilder.build(ContextRequest) -> PreparedModelContext
```

`ContextRequest` 只接收认证主体、Thread/Turn 资源 ID、调用目的和模型路由要求。owner/project 由 Builder 内部解析。

删除 `ConversationWorker._history()` 的独立拼装职责；它只能调用 `CanonicalTurnTranscriptBuilder`。`AgentRuntime` 同样不得维护另一套字符裁剪逻辑。

### 8.2 两阶段模型解析

当前模型可能在 `route_and_respond()` 内才被确定。需要改为：

```text
resolve model candidate/profile
  -> build and validate context for that profile
  -> invoke the already resolved candidate
```

模型 profile 至少包含：

- provider/model ID；
- context window；
- max output tokens；
- tokenizer adapter 和版本；
- provider 固定序列化开销；
- tool schema 计数方式。

无法获得经过 golden vectors 验证的 tokenizer 时，该模型不能作为本版受支持模型发布。不得用 `chars/4` 冒充精确 Token。

### 8.3 预算公式

```text
input_budget = context_window - max_output_tokens - safety_margin
```

预算必须覆盖最终网关请求中的所有内容：system prompt、工具 schema、历史、计划、目标、MemoryEntry、Episode 和当前用户消息。

优先级：

1. 固定保护：system policy、工具协议、当前用户消息、当前 pending Ask/tool pair；
2. 高优先：活动计划/目标的当前版本；
3. 连续性：最近完整 Turn；
4. 权威资料：确认后的 MemoryEntry；
5. 历史经历：相关 Episode。

任何固定保护块单独超限时返回明确的 `CONTEXT_BLOCK_TOO_LARGE`，不得静默切半。

### 8.4 热窗口选择

算法：

1. 取得当前 Thread 的完整终态 Turn；
2. 从最新 Turn 向前逐个计算真实 Token；
3. 以完整 Turn 为单位装箱；
4. 达到历史预算后停止；
5. 将选中的 Turn 恢复为时间升序；
6. 记录被丢弃 Turn 及 `drop_reason=history_budget`。

`archived_through_seq` 改名或重新定义为 `episode_covered_through_seq`，只表示已经存在有效 Episode 的连续覆盖范围，不参与热窗口裁剪。

为防止原文与摘要重复注入，先确定热窗口最早 message sequence，再只选择 `end_message_seq` 早于该边界的 Episode。

### 8.5 记忆检索与装箱

先执行 SQL 作用域过滤，再打分：

- MemoryEntry：当前 owner 下的 user scope，以及与 Thread 权威 project 一致的 project scope；
- Episode：本版仅当前 Thread、`ACTIVE`、来源 Thread 未删除；
- sensitivity 不得扩大 Episode 作用域；
- `RAW_REFERENCE` 永远不作为候选。

MemoryEntry 与 Episode 分开渲染、分开预算：

```text
[confirmed memory]
- ...

[conversation episode: lossy, non-authoritative]
- ...
```

Episode 排序采用确定性组合：同 Thread硬前置、query 词项相关度、open-loop 命中、时间衰减、稳定 ID。首版使用 SQLite FTS/词项匹配即可，不引入向量检索。

每个候选必须整体加入或整体丢弃，并记录：source ID、真实 Token、score、include reason 或 drop reason。

### 8.6 最终 Token 断言

所有块装配后，使用网关实际 serializer 生成最终 messages/tools payload，再用当前 tokenizer 重新计数。

若超预算：

1. 按确定性丢弃顺序移除最低优先级的可选原子块；
2. 重新序列化并计数；
3. 无可移除块仍超限则返回 `CONTEXT_OVERFLOW`；
4. 禁止向同一网关盲目重试完全相同的超限请求。

## 9. Context Pin 与重试

### 9.1 Binding

Pin binding 至少包含：

- principal/owner；
- Thread、Project、parent type/id；
- invocation ID、purpose、query hash；
- model/profile、context window、max output；
- tokenizer、serializer、renderer、prompt versions；
- 各类实际预算；
- source IDs、source versions、source hash；
- 最终 payload hash。

唯一键改为 `(owner_id, model_invocation_id)`。唯一冲突后必须读取并逐字段验证，禁止 `INSERT OR IGNORE` 后静默沿用旧 Pin。

### 9.2 存储

拆分为：

- `model_context_pins`：binding、状态、hash、TTL；
- `model_context_pin_items`：规范化 source type/id/version；
- `model_context_pin_payloads`：最终 canonical payload、hash、`expires_at`。

`budget_json` 只保存预算和统计，不保存正文。payload 不进入日志和审计事件。

### 9.3 生命周期

```text
ACTIVE -> EXPIRED
ACTIVE -> INVALIDATED
ACTIVE -> CONSUMED/terminal -> grace period -> GC
```

- TTL 覆盖最大模型调用重试窗口；
- TTL 内且 binding 完全一致时字节级重放；
- TTL 到期返回 `CONTEXT_PIN_EXPIRED`；
- Episode、Revision、Thread 或其他来源删除时，在同一事务使关联 Pin 失效并清除 payload；
- 来源内容或作用域变更时，旧 invocation 不得继续重放；
- 后台清理过期 payload 和 items。

## 10. 数据库迁移

通过项目现有迁移机制新增一次显式迁移，不在运行时堆叠临时 `ALTER TABLE`。

### 10.1 Scope 约束

- `threads` 增加 `UNIQUE(id, owner_id)`；
- `conversation_archive_state(thread_id, owner_id)` 使用复合外键；
- `memory_episodes(thread_id, owner_id)` 使用复合外键；
- Episode 的 `project_id` 必须等于来源 Thread 的 `project_id`，本版服务层固定 `retrieval_policy='thread'`；
- 迁移前扫描 owner/thread/project 错配；无法唯一归属的数据进入 quarantine，不猜测 owner。

### 10.2 `memory_archive_jobs`

建议字段：

```text
id, owner_id, thread_id,
start_message_seq, end_message_seq, source_hash,
prompt_version, tokenizer_version,
status, available_at, attempts, max_attempts,
lease_owner, lease_epoch, lease_until,
last_error_code, last_error_json,
created_at, updated_at, finished_at
```

幂等唯一键：

```text
(owner_id, thread_id, start_message_seq, end_message_seq,
 source_hash, prompt_version)
```

### 10.3 Episode 字段

在现有字段基础上补充：

- `schema_version`；
- `summary_json` 或结构化 synopsis；
- `outcomes_json`；
- 每个结构项的 source refs；
- `source_message_ids_json`；
- `source_token_count`、`summary_token_count`；
- `tokenizer_version`；
- 明确的 `model_id`、`prompt_version`；
- `deleted_at`。

重建状态约束，生产检索状态只允许 `ACTIVE`；旧 `RAW_REFERENCE` 迁移为不可检索的归档记录。

### 10.4 Fencing 与原子提交

Claim 时原子增加 `lease_epoch`。heartbeat、成功、失败都必须匹配：

```text
job id + lease_owner + lease_epoch + expected status + source_hash
```

最终事务按顺序执行：

1. CAS 验证当前 Worker；
2. 重新验证 source hash；
3. 插入幂等 Episode；
4. 单调推进连续 coverage cursor；
5. 标记 job `COMPLETED`；
6. 提交事务。

任何一步失败则整个事务回滚。过期 Worker 永远不能晚提交。

## 11. 代码改造清单

| 文件/模块 | 改造内容 |
|---|---|
| `backend/app/db.py` / migrations | 新增 archive job、复合 scope 约束、Episode v2、Pin 三表 |
| `backend/app/transcript.py` | 新增 scope resolver、规范化 Turn/Ask/generation Builder |
| `backend/app/memory_archive.py` | 改为 durable worker、真实 summarizer、严格校验、fenced commit |
| `backend/app/memory_v2.py` | Episode 结构存储、thread-only 检索、分型渲染、Pin binding/失效 |
| `backend/app/context.py` | 实现 TokenBudgetPlanner 和统一 ContextBuilder |
| `backend/app/conversation.py` | 移除独立 `_history()` 逻辑；终态事务写 outbox；使用统一 Builder |
| `backend/app/runtime.py` | 移除 `local-user` 和字符裁剪；通过 Turn/Thread 解析 scope |
| `backend/app/live_model.py` | 分离 model resolve 与 invoke，暴露实际 profile/tokenizer |
| `backend/app/model_gateway.py` | 暴露最终 serializer/token count；overflow 不盲重试 |
| `backend/app/startup.py` | 注入 summarizer、启动/恢复 archive worker 和 Pin GC |

禁止 Conversation 和 Agent Runtime 绕过统一 Builder 直接查询消息或记忆表。

## 12. 分阶段实施

### Phase 0：测试锁定当前缺陷

- 为最旧 50 条、interrupted generation、Ask 跨 Thread、默认 `local-user` 写失败测试；
- 为无 summarizer 的 1800 字符伪摘要写失败测试；
- 为 stale lease、Episode/cursor 分裂和 Pin 跨 scope 复用写失败测试；
- 记录现有数据库污染扫描结果。

完成标准：所有已知缺陷都有稳定复现测试。

### Phase 1：Scope 与 Transcript

- 实现 `ResolvedMemoryScope`；
- 实现状态矩阵和 `CanonicalTurnTranscriptBuilder`；
- Conversation、摘要 Worker、Agent Runtime 全部切换到 Builder；
- 消除摘要/上下文生产路径中的 `local-user`。

完成标准：同一 Turn 在在线历史和摘要输入中生成同一组规范事件；Ask exactly-once。

### Phase 2：Token 上下文

- 建立受支持模型 profile 和 tokenizer adapter；
- 将路由拆为 resolve/build/invoke；
- 实现完整 Turn 逆向装箱和原子上下文块；
- 实现最终 serializer Token 断言；
- 热窗口与 Episode coverage 解耦。

完成标准：所有受支持模型、多语言和边界输入的最终请求都不超过预算。

### Phase 3：可靠摘要

- 新增 outbox/job 和 Archive Worker；
- 实现严格 Episode JSON 协议；
- 实现 source refs、hash 和 sensitivity 验证；
- 实现 retry/dead-letter；
- 实现 lease epoch、CAS、Episode/cursor/job 原子提交。

完成标准：错误模型输出、进程崩溃和双 Worker 竞争均不能生成错误 Episode 或推进错误游标。

### Phase 4：分型检索与 Pin

- Episode 固定 thread-only；
- MemoryEntry/Episode 分型检索、渲染与预算；
- 增加 relevance + recency 排序和原文重叠排除；
- 重构 Pin binding、items、payload、TTL、失效和 GC。

完成标准：跨 scope 无命中；删除来源后无可重放正文；合法 retry 字节级一致。

### Phase 5：迁移、故障测试与发布

- 执行旧数据检查和迁移；
- 运行并发、崩溃点、lease 抢占和删除故障注入；
- 连续运行专项与全量测试；
- 观察 dead-letter、overflow、scope conflict 和 Pin invalidation 指标；
- 完成灰度和回滚演练。

## 13. 测试方案

### 13.1 Transcript

- 超过 50 条消息时选择最新完整 Turn；
- LIMIT/预算边界不会拆 user-assistant Turn；
- 多 generation 只保留最终有效回答；
- interrupted、streaming、cancelled assistant 永不出现；
- answered/pending/multiple Ask 顺序正确且 exactly-once；
- `turn_asks` 不跨 Thread；
- FAILED/CANCELLED 状态符合状态矩阵。

### 13.2 Token 预算

- 中文、英文、混合文本、Emoji、代码、长 URL；
- system/tool schema/current user 固定开销；
- 多个模型 profile 的 tokenizer golden vectors；
- 属性测试保证最终序列化请求 `tokens <= input_budget`；
- 单个保护块过大时返回明确错误；
- 不发生半个 Turn、tool pair、Episode 或 MemoryEntry 截断。

### 13.3 摘要

- 合法结构化摘要正常生成；
- 非法 JSON、未知字段、空摘要、超长输出失败关闭；
- 虚构、批外、跨 owner 的 source ID 被拒绝；
- timeout、rate limit、模型异常正常重试；
- secret 与敏感度只能升级；
- source hash 改变时旧任务不能提交；
- 没有 summarizer 时服务启动失败或摘要功能显式不可用，绝不 fallback；
- `RAW_REFERENCE`/错误占位文本永不进入上下文。

### 13.4 并发与事务

- 两个 Worker 只能有一个 claim 成功；
- lease 过期后的旧 Worker 晚提交失败；
- 每个事务崩溃点均不产生半提交；
- cursor 单调、Episode 范围无错误重叠；
- 重试幂等，不重复生成相同 Episode；
- attempts 按 source range/hash 独立计算。

### 13.5 隔离与 Pin

- Alice/Bob × 同/不同 Thread × 同/不同 Project 矩阵；
- 伪造 owner/project/resource ID 返回 403/404；
- 相同 invocation ID 跨 owner、query、scope、purpose、model、budget 均 conflict；
- TTL 内合法重试 payload hash 完全一致；
- TTL 到期明确失败；
- Episode/Thread/Revision 删除后 Pin invalidated 且 payload 清空；
- 敏感 Episode 永不跨 Thread；
- payload 不进入日志、audit metadata 或错误响应。

## 14. 发布门禁

以下条件全部满足才能发布：

1. 摘要/上下文已知 P0、P1 缺陷清单为零；
2. 专项测试连续运行 3 次无 flaky；
3. 后端全量测试通过；
4. 并发和故障注入测试通过；
5. 所有支持模型通过 tokenizer golden vectors 和预算属性测试；
6. 数据迁移前后 scope 污染扫描为零；
7. 生产摘要/上下文路径中 `local-user` 硬编码为零；
8. `chars/4`、消息数量和字符串截断不再作为正确性边界；
9. 无 `RAW_REFERENCE` 或失败占位文本可被 Provider 选中；
10. 删除 Episode/Thread 后，数据库、Pin payload 和新模型上下文中均不存在摘要正文；
11. 旧 lease 晚提交、Pin 错绑或跨 owner/project 命中任一测试失败时立即阻断发布。

建议验证命令：

```powershell
cd backend
python -m pytest -q tests/test_transcript.py tests/test_context_budget.py
python -m pytest -q tests/test_memory_archive.py tests/test_context_pin.py
python -m pytest -q
```

## 15. 可观测性

新增不含正文的指标和结构化事件：

- archive job queued/running/retry/dead-letter 数；
- summary input/output Token、耗时、错误分类；
- lease lost、CAS conflict、source hash changed；
- context 各分区 Token、included/dropped 数与原因；
- final budget headroom 和 overflow 数；
- Pin hit/conflict/expired/invalidated/GC 数；
- scope validation failure 数。

日志只记录 ID、状态、计数、hash 和错误码，禁止记录 Transcript、摘要正文、最终 prompt 或 Pin payload。

## 16. 灰度与回滚

### 16.1 灰度

- 先启用新 Transcript 与 Token Planner 的 shadow 比对，不调用旧字符串截断结果；
- 摘要 Worker 先小比例启用，观察 schema failure、dead-letter 和 CAS conflict；
- 新 Episode Provider 只读取 v2 且通过校验的 `ACTIVE` 数据；
- 逐步启用 Context Pin，监控 conflict 与 invalidation。

### 16.2 回滚

- 回滚应用时停止新 Worker，不回退数据库中已经提交的迁移；
- 新表保留，旧应用不得读取 v2 Episode；
- 已经生成的 v2 Episode 可暂时从 Provider 禁用，但原始消息始终保留；
- 不能回滚到“原文拼接截断作为 ACTIVE 摘要”的旧行为；
- 不能回滚到按最旧 50 条消息装配上下文。

## 17. 完成定义

本开发方案完成必须同时满足：

- 唯一 Transcript Builder 已接管 Conversation、Agent Runtime 和摘要输入；
- 上下文按实际模型 Token 预算装配并完成最终断言；
- LLM 摘要严格结构化、逐项可追溯、失败关闭；
- Episode、coverage 和 job 具备 fenced 原子提交；
- owner/thread/project 由数据库权威解析并具有约束；
- Episode 被明确作为非权威历史摘要，首版仅 Thread 内使用；
- Pin 完整绑定、有 TTL、可失效、可清除；
- 所有回归、隔离、迁移、并发和故障测试通过；
- 发布门禁中没有例外或“上线后补测”。

## 18. 评审投票记录

| 评审角色 | 总体意见 | 发布条件 |
|---|---|---|
| 正确性评审 | 条件通过 | 唯一 Transcript、Token 硬预算、DB scope、fenced 原子提交全部实现 |
| 隐私隔离评审 | 条件通过 | Pin 不错绑、删除可失效、敏感内容不扩域、生产路径无 `local-user` |
| 摘要语义评审 | 条件通过 | Episode 非权威、严格来源引用、失败关闭、在线与摘要共用 Turn 语义 |

将所有条件纳入本文后，方案获得 3/3 评审通过。任何 P0 条件从实现或测试中移除，都需要重新评审，不得视为仍然通过。
