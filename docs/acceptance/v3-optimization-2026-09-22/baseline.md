# T00 基线快照 —— V3 自进化机制优化

日期：2026-09-22
任务书：`docs/V3-自进化机制优化开发任务书-2026-09-22.md`
本轮验收目录：`docs/acceptance/v3-optimization-2026-09-22/`

---

## 1. 受测代码版本

| 项 | 值 |
| --- | --- |
| 分支 | `front0920` |
| HEAD | `532378290b649e33d5048541aa984c9cb893fe87` |
| **HEAD 能否充当受测版本** | **不能** |
| 受测版本的实际标识 | `manifest.json` 中 41 个文件的 SHA-256 |

**为什么 HEAD 不能用**：本轮 V3 的十个核心文件**全部未跟踪**，HEAD 里根本没有它们。
逐项核实（`git cat-file -e HEAD:<path>`）：

```
NOT-HEAD  backend/app/learning_pipeline.py
NOT-HEAD  backend/app/learning_targets.py
NOT-HEAD  backend/app/learning_contract.py
NOT-HEAD  backend/app/learning_promotion.py
NOT-HEAD  backend/app/learning_eval.py
NOT-HEAD  backend/app/learning_v3_schema.py
NOT-HEAD  backend/app/learning_legacy.py
NOT-HEAD  backend/scripts/learning_v3_acceptance.py
NOT-HEAD  backend/tests/test_learning_v3_wiring.py
NOT-HEAD  backend/tests/integration/test_learning_v3_postgres.py
```

这印证了任务书 T00 的判断：「现有 V3 大量文件未跟踪，不能仅以 HEAD 作为验收代码版本」。
因此任何验收结论必须引用 `manifest.json` 里的内容指纹，而不是提交号。

> 附带修正一条过期记忆：此前记录的「`.git` 对象库被清空、`git status` 报 `fatal: bad object HEAD`」
> **已经不成立**。实测 `rev-parse HEAD` / `status` / `branch -a` 全部正常，两个 `.pack` 完整
> （`pack-2ca7dcc3` 6.2 MB / `pack-eb456739` 1.2 MB）。仓库可用，但按既有约定仍不对它做写操作。

## 2. 受测文件清单（41 个）

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| 已跟踪但被修改 | 6 | 全部含 V3/learning 改动，全部属于本轮范围 |
| 未跟踪（展开目录后） | 35 | V3 新增实现 / 测试 / 迁移 / 脚本 / 文档 |

**已跟踪的 6 个改动**（`worktree.patch`，589 行）：

| 文件 | 增/删 | V3 关键词命中 | 性质 |
| --- | --- | --- | --- |
| `backend/app/learning.py` | +53 / −307 | 73 | 大重构：legacy 逻辑外迁到 `learning_legacy.py` |
| `backend/app/startup.py` | +39 / −2 | 24 | 装配点 |
| `.env.example` | +26 / −0 | 11 | V3 开关文档 |
| `backend/app/db.py` | +23 / −1 | 10 | schema head / 连接 |
| `backend/app/model_admin.py` | +5 / −0 | 5 | 模型管理接线 |
| `backend/app/model_control.py` | +2 / −0 | 2 | 模型控制接线 |

**未跟踪的 V3 实现**（按行数）：

| 文件 | 行 | sha256 前缀 |
| --- | --- | --- |
| `backend/app/learning_pipeline.py` | 477 | `b443f736c6b7` |
| `backend/app/learning_promotion.py` | 566 | `c41a1e6f25ba` |
| `backend/app/learning_targets.py` | 376 | `0f11f23a0b6c` |
| `backend/app/learning_eval.py` | 373 | `fd6fd8a641a4` |
| `backend/app/learning_agent.py` | 452 | `408bc762363e` |
| `backend/app/learning_decision.py` | 466 | `cf9c10b06d47` |
| `backend/app/learning_legacy.py` | 339 | `41062788d4ef` |
| `backend/app/learning_contract.py` | 144 | `ff07a5b08974` |
| `backend/app/learning_v3_schema.py` | 84 | `78932876fd8b` |
| `backend/alembic/versions/20260921_0021_learning_decisions.py` | 19 | `976cd685f35a` |
| `backend/alembic/versions/20260921_0022_learning_promotions.py` | 19 | `91bf169fa272` |
| `backend/alembic/versions/20260921_0023_learning_decision_support.py` | 22 | `00280c0d2dfd` |

未跟踪的测试 10 个（`test_learning_*.py`，含 integration）、脚本 5 个、evals 2 个、文档 3 个，
完整清单与哈希见 `manifest.json`。

**本轮新增的工具**（也纳入 manifest）：`backend/scripts/v3_baseline_manifest.py`（生成本快照）。

## 3. 测试环境

| 项 | 值 |
| --- | --- |
| Python | 3.13.0（`D:\pycharm\python.exe`，**必须用它**——托管 3.13.12 无 `httpx`） |
| pytest | 9.1.1 |
| OS | Windows 11 (10.0.22631) |
| SQLAlchemy / Alembic | 2.0.51 / 1.19.2 |
| psycopg | 3.3.5（`psycopg2` / `asyncpg` 未安装） |
| httpx / pydantic | 0.28.1 / 2.13.4 |
| PostgreSQL | **16.13**（容器 `better-postgres-1`，`Up 10 days (healthy)`，`127.0.0.1:5432`） |
| PG 扩展 | `pg_trgm`, `plpgsql`, `vector` |
| PG 超级用户 | `better_agent`（**不是** `postgres`） |

`--basetemp` 统一落在 `C:` 盘（`D:` 小文件 I/O 慢 9.2 倍，会让计时类用例成片假失败）。

## 4. 测试收集清单

| 项 | 值 |
| --- | --- |
| 收集结果 | **1837 tests collected in 6.31s**，退出码 0 |
| 涉及文件 | 172 个 |
| 原始输出 | `collect-raw.txt` |
| node ID 清单 | `collect-nodes.txt`（1837 行，已排序） |

收集阶段无 import 错误。用例最多的文件：`test_live_model.py` 63、`test_research_engine.py` 61、
`test_static_archive_policy.py` 46、`test_conversation_worker.py` 41、`test_learning_agent.py` 41。

## 5. 历史失败与错误（**历史基线，本轮尚未复跑**）

来源：`outputs/regression-repeat.txt`（2026-09-22 04:35，带 `-rfE` 的那轮，清单完整）。

```
19 failed, 1714 passed, 4 skipped, 100 errors in 898.47s (0:14:58)   EXIT=1
```

> ⚠️ **这是历史数字，不是本轮结论。** 本轮全量回归要等 T16 才跑；在此之前任何报告都不得把
> 「19 failed / 100 errors」写成当前测试通过或当前失败。

### 5.1 19 个 FAILED（node ID 见 `historic-failed.txt`）

| 文件 | 数量 | 归因 |
| --- | --- | --- |
| `test_cost_control.py` | 10 | 未提交的 `costs.py` 价格快照链路 |
| `test_plan_security.py` | 3 | Windows 符号链接用例 |
| `test_real_evaluation.py` | 2 | costs.py |
| `test_m5_release_gates.py` | 2 | costs.py |
| `test_golden_journey.py` | 1 | 本机 DNS 解析不到 `sqlite.org` |
| `test_evaluation_api.py` | 1 | costs.py |

按任务书 T03 的要求，**这 6 项归因不能默认沿用**，T03 要逐项重新核实。

### 5.2 100 个 ERROR（node ID 见 `historic-errored.txt`）

**全部来自 `tests/integration/*`**，根因是本机 `docker compose` 插件不可用
（`unknown shorthand flag: 'f' in -f`，exit 125），conftest 的 `postgres_url` fixture 直接
`pytest.fail`。按文件聚合（前 5）：

| 文件 | ERROR 数 |
| --- | --- |
| `test_goal_integrity_postgres.py` | 14 |
| `test_postgres_queue_and_embeddings.py` | 12 |
| `test_sqlite_postgres_migration.py` | 10 |
| `test_budget_waiting_and_operations.py` | 9 |
| `test_goal_tool_recovery_postgres.py` | 7 |

**其中 `test_learning_v3_postgres.py` 占 4 个 ERROR** —— 这正是 T02 要跑的 4 项，
也是 T01 必须先解锁的直接原因。T03 要用的 `test_root_task_budgets.py` 占 5 个。

注意：**PG 容器本身是健康的**（见 §3）。ERROR 不是数据库不可用，而是 compose 插件坏了导致
fixture 拿不到 `TEST_DATABASE_URL`。所以 T01 不必先修 Docker 插件——这正是任务书给的路子。

## 6. 本阶段暴露的阻断点

| # | 阻断点 | 影响 | 处置 |
| --- | --- | --- | --- |
| 1 | V3 核心文件未跟踪 | HEAD 无法充当受测版本 | 已用 `manifest.json` 内容指纹替代 |
| 2 | `tests/integration/*` 全 E（100 个） | §66 Testing 的 Integration 项无法闭合 | T01 用独立测试库 + `TEST_DATABASE_URL` 绕过 |
| 3 | 历史失败清单只能从 `regression-repeat.txt` 取 | `regression-clean.txt` 那轮 `-r` 被覆盖，只有 E 清单 | 已固定 `-rfE` 连写 |

## 7. 本任务产物

| 文件 | 内容 |
| --- | --- |
| `baseline.md` | 本文档 |
| `manifest.json` | 41 个受测文件的 sha256 / 字节数 / 行数 / 是否在 HEAD |
| `worktree.patch` | 6 个已跟踪文件的完整 diff（589 行） |
| `worktree-status.txt` | `git status --porcelain` 原始输出 |
| `collect-raw.txt` | `pytest --collect-only -q` 原始输出 |
| `collect-nodes.txt` | 1837 个 node ID（排序后） |
| `historic-failed.txt` | 19 个历史失败 node ID |
| `historic-errored.txt` | 100 个历史错误 node ID |

## 8. 复现命令

```bash
cd D:/RAG/better
D:/pycharm/python.exe backend/scripts/v3_baseline_manifest.py \
  --out docs/acceptance/v3-optimization-2026-09-22/manifest.json
git -c core.quotepath=false status --porcelain=v1
git diff > docs/acceptance/v3-optimization-2026-09-22/worktree.patch
cd backend
D:/pycharm/python.exe -m pytest -q --collect-only -p no:cacheprovider \
  --basetemp="C:/Users/wym/AppData/Local/Temp/better-v3-collect"
```

## 9. 状态

**T00 完成。** 验收口径：

- ✅ 任一报告可定位到实际受测代码 —— 通过 `manifest.json` 的 sha256，不依赖 HEAD
- ✅ 未将历史 19 failed / 100 errors 写成当前通过 —— §5 显式标注为历史基线
- ✅ 收集清单与历史失败 node ID 已落盘，本轮复跑后按 **node ID 集合**比较，不靠进度条推断
