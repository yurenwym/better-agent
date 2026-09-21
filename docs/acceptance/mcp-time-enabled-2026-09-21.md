# 时间 MCP 服务启用验收

2026-09-21，front0920。

根目录 `.env` 已加入 time 服务：stdio 启动 `D:/pycharm/python.exe -m mcp_server_time --local-timezone Asia/Shanghai`，关闭环境全量继承，仅开放只读 `get_current_time` 和 `convert_time`。超时 30 秒，结果上限 16,384 bytes。

项目重启后 PID **66480**，地址 http://127.0.0.1:8000，readiness READY。

通过实际 HTTP API 创建“时间 MCP 上线验证”对话，使用运行中的 PostgreSQL、生产路由网关和真实 DeepSeek，连续两轮均 COMPLETED：

1. 查询上海当前时间：调用 `mcp__time__get_current_time`，返回 2026-09-21 10:40:09 +08:00。
2. 上海上午 09:30 换算纽约：调用 `mcp__time__convert_time`，返回前一天 21:30 −04:00。

数据库核对两个调用均 EXECUTED、ToolResult.ok=true；后续模型请求也成功。

首次上线暴露此前隔离测试未覆盖的缺陷：聊天工具循环的下一次请求复用了 `conversation:<turn_id>` 幂等身份，生产 ModelControlStore 因请求正文变化拒绝调用。已修复 `live_model.py`：首个请求保留原身份，后续请求以 `route_and_respond_tool_N` 区分，重复失败后的收尾请求也独立标识；同一 turn 的路由、预算与上下文关联仍继承原上下文。

新增使用真实 RoutedModelGateway 与调用账本的双工具循环回归测试。相关套件 `test_routed_model_gateway.py`、`test_chat_goal_tools.py`、`test_mcp_chat_loop.py`：**48 passed in 56.60s**。`git diff --check` 通过。

证据目录：`docs/evaluation/mcp-time-enabled-2026-09-21/`。

- `chat-results.json`：修复前失败记录，保留。
- `chat-results-fixed.json`：重启修复后两轮真实 HTTP 对话。
- `db-verification.json`：只读查询 PostgreSQL 的执行结果与模型调用记录。

本次证明时间服务在实际项目中可用；不涉及远程 HTTP MCP、写工具或新版协议兼容。
