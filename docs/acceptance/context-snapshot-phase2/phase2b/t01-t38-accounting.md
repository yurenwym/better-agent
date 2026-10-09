# Phase 2B T01–T38 对账（2026-10-09 最终复验）

38 pass / 0 partial / 0 pending。来源 partial 不等于测试 partial。
节点选择规则见 test-contracts.json；展开的实际通过节点、JUnit 批次见 evidence.json。
scripts/build_snapshot_m2_report.py 验证收集、实际执行及发送记录；未匹配或失败则退出非零。
历史状态保留在验收报告历史段与 evidence-checkpoint-1a12532.json。

| ID | 状态 | 任务 | 证据边界 |
|---|---|---|---|
| T01 | pass | B00/B09 | AST 候选、网关装配及人工动态分派追踪；以 callsite-contracts 的业务组及分支为分母。 |
| T02 | pass | B01 | 独立连接在 mock provider 边界读已提交绑定；记录器故障注入证明未绑定发送会记失败。 |
| T03 | pass | B01 | snapshot/invocation/execution binding 三类写入失败原子回滚，零发送。 |
| T04 | pass | B02 | Research 八方法逐项验证最终输入、role/purpose、owner；excerpt 来源保持 partial。 |
| T05 | pass | B02 | 非法 JSON 修复新调用、新 child span，原快照不变。 |
| T06 | pass | B02 | 网络重试两个 attempt 共用原快照，独立还原输入。 |
| T07 | pass | B03 | 两章节与报告修复三份快照，外部 evidence/stable 修改不改旧输入或 pinned bundle。 |
| T08 | pass | B03 | Research Worker 异常/取消恢复并 reset；RUNNING 调用不重发；Learning 真实发送后 UNKNOWN 及 Evolution REQUESTING 恢复 UNKNOWN 均不自动重发。 |
| T09 | pass | B04 | 两种真实专家汇总路径，fan-out/结果汇总/Judge 各自绑定。 |
| T10 | pass | B04 | 两 owner 交错、一专家异常、跨 owner 拒读；PG 真实根预算分别计数 4/5。 |
| T11 | pass | B05 | 真实 Learning job 分发 generator/extractor/Prompt；PG job owner/root/bundle 及关键来源引用核对。 |
| T12 | pass | B05 | 真实 Skill pipeline 生成后独立 Judge，rubric digest 与实际候选/基线输出。 |
| T13 | pass | B06 | Skill selector/两臂/Judge + Prompt 两固定版本/Judge；冻结离线 case，不污染 Judge。 |
| T14 | pass | B06 | 组合证据：真实授权批次向 proposer 传非默认 owner；真实 LiveBehaviorRunner/proposer 的受控发送独立绑定该 owner，缺身份零发送。 |
| T15 | pass | B06 | 真实 evaluation HTTP API/Worker/LiveEvaluationRunner：60 cases、240 次受控 direct 发送；两臂与双 Judge 独立、幂等重放零新增。 |
| T16 | pass | B07 | 真实归档 claim 摘要后保存失败回滚，授权重试相同消息、新快照、成功后不重发。 |
| T17 | pass | B07 | 引用解析超时/取消后第二 owner 请求，旧候选输入不变、无 ambient 残留。 |
| T18 | pass | B07 | 计划文档/已有计划/Research/记忆修复/无历史/依赖拒绝/短路及 ask 澄清；零调用单列。 |
| T19 | pass | B08 | 管理端指定未激活版本、无 stable、可信 owner，不激活 channel。 |
| T20 | pass | B08 | 跨 owner version 拒绝且零发送，含 PG。 |
| T21 | pass | B08 | 缺存储/凭据/预算/PG 写快照失败均零发送、失败状态，未降级离线。 |
| T22 | pass | B08 | startup 与 API fallback 均保留真实 control store。 |
| T23 | pass | B10 | 真实 startup learning OFF，Research/对话/管理仍有发送及已提交快照。 |
| T24 | pass | B10 | partial 正向、已知引用冲突零新增、缺位置拒绝、技能更新与撤销、Learning 源撤销。 |
| T25 | pass | B09 | 默认无 store direct、无身份 routed 在发送前拒绝；provenance 不提供旁路。 |
| T26 | pass | B09 | 显式 offline opt-in 实际发送一次；live CLI smoke 显式离线，排除生产分母。 |
| T27 | pass | B09 | routed 内部 attempt 不二次绑定；controlled direct 单独冻结。 |
| T28 | pass | B09 | API body/query 与工具注入 offline 无效；全生产构造点不读取用户离线参数。 |
| T29 | pass | B10 | D08–D12 usage/撤销/无不安全重试/结算保护；单测显式 observe 验证记账，预算 enforce 另测。 |
| T30 | pass | B10 | PG 并发 finish 各维度扣一次/事件一次；事件失败整笔回滚并可重试。 |
| T31 | pass | B11 | 真实隔离 PG：新入口、跨 owner、故障、旧快照兼容、不可变与结算；不使用开发库。 |
| T32 | pass | B12 | 静态枚举+装配追踪+独立发送记录+pytest 收集+JUnit 实际通过，构建报告时全量对账。 |
| T33 | pass | B06A | HTTP goal/messages/approve 生命周期；普通无源 goal 不反思，测试在 approve 前显式附加真实持久化 source turn 覆盖可信反思；tenant-b 与服务 owner 独立验证。 |
| T34 | pass | B06A | 真实 propose_execution 投影，claim 失败零发送、READY 保存后中断恢复不重发；身份/版本与 turn 一致。 |
| T35 | pass | B06A | daily Worker/period service/adjustment API 修复及失败；PG tenant-b 三种操作各自 goal_operation root，同操作修复共享 root。 |
| T36 | pass | B04/B06A | 专家、Research、Runtime 三种 Judge purpose/owner 独立绑定；另补 conversation Judge。 |
| T37 | pass | B09 | 无 context/空 owner/悬空 Research source/Behavior owner 缺失均拒绝且零发送；Worker 内部记录失败，不声称异常冒泡 API。 |
| T38 | pass | B07 | 同一 turn 两辅助调用与主调用的 invocation/snapshot/span 均独立且为 sibling。 |
