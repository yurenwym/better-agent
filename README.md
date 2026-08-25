# better-agent

本地单用户 Personal Agent Runtime：澄清 → 研究 → 计划 → 执行 → 每日复盘 → 调整 → 成长，并提供三层记忆、证据优先的深度研究、定时研究、通知、专家协同和受控 Evolution。服务默认只监听 loopback，SQLite 使用 WAL，事件日志 append-only。

## 启动

Windows PowerShell 和跨平台环境都可以从仓库根目录运行：

```powershell
python scripts/start.py
```

启动脚本先构建 `frontend`，再以 `127.0.0.1:8000` 启动 FastAPI，并在 `frontend/dist` 存在时由 FastAPI 同源托管前端。

没有配置模型时，启动使用本地确定性 MockModelGateway，便于检查完整 UI/API 闭环；配置单一 OpenAI-compatible Profile 后才会发起真实模型请求：

```powershell
$env:AGENT_MODEL_API_KEY = "<set-in-process-only>"
$env:AGENT_MODEL_BASE_URL = "https://example.test/v1"
$env:AGENT_MODEL_ID = "example-model"
python scripts/start.py
```

也可以让启动进程优先从本地 `LLM_AP.txt` 读取 Profile。文件只把 Key 放进当前进程环境，不会写入数据库、事件、导出或前端：

```powershell
$env:LLM_AP_PATH = "D:\Users\王一鸣\Desktop\直到尽头\LLM_API.txt"
python scripts/start.py
```

`LLM_BASE_URL` 可以填写 OpenAI-compatible API 根地址（如 `/v1`）或模型探测地址（如 `/v1/models`）；启动时会安全规范化。Web 深度研究默认使用 DuckDuckGo，也可设置 `RESEARCH_SEARCH_PROVIDER=tavily` 并通过环境变量 `TAVILY_API_KEY` 提供密钥；不会在供应商之间静默回退。`RESEARCH_SEARCH_BASE_URL` 仅用于替换 DuckDuckGo 兼容搜索页。本地资料放入 `data/research_notes/`，请求研究时选择 `local_note` 来源。

## 测试与评测

```powershell
Set-Location backend
python -m pytest -q
python -m app.eval run --suite v1 --mode deterministic
Set-Location ..
Set-Location frontend
npm install
npm test -- --run
npm run build
Set-Location ..
```

评测结果写入被 Git 忽略的 `evals/results/`。真实模型 smoke/质量评测必须先使用指定的 `LLM_AP.txt`：

```powershell
Set-Location backend
python -m app.eval run --suite v1 --mode live --llm-ap "D:\Users\王一鸣\Desktop\直到尽头\LLM_API.txt"
```

`live` 会保存脱敏的网关/请求统计和待人工质量评分报告，不把模型原文或 Key 写入报告。基线比较：

```powershell
python -m app.eval compare baseline.json latest.json
```

## 数据与安全

运行数据位于 `data/agent.db`、`data/memory/` 和 `data/artifacts/`，均被 `.gitignore` 排除。默认导出为脱敏 JSONL；`WRITE` 工具必须绑定 `run_id`、`tool_call_id`、参数 Hash 并获得显式审批。状态、预算、Checkpoint、计划版本、工具结果和 `memory.applied` 都保留在轨迹中。

## 当前边界

当前版本不包含 RAG/向量检索、MCP、Shell、Redis、Celery、微服务、工具并行、跨厂商静默 fallback、隐藏思维链持久化或自动修改核心安全策略。多 Agent 仅支持受控 Expert Run；Evolution Candidate 仍需独立证据、确定性评测和人工批准，不能表述为完全自主进化。

## 测试

```powershell
Set-Location backend
python -m pytest -q
python -m app.eval run --suite v1 --mode deterministic
Set-Location ..\frontend
npm ci
npm test -- --run
npm run build
npx playwright install chromium
npm run e2e
```
