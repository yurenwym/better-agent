# PostgreSQL + pgvector 唯一权威数据库开发方案

> 状态：经数据库迁移、记忆检索、测试与故障恢复三个方向并行研究和交叉评审后形成的推荐方案。
>
> 核心决策：PostgreSQL 是运行时唯一权威数据库；SQLite 仅作为一次性迁移输入和切换证据。记忆召回采用“作用域硬过滤 -> pinned -> HNSW 向量语义召回 -> 质量判断 -> 同作用域精确向量恢复 -> 关键词降级/补充 -> 作用域背景补齐 -> Token 裁剪”。首期启用 HNSW，同时保留 exact scan 作为召回质量恢复路径和评估基线。

## 1. 背景

当前 Better Agent 使用 SQLite 保存全部业务状态和记忆，长期记忆通过 `memory_entries`、`memory_revisions`、`memory_episodes` 管理，通过 FTS5 和 Python 子串匹配召回。Conversation 与 Agent Runtime 统一调用：

```python
MemoryContextProvider.select(request) -> MemoryContextBundle
```

项目已经具备并发 Turn Worker、租约、幂等、Context Pin、记忆版本和 Markdown 投影，但 SQLite 仍带来三个限制：

1. 语义相同、措辞不同的记忆可能无法被 FTS5 命中。
2. 多进程 worker 下，SQLite 的 `BEGIN IMMEDIATE` 与单文件写锁不能平移为 PostgreSQL 默认事务语义。
3. 当前数据库实现和业务 SQL 深度绑定 SQLite 方言，不能靠替换连接字符串完成迁移。

仓库静态审计显示，约 38 个后端文件直接执行 SQL，存在约 1025 次 `.execute()` 调用。SQLite 专用行为包括 `?` 占位符、`PRAGMA`、FTS5、`BEGIN IMMEDIATE`、`INSERT OR IGNORE/REPLACE`、SQLite Trigger、`datetime('now')` 和 `json_extract`。因此，最小修改不是引入 ORM 重写所有领域，而是保留现有业务接口，仅集中替换数据库 seam 和少数依赖数据库并发语义的热路径。

## 2. 问题与约束

### 2.1 必须解决

- PostgreSQL 必须成为 threads、turns、jobs、events、memory、trace 等全部状态的唯一权威源。
- 不允许运行时 SQLite/PostgreSQL 双写，也不允许 PostgreSQL 故障时静默回读 SQLite。
- 保持现有 Service、API、`MemoryContextRequest` 和 `MemoryContextBundle` 形状稳定。
- 不同 thread 可以并行，同一 thread 任意时刻最多一个 RUNNING turn，并且约束在多进程下成立。
- Embedding 外部服务故障、限流或向量尚未生成时，记忆保存和正常回答必须继续可用。
- owner/project/status/current revision 必须在 SQL 中硬过滤，不能先全局向量搜索再由应用过滤。
- Context Pin 重试必须复用完全相同的记忆包，不能重复调用 Embedding 或改变检索策略。

### 2.2 本期不做

- 不引入 SQLAlchemy ORM、Redis、Celery 或独立向量数据库。
- 不把同步数据库层整体改成 async。
- 不在迁库同时把全部 TEXT 时间改成 `timestamptz`、全部 JSON TEXT 改成 `jsonb`、整数布尔改成 boolean；只有 lease 等并发关键时间改为数据库原生时间。
- 不在首期向量化 Episode；它继续按当前 thread 召回并使用独立 Token 预算。
- 不实现跨 thread/project 的 Episode 语义召回。
- 不把 Markdown 投影提升为权威数据源。

## 3. 推荐技术栈

| 能力 | 选择 | 原因 |
|---|---|---|
| 权威数据库 | PostgreSQL 固定大版本 | 支持事务、行锁、约束、多进程并发和可恢复备份 |
| Python 驱动 | `psycopg 3` | 与当前同步业务代码匹配 |
| 连接池 | `psycopg_pool.ConnectionPool` | 保留同步 `connection()/transaction()` 使用方式 |
| Schema 迁移 | Alembic + 手写 SQL migration | 不引入 ORM，显式管理 extension、trigger、partial index |
| 向量扩展 | pgvector | 向量与权威 revision 保持同库事务关系 |
| 关键词召回 | PostgreSQL FTS + `pg_trgm` | 替代 FTS5，覆盖精确标识和中文子串 |
| Embedding | 硅基流动 `Qwen/Qwen3-Embedding-4B`，OpenAI-compatible `EmbeddingProvider` adapter | 官方支持 1024 维；通过 adapter 保留供应商替换能力 |
| 后台处理 | 现有 lease worker 模式抽成 `DurableQueue` | 不引入新队列基础设施 |

Compose 增加带 pgvector 的 PostgreSQL 服务、healthcheck 和持久卷。生产应用只接受 `DATABASE_URL`；Embedding 密钥只通过 `EMBEDDING_API_KEY` 环境变量引用，不进入数据库、日志或轨迹。

本机环境已验证 Docker Desktop Engine `29.2.1`、Docker Compose `v5.0.2` 可用，且已有 `pgvector/pgvector:pg16` 镜像。临时容器实测完成 `CREATE EXTENSION vector`、`vector(1024)` 建表和余弦距离查询，扩展版本为 `0.8.2`。正式 Compose 应固定镜像 digest 或经验证的明确 tag，避免浮动升级。

### 3.1 为什么首期不把数据库层整体改成 async

这不是 PostgreSQL 或 psycopg 的能力限制，而是为了隔离迁移变量：

- 当前约 383 处 `db.connection()/transaction()` 调用和大量同步 Service 会被 `await` 沿调用链传播到 API、worker、测试和事务回调；这不是数据库 adapter 内部的一处替换。
- PostgreSQL 迁移已经需要重建 `BEGIN IMMEDIATE` 隐含的并发语义、任务 fencing、SQL 方言和触发器。同步/异步同时切换会让回归难以判断来自驱动模型还是数据库语义。
- 当前目标并发为 4-16 个 worker，数据库事务短，主要长等待来自模型与 Embedding HTTP。全量 async 不会让 SQL 本身更快；它主要改善大量并发连接等待时的事件循环利用率。
- Embedding HTTP 必须非阻塞，但不要求全部业务持久化一起 async 化。查询检索可以放入运行时拥有的有界执行器，后台向量任务可使用独立异步 HTTP adapter，且二者都受统一 deadline 限制。

因此首期选择同步 `psycopg_pool.ConnectionPool`，并将以下指标作为是否迁移 `AsyncConnectionPool` 的证据：事件循环延迟、连接池等待、数据库并发数、请求吞吐和 `context_ms`。若 PostgreSQL 切换后的目标负载下出现事件循环 p95 延迟或 pool wait 超过发布门槛，再把“全量 async 数据库接口”作为独立阶段实施和回归；不在唯一权威源切换的同一发布中完成。

## 4. 目标架构

```text
Conversation / Agent Runtime
        |
        | 保持 select(request) 接口
        v
MemoryContextProvider
        |
        +-- Context Pin 命中 --------> 原样返回
        |
        +-- scope/pinned 查询
        +-- query embedding
        +-- pgvector HNSW cosine
        +-- scoped exact-vector 质量恢复
        +-- PostgreSQL FTS / pg_trgm 降级或补充
        +-- scoped importance backfill
        +-- Episode thread-only 查询
        +-- 排序、预算裁剪、Pin
        |
        v
PostgreSQL + pgvector（唯一权威源）

MemoryRevision 提交
        |
        +-- revision/current pointer/evidence
        +-- embedding job outbox（同一事务）
                         |
                         v
                Embedding Worker
                         |
                         v
                  memory_embeddings
```

### 4.1 数据库 seam

保留两个现有入口，增加一个深模块：

```python
Database.connection()
Database.transaction()

DurableQueue.enqueue(...)
DurableQueue.claim_next(...) -> LeaseToken
DurableQueue.heartbeat(token)
DurableQueue.request_cancel(...)
DurableQueue.finish(token, commit_result)
DurableQueue.fail(token, ...)
DurableQueue.recover_expired(...)
```

`Database` 只隐藏连接池、row/cursor 兼容和事务提交，不模拟 `BEGIN IMMEDIATE`。`DurableQueue` 拥有完整状态转换，所有结果写入必须与 `(lease_owner, lease_epoch)` fencing 校验处于同一事务，业务模块不得绕过它直接修改 job 状态。

### 4.2 SQL 迁移策略

- 为控制本次修改范围，现有业务查询暂由数据库 adapter 进行 token-aware qmark bind 转换；转换器必须跳过字符串、标识符、注释、PostgreSQL dollar quote 及 JSONB `?`/`?|`/`?&` 运算符，并有独立契约测试。新增 PostgreSQL 专属查询直接使用 `%s`。
- codemod 必须识别 SQL 字面量、注释、quoted identifier 和 JSONPath，并输出动态 SQL 的人工审计清单。
- `INSERT OR IGNORE/REPLACE`、FTS5、Trigger、时间函数、JSON 运算、`GROUP_CONCAT`、`MAX(seq)+1` 等逐项原生改写。
- CI 锁定 adapter 的词法边界并阻止 PostgreSQL 生产路径引入 `PRAGMA`、FTS5、`BEGIN IMMEDIATE` 等 SQLite 执行语义；动态标识符使用 `psycopg.sql.Identifier`。后续可单独执行全仓 `%s` codemod 并移除 adapter 转换，不作为本次最小迁移的发布前置。
- Alembic 由部署流程执行；应用启动只检查 revision 与 `vector`/`pg_trgm` extension，不允许多实例启动时自动跑 DDL。

普通业务时间与 JSON 首期保持原可观察类型，降低业务回归。lease、available、heartbeat 使用 `timestamptz` 和 `clock_timestamp()`；保留 TEXT 的 UTC 时间增加格式校验。

## 5. Worker 并发正确性

### 5.1 Turn 数据库不变量

`turn_jobs` 新增：

```text
thread_id    text NOT NULL
lease_epoch  bigint NOT NULL DEFAULT 0
```

使用 `(thread_id, turn_id)` 复合外键保证 job 与 turn 所属 thread 一致，并建立：

```sql
CREATE UNIQUE INDEX uq_turn_jobs_one_running_per_thread
ON turn_jobs(thread_id)
WHERE status = 'RUNNING';
```

partial unique index 是“每个 thread 最多一个 RUNNING turn”的最终数据库证明。建立索引前必须扫描并报告冲突，禁止自动选择一条覆盖其他记录。

### 5.2 Claim 与 fencing

- claim 使用短事务、固定锁顺序和 `FOR UPDATE SKIP LOCKED`。
- 先接管 lease 已过期的 RUNNING job，再领取同 thread 的后续 QUEUED job，防止半成品被越过。
- 同一事务锁候选 thread 和 job，执行条件 UPDATE 并 `RETURNING LeaseToken`。
- 唯一冲突应回滚当前 claim 或回到 savepoint 后继续找其他候选，不能让整个 worker 退出。
- heartbeat、delta、checkpoint、finish、fail 全部校验 owner、epoch、RUNNING 状态以及 `lease_until > clock_timestamp()`，且要求恰好更新一行。
- 所有 durable workers 在 PostgreSQL 切换前统一此协议，包括 turn、agent、research、archive、goal review、evaluation 和实际具备 lease 的 projection/tool claim。

## 6. Embedding 数据与任务

建议新增：

```text
embedding_profiles
  id, provider, model, model_revision, dimensions,
  document_prefix, query_prefix, status, calibration_version

memory_embeddings
  revision_id, profile_id, owner_id, scope_type, scope_id,
  content_hash, embedding vector(<固定维度>), created_at
  PRIMARY KEY (revision_id, profile_id)

memory_embedding_jobs
  revision_id, profile_id, content_hash, status,
  attempts, available_at, lease_owner, lease_epoch, lease_until,
  error_code, created_at, updated_at
  UNIQUE (revision_id, profile_id, content_hash)
```

不直接给 `memory_entries` 挂向量，因为 Entry 可变而 Revision 不可变。查询必须重新 JOIN `memory_entries.current_revision_id` 并校验 profile/content hash，旧 revision 向量即使保留也不能进入上下文。

保存或修改记忆时，在同一 PostgreSQL 事务完成：

```text
MemoryRevision
+ current_revision_id
+ evidence
+ memory_embedding_jobs
```

事务内不调用外部 Embedding。Worker 在事务外批量调用 provider，校验响应数量、顺序、有限数值、固定维度、profile 和 content hash，再使用 lease fencing 幂等提交。失败状态支持重试、指数退避、死信和人工重放。

首个 active profile 固定为：

```text
provider       = siliconflow
model          = Qwen/Qwen3-Embedding-4B
dimensions     = 1024
encoding_format = float
```

硅基流动官方接口声明该模型支持 `64/128/256/512/768/1024/2048` 维，因此 `1024` 是合法配置。Schema 使用 `vector(1024)`，Provider 响应必须严格校验长度为 1024。1024 维在存储、网络成本和检索质量之间更适合当前长期记忆场景，但仍需用项目标注集与 2048 维做离线对照后确认质量门禁；生产中不能在同一 profile 内动态改变维度。

模型升级或维度变化时创建新 profile，回填全部 ACTIVE current revisions，校准并原子切换；不同 profile、模型版本或维度的向量禁止比较。API Key 不进入 profile 表，只保存其环境变量引用。

## 7. 语义优先级联召回

### 7.1 召回顺序

```text
1. Context Pin 查询
2. owner/project/status/current-revision 硬过滤
3. pinned constraint/preference
4. query embedding + pgvector HNSW Top-K
5. HNSW 质量与 scoped embedding coverage 判断
6. 低质/不足时执行同作用域 exact-vector 恢复
7. lexical fallback / exact-token supplement
8. scoped importance backfill（保留旧行为）
9. 当前 thread Episode 排序
10. 去重、稳定排序、Token 预算裁剪和 Pin
```

这是语义优先的级联召回，不是每次固定执行两路：

- `semantic`：向量结果质量和当前作用域覆盖率均达标。
- `lexical_fallback`：Embedding 调用失败/超时/拥塞、profile 不匹配、覆盖不足或语义质量不达标。
- `adaptive_hybrid`：查询包含路径、URL、错误码、UUID、带命名空间的模型/版本等精确标识，语义结果通过后仍补充关键词结果。
- `pinned_only`：空查询或检索不可用时保留 pinned，再按既有作用域背景规则补齐。

不能以“向量返回了结果”视为成功，因为近邻搜索几乎总能返回内容。质量策略至少包含 top score、候选分布、当前作用域向量覆盖率和查询类别。

同时保留旧实现的重要行为：即使没有文本或语义命中，也从合规 ACTIVE memory 按 pinned、importance、稳定 ID 在剩余预算中补齐背景。这个 backfill 只能来自已完成 owner/project 硬过滤的集合。

### 7.2 阈值校准

阈值不硬编码拍脑袋。建立脱敏、版本化数据集，覆盖：

- 中英文偏好、约束、事实、决定和经验的同义问法；
- 无相关记忆、仅一条相关、多条相关三类查询；
- owner/project 相似诱饵；
- 路径、错误码、UUID、模型名和版本号；
- missing/stale embedding。

在 train split 扫描 top score、score floor、K、超时和覆盖率门槛，只在 held-out split 报告最终 Recall@K、Precision@K、MRR/no-result precision、精确标识成功率和时延。profile、文本构造或维度变化必须重新校准。policy/calibration/profile 版本进入 Context Pin binding hash。

### 7.3 查询时延上限

当前 `select()` 保持同步接口。两个 async 调用点把完整 select 放入一个进程级、运行时拥有的有界 blocking-I/O executor，不允许每次创建线程池。

一个统一的 context retrieval deadline 必须覆盖：

```text
executor wait + query embedding HTTP + vector SQL + lexical fallback
```

executor 无容量或剩余预算不足时立即走无网络 lexical 路径。查询 Embedding 单次请求不使用 provider retry；重试会把模型 TTFT 长尾放大。Provider 超时建议从实测的 300-800ms 区间校准，而不是写死。

新增轨迹指标：

```text
memory_retrieval_ms
executor_wait_ms
query_embedding_ms
vector_search_ms
keyword_search_ms
retrieval_mode
fallback_reason
embedding_profile_id
embedding_coverage_ratio
semantic_candidate_count
lexical_candidate_count
selected_revision_count
selected_episode_count
token_count / dropped_count
```

这些指标归属于现有 `context_ms`，不改变 `context_ms` 定义；轨迹默认只显示秒级总耗时、模式、候选到入选数量和降级原因，详细 ID 放在展开区。

### 7.4 pgvector HNSW 索引策略

首期创建并启用 HNSW cosine 索引：

```sql
CREATE INDEX memory_embeddings_hnsw_cosine
ON memory_embeddings
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

初始参数使用 `m=16`、`ef_construction=64`，查询启用 pgvector 0.8+ `hnsw.iterative_scan = strict_order`，`hnsw.ef_search` 初始候选值为 80。它们都必须通过基准校准，不能成为不可变业务常数。

HNSW 的 owner/project 条件可能在近似索引扫描后过滤，导致候选不足。因此：

- SQL 始终包含 owner/project/status/current revision 硬过滤，绝不在全局候选返回后仅靠 Python 过滤；
- 按 owner/project 选择性分桶，对 exact Top-K 验证 scoped Recall@K；
- HNSW 结果低于校准的分数、覆盖率或候选门槛时，先执行 scoped exact-vector fallback，因为 lexical 无法救回纯同义表达；然后才决定是否关键词降级；
- exact scan 始终保留，既作为线上恢复路径，也作为 HNSW Recall@K 的基准 oracle；
- 不为每个用户/项目创建一个 partial HNSW index；规模需要时采用有限 tenant bucket 或 PostgreSQL 分区策略；
- 在 embedding 覆盖率和 HNSW scoped recall 未达门禁前，生产回答保持 lexical 默认，HNSW 只做 shadow；达标后切为默认语义路径。

## 8. 分阶段开发计划

### 阶段 A：PostgreSQL 基础与 SQL 迁移

改动重点：

- `backend/pyproject.toml`、`backend/requirements.txt`：增加 psycopg、pool、Alembic、pgvector 客户端依赖。
- `compose.yaml`、`.env.example`、Dockerfile：增加 PostgreSQL/pgvector、`DATABASE_URL` 和 healthcheck。
- `.env.example` 只声明 `EMBEDDING_API_KEY=`、`EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1`、`EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B`、`EMBEDDING_DIMENSIONS=1024`，不写入真实密钥。
- `backend/app/db.py`：改为 pool、兼容 row、事务接口和 schema revision 检查。
- 新增 `backend/alembic/` 与 `alembic.ini`：建立完整 PostgreSQL schema。
- 运行 SQL-aware codemod，显式修复方言清单。

门禁：应用只连接 PostgreSQL；全量业务契约测试通过；启动不执行 SQLite schema/recovery；生产 PostgreSQL 路径不执行 SQLite 专属语义，qmark 兼容仅存在于经过词法测试的集中 adapter。

### 阶段 B：DurableQueue 与多进程并发

改动重点：

- 新增 `backend/app/durable_queue.py`。
- 改造所有 lease worker 使用 `LeaseToken`、DB clock 和 epoch fencing。
- `turn_jobs` 增加 thread_id/epoch/复合 FK/partial unique。
- 修复 `MAX(seq)+1`、幂等 UPSERT 和 append-only/frozen trigger 的 PostgreSQL 并发语义。

门禁：多连接、多进程 claim、接管、迟到写和无死锁测试通过；数据库中同 thread 双 RUNNING 永远为零。

### 阶段 C：Embedding 与 PostgreSQL 关键词召回

改动重点：

- 新增 `backend/app/embeddings.py`、`backend/app/embedding_worker.py`。
- 增加 profiles、vectors、jobs 表以及 PostgreSQL FTS/`pg_trgm` 索引。
- MemoryStore revision 事务写入 embedding job。
- 先保持 lexical 默认，完成异步回填和覆盖率观测。

门禁：记忆写入不等待 Embedding；故障可降级；ACTIVE current revision READY 覆盖率达到约定门槛。

### 阶段 D：语义级联与影子评估

改动重点：

- 保持 `MemoryContextProvider.select` 外部接口，内部引入候选检索实现。
- 增加进程级有界 retrieval executor、统一 deadline、Context Pin policy 元数据和轨迹指标。
- 建立标注集，影子运行 semantic，不影响实际回答。
- HNSW 从首期 schema 创建并在 shadow 模式运行；校准通过后启用 HNSW semantic-first，保留 scoped exact fallback。

门禁：召回质量优于 lexical baseline；精确标识不退化；跨 scope 泄漏为零；context p95 达标。

### 阶段 E：停写迁移与唯一权威源切换

改动重点：

- 新增一次性 SQLite -> PostgreSQL 迁移命令和 manifest 校验器。
- 完成停写、worker drain、最终导入、smoke、逐步放量和 PG 备份恢复 runbook。
- 移除正式构建中的 SQLite runtime 选择路径；SQLite 文件只读归档。

门禁：全部迁移不变量和验收用例通过。PostgreSQL 接受第一笔新写后，回退只能是旧应用继续连接 PostgreSQL、前向修复或 PostgreSQL PITR，禁止回切 SQLite。

## 9. 验收用例

| ID | 场景 | 通过标准 |
|---|---|---|
| A1 | 不同 thread 同时发问 | 两个 turn 同时 RUNNING、均正常流式完成，并发不超过上限 |
| A2 | 同 thread 连续提交 | 任意时刻最多一个 RUNNING；第二个在首个终态后领取，`queue_wait_ms` 正确 |
| A3 | 两进程/四 worker 竞争 100 jobs | 每个 job 仅一个有效终态，无同 thread 重叠、无永久饥饿 |
| A4 | worker 流式中退出并被接管 | epoch 增加；新 worker 完成；旧 worker heartbeat/delta/finalize 全被拒绝 |
| A5 | “喜欢简洁回答”对“别写太长” | semantic 命中正确 current revision，轨迹显示语义模式 |
| A6 | 错误码、路径、模型版本查询 | adaptive lexical 补充命中，精确召回不低于旧基线 |
| A7 | Embedding 暂停或超时/429/5xx | 记忆仍提交；回答不等待 document embedding；关键词降级且记录原因 |
| A8 | 部分向量回填 | coverage 不足时不会把高分少量结果误判为完整语义召回 |
| A9 | 记忆零匹配 | 仍按作用域和 importance 补齐旧有背景记忆，不发生行为静默退化 |
| A10 | 模型换版/维度变化 | 不跨向量空间比较；旧向量不能被新 profile 误用；可回填和原子切换 |
| A11 | owner/project 隔离 | semantic、exact、lexical、fallback、pinned 任一路径均零越界 |
| A12 | Context Pin 重试 | 第二次不调用 Embedding，返回相同文本、revision/episode IDs 和 hash |
| A13 | SQLite 停写迁移 | manifest 全绿，API 除允许差异外一致，正式应用无 SQLite 读写 |
| A14 | 应用回滚 | 前一兼容构建继续连接同一 PG，切换后新数据完整可见 |
| A15 | PostgreSQL 备份恢复 | 恢复实例通过 manifest 和核心旅程，达到声明的 RPO/RTO |
| A16 | Markdown 重建 | 删除投影后可完全从 PostgreSQL 重建，PG 数据不受文件状态影响 |
| A17 | 单条对话全流程 | 在只连接 PostgreSQL 的正式配置下，创建对话、提交用户消息、worker 领取 turn、准备历史/记忆上下文、执行 HNSW 或可解释降级、流式生成回答、提交消息与终态、写入耗时轨迹；页面可见完整回答和轨迹，重启应用后仍可读取，且全程无错误、无 SQLite 访问 |

## 10. 测试用例

### 10.1 单元与静态测试

- PostgreSQL row adapter：名称/位置读取、NULL、整数布尔、JSON text、UTC 时间、rowcount、提交和回滚。
- SQL codemod/lint：字符串、注释、JSONPath 中的 `?` 不误改；真实 bind 全改；SQLite 方言和不安全动态标识符被阻止。
- DurableQueue 状态机：合法转换、过期接管、epoch fence、取消/完成竞态、重放幂等。
- Embedding adapter：响应数量/顺序、维度、NaN/Infinity、timeout、429/5xx、错误脱敏。
- Cascade：semantic、lexical fallback、adaptive hybrid、pinned/background backfill 的确定性选择。
- 精确标识 detector：覆盖路径、URL、UUID、错误码、版本和普通含 `-`/`.` 自然语言的误报集。
- Ranking：pinned、相关性、importance、稳定 ID 和 Token 裁剪；reasons 与选中 ID 对齐。
- Pin：profile/policy/calibration 进入 binding hash，Pin 命中优先于 Embedding。

### 10.2 PostgreSQL 集成测试

- 从空库执行 Alembic，验证 extension、FK、CHECK、partial unique、trigger、GIN 和 vector 表。
- 使用独立连接同时 claim 同一 job、同 thread 两 job、不同 thread 两 job，成功数分别为 1、1、2。
- 两个独立进程循环竞争，验证无重复有效终态、无死锁；旧 epoch 的所有写更新零行。
- 所有 durable queue 覆盖 claim/heartbeat/retry/cancel/finish/fail/recovery。
- memory revision/current pointer/evidence/embedding job 全有或全无；中途异常完整回滚。
- 新 revision 在向量 READY 前可以 lexical 命中；编辑中途返回的旧向量不能覆盖新 revision。
- archived/purged/expired/stale/wrong-profile/wrong-dimension 均不能召回。
- semantic、lexical、hybrid、provider-error、missing-vector 各路径均执行 owner/project 隔离测试。
- Context Pin 在 profile 切换和 provider 故障时仍复用原 payload；删除源后按现有规则失效。
- PostgreSQL 不可用时明确失败，绝不访问 SQLite。

### 10.3 迁移测试

- Golden SQLite fixture 包含 Unicode、长 Markdown、NULL/空串、复杂 JSON、删除态、失败态、过期 lease、Context Pin 和投影故障。
- 导入生成逐表 count、主键集合 hash、规范化内容 hash、FK orphan、事件序号、identity 水位报告。
- 导入中断后目标库保持“未完成”状态，应用拒绝启动；重跑不会产生重复记录。
- 对旧 SQLite 和新 PostgreSQL 执行固定 Service/API 请求并比较结构化结果。
- PostgreSQL 重建 Markdown 后比较规范化字节 hash。
- 切换后静态和运行时证明无 SQLite 连接路径。

### 10.4 召回评估与性能测试

- 使用 train/held-out 标注集比较 lexical-only、semantic cascade、adaptive hybrid。
- 报告 Recall@K、Precision@K、MRR、no-result precision、exact-token success、fallback rate。
- 测量 executor wait、query embedding、HNSW、scoped exact fallback、lexical 和完整 context p50/p95/p99。
- 模拟 Embedding timeout、429、500、错误维度、慢 worker、worker crash 和 PostgreSQL 重启。
- Episode 保持 thread-only，但增加超长 thread/大量 ACTIVE Episode 的线性增长测试；若后续增加 SQL LIMIT，必须证明不改变现有 query-overlap + recency 排序。
- HNSW 必须按 owner/project 选择性分桶对 exact Top-10 比较 Recall@10，并验证 HNSW -> scoped exact -> lexical 的降级顺序。

### 10.5 端到端测试

- 发布阻断测试必须至少完整执行一条真实对话链路：PostgreSQL 初始化 -> thread/turn/message -> durable claim -> context + memory retrieval -> model stream -> message/event/metrics commit -> API/UI 查询 -> 应用重启后复查。每一阶段均断言成功，禁止用直接插表或跳过 worker/stream/持久化来替代。
- 创建 thread -> 并发 turn -> SSE 断线重连 -> Ask 继续 -> memory proposal/accept -> 下轮语义召回 -> 删除 thread/pin invalidation。
- 在 QUEUED、ROUTING、STREAMING、archive、embedding PENDING 状态分别重启应用。
- 多租户并行写入相似记忆，验证数据库、检索、轨迹、证据、投影全链路隔离。
- 从 PostgreSQL base backup + WAL/PITR 恢复到新实例并运行同一 smoke suite。

## 11. 发布门禁

以下全部满足才允许切换：

- 数据迁移行数、主键、规范化内容 hash、FK、事件序号和 memory pointer 100% 一致。
- 跨 owner/project 泄漏为 0；同 thread 双 RUNNING 为 0；旧 lease 迟到写为 0。
- PostgreSQL 全量测试通过，SQLite-only 测试不能作为发布凭据。
- ACTIVE current revision 的 Embedding 覆盖率达到校准门槛；建议 semantic 默认启用前不低于 99%。
- lexical DB p95 建议不高于 50ms，HNSW DB p95 建议不高于 50ms，exact-vector 恢复路径 p95 建议不高于 100ms；最终以同硬件基线批准。
- HNSW scoped Recall@10 相对 exact Top-10 不低于 0.95，目标 0.98；过滤后候选不足率低于 1%。
- Embedding 故障只在统一 context deadline 内增加有限等待，并成功降级。
- exact identifier Top-K 不低于 lexical baseline；semantic Recall@10 达到标注集批准值。
- 连接池等待、锁等待、deadlock、embedding backlog/fallback、context 时延和召回质量均已监控。
- 已完成一次计时 PostgreSQL 恢复演练并声明可达到的 RPO/RTO。
- 有仍兼容当前 PostgreSQL schema 的前一应用构建，支持应用回滚。

任何 owner 隔离失败、同 thread 双 RUNNING、旧 epoch 可写、迁移 hash 不一致、Context Pin 漂移或无可恢复 PostgreSQL 备份，均立即阻止发布。

## 12. 最终决策

采用 PostgreSQL + pgvector 并启用 HNSW，但仍将风险拆开交付：先迁权威数据库和并发语义，再接 Embedding outbox、HNSW schema 和 PostgreSQL lexical，随后影子评估并启用 HNSW semantic-first。exact scan 保留为过滤后召回恢复路径和质量基线，不能被 HNSW 完全替代。

这条路线的修改幅度最小体现在：不改上层业务接口、不引入 ORM/Redis/Celery、不同时 async 化、不长期双库；同时又不通过脆弱兼容层掩盖 PostgreSQL 与 SQLite 的真实并发差异。

## 13. 依据与研究记录

内部研究：

- `docs/research/postgres-minimal-migration.md`
- `docs/research/pgvector-retrieval.md`
- `docs/research/postgres-pgvector-test-strategy.md`

一手资料：

- PostgreSQL 行锁与 `SKIP LOCKED`：<https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE>
- PostgreSQL 事务隔离：<https://www.postgresql.org/docs/current/transaction-iso.html>
- PostgreSQL Partial Index：<https://www.postgresql.org/docs/current/indexes-partial.html>
- PostgreSQL Full Text Search Index：<https://www.postgresql.org/docs/current/textsearch-indexes.html>
- PostgreSQL PITR：<https://www.postgresql.org/docs/current/continuous-archiving.html>
- psycopg Connection Pool：<https://www.psycopg.org/psycopg3/docs/advanced/pool.html>
- Alembic：<https://alembic.sqlalchemy.org/en/latest/tutorial.html>
- pgvector：<https://github.com/pgvector/pgvector>
