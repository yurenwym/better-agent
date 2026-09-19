# better-agent

本地单用户 Personal Agent Runtime：澄清 → 研究 → 计划 → 执行 → 每日复盘 → 调整 → 成长，并提供分层记忆、语义检索、深度研究、通知、专家协同和受控 Evolution。PostgreSQL 是唯一权威运行时数据库，pgvector HNSW 保存并检索 1024 维记忆向量；Markdown 仅是可重建的用户可读投影。

## 启动

推荐通过 Docker Compose 启动 PostgreSQL、Alembic 迁移和应用：

```powershell
Copy-Item .env.example .env
# 在 .env 中设置本地数据库密码及所需模型密钥，不要提交 .env。
docker compose up --build -d
docker compose ps
```

应用地址为 `http://127.0.0.1:8000/`。`migrate` 服务必须成功完成且 PostgreSQL healthcheck 通过后，应用才会启动。开发环境也可以从仓库根目录运行：

```powershell
$env:DATABASE_URL = "postgresql://better_agent:<password>@127.0.0.1:5432/better_agent"
python scripts/start.py
```

需要边改前端边看效果时，改用 Vite dev server：改源码即时热更新，不必再手动 `npm run build`：

```powershell
Set-Location frontend
npm ci
npm run dev          # http://127.0.0.1:5173，/api 反向代理到 127.0.0.1:8000
```

dev server 只提供前端资源与热更新，后端仍按上面的方式运行；`/api` 请求、CSRF 校验与 SSE 事件流都经代理转发到后端，Host 校验照常通过。要发布或继续用单端口 `8000` 时，仍然执行 `npm run build` 产出 `frontend/dist`。

启动脚本会自动读取仓库根目录的 `.env`，且不会覆盖已经存在的进程环境变量。最简单的模型配置只需选择一家供应商并填写密钥：

```powershell
# .env（只保留你使用的密钥即可）
AGENT_MODEL_PROVIDER=deepseek
DEEPSEEK_API_KEY=<your-key>
```

同样支持 `OPENAI_API_KEY` 和 `ANTHROPIC_API_KEY`。只配置一个供应商密钥时会自动选择它；同时配置多个时必须通过 `AGENT_MODEL_PROVIDER=openai|anthropic|deepseek` 明确选择。标准供应商会使用受审阅的默认端点、模型、能力与价格快照。自定义 `AGENT_MODEL_*` 端点仍受支持，但未知模型必须同时配置五项 `AGENT_MODEL_PRICE_*` 费率、`PRICE_EFFECTIVE_AT` 和 `PRICE_SOURCE_URL`，不会用虚构价格绕过预算门禁。

`GET /api/model-readiness` 会在不访问供应商的前提下检查凭证引用、conversation/ask 角色路由、已生效价格和预算默认值。发送消息前也执行同一检查；配置不完整时直接返回结构化 `503`，不会先接受一个注定失败的任务。`network_verified=false` 表示尚未做真实供应商请求，不等同于配置失败。

旧的 `LLM_AP_PATH` 仅为兼容已有验收脚本保留，日常启动不再要求 txt 文件。

Turn worker 默认有 4 个并发 slot，可用 `BETTER_AGENT_TURN_WORKER_CONCURRENCY=1..16` 调整。不同 thread 可以并行，同一 thread 在数据库约束和租约 fencing 下最多运行一个 turn。

可以为主模型设置备用 Profile。备用模型只在主模型尚未输出内容且发生可重试失败时使用：

```powershell
$env:AGENT_FALLBACK_MODEL_API_KEY = "<set-in-process-only>"
$env:AGENT_FALLBACK_MODEL_BASE_URL = "https://api.siliconflow.cn/v1"
$env:AGENT_FALLBACK_MODEL_ID = "Qwen/Qwen3.5-122B-A10B"
```

长期记忆向量默认使用 `Qwen/Qwen3-Embedding-4B` 和 1024 维输出：

```powershell
$env:EMBEDDING_API_KEY = "<set-in-process-only>"
$env:EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
$env:EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-4B"
$env:EMBEDDING_DIMENSIONS = "1024"
```

检索顺序是 HNSW 语义召回优先；Embedding 不可用、调用失败或没有语义候选时，降级到 PostgreSQL FTS/`pg_trgm`。轨迹只保存模式、原因、候选数量和耗时，不保存查询正文、向量或密钥。

## 从 SQLite 一次性迁移

迁移前必须停写并先备份旧 SQLite。目标 PostgreSQL 必须已执行 Alembic 且业务表为空；重复导入会拒绝，不覆盖已有数据。

```powershell
Set-Location backend
$env:DATABASE_URL = "postgresql://better_agent:<password>@127.0.0.1:5432/better_agent"
python -m alembic -c alembic.ini upgrade head
python scripts/migrate_sqlite_to_postgres.py ..\data\agent.db `
  --database-url $env:DATABASE_URL `
  --manifest ..\data\sqlite-postgres-manifest.json
Set-Location ..
```

迁移命令会先检查 SQLite 外键孤儿，再逐表导入并比对行数和规范化 SHA-256。清单不一致即失败，不能切换应用流量。

## 备份、恢复与回滚

```powershell
# 逻辑备份
docker compose exec -T postgres pg_dump -U better_agent -d better_agent -Fc > better-agent.dump

# 恢复到预先创建的空数据库后，执行同一套验收测试
docker compose exec -T postgres pg_restore -U better_agent -d better_agent_restore --clean --if-exists < better-agent.dump
```

生产应另行配置 PostgreSQL base backup + WAL 归档并定期演练 PITR。PostgreSQL 接受第一笔新写入后，禁止回切旧 SQLite；应用版本回滚必须继续连接兼容当前 schema 的 PostgreSQL，数据故障使用 PostgreSQL 恢复或前向修复。

## 测试与验收

费用默认仅统计（`BETTER_AGENT_COST_MODE=observe`），不会因任务、每日或每月金额上限暂停。用量页显示基于 token 与价格快照的估算，缺少价格或 usage 时标记未知。调用次数、执行时限、用户取消、权限和进化审批仍生效；历史金额字段保留用于兼容，不作为默认运行门禁。

```powershell
Set-Location backend
python -m pytest -q
Set-Location ..\frontend
npm ci
npm test -- --run
npm run build
```

PostgreSQL 集成测试使用隔离的 Docker pgvector 实例。发布阻断用例 A17 会完整执行：建会话、提交消息、worker 领取、上下文与记忆检索、模型流式回答、消息/事件/四项耗时落库、API 查询和重启后复查，并在运行期禁止 SQLite 访问。

## 数据与安全

运行时状态全部位于 `DATABASE_URL` 指向的 PostgreSQL；`data/memory/` 和 `data/artifacts/` 是可重建投影或工作产物。生产启动没有 PostgreSQL 时会明确失败，不会回读或双写 SQLite。默认导出为脱敏 JSONL；密钥仅通过环境变量注入，不进入数据库、事件、轨迹或前端。

Web 深度研究默认使用 DuckDuckGo，也可设置 `RESEARCH_SEARCH_PROVIDER=tavily` 并通过 `TAVILY_API_KEY` 提供密钥。当前不引入 Redis、Celery 或长期双库；多 Agent 仅支持受控 Expert Run，Evolution Candidate 仍需独立证据、确定性评测和人工批准。
