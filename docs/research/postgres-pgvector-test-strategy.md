# PostgreSQL + pgvector 迁移测试、上线与回退策略

> 最终决策覆盖（2026-09-05）：首期启用 HNSW；同作用域 exact vector 保留为召回恢复路径和质量基线。实施以 `docs/superpowers/plans/2026-09-05-postgresql-pgvector-migration-plan.md` 为准。

> 定位：反方审计与测试架构输入。目标不是扩大改造，而是证明“PostgreSQL 是唯一权威数据库”在数据、并发、记忆语义和故障恢复上没有退化。
>
> 结论先行：可以迁移，但不能把它定义为“换驱动 + 替换 `?` 占位符”。最小可靠改造是保留现有 Service API，在其下引入一个很薄的 PostgreSQL 数据访问层，同时把少量依赖数据库并发语义的热路径改为 PostgreSQL 原生事务语句。首次切换应是停写迁移；切换后不双写 SQLite。回退是“应用回滚但仍连接 PostgreSQL”，不是把新数据反灌回旧 SQLite。

## 1. 背景与范围

当前项目把 SQLite 作为所有业务域的权威数据库，`Database.connection()` 返回 `sqlite3.Connection`，`Database.transaction()` 以 `BEGIN IMMEDIATE` 串行化写入。Schema、迁移和恢复逻辑集中在 `backend/app/db.py`，但 SQL 分散在业务 Service 中。仓库扫描显示约 1,000 个 `connection.execute(...)` 调用，且存在以下 SQLite 专用行为：

- `?` 参数占位符、`sqlite3.Row` 同时支持名称与位置取值；
- `PRAGMA`、`sqlite_master`、`executescript()`、FTS5 `MATCH`；
- `INSERT OR IGNORE`、`INSERT OR REPLACE`、`datetime('now')`、`json_extract(...)`；
- `AUTOINCREMENT` 和 SQLite `RAISE(ABORT, ...)` 触发器；
- ISO-8601 时间以 `TEXT` 返回，业务代码中多处直接调用 `datetime.fromisoformat(row[...])`；
- `BEGIN IMMEDIATE` 让许多“先读、再判断、再写”的事务在单文件数据库上意外获得了全局写锁保护。

因此，兼容 adapter 只能降低机械改动，不能模拟 SQLite 的事务语义。尤其不能通过简单 SQL 字符串改写来可靠转换触发器、FTS、JSON 表达式和 UPSERT 冲突目标。

本策略覆盖：

- 全部业务表从 SQLite 迁移到 PostgreSQL；PostgreSQL 是唯一权威数据源；
- pgvector 保存记忆 revision 的 embedding，并支持“语义优先、关键词降级/补充”的级联召回；
- Turn Worker 池、Agent/Research/Archive/Evaluation Worker 的任务领取、租约续期与 fencing；
- 每个 thread 同时最多一个运行中 turn；不同 thread 可并行；
- memory entry/revision/proposal/evidence/episode/context pin/Markdown projection 的既有语义；
- 一次停写切换、验证、应用回退和数据库灾备恢复。

不建议首期同时引入 ORM、异步数据库驱动、Redis/Celery、数据库分片或独立向量服务。它们会扩大变量，无法帮助证明迁移正确性。

## 2. 现有关键契约与容易漏掉的问题

### 2.1 只靠兼容 adapter 不可行

adapter 可以统一连接池、事务上下文、行访问和参数绑定，但下列行为必须显式迁移：

| SQLite 行为 | PostgreSQL 对应做法 | 失败后果 |
|---|---|---|
| `BEGIN IMMEDIATE` | 针对聚合根/任务行使用 `FOR UPDATE`、条件 `UPDATE ... RETURNING`、唯一约束；少数需要 `SERIALIZABLE` 并重试 | 竞态、重复版本、重复事件 |
| `INSERT OR IGNORE` | 明确 `ON CONFLICT (<约束列>) DO NOTHING` | 吞掉非预期约束错误，幂等语义改变 |
| `INSERT OR REPLACE` | 明确 `ON CONFLICT ... DO UPDATE`，保留主键和外键语义 | PostgreSQL 没有 SQLite 的 delete-then-insert 式 replace 语义 |
| FTS5 `MATCH` | `tsvector`/GIN，必要时 `pg_trgm`；中文分词能力单独验收 | 关键词降级失效 |
| JSON 文本 + `json_extract` | 首期可继续 `TEXT` 并用 PostgreSQL JSON 运算符转换；或定点改 `jsonb` | 路由、目标程序查询结果不同 |
| `TEXT` 时间 | adapter 对外规范为 UTC ISO 字符串，或一次性把领域层改为 aware `datetime` | 租约判断直接报错或时区错判 |
| SQLite trigger | PostgreSQL trigger function 或可证明等价的权限/规则 | 追加事件被修改、冻结版本被覆盖 |
| `MAX(seq)+1` | 锁住父聚合行后分配 seq，或用数据库 sequence 并保留域内连续性契约 | 并发下唯一冲突/乱序 |

最低限度的 adapter 契约应包括：`connection()`、`transaction()`、dict-like row、`execute/fetchone/fetchall/rowcount`、统一 JSON/boolean/timestamp 编解码和 SQL 方言模块。禁止在 adapter 中用正则通用翻译任意 SQL。

迁移作者提出的“一次性、可审查 codemod 将 `?` 改为 `%s`”优于运行时 SQL 翻译，可以采用，但必须满足四项门禁：

- codemod 必须理解 Python 字符串/SQL token，不能替换 SQL 字面量、JSONPath 或注释中的问号；
- 生成逐文件 diff，所有被改 SQL 经过静态解析或 PostgreSQL prepare/执行测试；
- CI 用 `rg`/AST 扫描阻断应用 SQL 中遗留的 `?` 占位符以及新增 SQLite 方言；
- 不把 `INSERT OR IGNORE/REPLACE`、FTS、trigger、JSON、日期函数混入同一机械转换，它们逐条人工确定语义。

首期继续让 JSON/时间/布尔按 `TEXT/TEXT/INTEGER` 暴露给业务层，是减少领域代码改动的合理选择，但这只是**接口兼容策略**，不是允许弱化数据库语义：

- 时间文本必须统一为固定宽度 UTC RFC 3339（例如始终六位微秒和 `+00:00`），数据库提供生成/比较函数；在证明全库格式一致前，SQL 中禁止直接依赖任意 ISO 字符串的字典序。
- 租约的到期判断必须使用 PostgreSQL `clock_timestamp()`（可格式化成规范 TEXT）或在 SQL 内显式转 `timestamptz`，不能使用应用传入的 `now` 作最终 fencing 条件。
- JSON TEXT 必须加 `CHECK` 保证可转换为 `jsonb`，所有 JSON 查询使用显式 cast；布尔 INTEGER 加 `CHECK(value IN (0,1))`。
- adapter 契约测试必须证明 PostgreSQL row 对外仍返回旧代码预期的字符串/整数，防止 psycopg 原生解码悄然改变类型。

### 2.2 PostgreSQL 多进程 claim 与同 thread 互斥

PostgreSQL 官方明确说明 `SKIP LOCKED` 会给出不一致视图，不适合普通查询，但适合多个消费者访问队列表。这意味着它可以解决“多个 Worker 不阻塞地领取不同 job”，但不自动解决“两个 job 属于同一 thread 时只能领取一个”。

现有 Turn claim 在一个 SQLite 写事务中先通过 `NOT EXISTS(active RUNNING job in same thread)` 找候选，再条件更新 job。在 PostgreSQL 默认 `READ COMMITTED` 下，两个事务可能同时看到同一 thread 没有 RUNNING job，并分别锁住该 thread 的两个 job，因而同时成功。

推荐首期使用 **CTE 领取 + thread/job 行锁降低竞争 + partial unique index 证明最终互斥**，避免增加新的锁表或依赖哈希 advisory lock：

1. 候选查询锁 `turn_jobs` 行：`FOR UPDATE OF turn_jobs SKIP LOCKED`；
2. 尝试对候选所属 `threads` 行执行 `SELECT ... FOR UPDATE SKIP LOCKED`；
3. 获得 thread 锁后，在同一事务重新检查该 thread 不存在其他有效 RUNNING job；拿不到则跳过/换候选；
4. 条件更新 job 并 `RETURNING turn_id, lease_epoch`；
5. 写 `turn.started`、`queue_wait_ms` 后提交；thread 锁随事务释放，但持久的 RUNNING 状态使下一次 claim 的重检拒绝同 thread job。

为避免候选行被先锁后因 thread 忙碌而导致本轮领取失败，可用单条 CTE 选择并更新，或在有限候选集上逐个尝试。行锁用于吞吐和减少冲突，partial unique 才是最终正确性证明；不能假设一条 CTE 内多个 `FOR UPDATE` 的物理加锁次序天然满足设计。查询需尽量固定全系统访问顺序，并设置 `lock_timeout`，再用 `EXPLAIN` 和故障/压力测试证明没有死锁。

还需要数据库级防线。可在 `turn_jobs` 冗余 `thread_id` 后建立 partial unique index：

```sql
CREATE UNIQUE INDEX uq_turn_jobs_one_running_per_thread
ON turn_jobs(thread_id)
WHERE status = 'RUNNING';
```

这是最强且最容易验收的方案；`thread_id` 由创建 job 时写入并以外键约束，应用 claim 捕获唯一冲突后继续领取其他 job。若坚持不冗余字段，则必须依赖 thread 锁协议，PostgreSQL 无法用跨表条件创建普通 partial unique index。

采用 CTE + partial unique 的方案还必须修订/明确：

- `turn_jobs.thread_id TEXT NOT NULL REFERENCES threads(id)` 必须在回填后再设 NOT NULL；写入时从已锁定的 turn 得到，禁止客户端提供。
- 通过 trigger 或禁止 UPDATE 权限确保 `turn_jobs.thread_id` 不可变；同时校验它始终等于 `turns.thread_id`。普通外键无法表达这条跨表相等约束，可考虑让 `turn_jobs(turn_id,thread_id)` 复合外键引用 `turns(id,thread_id)`。
- 创建 partial unique index 前，迁移程序必须断言每个 thread 最多一个 RUNNING；若生产副本已有重复状态，应停止迁移并人工恢复，不能任意保留一条。
- CTE 需要 `EXPLAIN` 与真实并发测试确认实际锁定对象和锁顺序；不能仅凭 SQL 评审推断安全。
- partial unique 冲突会中止当前 PostgreSQL 事务；代码必须回滚整个 claim 事务后领取下一候选，不能在 aborted transaction 上继续执行，除非显式使用 savepoint。
- expired RUNNING job 应优先被接管，否则它会持续占用 partial unique 键并使同 thread 后续任务饥饿；队列顺序与失败重试必须覆盖这一点。

租约 fencing 也不能只比较 `lease_owner` 和时间。不同进程或进程重启可能重用 owner；应让 `lease_epoch` 每次领取/接管递增，所有 heartbeat、checkpoint、message delta、终态提交均在 SQL `WHERE lease_owner=? AND lease_epoch=? AND lease_until>clock_timestamp()` 中验证，并检查 `rowcount=1`。数据库服务器时间是权威时钟，避免应用主机时钟漂移。当前 `memory_archive_jobs`、`agent_tasks` 已有 epoch 设计，但 `turn_jobs` 没有 `lease_epoch`；后者是本次 PostgreSQL schema 的必增字段，不能只抽取 `TurnJobQueue` 而遗漏。

同样的原子 claim 模式要用于 `agent_tasks`、`research_jobs`、`memory_archive_jobs`、evaluation jobs 和 tool execution claims；不能只改 Turn Worker 后假设其他队列天然安全。

### 2.3 SQLite 切换后不能把“回退”理解成自动回写

一旦 PostgreSQL 接受新写入，旧 SQLite 立即落后。让旧应用重新以 SQLite 为权威会丢失新 thread、turn、memory revision、approval、tool claim 和审计事件，且无法可靠合并两个自增序列/事件流。

所以切换契约必须是：

- 切换窗口前冻结写入并迁移；
- 切换后环境中不再存在可写 SQLite 连接；
- 应用版本回退必须使用兼容 PostgreSQL schema 的前一应用构建；
- 数据库回退只能恢复 PostgreSQL 的预切换备份/PITR，并接受切换后写入全部丢失；若不能接受，则只能前向修复；
- 不建设 SQLite/PostgreSQL 双写，也不提供“自动反向同步”。

### 2.4 embedding 未就绪是正常状态，不应阻塞记忆写入

记忆 revision 的提交必须独立于外部 embedding 服务。建议同一 PostgreSQL 事务写 revision、current pointer、evidence 和 `memory_embedding_jobs` outbox；后台 worker 生成向量后，以 `(revision_id, model_id, model_version, content_hash)` 做幂等提交。

状态至少为：`PENDING | READY | FAILED_RETRYABLE | DEAD_LETTER | STALE`。用户可见产品行为：

- `READY`：执行向量召回；质量不足或查询含精确标识时补充关键词；
- `PENDING/STALE`：不等待 embedding，直接走 PostgreSQL FTS/`pg_trgm`，轨迹记录 `lexical_fallback` 和原因；
- embedding API 超时/429/5xx：查询侧快速降级，不把整个回答判失败；写侧保留重试任务；
- 查询 embedding 维度/模型版本与索引不一致：禁止比较，直接降级；
- 无候选并不等于服务错误，返回空记忆上下文；pinned 约束/偏好仍按现有规则直接注入。

## 3. 最小可靠解决方案及门禁

### 阶段 A：固定兼容边界，不改变生产数据源

- 为 PostgreSQL 增加 `psycopg 3 + psycopg_pool`；保持同步 Service 接口，不同时 async 化。
- 使用 Alembic 建 PostgreSQL schema migration，不复用 `db.py` 中的 SQLite 恢复分支；应用启动只执行版本检查，不让多个应用实例同时运行自定义 DDL 恢复逻辑。
- 保留现有 SQLite 测试作为行为基线；新建 PostgreSQL 集成测试 profile，真实启动带 pgvector 的数据库。
- 将数据库方言差异集中到 schema/migration、claim SQL、FTS/向量检索和少量 UPSERT helper；普通 CRUD 通过薄 adapter 保留调用形状。
- CI 禁止 SQLite-only 测试绿灯作为迁移完成依据。

`TurnJobQueue` 是合适的最小抽取范围，但它必须拥有完整状态转换，而不只是 `claim_next()`：enqueue、claim/takeover、heartbeat、cancel、完成/失败、迟到提交拒绝和恢复查询。否则 Service 的其他分散 SQL 仍可能绕过 epoch fencing。所有写路径通过仓库静态检查和集成测试证明只经由该队列组件。

门禁：PostgreSQL 契约测试覆盖所有表、约束、触发器、Service CRUD 和事务回滚；不能用 mock 数据库替代。

### 阶段 B：迁移工具与只读影子验证

- 停写状态下从 SQLite 读取一致快照，按外键拓扑批量 `COPY` 到新 PostgreSQL。
- 每张表生成 manifest：源/目标行数、主键集合 hash、规范化内容 hash、最小/最大时间、孤儿数。
- 重建 PostgreSQL FTS、HNSW 索引；向量可异步回填，不阻塞业务切换，但必须有明确覆盖率。
- 影子读对同一组固定查询同时执行旧/新 Service，比较结构化结果，不把 PostgreSQL 结果返回用户。

门禁：第 4 节全部数据不变量成立；差异清单为空或逐项签字接受。

### 阶段 C：一次停写切换

1. 拒绝新写请求，停止所有 worker/scheduler，等待在途任务达到安全终态或租约过期；记录停写水位。
2. 备份 SQLite 文件及 WAL/SHM（在正确 checkpoint/关闭流程后），并对 PostgreSQL 做切换前备份。
3. 执行最终增量或重新全量导入；重复 manifest 校验。
4. 启动只连接 PostgreSQL 的单实例应用，执行 smoke test；再逐步增加 worker 并发。
5. 观察一个稳定窗口后，保留 SQLite 只读归档，不让应用具备其写路径。

门禁：任何不变量、核心 smoke、并发互斥或错误率门槛失败，保持停写并回到旧版本/旧 SQLite；不要在两边都已接收写入后切回。

### 阶段 D：embedding 回填与索引启用

- 首先上线 FTS 降级路径和 embedding job；在 HNSW 建立前使用精确向量扫描做小规模校准。
- 用标注查询集比较 exact pgvector Top-K 与 HNSW Top-K 的 recall，再启用 HNSW。
- owner/project/scope/status 必须在 SQL 中硬过滤。pgvector 官方指出近似索引过滤通常发生在索引扫描后，可能返回不足；pgvector 0.8+ 可启用 iterative scans，仍需以每租户召回测试证明结果数量和隔离性。
- 只有 embedding 覆盖率、质量和时延过门禁才把默认检索模式切到 semantic；否则保持 lexical，不影响主流程。

## 4. 数据迁移不变量

以下断言应由迁移校验程序生成机器可读报告，失败即禁止切换。

### 全库通用

- 每表源/目标行数相等；所有主键集合 hash 相等；迁移 manifest 记录 schema 版本和源文件 hash。
- 所有外键无孤儿；所有 enum/check/partial unique 约束可重放。
- `NULL`、空字符串、0、false 不互相转换；JSON 规范化后语义相等；UTC 时刻相等。
- append-only/frozen 表内容 hash 相等，且 PostgreSQL 权限/触发器确实拒绝 UPDATE/DELETE。
- owner 隔离字段非空且所有跨表 owner/thread/project 关系一致。

### Conversation、事件与 worker

- 每个 `(thread_id, client_turn_id)` 唯一；每个 turn 正好一个 turn job；job.thread_id 与 turn.thread_id 一致。
- `turn_jobs.lease_epoch` 非负且每次 claim/takeover 严格递增；任何历史 epoch 均不能修改 job、message 或事件。
- 每个 thread 的 `thread_events.seq` 唯一、严格递增；`threads.next_event_seq = max(seq)+1`（空流为约定初值）。
- `thread_messages.message_seq` 在 thread 内唯一；content length/hash、generation、status 不变。
- 非终态 job 的 lease/status/attempts/epoch 原样迁移；若选择切换时统一过期，必须形成显式迁移记录并由新 worker 接管。
- 任意时刻数据库中每个 thread 至多一个 `RUNNING` turn job。

### Memory

- 每个 ACTIVE `memory_entries.current_revision_id` 指向同 entry 的 revision；`revision_no` 从 1 单调递增且唯一。
- proposal 的决策状态、version、request digest、accepted revision 指针保持一致；owner 范围内幂等键冲突语义不变。
- evidence link 的 aggregate 存在；其 source 存在且 owner/thread/project/actor 合法；VERIFIED 数量与可解析 evidence 数量相等。
- episode 覆盖区间、source hash、source message IDs、archive cursor 无间隙/重叠退化。
- context pin 的 binding/query/scope/rendered hash、revision/episode items 和 payload TTL 保持一致；被删除 source 的 pin 仍为 invalidated 且 payload 已清除。
- Markdown projection 不是权威数据：从 PostgreSQL 重建后，逐文件规范化 byte hash 与迁移前一致；故障 intent 仍可重试。

### Embedding

- 一个 embedding 只对应不可变 revision + 指定模型版本 + content hash；修改正文必须创建/指向新 revision，不覆盖旧向量含义。
- vector 维度等于模型声明，元素有限，零向量（cosine 不可索引）被拒绝或标记失败。
- 删除/停用 entry 后，即使向量行尚在，也不能被查询召回；scope SQL 过滤不可由应用后过滤替代。

## 5. 验收用例（用户/系统级）

| ID | 场景 | 操作 | 通过标准 |
|---|---|---|---|
| A1 | 不同 thread 并行 | 同时向两个 thread 发问，模型端设置屏障 | 两个 turn 均进入 RUNNING；第二个不等待第一个结束；均正常流式完成 |
| A2 | 同 thread 串行 | 同一 thread 快速提交两个 turn | 任意时刻仅一个 RUNNING；第二个有正 `queue_wait_ms`，前一个终态后才开始 |
| A3 | 多进程竞争 | 启动至少 2 个应用进程/4 个 worker 同时 claim 100 个 job | 每 job 仅一次有效终态；无同 thread 重叠；不同 thread 达到配置并发 |
| A4 | worker 崩溃接管 | 首 worker 流式一半后 kill -9，租约过期后由另一进程接管 | 旧 generation 标记 interrupted，新 generation 完成；旧 worker 的迟到提交被拒绝 |
| A5 | 长期记忆语义命中 | 保存“偏好简洁回答”，询问“别写太长” | READY 时 semantic 命中；召回项属于当前 owner/scope；轨迹显示 semantic |
| A6 | 精确词补充 | 保存错误码/文件名/模型名后用精确标识查询 | lexical 补充命中，不因低语义相似度漏召回 |
| A7 | embedding 未就绪 | 暂停 embedding worker 后新增记忆并立即询问 | 记忆写入成功；回答不等待 embedding；走 lexical fallback 并记录原因 |
| A8 | embedding 故障 | 注入超时、429、5xx、错误维度 | 对话仍可回答；job 重试/死信符合策略；不写入无效 vector |
| A9 | 记忆证据隔离 | Alice/Bob 各有同 idempotency key 和相似内容 | 两者独立；跨 owner、thread、project、非用户证据均拒绝 |
| A10 | context pin 稳定 | 第一次调用后修改 memory，再以同 invocation 重试 | 返回原固定 payload；binding 改变、TTL 过期、source 删除分别按既有错误语义拒绝 |
| A11 | Markdown 重建 | 删除投影文件并触发 rebuild | PostgreSQL 内容不变；`USER.md`、`MEMORY.md`、projects 文件恢复且 hash 相等 |
| A12 | 切换完整性 | 对生产副本执行停写迁移与 smoke | 所有 manifest 不变量通过；旧 SQLite 无写入；重启后队列可恢复 |
| A13 | 应用回退 | 新版本制造非破坏性故障，部署前一兼容构建 | 前一构建继续连接同一 PostgreSQL，既有新数据可见且可处理 |
| A14 | 数据库恢复 | 从 base backup + WAL/PITR 恢复到指定时间 | manifest、关键业务旅程和 worker 恢复测试通过，RPO/RTO 达标 |

## 6. 分层测试用例

### 6.1 单元与契约测试

- row adapter：按名称/位置读取、`rowcount`、`None`、boolean、JSON、UTC timestamp、rollback；业务层看到的类型必须稳定。
- SQL 方言：每个 `ON CONFLICT` 明确冲突目标；同 idempotency key 同 payload 返回原结果，不同 payload 返回冲突。
- codemod：含 SQL 字面量 `?`、JSONPath、注释、三引号、多参数批量执行的 fixture 验证仅占位符被改；CI 阻断遗留 SQLite 参数风格。
- 时间：数据库时钟生成 lease，测试应用时钟前后漂移 5 分钟不影响接管正确性。
- embedding：content hash、模型版本、维度校验、指数退避上限、死信和手工重放幂等。
- ranking：pinned 永远保留；阈值不足触发 fallback；精确 token 触发 lexical 补充；token budget 与确定性 tie-break 不变。

### 6.2 PostgreSQL 集成测试

所有现有 `backend/tests` 先按领域迁移为 PostgreSQL profile，尤其是：

- `test_conversation_worker.py`、`test_restart_recovery.py`：claim、续租、接管、stream generation、取消、失败终态；
- `test_agent_tasks.py`、`test_agent_worker.py`、`test_research_service.py`、`test_memory_archive.py`、`test_tool_execution_claims.py`：所有持久队列的 fencing；
- `test_memory_v2.py`、`test_memory_api_isolation.py`、`test_memory_conversation_flow.py`、`test_checkpoint.py`：revision、CAS、evidence、scope、pin；
- `test_events.py`、`test_invariants.py`、`test_db.py`：append-only、事件序列、约束和 migration checksum；
- `test_plan_documents.py`、`test_plan_fault_injection.py`：数据库提交与文件投影之间的可恢复 intent。

新增真实并发测试必须使用独立 PostgreSQL connection/进程，不能只用 asyncio task 共用单连接：

1. 两个事务同时领取同一个 job，恰好一个成功；
2. 两个事务分别领取同 thread 的两个 job，恰好一个成功；
3. 两个事务领取不同 thread，二者都成功且不存在全局串行；
4. 旧 epoch 在新 epoch 接管后尝试 heartbeat/delta/finalize，全部更新 0 行；
5. 1000 次竞争循环无重复终态、无死锁；发生 serialization/deadlock 时只重试整个幂等事务。
6. CTE 候选遇到同 thread active/expired job 时不会饿死其他 thread；expired job 被优先接管。
7. partial unique 冲突后连接已正确 rollback，可继续用于下一事务；连接池不会归还 aborted connection。
8. 并发创建同 thread 的两个 job，复合外键/约束不允许 `turn_jobs.thread_id` 与 `turns.thread_id` 漂移。

### 6.3 迁移与影子比较测试

- 构造包含空值、Unicode、长 Markdown、大 JSON、删除态、失败态、过期 lease、断裂投影 intent 的 golden SQLite fixture。
- 导入两次：首次成功；第二次要么清库重建，要么严格幂等，不产生重复行。
- 导入中途断连后重跑，不能留下被误认为完整的数据库；使用 migration run 状态和 manifest 标记完整性。
- 对固定 API 请求比较 SQLite 与 PostgreSQL JSON 响应，忽略明确允许变化的时间/内部 ID。
- 对固定记忆查询比较 scope/pinned/版本集合；FTS 排序变化单独用标注集验收，不要求逐分数相等。

### 6.4 端到端测试

- 完整流程：创建 thread -> 并发 turn -> SSE 断线重连 -> Ask 继续 -> memory proposal/accept -> 下轮召回 -> 删除 thread/pin invalidation。
- 重启流程：在 QUEUED、ROUTING、STREAMING、archive summarizing、embedding pending 各状态重启应用。
- 多租户流程：并行生成相似内容，确认 API、FTS、pgvector、evidence、projection 全链路没有跨 owner 泄漏。
- 备份恢复环境执行同一 smoke suite，不能只证明 `pg_restore` 命令退出 0。

## 7. 故障注入矩阵

| 注入点 | 预期行为 |
|---|---|
| claim 锁行后、UPDATE 前断连 | 事务回滚，其他 worker 可领取 |
| job RUNNING 提交后、模型调用前进程死亡 | lease 到期后接管，attempt/epoch +1 |
| message delta 与 event append 之间异常 | 二者在同一事务，不出现内容/offset 不一致 |
| 旧 worker 在接管后返回模型结果 | 所有持久化因 epoch fencing 被拒绝 |
| memory revision 写入后、embedding API 前死亡 | outbox job 仍在，记忆可用 lexical 检索 |
| embedding API 成功、写 vector 前断连 | 重试同键提交，不产生重复/错版本向量 |
| vector 查询超时或 extension 不可用 | 快速 lexical fallback，主回答不失败 |
| HNSW 构建时应用运行 | 业务写读可继续；启用前 exact/lexical；监控索引进度和资源 |
| projection 文件写失败 | PostgreSQL 保留 FAILED intent，可重试；MD 不成为新事实源 |
| 数据库主连接中断 | 事务结果未知时不盲目重放非幂等外部副作用；tool claim 进入 reconciliation |
| 迁移 COPY 中途失败 | 目标标记未完成，不允许应用启动；清理/重跑后 manifest 一致 |
| 切换后发现严重应用错误 | 回退到仍支持当前 PostgreSQL schema 的应用；数据库继续为权威源 |

## 8. 性能与质量门槛

阈值先以同一硬件、同一模型 mock 的 SQLite 基线压测确定，以下是建议的发布门槛而非脱离环境的绝对承诺：

- claim SQL：空闲队列 p95 <= 20 ms，竞争下 p99 <= 100 ms；锁等待 p99 <= 250 ms；死锁率为 0（若出现必须有受控整事务重试并告警）。
- 并发：`N` 个不同 thread 在 worker concurrency=`N` 时确实并行，吞吐不低于 SQLite 当前基线的 90%；同 thread 重叠为 0。
- 普通 CRUD/API：数据库段 p95 不劣于基线 25%，5xx 不增加；连接池等待 p95 <= 50 ms。
- 记忆 lexical fallback：p95 <= 50 ms（不含网络）；exact vector 校准集 p95 <= 100 ms；HNSW p95 <= 50 ms。
- HNSW 质量：对 exact Top-10 的 Recall@10 >= 0.95；owner/project 过滤后的结果不足率低于标注阈值（建议 <1%），不足时 iterative scan/lexical fallback 生效。
- embedding 查询：超时预算建议 300-800 ms，超过立即 fallback；不可把 embedding 网络延迟累加到模型 TTFT 的长尾而无上限。
- 覆盖率：切 semantic 默认前，ACTIVE current revisions 的 READY 覆盖率 >= 99%；其余全部有可解释状态且 lexical 可检索。
- 数据：迁移不变量 100% 通过；跨 owner 泄漏为 0；重复有效终态为 0；context pin hash 漂移为 0。
- 恢复：RPO/RTO 由产品明确；至少执行一次计时恢复演练。个人本地部署建议 RPO <= 5 分钟、RTO <= 30 分钟，达不到则不宣称已具备该恢复能力。

## 9. 切换、停止与回滚判据

### 允许切换

必须同时满足：全量 PostgreSQL 测试通过；manifest 全绿；A1-A12 通过；真实多进程竞争无同 thread 重叠；备份可恢复；监控已覆盖连接池、锁等待、deadlock、claim latency、embedding backlog/fallback、HNSW recall；值班者掌握停写和恢复步骤。

### 自动停止发布/保持停写

任一情况触发：数据 hash/行数不一致；外键孤儿；owner 隔离失败；同 thread 双 RUNNING；旧 lease 可提交；事件 seq 冲突；context pin 漂移；投影被误当权威；迁移状态不完整；无可验证 PostgreSQL 备份。

### 切换后的应用回退

若 schema 保持 expand/contract 兼容，部署前一版本但仍连接 PostgreSQL。回退前确认旧构建不依赖已删除列/约束；首期只做 additive schema，旧列至少跨一个稳定版本再删除。

### 数据库恢复/前向修复

- 有数据损坏：停止写入，根据 WAL/PITR 恢复到新实例，跑 manifest 和 smoke 后切连接；
- 仅新应用有 bug：优先应用回退或前向修复，不恢复数据库；
- 要恢复预切换 SQLite：必须明确接受所有切换后写入丢失，并再次停写，绝不能把 SQLite 与 PostgreSQL 两条历史自动合并。

PostgreSQL 官方说明 `pg_dump` 可在并发使用时生成一致导出且不阻塞读写，但它只备份单个数据库，且官方提示复杂生产场景通常不应仅靠它做常规备份。生产恢复策略应结合 base backup + WAL 持续归档/PITR，并实际演练。SQLite 文件只保留为迁移证据和离线归档，不再参与运行时回退。

## 10. 最小方案可能漏掉的事项清单

- 把 `SKIP LOCKED` 当作同 thread 互斥，而没有 thread 锁/partial unique index；
- 抽出 `TurnJobQueue` 但 completion/message/event 仍可绕过它写库，导致 epoch fencing 只覆盖 claim；
- `turn_jobs` 冗余 `thread_id` 没有复合外键/不可变约束，partial unique 保护了错误的分组键；
- 只给 Turn job 加 epoch，其他 durable worker 仍只比较 owner；
- 应用时间与数据库时间混用导致租约早退/晚退；
- 把不同格式的时间保存在 TEXT 后继续做字典序比较；
- psycopg 自动返回 `datetime`/dict/list/bool 后，现有 `fromisoformat/json.loads/bool(row)` 代码失效；
- codemod 误改 SQL 字面量/JSONPath 中的 `?`，或漏改动态 SQL；
- 保留 `MAX(seq)+1` 却不锁父行；
- 通用替换 `INSERT OR IGNORE`，未指定正确冲突约束；
- PostgreSQL trigger 未复刻 append-only/frozen/scope 约束；
- 中文 FTS 使用默认 `english/simple` 配置后召回质量下降；
- HNSW 在 owner/project 过滤后候选不足，却被误判为“没有相关记忆”；
- 新 revision 已提交但 embedding 未就绪时阻塞回答，或错误使用旧 revision 向量；
- embedding 模型切换时混合不同向量空间；
- 把 Markdown 投影纳入双写权威，放大跨介质一致性问题；
- 切换后仍保留可写 SQLite 配置，形成无告警分叉；
- 宣称可回退但没有兼容 PostgreSQL 的旧应用构建，也没有恢复演练；
- 单元测试全部通过，但没有独立连接/多进程真实锁竞争测试。

## 11. 官方依据

- PostgreSQL `SELECT` locking clause：`SKIP LOCKED` 会跳过无法立即锁定的行，产生不一致视图；适合 queue-like table 的多消费者，而非通用查询。<https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE>
- PostgreSQL explicit locking：行锁只阻塞同一行的 writer/locker，并在事务结束释放；应用必须考虑死锁和一致的锁顺序。<https://www.postgresql.org/docs/current/explicit-locking.html>
- PostgreSQL transaction isolation：`READ COMMITTED` 是默认级别；并发更新与谓词判断需要条件更新、显式锁或更高隔离，并为 serialization failure 设计整事务重试。<https://www.postgresql.org/docs/current/transaction-iso.html>
- PostgreSQL partial indexes：可用谓词只约束满足条件的行，适合表达每 thread 仅一个 `RUNNING` job。<https://www.postgresql.org/docs/current/indexes-partial.html>
- PostgreSQL `INSERT ... ON CONFLICT`：提供原子 UPSERT，但必须按业务唯一约束明确 conflict target。<https://www.postgresql.org/docs/current/sql-insert.html#SQL-ON-CONFLICT>
- PostgreSQL `pg_dump`：并发使用期间生成一致导出且不阻塞读写，但只覆盖单数据库，并非复杂生产常规备份的完整替代。<https://www.postgresql.org/docs/current/app-pgdump.html>
- PostgreSQL continuous archiving/PITR：base backup 与 WAL 归档共同支持恢复到指定时间点。<https://www.postgresql.org/docs/current/continuous-archiving.html>
- pgvector 官方 README：HNSW/IVFFlat、exact search、过滤、iterative scans、多租户与索引调参说明；近似索引过滤可导致返回不足。<https://github.com/pgvector/pgvector>

## 12. 最终建议

批准“PostgreSQL + pgvector 为唯一权威数据库”的目标架构，但把交付拆成四个可回退发布门：数据库兼容层与原生并发热路径、停写迁移和影子验证、唯一权威源切换、embedding/HNSW 启用。代码改动最小不等于改动文件数最少；真正的最小方案是保留现有领域 Service 和 API，集中改造数据库边界，同时对 claim、序列分配、幂等 UPSERT、FTS/向量检索这些不能兼容模拟的路径使用 PostgreSQL 原生能力。
