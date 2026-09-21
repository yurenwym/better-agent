# MCP 接入阶段 1 验收报告

日期：2026-09-20。分支：`front0920`。方案：`docs/superpowers/plans/2026-09-20-mcp-harness-integration.md`。
证据文件：`docs/acceptance/mcp-phase1-evidence-2026-09-20.json`（19 个用例的真实观测值，非人工填写）。

2026-09-21 补测：[真实模型验收与范围更正](mcp-live-model-acceptance-2026-09-21.md)。5/5 轮真实 DeepSeek + 真实 stdio MCP 通过，专项重跑 101 项通过；使用隔离运行时，不代表运行中服务或远程 HTTP 已验收。

## 1. 结论

本批确定性验收覆盖第 4 节列出的协议与 Harness 场景；模型由 scripted gateway 驱动，不能据此认定完整真实模型验收通过。MCP 服务是真实子进程（`mcp-server-time`、`mcp-server-fetch`，以及本仓库自建的 `tests/mcp_test_server.py`），走真实 stdio 握手、真实 `tools/list`、真实 `tools/call`。HTTP 项仅覆盖失败路径，不代表远程 HTTP 成功调用已验证。

阶段 2（写工具、审批绑定、未知结果与恢复）的代码与测试已一并落地并覆盖，因为审批绑定和 `ToolReconciliationRequired` 复用的是既有 Harness 路径，单独留到"下一阶段"反而会先引入不安全的写路径。

阶段 3（Tool Search / 按需加载）评估结论：**暂不引入**，理由见第 7 节。

**原批次未完成项**：本批没有做真实模型端到端，模型侧由 scripted gateway 驱动。2026-09-21 复核发现当前进程与 Windows 用户环境均有 DeepSeek 凭据；撤回“本机没有配置任何 provider 凭据、需要提供新 key”的判断。真实模型补测另行记录，历史证据 JSON 保留原样。

## 2. 版本基线：与方案文档的偏差

方案第 2 节基于对发布说明的阅读，与实际可安装的 SDK 不一致。实施以实测为准，已回写进方案文档的 2.1 节。

| 项目 | 方案声称 | 实测 | 证据 |
| --- | --- | --- | --- |
| Python SDK | v2 | `mcp==1.29.0` | `evidence.environment.sdk_version` |
| 协议版本 | 2026-07-28 | `2025-11-25` | `evidence.environment.protocol_version_latest` |
| initialize 握手 | 已移除 | 仍然存在 | `cases.reference_time_server.protocol_version` |
| `Mcp-Session-Id` | 已移除 | Streamable HTTP 仍使用 | 由 SDK 管理，业务状态不依赖 |
| 参考服务 | — | `server_info = {"name": "mcp-time", "version": "1.29.0"}` | `cases.reference_time_server.server_info` |

版本兼容边界更正（2026-09-21）：当前 `ListToolsResult` 的字段为 `meta/nextCursor/tools`，`CallToolResult` 为 `content/isError/meta/structuredContent`。实现读取 `result.meta` 内的缓存与续接提示，自建服务也通过 `_meta` 注入这些值；这是当前适配器扩展约定，不证明旧版标准拥有新版顶层 `ttlMs/cacheScope/resultType` 语义。参考服务未提供 TTL 时，代码默认 private/立即陈旧，不能把客户端默认值说成服务端下发。当前代码显式使用 `ClientSession.initialize()`；升级 SDK 或协议仍需适配类型、生命周期并重新跑兼容测试，不能承诺只改版本号。

## 3. 交付物

新增：

| 文件 | 内容 |
| --- | --- |
| `backend/app/mcp_client.py` | 客户端管理器：配置解析（含凭据引用）、会话生命周期、发现缓存、远端调用、资源释放 |
| `backend/app/mcp_tools.py` | MCP 工具定义 → `ToolSpec` 映射、JSON Schema 校验、本地风险策略、结果转换、审批绑定、回执恢复 |
| `backend/tests/mcp_test_server.py` | 基于官方 SDK 的真实 stdio 服务，用于可控地制造 TTL、分页、断连、定义变化、大结果等场景 |
| `backend/tests/test_mcp_client.py` | 28 项：配置、握手、缓存、分页、断连、重连、凭据遮蔽 |
| `backend/tests/test_mcp_tools.py` | 35 项：命名、白名单、风险策略、schema 校验、结果映射、审批绑定、artifact、回执恢复 |
| `backend/tests/test_mcp_chat_loop.py` | 19 项：真实 `ManagedTurnWorker` 驱动的完整回合，含审批暂停/续接、取消、上下文预算 |
| `backend/tests/test_mcp_acceptance.py` | 19 项：方案第 6 节验收矩阵，写出证据 JSON |
| `backend/scripts/mcp_env_sample_acceptance.py` | 把 `.env.example` 里那段示例原样取出并端到端跑通（发现→注册→真实调用→越权拒绝） |
| `docs/acceptance/mcp-phase1-evidence-2026-09-20.json` | 机器可读证据 |

修改：

| 文件 | 改动 |
| --- | --- |
| `backend/app/tools.py` | `ToolSpec` 增 `source` / `server_id` / `remote_name` / `definition_digest`；增 `unregister` / `specs`；`execute_async` 支持异步回执恢复 |
| `backend/app/chat_tools.py` | 允许集合纳入显式启用的 MCP 工具；审批绑定优先取 MCP 绑定；捕获 `ToolReconciliationRequired` |
| `backend/app/runtime.py` | `conversation_tool_allowance` 统一纳入 MCP 工具，保留 Skill 权限交集 |
| `backend/app/conversation.py` | 回合开始刷新已连接服务的目录；runner 注入 `mcp_sync` |
| `backend/app/startup.py` / `backend/app/main.py` | 启动时构建并注册，关闭时释放子进程 |
| `backend/pyproject.toml` / `backend/requirements.txt` | 锁定 `mcp==1.29.0`、`jsonschema>=4.23,<5`；可选组 `mcp-acceptance` |
| `.env.example` | `BETTER_AGENT_MCP_SERVERS` 的字段说明与两个可直接粘贴的示例 |

## 4. 验收矩阵

方案第 6 节逐项对照。证据列指向 `mcp-phase1-evidence-2026-09-20.json` 的 `cases`。

| # | 要求 | 结果 | 证据（实测值） |
| --- | --- | --- | --- |
| 1 | 正常多轮调用 | 通过 | `normal_multi_turn`：`get_current_time` + `convert_time` 两个 tool_call_id 均回填 |
| 2 | 有效 TTL 内不重复发现 | 通过 | `ttl_reuse_and_expiry.requests_within_ttl = 1` |
| 3 | TTL 到期失效 | 通过 | `ttl_reuse_and_expiry.requests_after_expiry = 2` |
| 4 | 变更通知失效 | 通过 | `tool_change_invalidation.invalidations = 1` |
| 5 | 缺失 TTL 提示按立即陈旧 | 通过 | `missing_ttl_hint`：2 次发现、0 次命中 |
| 6 | private 授权上下文隔离 | 通过 | `private_scope_isolation`：2 次发现，scope = `owner-a` / `owner-b` |
| 7 | 分页遍历 | 通过 | `pagination`：3 页、5 个工具、cursors `[null, "2", "4"]` |
| 8 | 未知工具 | 通过 | `unknown_invalid_and_out_of_scope.unknown_tool = rejected` |
| 9 | 非法参数 | 通过 | 同上 `missing_argument = rejected`；另有 schema 失败位置单测 |
| 10 | 越权资源 | 通过 | 同上 `out_of_scope_resource = rejected`，而 `authorised_resource = repo-A:a.md` 放行 |
| 11 | 断连 | 通过 | `disconnect_and_timeout`：2 次重连、2 次调用失败如实上报 |
| 12 | 超时 | 通过 | 同上；写工具超时进入待核对，见第 5 节 |
| 13 | 取消 | 通过 | `cancellation`：回合 `CANCELLED`，远端无写入 |
| 14 | 大结果 | 通过 | `large_result`：40972 → 2048 字节上限，artifact `mcp-results/ctl/big_result-e208a0a973e58655.json`，节选 2000 字符 |
| 15 | 定义变化 | 通过 | `definition_change`：摘要 `88647186…` → `807cbf28…`，返回 `MCP_DEFINITION_CHANGED` |
| 16 | 既有业务工具回归 | 通过 | `business_tool_regression.offered_tools` 含全部原生目标工具；`test_chat_goal_tools.py` + `test_goal_tools.py` 全绿 |
| 17 | 进程关闭 | 通过 | `test_close_releases_child`：`close()` 后子进程退出 |
| 18 | 单个服务不可用不阻断聊天 | 通过 | `unavailable_server`：`registered = 0`，同一回合普通聊天仍 `COMPLETED` |

补充覆盖（方案未点名但属于"必须诚实"的边界）：

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| 不支持的内容块 | 通过 | `unsupported_content`：返回 `MCP_UNSUPPORTED_CONTENT` + `["image"]`，不静默丢弃、不谎报成功 |
| `input_required` | 通过 | 同上：`MCP_INPUT_REQUIRED_UNSUPPORTED`，不自动应答 |
| HTTP 传输配置可用 | 通过 | `http_transport`：配置解析通过，不可达时给出端点与真实原因（`SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING]`），不是空消息 |
| 空目录 | 通过 | `empty_catalogue`：`tools = 0`，不报错 |
| 无工具能力的服务 | 通过 | `protocol_error`：明确报 "does not advertise tool support" |

## 5. 阶段 2：写工具、审批与恢复

方案第 6 节阶段 2 的三条要求，代码与测试都已落地：

1. **本地策略判定副作用，不凭 `readOnlyHint` 授权。** `McpServerConfig.risk` / `default_risk` 决定 `ToolRisk`，远端注解只作为参考。测试 `test_local_risk_policy_governs_and_the_server_hint_does_not` 用一个自称只读的服务验证：本地标成 `WRITE` 就必须走审批。
2. **审批绑定服务配置版本 + 定义摘要 + 资源 + 参数。** `approval_binding()` 产出 `{server_id, config_version, remote_name, definition_digest, protocol_version}`，与 `ApprovalService` 的 `binding_digest` 一起落库。批准后若目录被刷新、定义或凭据变化，执行前 `_stale_approval()` 复验并返回 `MCP_APPROVAL_STALE`，不会用旧审批静默跑新定义。测试 `test_definition_change_between_proposal_and_resume_refuses_the_write` 用真实服务在审批与续接之间改写工具定义。
3. **远端写入超时/断连进入待确认状态。** 复用既有 `ToolReconciliationRequired` 与 `tool_execution_claims`，不新建执行账本。只有配置里用 `receipt_tools` 声明了可验证的只读回执工具时，才允许恢复；否则保持未决，不用本地"尚未完成"推断远端没执行。测试覆盖有回执（恢复成功）与无回执（保持未决）两条路径。

## 6. 测试与回归

MCP 专项：

```
tests/test_mcp_client.py     28 passed
tests/test_mcp_tools.py      35 passed
tests/test_mcp_chat_loop.py  19 passed
tests/test_mcp_acceptance.py 19 passed
```

既有业务工具回归：`test_chat_goal_tools.py` + `test_goal_tools.py` 全绿。

原批次报告全量回归 `pytest tests/ -q --ignore=tests/integration`：**1486 passed, 20 failed**。下表保留当时的原因分类，但没有 MCP 改动前后在同环境下的对照结果，不能把“全部与本方案无关”视为已证明；2026-09-21 核对时 `costs.py` 无工作区修改，故也不能继续称为“用户未提交改动导致”。

| 失败集合 | 数量 | 原因 |
| --- | --- | --- |
| `test_cost_control.py` | 10 | 原报告定位到价格快照链路；是否既有失败需基线对照 |
| `test_m5_release_gates.py` | 2 | 同上（预算门） |
| `test_real_evaluation.py` | 2 | 同上（评审预算） |
| `test_evaluation_api.py` | 1 | 同上 |
| `test_plan_security.py` | 3 | Windows 符号链接用例，属既有失败 |
| `test_tavily_search.py` | 1 | 环境性：`MockTransport` 提供结果后，`validate_public_url` 需真实 DNS 解析 `sqlite.org`，本机解析不到，全部结果被拒 |
| `test_golden_journey.py` | 1 | 同一根因（该旅程走 tavily 研究路径）；该文件不 import 任何 mcp 模块 |

原报告将后两项定位到 tavily 的 DNS 校验。测试文件未直接 import MCP 不能排除 startup/runtime 等间接依赖的影响；要认定为无关回归，仍需同环境下的对照或独立失败复现证据。本报告不将全量套件标为通过。

## 7. 阶段 3 评估：暂不引入 Tool Search

方案的触发条件是"工具规模足够大"。当前实测规模：

- 单个服务注册 2–5 个工具（参考服务 2–3 个，受控服务分页场景 5 个）。
- `enabled_tools` 白名单是当前的控制手段，示例配置里两个服务共注册 3 个工具。

在这个规模下引入 Tool Search 只会增加一层间接，不会减少上下文。已做的两件事足够：

1. `McpToolAdapter.sync()` 在**没有白名单且注册超过 10 个工具**时打 `logger.warning`，并在返回值里给出 `whitelisted` 字段，让"全目录灌进上下文"这件事在运维面上可见。
2. 模型可见集合与执行授权保持分离：`conversation_tool_allowance` 决定模型能看见什么，`ChatToolRunner` 在每次执行时重新校验 allowance。搜索命中不会自动获得执行权限——这条约束已经在代码结构里，将来加搜索时不需要改。

触发重新评估的条件：任一服务的工具数进入几十量级，或已注册的 MCP 工具 schema 总量在消息预算里占比可观测地上升。

## 8. 复现

```bash
# MCP 专项
cd backend
D:/pycharm/python.exe -m pytest tests/test_mcp_client.py tests/test_mcp_tools.py \
  tests/test_mcp_chat_loop.py tests/test_mcp_acceptance.py -q -p no:randomly

# 重新生成证据
MCP_ACCEPTANCE_EVIDENCE=../docs/acceptance/mcp-phase1-evidence-2026-09-20.json \
  D:/pycharm/python.exe -m pytest tests/test_mcp_acceptance.py -q -p no:randomly

# 验证 .env.example 里的示例配置真的能用
D:/pycharm/python.exe backend/scripts/mcp_env_sample_acceptance.py
```

最后一条的实测输出（2026-09-20）：

```
documented example: 2 servers -> ['time', 'fetch']
  time: registered=2 whitelisted=True rejected={} protocol=2025-11-25
  fetch: registered=1 whitelisted=True rejected={} protocol=2025-11-25
registered tools: ['mcp__fetch__fetch', 'mcp__time__convert_time', 'mcp__time__get_current_time']
time call ok: True | { "timezone": "Asia/Shanghai", "datetime": "2026-09-20T23:39:01+08:00", ... }
out-of-scope fetch refused: 参数 url 的值不在本地授权的资源范围内
in-scope fetch ok: True | Contents of https://example.com/: Example Domain ...
```

## 9. 已知限制

1. **原批次未做真实模型端到端。** 2026-09-21 已找到既有 DeepSeek 凭据；真实模型补测单独出具报告，原批次不冒充真实模型结果。
2. **spawn 失败的原因仍可能不具体。** SDK 把"进程起来后立刻退出"（如 `python -c "sys.exit(3)"`）封装成 `McpError: Connection closed`，看不到退出码。已缓解两处：失败消息现在带上尝试启动的命令行（`test_a_failed_server_names_the_command_it_tried_to_start`），运维至少知道是哪个服务、哪条命令；异常展平会沿 `__cause__` 链找到真实原因，HTTP 失败从 `ConnectError: ConnectError('')` 变成 `SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING]`。要拿到子进程退出码需要在 SDK 之外包一层进程监视，当前不做。
3. **一个服务配置非法会拖垮整组** —— 已修。原先 `parse_server_configs` 遇到第一个坏条目就抛错，`load_manager_from_env` 把整组降级成"无服务器"：B 服务的一个 typo 会让 A 服务的工具全部消失，而运维以为两个都在跑。现在按条目降级，坏条目连索引和原因一起进 `manager.config_rejections`，好条目照常注册（`test_one_broken_entry_does_not_disable_the_other_servers`）。仍然是 fail-closed：被丢的条目不会半注册。
4. **凭据可能从 url 或 args 漏进事件** —— 已修。`public_view()` 声称"无凭据值"，却原样返回 `url` 和 `args`；`https://user:token@host/mcp?api_key=...` 或 `--api-key=xxx` 都会进事件正文，与方案第 4 节"凭据不进入事件正文"冲突。现在 URL 去掉 userinfo/query/fragment，敏感 flag 的值替换为 `<redacted>`（flag 名保留，命令仍可诊断）。测试 `test_a_credential_outside_env_indirection_is_scrubbed_from_the_public_view`。
5. **`resource_allow` 是精确值匹配，不是通配。** 写 `https://example.com/*` 不会匹配任何 URL。已在 `.env.example` 的示例里用精确值。若将来需要前缀匹配，需要显式实现并加测试，不能靠运维猜。
