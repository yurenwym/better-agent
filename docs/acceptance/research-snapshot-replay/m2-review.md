# M2 独立复核（R00）

复核日期：2026-10-09。源码基线：`15b44939a4e7122e0c65e4b35bbaa76307e65d22`。结论：**PASS（限定历史专项证据范围）**。

## 证据与核对结果

- 独立只读脚本 `scripts/review_snapshot_m2.py` 对原 evidence、38 项测试契约、21 个业务入口组的全部登记分支、JUnit、原始发送记录进行交叉核对，未改写历史文件。结果见 `logs/m2-independent-review.json`。
- 七个选用批次的 JUnit 有 1089 个不同通过节点；598 次发送对应 596 个 invocation。所有发送都关联成功执行节点、已登记入口和有效提交绑定；binding example 均在原始分母中。SHA256 同时按工作区字节和 `git show 15b4493:path` 字节记录。本次工作区摘要全部匹配原值；提交字节差异可由换行转换辨认，未覆盖历史摘要。
- 源码人工核查：`CommittedSnapshotTransport.record` 在审计前计数，在 finally 写记录，故失败审计保留；其失败自测使用独立临时记录，不混入生产样本。静态清单区分共享 transport、动态 Judge 分派、HTTP 装配和 CLI；21 是入口组数，不能解释为原始 AST 调用点数。
- T08 的 Research 取消/异常恢复、unfinished invocation 零重发、Learning 真实故障和 Evolution UNKNOWN 是组合证据。T14 分别验证授权 batch owner 与实际 adapter 身份，不冒称一个非默认 owner HTTP 端到端用例。T28 body/query/tool 反向断言加全树装配审计。T34 claim 失败零发送与 READY 幂等恢复。T35 daily/period/adjustment 加 PG program owner/root。T36 各类 Judge 独立调用。T37 缺 owner/source 的零调用分支不要求正向发送记录。相关断言均实际存在，不能仅以收集成功替代执行。
- 版本：原 evidence 指向 `1a12532` 且 dirty=true。`git diff 1a12532 15b4493` 可确认最终来源元数据、测试、记录器和证据增量进入 enclosing commit。历史批次无执行时逐文件源码摘要，无法逐字证明脏工作区与最终提交完全相同；这是历史证据的可核实程度限制。本轮会重新执行涉及的新接缝及既有回归，结果另存，绝不把重新运行历史报告脚本称为当前测试通过。
- 原失败 learning 批次及修复批次仍保留。未发现阻碍快照装载/Research 回放的真实功能缺口。全部 provenance 仍为 partial；没有完整任务轨迹，不自动构造历史场景。

## 扩展接缝

请求级：`ModelInputSnapshotStore.load_for_invocation` → 原 envelope → `to_request`。业务 sidecar 使用明确版本，重渲染逐字段对照；无 sidecar 时允许请求诊断，但拒绝两臂业务转换。只支持 researcher/write_research_section 无工具输入。

场景级：真实 `LiveResearchModel`、`ResearchEngine`、`RoutedModelGateway`，固定 provider script/retriever/date/证据 ID；隔离 SQLite 或 guarded PG，独立调用和评估预算。缺 fixture 必须立即失败。历史单次请求没有检索/步骤 sidecar 时标 request 或 unreplayable。

最小修改集合：新增 `app/research_replay.py`；Research 日期/证据 ID 注入及缺 fixture 异常传播；扩展既有 Research evaluator 的成本 unknown 处理；专项测试、既有 PG 文件补验、CLI 文档、合成 fixture、验收脚本与证据。发布形状和阈值沿用 M5。
