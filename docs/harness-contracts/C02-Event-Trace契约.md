# C02：统一 Event Envelope / Trace

前置：C01 与 T04 完成。结果是事件 payload 的语义基础。

## 1. 现状与范围

现有 `backend/app/events.py` 包含 Event、ThreadEvent 及各自 store；`execution_context.py` 已定义 HarnessExecutionContext、trace/span 和服务端身份工厂；`harness_context_store.py` 提供持久化支持。

`transcript.py` 中的 TranscriptEvent 是规范转录结构，涉及历史 hash；`research/models.py` 的 ResearchEvent 是领域事件。它们不应被机械替换成通用外壳。

统一的是主链路持久化事件的外壳与映射。保留领域 payload、现有 event type、前端订阅和投影行为。不建立第三套独立事件存储，不将 token delta 全部变成审计事件。

## 2. EventEnvelope v1

建议新增纯类型模块 `backend/app/event_envelope.py`，依赖 C01 和现有纯执行上下文；不得依赖 DB/store/业务服务。

| 字段 | 约束 |
|---|---|
| schema_version | 外壳版本，初始 1；不得与旧 Event.schema_version 混用 |
| event_id | 唯一稳定 ID；重试同一事件写入时复用 |
| type / payload_version | 沿用领域事件名称；payload 版本独立演进 |
| occurred_at | UTC 发生时间；不承担排序保证 |
| source / actor | source 表示生产组件，actor 保留现有发起者含义 |
| stream_kind / stream_id / seq | thread 或 run 流内递增游标，无全局顺序承诺 |
| context | 复用 HarnessExecutionContext 的身份与 trace/span 字段 |
| causation_event_id | 直接触发本事件的事件 ID；根事件可空 |
| operation_id / attempt | 逻辑操作及执行尝试；恢复/重试规则见下文 |
| payload | 领域 JSON 数据，按 type 校验必要字段 |
| outcome | 可选 C01 结果；结束事件要求携带对应结果 |
| snapshot_refs | 可选现有输入快照引用，不复制提示词正文 |

新增字段必须有主链路使用点。context 内 owner/thread/turn/run/task 等字段不再在外壳复制第二份；stream 身份必须与 context 一致。

新主链路持久化事件的 context 从可信服务端上下文取得，缺失或不一致时阻断相应执行，禁止从模型参数、随机新根身份或“默认用户”补齐。身份错误本身可写入现有安全诊断，不伪造正常轨迹。

## 3. 身份、因果与执行尝试

- trace/span 继续由现有工厂创建；event_id 不充当 span_id。
- 一个语义操作可有多个事件，使用同一操作 span；子模型/工具调用创建子 span。
- 同一逻辑操作重试：operation_id 不变，attempt 单调增加，创建新的 attempt span；明确保留原始工具 call_id/审批 ID 映射。
- 审批等待：持久化 context、operation_id、审批引用及因果事件引用。恢复读取这些记录，不依赖内存；根 trace 不变，恢复产生新 span，并由审批决定事件指向恢复。
- 研究/专家移交：父事件记录实际 job/task 引用；子任务继承可信 trace，并使用子 span。保留目标的持久化身份校验，不以跨 owner 引用拼接。
- 用户后续发起全新任务创建新 trace；不能仅因 thread 相同无限复用旧 trace。
- causation 与 parent_span 含义不同：前者是事件因果，后者是执行嵌套。跨 stream 通过引用关联，不建立全局序号。
- 不要求本轮重写研究内部每个事件，仅要求工具移交边界和已有任务生命周期能关联。

## 4. 持久化与一致性

复用现有 events / thread_events；在现有表增加必要可空元数据列/JSON 列及约束即可。T06 必须明确单一事实来源，避免旧字段与新外壳各自可独立修改。

- 业务状态与关键语义事件使用同一数据库事务：审批创建/决定、检查点更新、终态等。
- 外部副作用无法和 DB 组成原子事务；发送前持久化执行意图，发送后记录确认。崩溃窗口由 C01 unknown 与既有对账机制处理。
- 同一流并发 append 必须安全分配 seq，使用数据库锁/原子计数和唯一约束，禁止裸 MAX(seq)+1。
- event_id 或明确的生产者幂等键建立数据库唯一约束；同 ID 同内容返回原记录，同 ID 异内容显式报冲突。
- 幂等事件写入只防重复记录，不等于外部工具恰好执行一次。
- 事务回滚不产生可见语义事件。事务提交后的通知/投影失效可以重读持久化事件恢复；禁止把未提交事件提前推给订阅者。
- stream seq 可以有间隙，但不得重复或倒退；客户端按 after_seq 增量读，重连允许重复投递，由 event_id 去重。
- 保留历史数据，采用加字段与读适配；不批量重写历史事件 ID、seq、转录 hash 或快照摘要。

## 5. 历史兼容与访问控制

历史行缺失 context 时，以显式 legacy 只读投影输出；标明字段缺失，不假装符合新执行记录的必填约束。能从持久化可信关联推导时须标记来源；不能随机补 trace。

旧 API/SSE 输出通过投影保持字段名、type、seq、plan.document_ready 等事件含义。新外壳内部接入不强制前端同步改版。TranscriptEvent.canonical 与 hash 算法保持不变。

读取 trace、outcome、snapshot 引用前检查 owner 及关联资源权限；随机 UUID 不是权限。payload 使用既有脱敏能力并补其缺口；公开事件不可直接包含原始异常、鉴权头、密钥或完整私有快照。内部执行数据与公开投影须区分，不能因脱敏破坏业务恢复参数。

未知新版本/事件类型可只读保留或跳过并显式记录兼容状态，不得作为执行指令；不得令整个历史流不可读。

## 6. 禁止

第二套 trace 生成器、全局事件总序、Kafka/通用 EventBus、新遥测 SDK、业务 payload 万能化、未使用的事件注册插件、以重写旧事件“清洗数据”、仅记录成功路径。

统一结果和事件不得改变授权、来源撤销、预算检查与审批执行时点。

