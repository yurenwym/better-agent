# MCP 真实模型补测与验收范围更正

日期：2026-09-21；分支：front0920。结果：**5/5 轮真实模型对话通过；MCP 专项重跑 101 passed in 121.85s**。

## 1. 实际验证的链路

使用已有 DeepSeek 凭据，由项目 config loader 读取，未输出密钥。当前进程和 Windows 用户环境均有凭据，因此撤回原报告“本机没有 provider 凭据”的判断。

```text
LiveConversationModel → 实际 DeepSeek API → 模型自主返回 tool_calls
  → ManagedTurnWorker / ChatToolRunner / ToolRegistry
  → MCP Adapter → 真实 stdio 子进程
  → 持久化 ToolResult → 后续真实模型请求 → 最终回答
```

- 模型：deepseek-flash，端点 https://api.deepseek.com。
- SDK：mcp 1.29.0；两个 MCP 服务实际协商协议均为 2025-11-25。
- time：安装的 mcp_server_time 参考服务，提供 get_current_time、convert_time。
- check：仓库内真实协议服务 tests/mcp_test_server.py，提供 sum_numbers、echo。它是受控测试服务，不能称为外部业务系统验收。
- 数据库：独立 SQLite；使用生产 AgentRuntime、ManagedTurnWorker 和实时 HTTP ModelGateway。网关观察子类仅记录请求/响应元数据后原样调用父类，不注入模型回答或工具调用。
- 本批仅调用白名单内只读工具；两台子进程关闭后释放。未更改运行中服务的 MCP 配置，也未重启 PID 21276。

## 2. 逐轮结果

| 轮 | 场景 | 模型自主选择的 MCP 工具 | 结果 |
| ---: | --- | --- | --- |
| 1 | 查询上海当前时间 | mcp__time__get_current_time | COMPLETED；返回 2026-09-21 10:22:36 +08:00 |
| 2 | 上海 09:30 换算纽约时间 | mcp__time__convert_time | COMPLETED；返回前一天 21:30，UTC−04:00 |
| 3 | 计算 173 + 281 + 547 | mcp__check__sum_numbers | COMPLETED；模型回答 1001 |
| 4 | 回显本次随机生成的文本 | mcp__check__echo | COMPLETED；返回 echo:mcp-live-42e3f42315c84ae0 |
| 5 | 再次查询 UTC 当前时间 | mcp__time__get_current_time | COMPLETED；返回 2026-09-21 02:23:16 +00:00 |

共 14 次实际供应商模型请求（含辅助分类）、5 次 MCP tools/call，MCP call_failures=0、reconnects=0。14 次模型请求不是 14 轮对话，也不用于跨调用相加计算上下文误差。

每轮检查：最终状态完成、非空回答、真实模型返回预期工具名、真实远端执行成功、工具结果通过持久化调用 ID 回填到后续模型请求、供应商 usage 存在；求和、随机回显、UTC 额外检查回答内容。另从数据库记录的 ToolResult 重建正文摘要，**5/5 与后续模型请求中的工具消息 SHA-256 一致**。

本批两个服务未提供正 TTL，工具目录共发现 17 次、cache_hits=0。这符合当前“无 TTL 则立即陈旧”的行为，本批不能用作缓存命中证明；缓存场景仍由专项测试覆盖。

## 3. 验收脚本及原始证据

- 脚本：backend/scripts/mcp_live_model_acceptance.py。
- 最终完整记录：docs/evaluation/mcp-live-model-2026-09-21/results.json。
- 回填正文摘要核对：docs/evaluation/mcp-live-model-2026-09-21/result-payload-check.json。
- 第一批检查器误报原始记录：docs/evaluation/mcp-live-model-2026-09-21/initial-verifier-failure.json。

第一批 5 轮也实际完成，但检查器误拿 provider call ID 与 Harness 持久化 call ID 比较；应用正常将工具交换双方 ID 改写为持久化 ID。修正检查后完整重跑第二批，5/5 通过。保留第一批失败结果，不回填或改写原观测值。

复跑（会产生真实供应商费用，输出目录须不存在）：

```powershell
python backend/scripts/mcp_live_model_acceptance.py --out data/acceptance/mcp-live-next-run
```

## 4. 专项回归

```powershell
cd backend
python -m pytest tests/test_mcp_client.py tests/test_mcp_tools.py `
  tests/test_mcp_chat_loop.py tests/test_mcp_acceptance.py -q -p no:cacheprovider
# 101 passed in 121.85s
```

本次未重跑全量套件；原报告的 1486 passed / 20 failed 不等于全量通过。没有改动前后同环境对照，不能仅凭测试“不 import MCP”认定失败无关。本次核对 costs.py 无工作区改动，已修正文档对未提交价格代码的归因。

## 5. 协议声明与结论边界

1. **真实模型 + 真实 stdio MCP 只读循环已补齐**。隔离环境不等同于运行中 HTTP/PostgreSQL 应用已经配置并启用 MCP；当前服务部署、远程 Streamable HTTP 成功路径及真实模型写入审批不在本次验证范围。
2. **没有验证新版无状态协议兼容**。当前 ClientSession.initialize()、旧 SDK 类型及 _meta 扩展不能代替新版顶层 ttlMs/cacheScope/resultType 测试；升级必须重新适配和验证。原报告“两个版本语义一致，只改版本号即可”已撤回。
3. **阶段 2 的证据仍是确定性模型 + 真实受控 MCP 服务**。此次仅补测真实模型读取能力，不宣称已验证真实模型写入、未知状态恢复或第三方业务幂等。
4. **HTTP 失败可诊断不代表 HTTP 可成功调用**。原验收 HTTP 项访问不可达端点，仅证明失败路径；stdio 是当前成功端到端证据覆盖的传输。

原阶段报告：docs/acceptance/mcp-phase1-acceptance-2026-09-20.md；开发方案：docs/superpowers/plans/2026-09-20-mcp-harness-integration.md。两者已同步纠正凭据、协议兼容与失败归因的表述，原机器证据保留。
