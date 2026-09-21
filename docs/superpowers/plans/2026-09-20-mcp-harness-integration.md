# Better MCP 接入与 Harness 渐进开发方案

日期：2026-09-20。状态：**阶段 1 与阶段 2 已实施，确定性 MCP/Harness 验收已有记录；真实模型补测独立记录**（原验收报告 `docs/acceptance/mcp-phase1-acceptance-2026-09-20.md`，证据 `docs/acceptance/mcp-phase1-evidence-2026-09-20.json`）；阶段 3 经评估暂不引入，见第 9 节。协议与 SDK 版本以第 2.1 节实测值及兼容边界为准。

2026-09-21：隔离生产 worker 的真实 DeepSeek + 真实 stdio MCP 5/5 轮通过，专项重跑 101 passed。见 `docs/acceptance/mcp-live-model-acceptance-2026-09-21.md`；未将此结果扩展为远程 HTTP、新版无状态协议或生产部署验收。

## 1. 已确定的架构边界

MCP 负责外部能力的标准化接入；Better 的 Harness 掌握 Tool Registry、权限、审批、预算、Context、Recovery 和 Observability。Better 首先作为 MCP Client，不要求把现有业务工具改造成 MCP Server。

执行顺序：

```text
LLM 提出 Tool Call
  → ChatToolRunner（聊天入口；任务入口复用同一工具执行边界）
  → Tool Registry：解析工具身份与 metadata
  → 参数校验、目标资源解析
  → Policy / Permission / Approval / Budget
  → Tool Executor
      ├─ Native handler
      └─ MCP Adapter → MCP Server
  → 统一 ToolResult
  → Event Store / Context
  → LLM
```

Registry 先解析工具，再进行风险与授权判断。工具来源、服务实例及定义版本由注册记录决定；资源范围由校验后的参数结合服务端配置解析，例如 repo-A，不能把模型提供的 owner 或 approved 字段作为可信授权。工具风险可随动作、资源和参数变化。

上述分层描述职责边界，不要求第一阶段新增独立 Policy/Executor 框架。复用现有 ToolRegistry.authorize、execute_async、ApprovalService 和 ChatToolRunner；在现有路径内补齐 MCP metadata 和适配器即可。

## 2. 协议与 SDK 基线

已读取正式发布说明、规范和 Python SDK 官方文档：

- MCP 2026-07-28 已正式发布，协议核心为 stateless；新版移除 initialize/initialized 交换及 Mcp-Session-Id。不能为新版强加旧式初始化握手。
- Python SDK v2 是当前稳定发布线，支持 2026-07-28 和此前版本。实施时选择并锁定一个实测通过的具体 2.x 版本，不以 main 分支作为部署依赖。
- MCP Client Manager 管理连接、协议版本选择/兼容处理、能力发现、工具发现、调用、超时、重连以及客户端资源生命周期。
- 新版远程调用不依赖隐藏的长生命周期 session 保存业务状态。业务需要跨调用状态时，用显式 handle 等方式传递；调用关联、审批和恢复状态仍由 Better 持久化。
- 如首个实际服务仍使用旧协议，由 SDK 的兼容路径处理其初始化与 session，不能据此让 Better 业务状态依赖该 session。验收记录实际 SDK 与协议版本。
- 本地子进程型服务使用 stdio；独立部署/远程服务使用 Streamable HTTP。第一阶段只实现所选服务需要的一种传输。

协议无状态不等于客户端不能复用连接池，也不意味着业务操作天然幂等。

### 2.1 实施记录：与上述基线的偏差（2026-09-20 实测）

本节原始描述基于对发布说明的阅读，**与本机实际可安装的 SDK 不一致**。实施时以实测为准并锁定：

| 项目 | 本节原始声称 | 实测结果 | 处理 |
| --- | --- | --- | --- |
| Python SDK 发布线 | v2 | 本机安装并锁定 `mcp==1.29.0`；此安装结果不证明其他发布线不可获取 | 按 1.29.0 实施并锁定 |
| 协议版本 | 2026-07-28 | `LATEST_PROTOCOL_VERSION = "2025-11-25"` | 按 2025-11-25 实施 |
| initialize 握手 | 已移除 | 仍然存在，SDK 会话按标准握手初始化 | 正常握手，未强行跳过 |
| `Mcp-Session-Id` | 已移除 | Streamable HTTP 传输仍使用 | 交给 SDK 管理，业务状态不依赖它 |
| 缓存提示语义 | 顶层 `ttlMs` / `cacheScope` | 当前从 `_meta` 扩展读取；缺失时客户端默认 private/立即陈旧 | 区分扩展约定与新版标准字段 |

结论：**当前实现验收基线为 SDK 1.29.0 / 协议 2025-11-25**。受控服务通过 `_meta` 注入缓存和续接提示；这不能证明新版顶层字段或 MRTR 已兼容。当前实现显式创建 ClientSession 并 initialize，升级到 v2 时必须核对生命周期、字段映射并重跑测试。第 3 节保留目标缓存规则，不将其等同于当前实现已完整支持新版协议。

参考服务 `mcp-server-time` / `mcp-server-fetch` 的握手结果：`server_info = {"name": "mcp-time", "version": "1.29.0"}`，协议 `2025-11-25`。

## 3. 工具发现、缓存与注册

```text
配置并启用 MCP Server
  → 按目标协议完成所需的能力发现
  → tools/list（遍历分页）
  → 校验定义、生成稳定名称及定义摘要
  → 按本地策略生成 ToolSpec
  → 缓存目录并注册
  → 多轮复用目录；仅把本轮启用工具的 schema 发给模型
  → 到期后下一次需要时 / 收到变更通知 / 手动刷新
  → 重新发现并更新目录
```

缓存规则：

- 使用规范中的 ttlMs / cacheScope；TTL 是新鲜度提示，不是轮询间隔，也不是工具定义不会变化的保证。
- 正 TTL 的结果在有效期内复用；ttlMs=0、负值或缺失按立即陈旧处理。第一版不额外延长有效期；缺少缓存提示的旧服务可能需要更频繁发现，不能承诺所有服务都无需重复 tools/list。
- 缓存键包含服务配置版本、协议版本、方法及影响结果的参数（含分页 cursor）。private 必须按授权上下文隔离，凭据变更不得复用旧授权上下文缓存。第一版 public 也可按身份隔离，不必实现跨用户共享缓存。
- 分页按各页 TTL 管理；cursor 失效时废弃旧分页并从头读取。一次目录刷新完成后整体替换当前目录，避免半更新的 Registry；不宣称远端分页具有原子快照一致性。
- 收到所支持的工具变更通知时立即使缓存失效。工具不存在等错误可触发重新发现，但刷新目录不等于授权重试原来的写调用。
- resultType=input_required，以及携带 inputResponses/requestState 的续接请求结果不进入此目录缓存。
- 目录缓存只保存工具定义，不缓存工具执行结果；已有调用回执复用继续遵守原幂等与恢复约束。

每次调用都检查当前权限和启用状态。发送给模型的工具定义记录本地摘要；执行或审批续接时若发现相关定义/配置变化，不使用旧审批静默执行新工具。TTL 不能提供服务端执行语义永不变化的保证。

## 4. 接入现有代码

| 文件 | 开发内容 |
| --- | --- |
| backend/app/mcp_client.py（新增） | 薄封装官方 SDK，负责客户端资源、发现缓存及远端调用；优先复用 SDK 已有能力 |
| backend/app/mcp_tools.py（新增） | MCP 工具定义到 ToolSpec 的映射、参数校验、风险策略、结果转换 |
| backend/app/tools.py | 最小增补 source/server/remote_name/definition_digest 等 metadata；复用 authorize 和 execute_async，不另建执行账本 |
| backend/app/chat_tools.py | 将固定 GOAL_TOOL_NAMES 限制扩展为原业务工具与当前会话显式启用的 MCP 工具；保留既有 Skill 权限交集 |
| backend/app/runtime.py | 同步修改 conversation_tool_allowance，统一得到实际允许调用的工具集合 |
| backend/app/startup.py 与实际应用生命周期入口 | 加载服务配置、构造适配器、关闭客户端资源；单个可选 MCP 服务不可用不阻断普通聊天 |
| backend/pyproject.toml 与依赖锁定文件 | 加入实测通过的 SDK 版本及成熟 JSON Schema 校验库 |

ToolSpec 身份至少能定位 source=native/mcp、稳定 server_id、远端工具名及定义摘要。模型可见名称通过确定性映射满足当前模型供应商的命名约束，并检测冲突。

第一阶段配置只需包含服务标识、传输地址或进程参数、凭据环境变量引用、启用工具白名单、本地风险策略、超时和结果大小限制。凭据不进入模型 schema、prompt 或事件正文。

现有 _validate_schema 只做部分基础检查，MCP 参数使用支持目标 JSON Schema 方言的成熟校验器。保留 $defs/$ref 等语义；禁止校验器自动联网获取不受控引用。模型供应商不支持的 schema 应明确拒绝该工具或做有测试支撑的等价转换，不能静默删约束。

## 5. 结果与上下文

- 映射文本、structuredContent、isError 和协议错误到既有 ToolResult；保留工具调用 ID 与结果对应关系。
- 第一阶段优先支持文本/结构化只读结果；不支持的内容块明确返回能力限制，不悄悄丢弃并声称成功。
- 结果有独立大小上限；过长内容保存为 artifact，返回截断标记、可用节选和引用。若首版提供继续读取能力，沿用同一资源权限边界。
- 每次后续模型调用仍使用目标 profile 的 counter 对消息和本轮 schema 整体计量。
- 目录可以缓存全部已发现定义，但模型上下文只包含会话启用且允许的少量工具，禁止将全目录常驻注入。
- 第一阶段不启用 MCP sampling、MRTR elicitation 或 Tasks 等额外能力。遇到 input_required 返回明确的未支持状态，不自动应答、不把它当作成功结果。需要时另接入 Better 的持久化暂停/续接机制。

## 6. 开发阶段与验收

### 阶段 1：一个服务、少量只读工具、完整 Loop

1. 选定实际有用的 MCP Server、2–3 个只读工具及其传输方式，记录协议版本和 schema 样例。
2. 固定 SDK 版本，完成配置、发现缓存和 ToolSpec 注册。
3. 打通聊天入口的工具允许集合、参数及资源权限校验、执行和结果回填。
4. 接入事件与上下文预算，验证取消、超时、大结果和进程关闭。
5. 使用真实模型与真实 MCP Server 完成端到端验收；不能用 mock MCP 结果代替真实调用证据。

必须覆盖：正常多轮调用；有效 TTL 内不重复发现；TTL 到期和变更失效；private 授权上下文隔离；分页；未知工具；非法参数；越权资源；断连；超时/取消；大结果；定义变化；既有业务工具回归。真实验收报告记录服务、协议、模型、tool_call_id、结果和失败处理，目录缓存行为用可计数日志或测试验证。

阶段 1 已包含权限与只读风险策略；阶段 2 扩展的是写入所需的审批与恢复，不是届时才增加权限。

### 阶段 2：写工具、审批、未知结果与恢复

- 本地策略判定副作用，不仅凭 readOnlyHint 自动授权。
- 审批绑定服务配置版本、工具定义摘要、资源及参数；身份由 Harness 注入。
- 远端写入超时/断连可能已生效，应进入结果待确认状态。只有服务提供可验证幂等或查询回执时，才能自动恢复或重试。
- 复用现有 ToolReconciliationRequired 及调用回执语义；外部状态恢复依据远端证据，不用本地“尚未完成”推断远端没有执行。

### 阶段 3：更多工具、搜索与按需加载

工具规模足够大后再增加 Tool Search 和 Lazy Loading。发现目录、模型上下文可见集合、执行授权保持三个独立概念；搜索命中不会自动获得执行权限。

## 7. 后续实施的待定项

正式接入的具体目标服务（部署地址/进程、凭据来源、允许资源和工具名单）由运维在 `BETTER_AGENT_MCP_SERVERS` 中指定，字段说明与可直接粘贴的示例见 `.env.example`；`backend/scripts/mcp_env_sample_acceptance.py` 会取出该示例原样跑通，用来证明文档没有说谎。公共适配层已完成（第 9 节），受控测试服务为 `backend/tests/mcp_test_server.py`。

## 8. 官方依据（2026-09-20 已读取）

- 正式发布说明：https://blog.modelcontextprotocol.io/posts/2026-07-28/
- 候选发布说明（背景，正式规则以前者与规范为准）：https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- 工具规范：https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- 缓存规范：https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching
- Python SDK 稳定版本说明：https://github.com/modelcontextprotocol/python-sdk
- Python Client 文档：https://py.sdk.modelcontextprotocol.io/client/

## 9. 实施状态（2026-09-20）

完整验收记录见 `docs/acceptance/mcp-phase1-acceptance-2026-09-20.md`，机器可读证据见 `docs/acceptance/mcp-phase1-evidence-2026-09-20.json`。要点：

- **阶段 1**：14 项验收要求全部通过，全部为真实 MCP 服务调用（官方 `mcp-server-time` / `mcp-server-fetch` 与 `backend/tests/mcp_test_server.py`），无 mock 替代。`backend/tests/test_mcp_{client,tools,chat_loop,acceptance}.py` 共 101 项测试。
- **阶段 2**：写工具、审批绑定（配置版本 + 定义摘要 + 资源 + 参数）、远端写入待核对状态与回执恢复均已实施并覆盖。放在本阶段而非后置，是因为审批与 `ToolReconciliationRequired` 复用既有 Harness 路径，先只读后写反而会先引入不安全的写路径。
- **阶段 3**：暂不引入 Tool Search / Lazy Loading。当前实测规模（单服务 2–5 个工具、示例配置共 3 个）不足以摊薄这一层间接。已做的是：`enabled_tools` 白名单作为纪律，`sync()` 在无白名单且注册 >10 个工具时告警，并在返回值里给出 `whitelisted`。触发重新评估的条件见验收报告第 7 节。
- **未满足项**：第 6 节阶段 1 第 1.5 点要求的"真实模型"端到端未做——本机没有 provider 凭据，模型侧由 scripted gateway 驱动。协议、缓存、权限、审批、恢复、结果处理的结论不依赖它；"真实模型面对这些 schema 的行为"未经实测。

实施中发现并修正的三个问题（都已加测试）：

1. 一个服务配置非法会拖垮整组：原 `parse_server_configs` 遇首个坏条目即抛错，整组降级为"无服务器"，B 的 typo 会让 A 的工具全部消失。改为按条目降级并记录 `config_rejections`，仍 fail-closed（被丢的条目不半注册）。
2. 凭据可能从 `url` 或 `args` 漏进事件正文：`public_view()` 声称"无凭据值"却原样返回二者。现在 URL 去掉 userinfo/query/fragment，敏感 flag 的值替换为 `<redacted>`。
3. 失败原因不可运维：SDK 把 spawn 失败封装成 `McpError: Connection closed`，httpx 的 `ConnectError` 消息为空。现在失败消息带上尝试启动的命令/端点，并沿 `__cause__` 链找到真实原因。
