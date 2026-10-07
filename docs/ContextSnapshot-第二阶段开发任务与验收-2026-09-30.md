# ContextSnapshot 第二阶段开发任务与验收

日期：2026-09-30

代码核对基线：`dcfc54e358bdc8ae92e2ea1a41bf56694ea080f7`

文档状态：待开发。本文是实施要求，任务、接口、字段和测试用例均不表示已经实现或通过。

前置交付：[HarnessExecutionContext 第一阶段](HarnessExecutionContext-第一阶段开发任务与验收-2026-09-30.md)；已完成修复见 [第一阶段复测记录](acceptance/harness-context-phase1/harness-context-phase1-repair-2026-09-30.md)。

## 1. 目标与交付边界

让每次逻辑 LLM 调用在发出网络请求前，持久化一份不可变的最终输入快照；调用记录、实际发送、重试、fallback 均绑定并使用该快照。

两个上下文各司其职：

| 对象 | 回答的问题 | 生命周期 |
|---|---|---|
| HarnessExecutionContext | 谁调用、属于哪个任务、使用哪个预算和运行版本 | 逻辑执行身份；沿任务/调用链派生 span |
| ModelInputSnapshot | 本次逻辑 LLM 调用到底提供了什么输入 | 一次 invocation；所有 attempt 共享 |

代码类型建议命名为 `ModelInputSnapshot`，避免与 `app/context.py` 中现有组装阶段的 `ContextSnapshot` 混淆。本文标题中的 ContextSnapshot 指第二阶段能力。

开发管理正式拆成两个子阶段，分别交付与验收，不在同一个 PR 中完成 2A 和 2B。保留 M1/M2 作为验收门禁编号：

| 开发阶段 | 任务范围 | 验收门禁 | 交付方式 |
|---|---|---|---|
| Phase 2A | P00～P10 | M1 | 最小链路独立 PR；必要时拆成更小、可验证的 PR |
| Phase 2B | P11～P14 | M2 | 2A 合并并通过 M1 后，以独立 PR 逐批扩大入口覆盖 |

- **M1：最小链路完成**。对话 → LLM → Tool → 工具内部 LLM，含审批恢复、重试与 fallback。
- **M2：生产入口全部覆盖**。在 M1 基础上，覆盖 Research、Learning、Judge、摘要、专家及管理端真实模型检测等所有可达生产入口；完成后才可宣称“每次生产 LLM 调用均绑定快照”。

Phase 2A 的优先级依次为：

1. **最终逻辑输入完整冻结**：实际发送、估算和重试使用同一冻结内容，这是核心验收。
2. **关键 Memory / Skill / Tool 来源可追溯**：复用已存在的版本、绑定、工具调用 ID 和 schema 摘要，保留现有授权与撤销检查。
3. **逐步提高 provenance 完整率**：历史/摘要/附件片段定位、完整 dropped 原因等不作为 2A 的前置重构要求。

即使 `provenance.status=partial`，也可以通过 2A 核心验收，前提是最终输入完整、真实且不可变，已声明的资产引用准确，当前授权与撤销检查没有缺口。来源元数据不完整不等于输入内容可以缺失；不能用 partial 掩盖已知来源冲突或授权检查失败。

不在本阶段内：重构 PolicyEngine/TaskRuntime/ToolRegistry，改检索算法，建设通用事件平台或 Trace UI，跨 invocation 内容去重，JEV 预算调整，要求模型输出逐字可复现。其他 Runtime 的输入冻结不等于其执行身份已全面迁移到 HarnessExecutionContext。

## 2. 已核对的代码与缺口

| 代码位置 | 当前能力 | 本阶段要求 |
|---|---|---|
| `backend/app/context.py` | ContextAssembler 返回 blocks/text/hash | 保留组装能力，最终发送快照在网关边界另行冻结 |
| `backend/app/live_model.py` | 对话使用最终 bounded_messages；还有修复与辅助调用 | 收集实际来源，最终组装后冻结；逐次调用正确分配 invocation |
| `backend/app/model_gateway.py` | ModelRequest 为 frozen dataclass，但内部 list/dict 可变；有 direct complete 和协议适配器 | 深度冻结；direct 受控调用同样绑定；协议转换只能读取快照派生副本 |
| `backend/app/model_control.py` | 已计算 request/tool/system 摘要；context_snapshot_digest 默认空；RoutedModelGateway 管理 attempts | 绑定可读取快照，事务一致，retry/fallback 只读冻结输入 |
| `backend/app/agents.py`、`db.py` | agent_context_snapshots 用于任务上下文 | 不复用为单次模型输入；可作为来源引用 |
| `backend/app/learning_assets.py` | freeze_request 保存资产引用，发送前检查撤销 | 复用资产检查；逐步接入明确来源，不另造一套撤销状态 |
| `backend/app/goal_programs.py`、`goal_program_compiler.py` | 工具内部编译继承 harness；可能有模型修复调用 | 首次编译和修复分别绑定输入快照 |
| `backend/app/startup.py` | 组装 RoutedModelGateway 与控制存储 | 生产入口装配必须提供快照存储 |
| `backend/app/model_admin.py` | _verify_live 直接创建无 control_store 的 ModelGateway | M2 接入受控 direct 通道；验证未激活 profile 时不能依赖 stable 路由 |
| `backend/app/eval.py`、`evals.py`、`real_evaluation.py` | 离线评估、模拟及真实模型调用并存 | 区分测试桩、离线脚本和生产可达真实调用，不能按文件名整体排除 |

## 3. 不可变契约

### 3.1 冻结时机与数据流

```text
检索/读取版本 → 组装消息 → 历史裁剪/压缩 → 最终 ModelRequest
                                                    ↓
                                   同步规范化序列化并冻结
                                                    ↓
                      从冻结内容进行容量准入与调用绑定检查
                                                    ↓
                  同事务写入快照、invocation、执行身份关联
                                                    ↓ commit
                         当前资产/授权/预算检查 → 创建 attempt
                                                    ↓
                          从快照构造 attempt 独立请求并发送
```

冻结前不得在网关内先 await 再复制调用方的可变请求。冻结后不得继续将原始 request 用于容量检查、资产识别、计费估算或发送，防止“记录一份、发送另一份”。适配器必须使用从冻结内容派生的副本。

被容量准入拒绝且未发送的请求不要求创建成功的 invocation；需要保留现有拒绝原因。持久化失败时必须零网络请求。

### 3.2 快照 envelope

建议 v1 结构如下；实现时显式定义字段，不允许任意对象自动转字符串进入摘要：

```yaml
schema_version: model-input-snapshot-v1
binding:
  owner_id: ...
  runtime_bundle_id: ...       # 可为 null，但必须与 invocation 精确一致
  role: ...
  purpose: ...
request:
  messages: []                 # 最终消息及其顺序，含 tool call/result
  tools: null                 # 保留 null/[] 原始区别，不做隐式改写
  temperature: null
  max_tokens: null
  thinking: null
  response_format: null
  single_attempt: false       # 调用执行选项，恢复时不能遗漏
provenance:
  status: partial             # 来源确实齐全时才能标 complete
  sources: []
  assembly: null              # 有则包含组装器/计数器版本及裁剪记录
```

`sources` 的完整目标包含 kind、id、版本或 content_digest、是否 included、消息/片段位置及 dropped 原因。Phase 2A 优先记录现有链路已提供的关键 Memory/Skill 版本，以及 Tool 调用 ID、schema 摘要和结果关联；提供的引用必须校验一致。历史、摘要、附件的逐片段位置以及完整 dropped 清单允许暂缺，并明确标记 partial；不为补齐这些元数据重写检索、归档或上下文组装链路。实际发送文本仍须完整保存在 messages 中，不能只有可变路径或资产 ID。

调用源提供来源元数据；网关负责冻结和格式/身份校验，不再在网关发起新检索。M1 对可获取的 Memory/Skill 版本用显式引用；无法可靠关联的片段标记 partial，不能通过正文子串命中宣称完整来源。不要在本阶段重做通用上下文组装器。

来源元数据是冻结时的记录；后续提高 provenance 完整率只影响新快照，不能向旧 envelope 追加来源并改写其摘要。

#### 正式原则：逻辑输入与供应商请求分层

```text
ModelInputSnapshot = provider-independent logical input
Attempt payload    = profile/provider 解析后的真实 HTTP body
```

“最终输入”指业务组装、裁剪完成后的最终逻辑输入，不表示直接存储供应商 HTTP body。协议转换、profile 默认值解析及供应商字段命名发生在 attempt 层。它们只从冻结内容派生，不能反写 snapshot。

例如逻辑快照的 `max_tokens=null`，若某次 OpenAI-compatible attempt 按已固定的 profile/适配器规则解析为 `max_tokens=4096`，该值属于该 attempt；快照仍为 null。这是设计示例，不表示当前所有 OpenAI 适配器都默认发送 4096。fallback 可使用另一 profile 的默认值，但必须有可追溯的版本/解析依据，不能修改消息、工具或快照正文。测试见 G10/G11。

### 3.3 摘要定义

| 字段 | 定义 |
|---|---|
| execution_context_digest | 第一阶段执行身份摘要，保持原定义 |
| context_snapshot_digest | v1 整个 envelope 的规范化 JSON UTF-8 字节 SHA-256 |
| request_digest | 保留现有请求摘要算法与字段集合，避免破坏已有幂等记录；不与新摘要混用 |
| tool_schema_digest / system_prompt_digest | 保留现有用途，与同一冻结请求计算结果一致 |
| attempt provider_payload_digest | 可选记录协议转换后的实际 body 摘要；不含凭据，不等于输入快照摘要 |

规范化：对象键排序、紧凑分隔符、UTF-8、禁止 NaN/Infinity；数组顺序不变；禁止静默裁剪或 Unicode 文本替换；未知 schema 版本拒绝解析。可选字段统一显式表示，规则发布后不得在 v1 内暗改。

消息、工具或来源发生变化时快照摘要随之变化。数据库 ID、created_at 不进入 envelope；两次不同 invocation 使用相同内容时可有相同摘要，但仍保存独立行。

### 3.4 不可变实现方式

以规范化 JSON 字节串作为冻结后的权威内容，或使用等价的深度不可变结构；仅 frozen dataclass 加可变 dict/list 不合格。

建议 API（可调整名字，但必须保持行为）：

```python
freeze_model_input(request, context, provenance) -> ModelInputSnapshot
snapshot.to_request() -> ModelRequest  # 每次返回互不共享的深拷贝
begin_invocation(profile, snapshot, context, route) -> ModelCallHandle
load_model_input_snapshot(owner_id, snapshot_id) -> ModelInputSnapshot
```

业务方不能把任意 snapshot ID 或 digest 当作可信绑定直接传入。若兼容接收预绑定值，必须读取快照、验证 owner/bundle/role/purpose、重算摘要并比较输入，否则拒绝。

### 3.5 持久化

新增 `model_input_snapshots`：

| 字段 | 约束 |
|---|---|
| id | 主键 |
| owner_id | 非空，来自可信 ModelCallContext |
| schema_version | 非空，必须为受支持版本 |
| content_json | 非空，完整 envelope；读取时规范化验证 |
| content_digest | 非空，v1 SHA-256 |
| created_at | 非空 |

`model_invocations` 新增可空 `context_snapshot_id` 外键，复用现有 `context_snapshot_digest`。不将该 ID 放到 turns 上，因为一轮对话可有多个快照。也不在 HarnessExecutionContext 中添加输入摘要，避免改变执行身份协议。

事务规则：

1. 核心硬要求仅为 `ModelInputSnapshot + ModelInvocation + execution context binding` 三者同事务完成；任一核心写入异常全部回滚且零网络请求。
2. snapshot 内容、owner、版本、摘要创建后禁止 UPDATE；以数据库约束/触发器保护不可变字段，SQLite 与 PostgreSQL 行为一致。
3. invocation 已有快照绑定不得换成另一份，也不得清空。即使新快照内容相同也不得改 ID。
4. 保留 invocation 正常 status/attempt 更新；不可变保护不能阻止状态机运行。
5. 新增快照绑定应与 invocation 创建同时完成，不对历史行“首次补绑”；历史行 ID 为空时只能作为历史不完整记录读取。
6. ID/digest 成对一致；复用的 digest 字段可能在旧数据中非空而无 ID，此类只按 legacy 读取，不伪造历史完整快照。
7. JSON.binding 与列值、invocation 的 owner/bundle/role/purpose 严格相等，null 也比较；摘要自洽不能替代关联校验。
8. 幂等键同时验证请求、快照内容与调用身份。相同键相同语义沿用现有 replay/conflict 协议，不重发；不同输入、owner、bundle 或执行身份拒绝。并发冲突转换为明确领域错误，不能泄漏数据库异常。

Learning Asset 不属于必须扩大到的原子事务边界。现有资产写入若天然接受同一连接，可继续同事务完成；否则保留模块边界，不为本阶段强行合并 Learning 事务。两种实现都必须保证资产引用与冻结输入/调用身份校验一致，并保留每次发送前的授权、有效性及撤销检查。

若必需的资产记录或检查在独立步骤中失败，核心三者可以已完整提交，但本次调用必须零网络请求，并按现有失败/恢复语义记录结果；不能留下成功状态，也不能以最终一致性为由先发送再补校验。关闭可选 Learning 服务本身不是失败，不能因此阻止无此依赖的正常快照绑定。

迁移仅新增版本，不修改已发布迁移。PostgreSQL 从 `20260930_0024` 升级；实现前检查是否已有更高版本，避免版本冲突。

### 3.6 重试、恢复与安全边界

| 场景 | 约束 |
|---|---|
| 同 invocation 的网络 retry | 复用快照，每次构造独立请求对象 |
| fallback 切换 profile | 逻辑快照不变；profile/实际参数默认值差异记录在 attempt，不静默改输入 |
| fallback 容量不足 | 保留现有跳过或拒绝行为，不裁剪原快照 |
| 工具结果返回、JSON 修复、重新压缩 | 新 invocation、新快照；同一 purpose 也不代表同一调用 |
| 等待审批后继续 | Tool logical Context/trace/span 保持不变；重新鉴权；新增内部 LLM 使用 child span 和新快照 |
| 资产在冻结后撤销 | 保留快照作为历史事实，但发送前检查应阻止使用；不可自动换成最新版 |
| 进程在持久化后、响应落库前退出 | 沿用现有 UNKNOWN/reconciliation 规则；保存了输入不代表外部调用可安全自动重放 |

实际请求中引用远程 URL 的内容可能变化。M1 保留引用并声明不能复原外部资源字节；要求可复原的附件应使用已有不可变 artifact 及内容摘要，不能宣称仅保存 URL 就完成附件归档。

快照可能包含用户文本，只允许同 owner 的授权读取。普通事件与列表只输出 ID/digest/计数，不输出正文；凭据和 Authorization header 不进入快照。删除与保留沿用已有数据生命周期策略，本文不建立新的无限期保留机制。输入冻结保证审计可还原，不保证 LLM 输出逐字一致。

## 4. 实施顺序与小任务

Phase 2A 实施顺序：`P00 → P01 → P02 → P03 → P04 → P05 → P06 → P07 → P08 → P09 → P10 → M1 验收/合并`。

Phase 2B 实施顺序：`2A 合并 → P11 → P12 → P13 → P14 → M2 验收`。2A PR 不顺带迁移全部 Research/Learning/管理端入口；2B 不以补齐所有来源片段为输入冻结的前置条件。

每项完成必须提交对应测试与证据，不能只将状态改为“完成”。以下验收编号对应第 5 节。

### P00：登记真实调用入口与基线

- 改动位置：文档；只读核对 startup、所有 gateway.complete、ModelGateway 构造和直接 _attempt/HTTP 路径。
- 工作：记录调用方、网关类型、是否生产可达、owner 来源、Phase 2A/2B 归属和 M1/M2 门禁、当前测试。将 admin 模型检测单列；测试桩/离线脚本也明确标记。2A 清单必须登记 2B 入口，但不要求提前完成迁移。
- 交付：`docs/acceptance/context-snapshot-phase2/callsite-inventory.md`，以及第一阶段专项测试基线。
- 验收：所有真实调用入口有归属；不能只搜索一个网关类即宣称覆盖。对应 C01、R01。

### P01：实现冻结对象与规范化

- 新增建议文件：`backend/app/model_input_snapshot.py`。
- 工作：定义 envelope、版本、序列化、解析、摘要、独立请求副本；覆盖 ModelRequest 所有字段，拒绝不支持类型和未知版本。
- 交付：无数据库依赖的快照类型和单测。
- 验收：U01～U06；原对象和副本的嵌套修改均不能改变权威内容。

### P02：增加数据库迁移与不可变约束

- 改动：`db.py`、新增 Alembic 迁移、schema head。
- 工作：新增快照表及 invocation 外键、索引；禁止内容及绑定被覆盖；保持旧行可读取、invocation 状态可更新。
- 交付：SQLite/PostgreSQL 等价迁移，明确回滚策略；有数据时不得通过 downgrade 默默删除审计信息。
- 验收：P01～P03、P07；从旧版本和空库均可升级。

### P03：实现持久化与一致性检查

- 新增建议文件：`backend/app/model_input_snapshot_store.py`。
- 工作：提供使用调用方事务的 insert/bind/load API；加载重算摘要，验证 owner 与列/JSON 一致；只读查询不返回其他 owner 的记录。
- 交付：存储服务与领域错误，不在存储内部另开独立提交事务。
- 验收：P04～P08；摘要自洽但身份冲突仍拒绝；并发不能换绑。

### P04：改造 begin_invocation 的事务边界

- 改动：`model_control.py` 中 ModelCallContext/Handle、begin_invocation。
- 工作：增加快照 ID 的调用级关联；由网关绑定摘要，拒绝伪造预绑定值；所有派生摘要从冻结内容计算；快照、invocation、执行身份绑定三者同事务写入。Learning Asset 按第 3.5 节接入，不要求改造为强耦合事务。
- 工作：加强幂等比较。新调用派生时清理父调用输入绑定；不要清理同 invocation 的重试绑定，不凭 role/purpose 字符串推断所有调用边界。
- 验收：U07、P05、P06、I04；持久化失败不得出现网络请求，幂等重放不得重发。

### P05：接入 RoutedModelGateway

- 改动：`model_control.py` 的 complete、attempt 执行和请求估算路径。
- 工作：同步冻结后再准入；实际发送、资产检查和估算只消费冻结内容；每个 attempt 独立反序列化；保留现有路由、预算、取消、输出重置和错误语义。
- 验收：G01～G05、G07～G09；保存内容与 mock transport 收到的逻辑输入一致。

### P06：接入 direct ModelGateway 与协议转换

- 改动：`model_gateway.py` 的受控 complete、provider payload 构造；共享 P05 的冻结代码，不复制两份规则。
- 工作：区分 direct 顶层调用和 routed 内部 _attempt；内部 attempt 不创建第二个 invocation/snapshot。低层无持久化测试工具必须显式区分，不作为生产旁路。
- 验收：G06、G10、G11；OpenAI-compatible/Anthropic/Gemini 适配从同一冻结逻辑输入生成请求，默认参数差异有 profile 依据。

### P07：接入对话最终输入及来源

- 改动：`live_model.py`、必要的 `conversation.py` 调用参数。
- 工作：先完整冻结 bounded_messages、工具 schema、tool result 和参数，再接入现有关键 Memory/Skill/Tool 来源。历史/摘要/附件的精细定位和完整 dropped 原因可后续补齐；缺失标 partial，已知裁剪掉的内容不得标 included。
- 工作：每轮工具循环、澄清、修复和压缩后的再次请求都有独立 invocation；修改组装结果不能覆盖之前快照。
- 验收：I01～I05、U08/U09；任一 invocation 能读取与最终发送一致的逻辑输入；partial provenance 不影响核心冻结通过验收。

### P08：接入工具内部编译及审批恢复

- 改动：`goal_programs.py`、`goal_program_compiler.py`、必要的 chat_tools 透传。
- 工作：工具内部编译继承第一阶段身份，建立自身输入快照；编译器 JSON 修复建立新调用。审批恢复保留原 Tool 身份，同时做当前授权检查。
- 验收：I06、A01～A04；stable 切换不改变已固定 bundle；审批恢复不能沿用别的 LLM 快照。

### P09：对接已有学习资产冻结与撤销

- 改动：`learning_assets.py` 及来源提供方。
- 工作：将现有资产记录与本次 input snapshot 关联或可追溯，验证引用一致；天然能同事务则沿用，否则按独立步骤完成所需校验，不重构 Learning 事务。attempt 前沿用当前撤销检查；显式来源优先，partial 来源不得标 complete。
- 工作：不借此改 JEV 预算、学习算法或扩大新资产授权；冻结后撤销应明确失败，不自行替换内容。
- 验收：A05、A06、A08、I05；快照不可变与当前授权检查可以同时成立；资产步骤独立失败时不得发送，也不要求撤销已原子提交的核心三者。

### P10：最小链路验收与读取能力

- 改动：存储查询、已有授权审计入口及验收脚本；如复用 control_exports，默认只导出 ID/digest。
- 工作：提供按 owner/invocation 查询快照的后端方法，不建设新 UI；明确 legacy/missing/integrity_error 的区分，不能把损坏记录降级成 legacy。
- 工作：完成 Phase 2A / M1 全套测试，将命令、结果、数据库隔离信息和已知缺口落盘，以独立 PR 交付。
- 验收：A07、M1 门禁。完成此项仅可宣称 Phase 2A 最小链路完成，不因 2B 尚未迁移或来源精细度未全覆盖而阻塞该交付。

### P11：迁移 Research 与专家调用

- 改动：`research/live.py`、`agents.py` 及 P00 找出的对应调用方。
- 工作：每次规划、研究写作、审查/专家调用绑定最终输入；agent_context_snapshots 保持任务快照职责，仅作为来源，不能当作模型输入复用。
- 验收：C02；每类调用至少一条真实业务链路配 mock transport 的集成用例，检查 owner/role/purpose、内容和 attempt 关联。

### P12：迁移学习、Judge、摘要及辅助调用

- 改动：learning_agent/extraction/eval/prompt/replay、evolution、memory_archive/reference、live_model 辅助调用等，以 P00 清单为准。
- 工作：为非对话调用补齐可信调用上下文与快照；无 harness 的遗留 Runtime 可使用明确 owner 的 ModelCallContext，不伪造第一阶段身份。生产调用不能静默退回 local-user。
- 验收：C03；分别验证学习生成、Judge、回放、摘要/引用解析等类别，不用一个普通 complete 测试替代业务入口覆盖。

### P13：收口管理检测和无存储旁路

- 改动：`model_admin.py`、startup 及入口清单中的其他生产旁路。
- 工作：管理端模型验证使用授权 owner 和待验证 profile 的受控 direct 调用，不要求 profile 已被 stable 激活；所有生产可达真实发送必须经过快照绑定。
- 工作：生产装配缺少存储时启动或调用明确失败；离线/测试模式显式配置且不由用户 HTTP 参数控制。禁止“存储失败继续发送”兼容分支。
- 验收：C01、C04～C06；不能因关闭可选 learning 服务而关闭输入快照。

### P14：全入口验收与交付

- 工作：复核清单中每一行的自动化用例；跑第二阶段专项、第一阶段及相关网关/业务回归、隔离 PostgreSQL 集成测试，记录全部命令和退出码。
- 交付：接入说明、验收报告、结构化 evidence JSON、日志、迁移说明；保留失败轮次及修复说明，不覆写成只有成功结果。
- 验收：M2 门禁；报告区分“全输入已冻结”和“来源完整率”，不得把 partial 统计为完整来源。

## 5. 自动化测试用例

以下每个 ID 至少对应一个可独立定位的测试；参数化产生的数量另行报告，不把本表项数当作 pytest 实际数量。测试名建议包含 ID。

Phase 2A 执行 U/P/G/I/A、R01，并完成 C01 的全入口登记及 2A 范围核对；Phase 2B 补齐 C01 的全生产动态覆盖、C02～C06、R02，并重跑相关 2A 回归。任务编号 Pxx 与本节持久化用例 Pxx 属于不同编号域，验收记录必须注明“任务”或“测试”。

### 5.1 单元测试 U

建议文件：`tests/test_model_input_snapshot.py`。

| ID | 输入/操作 | 必须断言 |
|---|---|---|
| U01 | 相同数据调整对象 key 顺序后冻结 | canonical bytes 和 digest 相同；数组顺序改变则不同 |
| U02 | 参数化修改消息、工具嵌套 schema、response_format、来源版本、single_attempt | 对应快照摘要改变，其他快照不变 |
| U03 | 冻结后修改原 request 的消息和嵌套工具参数 | snapshot.to_request 仍为原始内容 |
| U04 | 修改第一次 to_request 的嵌套对象 | 第二次 to_request 和摘要不变，两个副本无共享可变子对象 |
| U05 | 未知版本、缺字段、非法类型、NaN/Infinity | 明确拒绝，不能 str(object) 或静默修正 |
| U06 | 中文、换行、空字符串、null/[]、tool_call_id 等往返序列化 | 内容和顺序无损，全部 ModelRequest 字段有往返覆盖 |
| U07 | 使用不匹配的预绑定 ID/digest；新调用继承父快照 ID | 拒绝前者；后者必须清除并重新冻结；正常 retry 仍保留原绑定 |
| U08 | 组装候选含 A/B Memory，只把 A 放入最终输入 | A included，B dropped；未知来源标 partial，不伪造 complete |
| U09 | 最终输入完整，但缺历史/摘要/附件片段位置或部分 dropped 元数据，标记 partial | 冻结、持久化、发送正常；实际逻辑输入与快照一致；已有关键引用仍受校验；不能将状态伪报 complete |

### 5.2 持久化与迁移 P

建议文件：`tests/integration/test_model_input_snapshot_postgres.py`；相关纯存储规则亦在 SQLite 验证。

| ID | 输入/操作 | 必须断言 |
|---|---|---|
| P01 | 带历史 invocation 的 0024 数据库升级 | 历史记录保留；快照 ID 为空且标 legacy；已有 digest 不被伪造回填 |
| P02 | 空 PostgreSQL 数据库从头迁移，SQLite 初始化 | head、列、索引和约束齐全，升级不修改历史迁移 |
| P03 | 正常事务创建快照+invocation，随后更新 invocation 状态 | 绑定完整，外键有效，状态更新成功 |
| P04 | 修改 JSON/digest 任意一方；或构造摘要正确但 owner/bundle/role 冲突的 envelope | 写入/读取拒绝；损坏不能作为 legacy 返回 |
| P05 | 分别注入快照、invocation、execution context binding 写入异常 | 核心三者整个事务回滚，无半条关联记录，transport 调用次数为 0；独立资产步骤失败另见 A08 |
| P06 | 两个事务用相同幂等键并发；分别为相同及不同输入/身份 | 最多创建一个 invocation，败者为 replay/conflict；不同输入不能覆盖，测试用屏障确保真实重叠 |
| P07 | 直接 SQL 修改快照内容、owner、digest 或替换/清空已有绑定 | 数据库约束拒绝；不能仅由 Python 属性 frozen 保证 |
| P08 | 相同输入创建两次不同 invocation；另一 owner 读取 | 两份独立快照允许同 digest；跨 owner 不可读且错误不泄漏正文 |

### 5.3 网关及协议 G

建议文件：`tests/test_snapshot_gateway.py`，使用 mock transport 捕获最终 body；不调用付费模型。

| ID | 输入/操作 | 必须断言 |
|---|---|---|
| G01 | Routed 正常单次请求 | 网络开始前事务已提交，invocation ID/digest 非空，发送与冻结输入一致 |
| G02 | freeze 后用事件屏障暂停，外部修改原始 messages/tools，再继续 | 估算、持久化、发送均使用冻结旧值 |
| G03 | 第一次 attempt 暂时失败，修改该 attempt 请求对象，再 retry | 两次共享快照 ID/digest；第二次输入未被污染 |
| G04 | primary 暂时失败后切换 fallback | 一个 invocation/快照，多个 attempt/profile；逻辑输入不变 |
| G05 | fallback 容量较小 | 明确跳过或失败；不截短快照内容，不产生隐藏的新输入 |
| G06 | direct gateway 正常调用及 routed 内部 _attempt | direct 有一次绑定；routed 不重复写两份调用/快照 |
| G07 | 持久化失败或绑定冲突 | 零网络请求，不进入 fallback 规避一致性错误 |
| G08 | 取消发生于创建前或提交后发送前 | 零网络请求，已有 invocation 按既有取消协议结束，无悬挂成功状态 |
| G09 | 先输出部分 token 再失败 | 沿用现有禁止不安全 fallback/输出重置规则；输入快照始终不变 |
| G10 | 参数化 OpenAI-compatible/Anthropic/Gemini 协议 | 实际 body 等于各适配器从冻结输入生成的 body；系统消息移动等转换可解释 |
| G11 | request.max_tokens=null 且各 profile 默认不同 | 原始快照仍为 null；实际参数按对应 profile/协议解析，可由 attempt 还原，不回写快照 |

### 5.4 最小链路 I

建议文件：`tests/test_snapshot_flow.py`。

| ID | 场景 | 必须断言 |
|---|---|---|
| I01 | 用户对话 → 首次 LLM | 保存最终裁剪后的 messages、tools、参数；组装前未发送内容不冒充最终输入 |
| I02 | 首次 LLM → 两个工具 → 下一次 LLM | 两次 invocation 分别绑定快照；后一份含实际 tool result，前一份不变 |
| I03 | 同 purpose 连续两次 LLM，输入不同 | 新 invocation/快照，不能因 purpose 相同复用旧输入 |
| I04 | JSON 修复或 context overflow 后重新压缩再调用 | 修改输入必须建立新 invocation；同幂等键直接换输入明确冲突 |
| I05 | Memory/Skill 更新后下一轮对话 | 旧快照保留原内容/版本；新快照按本轮已授权选择记录，不自动改旧行 |
| I06 | Tool → 编译 LLM → 修复 LLM，中途切换 stable/学习策略 | bundle 和预算继承第一阶段规则；不同输入对应独立调用快照及正确 span |

### 5.5 恢复、授权与审计 A

建议文件：`tests/test_snapshot_recovery.py`。

| ID | 场景 | 必须断言 |
|---|---|---|
| A01 | WRITE 等待审批，重启后批准并新增内部 LLM | 原 Tool Context/trace/span 不变，内部 LLM 新 child span、新快照 |
| A02 | 等待审批期间撤销工具权限 | 恢复重新鉴权，工具及内部 LLM 均不执行；旧快照不代表旧授权仍有效 |
| A03 | invocation 已提交、响应未落库时模拟进程退出 | 输入可读取；沿用 UNKNOWN/reconciliation，不以“有快照”为由自动重发副作用 |
| A04 | 读取旧的无快照 invocation 与旧的无 Context 审批记录 | 前者只标 legacy；后者保留第一阶段拒绝恢复策略，不能凭新快照补造身份 |
| A05 | 冻结后、发送前撤销已纳入的 Memory/Skill/Prompt | 现有撤销检查拒绝调用，快照未被替换；不调整 JEV 预算逻辑 |
| A06 | 第一次 attempt 失败后、第二次前撤销资产 | 第二次发送前拒绝，不能因首次通过就跳过复查 |
| A07 | 同 owner 查询、跨 owner 查询、普通事件/导出、凭据配置 | 正确授权；事件只含引用/摘要；配置中的 API Key 和认证 header 不进入快照或日志 |
| A08 | 资产引用冲突或必需资产步骤失败；按实现覆盖天然同事务或独立步骤 | 两种模式均零网络请求，不能跳过校验；同事务失败全部回滚；独立步骤失败允许核心三者完整保留，但调用必须有失败/恢复状态，不能伪报成功；不为测试强制引入第二种事务实现 |

### 5.6 全入口覆盖与回归 C/R

| ID | 场景 | 必须断言 |
|---|---|---|
| C01 | 核对全部真实网络入口与 P00 清单 | 每项有接入状态和测试；静态搜索仅辅助，不能代替动态测试 |
| C02 | Research 与各专家角色代表性业务调用 | 所有实际 invocation 均绑定快照，任务快照与模型输入快照不混用 |
| C03 | 学习生成、Judge、回放、摘要、引用解析等 | 每类有动态覆盖；owner/role/purpose 正确，生成与 Judge 的输入各自独立 |
| C04 | 管理员验证一个未激活 profile | 用该 profile 的受控 direct 调用，有可信 owner 和快照，不错误转入 stable |
| C05 | 生产入口缺少快照存储或尝试无存储 gateway | 明确失败且零外部调用；离线测试模式不能由用户参数开启 |
| C06 | 关闭可选 learning 功能后正常生产调用 | 仍然有输入快照，不依赖学习服务才能保存 |
| R01 | 第一阶段 Context 专项、网关路由、对话、工具审批及编译回归 | 无新增失败；单独列明所有执行与未执行范围，不照搬历史测试数量 |
| R02 | 迁移后的生产入口测试集合统计 | M2 所有实际发出的 invocation 快照绑定覆盖率为 100%；partial provenance 单独统计 |

## 6. 验收门禁

### Phase 2A / M1：最小链路完成

必须同时满足：

1. P00～P10 完成，U/P/G/I/A 对应自动化用例通过，R01 通过，C01 完成全入口登记及 2A 范围核对；通过独立 PR 交付。
2. 新增最小链路 invocation 全部有快照 ID、非空 digest、可读取且校验一致的完整最终输入。
3. 网络发送只从冻结内容派生；修改外部对象、attempt 副本和并发绑定不能改变该调用输入。
4. retry/fallback 复用快照；工具续接/修复/重新压缩建立新调用。
5. 审批恢复、当前授权、预算和资产撤销行为无退化。
6. SQLite 与隔离 PostgreSQL 迁移/并发/不可变约束均有证据。
7. 报告明确尚未迁移入口、partial 来源、外部附件引用限制；不能写“全部生产调用已覆盖”。
8. 原子性硬门禁仅覆盖 snapshot、invocation、execution context binding；Learning Asset 允许保持独立事务，但引用一致性、发送前校验及失败时零网络请求必须成立。
9. provenance=partial 可通过本门禁：U09 验证最终逻辑输入完整真实，关键来源引用准确；不要求逐片段追踪和完整 dropped 清单达到 100%。已知来源冲突、授权失败不能豁免。

### Phase 2B / M2：第二阶段全部完成

在 Phase 2A 已合并且 M1 通过的基础上，通过独立 PR 完成 P11～P14，C01～C06、R02 通过。生产网络路径无未登记旁路；缺少存储不能继续发送；提供逐入口覆盖清单。

“输入冻结覆盖率 100%”与“来源完整率”分别报告。前者是本阶段必需门禁；后者的 partial 项必须可定位、解释且不掩盖 Memory/Skill 撤销检查缺口。

## 7. 测试执行与证据要求

以下文件名是开发后的目标名称，当前尚未存在；不能将下列命令记为已通过。

```powershell
cd backend
python -m pytest tests/test_model_input_snapshot.py tests/test_snapshot_gateway.py tests/test_snapshot_flow.py tests/test_snapshot_recovery.py -q --tb=short
python -m pytest tests/test_execution_context.py tests/test_harness_context_flow.py tests/test_harness_context_approval.py tests/test_model_control.py tests/test_model_gateway.py tests/test_routed_model_gateway.py tests/test_chat_goal_tools.py tests/test_goal_tools.py tests/test_goal_tool_recovery.py tests/test_conversation.py tests/test_conversation_worker.py tests/test_conversation_protocol_v2.py tests/test_goal_programs.py tests/test_goal_program_compiler.py -q --tb=short
```

PostgreSQL 使用新建隔离测试库并经 `db_target_guard` 检查，将 `TEST_DATABASE_URL` 设为合格测试地址后执行；测试会清理表，禁止指向开发库：

```powershell
python -m pytest tests/integration/test_model_input_snapshot_postgres.py tests/integration/test_harness_context_postgres.py tests/integration/test_chat_goal_tools_postgres.py tests/integration/test_goal_tool_recovery_postgres.py tests/integration/test_root_task_budgets.py -q --tb=short
```

M2 按 P00 清单加入 Research/Learning/管理检测等相关测试文件，在验收报告记录完整命令，不用 `...` 省略。无需付费模型才能验收不可变性；如额外做真实请求冒烟，应另列模型、费用、调用数及限制，不替代自动化边界测试。

证据目录建议：`docs/acceptance/context-snapshot-phase2/`，入口清单共享，2A/2B 的验收报告与测试日志分别保存在 `phase2a/`、`phase2b/`，不以 2B 的后续结果覆盖 2A 的交付事实。至少包含：

- 调用入口清单，含任务 ID → 测试 ID → 实际 pytest 节点映射。
- 验收报告和结构化 evidence JSON：代码 commit/工作区状态、PR/交付范围、schema head、Python/数据库环境、Phase 2A/2B 与 M1/M2 状态。
- 每次测试的命令、起止时间、退出码、通过/失败/跳过数量及日志。
- 至少一条脱敏链路：invocation → execution digest → snapshot ID/digest → attempts；普通日志不复制完整用户输入。
- 失败记录与修复说明、未覆盖范围、历史兼容策略、partial 来源统计。

完成标准以代码、动态测试和持久化证据为准；本开发文档本身不构成验收通过证明。
