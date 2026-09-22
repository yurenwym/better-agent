# T01 + T02 —— 隔离 PostgreSQL 入口与 V3 数据库契约验收

日期：2026-09-22
任务书：`docs/V3-自进化机制优化开发任务书-2026-09-22.md` §3 T01 / T02
基线：`baseline.md`（受测代码见 `manifest.json`）

---

## 0. 一句话结论

**T01 完成，T02 完成。** 4 项 PG 契约测试从「环境 ERROR 掩盖下的 3 failed」变成 **4 passed**，
根因是一处 SQLite 专属的命名占位符；顺带补上了缺失的「拒绝非测试库」守卫，
并用真实开发库做了端到端验证（拒绝发生在 0.51 秒内，数据零变化）。

---

## 1. 关于「直接用 Docker 里的 PostgreSQL」

**用的就是这个实例，没有起新容器、没有装新东西。**

```
better-postgres-1   Up 10 days (healthy)   127.0.0.1:5432->5432/tcp
PostgreSQL 16.13 (Debian 16.13-1.pgdg12+1)
扩展：plpgsql, pg_trgm, vector
```

隔离只发生在 **database 这一层** —— 同一个实例里换一个库名，这是 PostgreSQL 的标准用法，
不是额外搭一套环境：

| 库 | 用途 | 规模 |
| --- | --- | --- |
| `better_agent` | 开发库 | 35 MB，**有真实数据** |
| `better_agent_v3_test` | 本轮测试库 | 13 MB，测试产生 |

### 为什么不直接把 `TEST_DATABASE_URL` 指向 `better_agent`

因为 `isolated_postgres_test` 是 **autouse** 的，它在**每个测试函数前后各跑一次**
`TRUNCATE ... RESTART IDENTITY CASCADE`，作用于**除 `alembic_version` / `embedding_profiles`
外的所有应用表**。实测开发库当前装着：

```
threads=46   thread_messages=305   goals=7   runs=7   model_profiles=35
schema=20260920_0020
```

直接指过去，这 46 个 thread / 305 条消息 / 35 个 model profile 会在第一个测试开始前被清空；
而且 `alembic upgrade head` 还会先把开发库从 `20260920_0020` 升到 `20260921_0023`，
改变它的 schema。任务书 T01 也正是这么要求的：

> 在迁移与 truncate 前验证目标是本次创建或明确登记的测试库；拒绝开发库、生产库及不明确的目标。

所以这不是「多搞了一层隔离」，而是**这条流水线的 fixture 本身就会清表**，
换库名是唯一安全的跑法。

---

## 2. T01 —— 建立隔离 PostgreSQL 测试入口

### 2.1 compose 实测：不是插件坏，是根本没装

```
$ docker compose version
docker: unknown command: docker compose
$ docker compose -f .../compose.postgres.yaml -p probe config
exit=125
```

`docker compose` **子命令不存在**（不是插件损坏）。所以 100 个 integration ERROR 的根因是
fixture 拿不到 PG，而不是 PG 不可用。任务书允许「不可用时通过独立测试库和 `TEST_DATABASE_URL`
运行，无需先修复整台机器的 Docker 插件」—— 走的就是这条路。

### 2.2 发现并修掉的真问题：目标完全不校验

改之前的 `postgres_url`：

```python
configured = os.getenv("TEST_DATABASE_URL")
if configured:
    if not configured.startswith(("postgresql://", "postgresql+psycopg://")):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")
    yield RedactedDatabaseUrl(configured)   # ← 任何库名都放行
    return
```

只要以 `postgresql://` 开头就接受。挡住开发库的唯一屏障是**操作者记得输对库名**。

### 2.3 新增守卫 `db_target_guard.py`

fail-closed，三条规则：

| 规则 | 内容 |
| --- | --- |
| 库名 | 必须匹配 `_test` / `_tests` / `_pgtest` 后缀，或在 `TEST_DATABASE_ALLOW` 里显式登记 |
| 黑名单 | `better_agent` / `postgres` / `template0` / `template1` 一律拒绝（**allowlist 不能覆盖黑名单**） |
| 主机 | 必须是回环（`127.0.0.1` / `localhost` / `::1`） |

**调用点三处**，保证拒绝早于任何变更：

| # | 位置 | 作用 |
| --- | --- | --- |
| 1 | 读 `TEST_DATABASE_URL` 时 | 最早拦截 |
| 2 | `alembic upgrade` 调用紧前方 | 紧贴变更点 |
| 3 | `_truncate_application_tables` 内部 | 纵深防御，防别处调用 |

错误信息脱敏：`[target=postgresql://better_agent:***@127.0.0.1:5432/better_agent]`。

### 2.4 T01 验收证据

**（a）单元测试 17 项全过** —— `t01-guard-tests.txt`

```
17 passed in 0.27s
```

含 4 类拒绝（应用库 / 非回环主机 / 无库名 / 不合约定）、3 类接受、密码不泄漏、
allowlist 不能覆盖黑名单，以及关键的 ordering 测试
`test_alembic_never_runs_against_a_rejected_target`（断言 `subprocess.run` 零调用）。

**（b）端到端：故意指向开发库，拒绝生效** —— `t01-refuses-dev-db.txt`

```
$ TEST_DATABASE_URL=postgresql://...@127.0.0.1:5432/better_agent pytest tests/integration/test_learning_v3_postgres.py
EEEE                                                        [100%]
refusing to migrate or truncate a non-test database:
  database 'better_agent' is an application database, not a test database
  [target=postgresql://better_agent:***@127.0.0.1:5432/better_agent]
4 errors in 0.51s      EXIT=1
```

**0.51 秒**失败 = 没连库、没跑迁移。**拒绝确实发生在任何迁移或 truncate 之前。**

**（c）开发库零变化**（验证前已 `pg_dump -Fc` 备份 2.1 MB，验证后删除）

| 指标 | 验证前 | 验证后 |
| --- | --- | --- |
| threads | 46 | **46** |
| thread_messages | 305 | **305** |
| goals | 7 | **7** |
| model_profiles | 35 | **35** |
| schema 版本 | `20260920_0020` | **`20260920_0020`** |
| V3 表数量 | 0 | **0** |

---

## 3. T02 —— 执行 V3 数据库契约验收

### 3.1 第一次真跑：3 failed（此前一直是环境 ERROR）

```
.FFF                                                        [100%]
3 failed, 1 passed in 25.21s
```

| 测试 | 结果 |
| --- | --- |
| `test_the_support_column_exists_on_the_authoritative_backend` | ✅ PASS |
| `test_the_decision_audit_is_append_only_on_postgres` | ❌ FAIL |
| `test_a_promotion_is_unique_per_candidate_on_postgres` | ❌ FAIL |
| `test_a_memory_cycle_runs_end_to_end_on_postgres` | ❌ FAIL（`status=UNKNOWN`，期望 `APPLIED`） |

> 这三项**历史上从未真正执行过** —— 它们一直躺在 `tests/integration/*` 的 100 个环境 ERROR 里。
> 历史验收报告的 21/21 PASS 不覆盖它们。

### 3.2 根因：SQLite 专属的命名占位符

```
psycopg.errors.SyntaxError: syntax error at or near ":"
LINE 1: ...VALUES (:id,:job_i...
                        ^
```

`app/learning_decision.py::_insert` 用的是 **`:name` 命名占位符 + dict 参数**：

```python
"VALUES (:id,:job_id,:owner_id,:learn,...,:created_at) "
"ON CONFLICT(owner_id,job_id) DO NOTHING",
dict(row))
```

而 `app/db.py::_postgres_sql` **只把 `?` 转成 `%s`，完全不认 `:name`**。
SQLite 原生支持 `:name`，所以 V3 单测一路绿灯；PostgreSQL 直接语法错误。

同一个方法里第二句查询用的却是 `?` —— 两种风格混用，说明是漏改而非设计。

**全仓扫描确认只此一处**（AST 遍历所有含 SQL 关键字的字符串字面量，含 f-string 与拼接；
其余 8 处命中均为 docstring `:class:` 或正则里的 `:true`/`:CASCADE`）。

### 3.3 修复

列名收敛成类常量，占位符与取值顺序全部由它派生：

```python
DECISION_COLUMNS = (
    "id", "job_id", "owner_id", "learn", "target", "subtype", "confidence", "support",
    "importance", "risk", "reason_codes_json", "input_digest", "decision_digest",
    "model_identity", "created_at",
)

@classmethod
def _insert(cls, connection: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    columns = cls.DECISION_COLUMNS
    connection.execute(
        f"INSERT INTO learning_decisions({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)}) "
        "ON CONFLICT(owner_id,job_id) DO NOTHING",
        tuple(row[name] for name in columns),
    )
```

选 `?` 而不是 `%s`：`?` 是项目既有约定，SQLite 和 PG 两边都认（PG 侧由 `_postgres_sql` 转换）。

### 3.4 修复后：4 passed

```
....                                                        [100%]
4 passed in 21.83s      EXIT=0
```

**一处改动解决了全部 3 个失败** —— 包括那个 `UNKNOWN`：它只是 `_insert` 抛异常被流水线
按保守策略归为 UNKNOWN 的连锁反应。

**无副作用**：V3 全量单测（10 个文件）

```
220 passed in 96.59s (0:01:36)      EXIT=0
```

### 3.5 schema 一致性核对

| 项 | 值 | 一致 |
| --- | --- | --- |
| `app/db.py::POSTGRES_SCHEMA_HEAD` | `20260921_0023` | ✅ |
| `alembic heads` | `20260921_0023 (head)`（**单 head**） | ✅ |
| 测试库 `alembic_version` | `20260921_0023` | ✅ |
| 迁移文件数 | 23 | — |

**升级路径实测**（临时库 `better_agent_v3_migtest`，验证后已删除）：

| 路径 | 结果 |
| --- | --- |
| 空库 → head | ✅ `20260921_0023` |
| 空库 → `20260920_0020`（模拟既有前序 schema） | ✅ 停在 `20260920_0020` |
| `20260920_0020` → head | ✅ 依次跑 `0021` → `0022` → `0023`，3 张 V3 表建成 |

注意开发库当前正停在 `20260920_0020`，所以第三条路径就是它将来升级要走的。

---

## 4. 复现说明（不含密码）

### 环境

| 项 | 值 |
| --- | --- |
| PostgreSQL | 16.13，容器 `better-postgres-1`，`127.0.0.1:5432` |
| 扩展 | `plpgsql`, `pg_trgm`, `vector` |
| 测试库 | `better_agent_v3_test`（**必须以 `_test` 结尾**，否则守卫拒绝） |
| 超级用户 | `better_agent` |
| Python / pytest | 3.13.0（`D:\pycharm\python.exe`）/ 9.1.1 |

### 建库（一次性）

```bash
docker exec better-postgres-1 psql -U better_agent -d better_agent \
  -c "CREATE DATABASE better_agent_v3_test;"
docker exec better-postgres-1 psql -U better_agent -d better_agent_v3_test \
  -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

### 跑测试

```bash
cd backend
export TEST_DATABASE_URL="postgresql://better_agent:<password>@127.0.0.1:5432/better_agent_v3_test"
"D:/pycharm/python.exe" -m pytest -q tests/integration/test_learning_v3_postgres.py \
  -rfE -p no:cacheprovider \
  --basetemp="C:/Users/wym/AppData/Local/Temp/better-v3-$(date +%s)"
```

`--basetemp` 必须在 `C:` 盘（`D:` 小文件 I/O 慢 9.2 倍，会让计时类用例成片假失败）；
`-rfE` 必须合写（`-rf -rE` 里后者覆盖前者，失败清单会静默消失）。

### CI

CI 里 `docker compose` 通常可用，不设 `TEST_DATABASE_URL` 即可走 compose 分支
（库名 `better_agent_test`，天然满足后缀约定）。若 CI 复用外部 PG，
设 `TEST_DATABASE_URL` 指向 `*_test` 库，或对特殊命名设 `TEST_DATABASE_ALLOW=<name>`。

---

## 5. 本任务产物

| 文件 | 内容 |
| --- | --- |
| `t01-guard-tests.txt` | 17 项守卫单测输出 |
| `t01-refuses-dev-db.txt` | 端到端拒绝开发库的完整输出 |
| `t02-pg-baseline.txt` | 修复前 3 failed（首次真跑） |
| `t02-pg-after-fix.txt` | 修复后 4 passed |
| `t02-v3-unit-after-fix.txt` | V3 全量单测 220 passed |

代码改动：

| 文件 | 改动 |
| --- | --- |
| `backend/app/learning_decision.py` | `DECISION_COLUMNS` 常量 + `_insert` 改 `?` 占位符 |
| `backend/tests/integration/db_target_guard.py` | **新增**，fail-closed 目标守卫 |
| `backend/tests/integration/conftest.py` | 三处接入守卫 |
| `backend/tests/integration/test_db_target_guard.py` | **新增**，17 项验收测试 |

## 6. 状态

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| T01 隔离 PG 入口 | ✅ 完成 | 17 passed + 端到端拒绝 + 开发库零变化 |
| T02 PG 契约验收 | ✅ 完成 | 4 passed，无 skip 无环境 ERROR；schema 三者一致；两种升级路径实测 |

**T02 的限制（照任务书）**：这 4 项使用模型与预算替身，不证明真实预算和真实模型闭环；
该证据由 T03、T16 补齐。
