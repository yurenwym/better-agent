# Replay-M3 验收报告

日期：2026-10-09（Asia/Shanghai）。当前结论：**两项审查修复专项通过，完整阶段验收待复验**。候选效果：**INSUFFICIENT_EVIDENCE**。发布资格：**false**。下方原 274 节点 PASS 为 `2edb2d8` 的历史测试结论，不能作为两项逻辑缺口不存在或当前代码完全验收通过的证明。

## 2edb2d8 审查修复

- P1：原实现只在入口检查历史来源，预检之后撤销 bundle 仍可发送。现在用调用专属 store view 将历史 invocation/digest 保存到新 snapshot provenance，复用 direct/routed 网关的每次发送与重试前准入检查，重新装载历史来源并检查当前撤销。研究臂和两个 Judge 都绑定该依赖；失败保留新 invocation/snapshot 和已有 attempt，原记录不变。共享 control store 不被替换或挂载来源依赖。
- P2：原 Judge 仅收到 left/right，rubric 摘要没有对应实际评分输入。现在两个匿名 Judge 都收到独立冻结的 task/evidence/rubric context，仅白名单业务字段入模；顺序、prompt 和 seed 固定，候选 prompt/臂/bundle/model 标识不入 Judge。Judge 版本升级 v2；task/evidence/rubric/blind_input 摘要对应实际请求。
- 新回归：预检后、创建网关时撤销来源零发送；首次 timeout 后撤销不发送第二次；Judge 撤销零发送；direct gateway 首次发送与 retry 同样拒绝；逐项改变任务、证据、rubric 改变两个 Judge 的请求，同时匿名输出顺序保持一致。来源依赖从新快照读取并验证，拒绝记录保留。
- 本轮范围：enforce 下回放专项及受影响 gateway/real_evaluation/静态旁路回归，另复跑隔离 PG entrypoint 文件。实际命令、JUnit、节点和源码摘要见 `review-fix-evidence.json`。`evidence.json` 和原配对 Judge 日志保留为旧版历史证据，新版绑定见 `logs/review-fix-judge-bindings.json`。
- 未执行全仓测试，也未重跑原完整 Replay-M3 所有门禁集合。两项缺口已由定向回归闭环；当前不恢复“完整阶段验收 PASS”。无付费模型调用。

## 完成内容

- R00：M2 独立只读复核完成，历史日志/JUnit/摘要/入口分支一致；脏工作区版本可核实程度限制详见 [m2-review.md](m2-review.md)。历史失败批次和摘要未覆盖。
- R01：可信 operator owner 下的只读 invocation 装载与导出；owner 隔离、legacy/corrupt/unknown/unsupported 分类，独立请求副本、null 保留、路径与双摘要、不可变 suite、derived 转换边界。
- R02–R03：8 类合成 DEV 使用真实 ResearchEngine、LiveResearchModel、受控 gateway 执行，固定日期/检索/步骤响应；JSON repair 新调用、timeout retry 同快照、未知引用拒绝、证据不足、来源指令判定、显式取消与响应丢失记录。没有外网 fallback。
- R04：复用 ResearchRoleReplayEvaluator 的允许片段渲染、交付转换和门禁；新增真实 Research runner 与双匿名 Judge 接缝，冻结 profile/价格/bundle、独立 evaluation root、当前来源撤销、预算拒绝和实际 attempt 费用。baseline 重渲染不匹配明确拒绝，缺业务 sidecar 不从 prompt 猜字段。
- R05：export/run/compare CLI、逐 case JSON、调用摘要关联、Markdown 差异、完整分母、重复运行一致性和已知退化判定。

## 实际验收证据

最终 `release` 批次使用 COST_MODE=enforce。日志、JUnit、精确命令、开始/结束时间、退出码、测试源码与生产接缝 SHA256 记录于 `logs/runs.ndjson` 和 `evidence.json`。最终数量以 evidence 的真实 JUnit 对账为准；每个 RP01–RP18 都映射到成功执行节点。

- SQLite 专项/回归：新增 RP、原快照、Research engine、Research entries、real_evaluation、M5 发布门禁、受控评估和生产旁路审计。
- guarded 临时 PostgreSQL：Research 双臂/双 Judge owner 隔离、独立 evaluation root 与四次 attempt 总 8 microusd 的模拟权威账本成本；预算耗尽零发送；snapshot/invocation 绑定事务失败零发送；原快照、Harness 和根预算回归。
- 两轮相同 suite：8/8 工程契约通过，输入 digest 和 semantic digest 全部一致；新调用/snapshot ID 不同。
- 实际 prompt 候选：8/8 工程契约通过；7 tie / 1 invalid（预期 UNKNOWN），无改善证据，结论 INSUFFICIENT_EVIDENCE。UNKNOWN 仍在计划分母 8 中。
- 明确退化脚本探针：2/8 工程契约通过；5 loss / 1 tie / 2 invalid，效果 FAIL。该探针用于判定器验证，不当作 prompt 在真实模型上的因果证据。
- 双 Judge 的脱敏真实绑定和固定 seed/rubric/prompt 摘要见 `logs/paired-judge-bindings.json`。不输出用户正文或 provider header。

## 失败与修复记录

- 初始 fixture 文件 Windows CRLF 导致字节摘要不匹配，改为明确 UTF-8/LF 写入，历史 suite 原封保留，新 suite 使用 v3。
- 初始离线 profile 缺 streaming capability；错误的 network 故障分类未触发 timeout retry；SQLite 把虚拟 root 喂给 PG 根预算 SQL。修复装配后隔离执行通过。SQLite root 仅作身份引用，实际预算在 PG 验证。
- `rp-first`：24 passed / 6 failed；`rp-second`：29 passed / 1 failed；`rp-third`：30 passed。修复原幂等键/provenance 丢失的测试装配、缺 fixture 分类、attempt price 列位置，以及安全场景 baseline 违规的问题。来源指令正常基线现在遵守安全规则，违规检测另有显式坏输出测试。
- `pg-first`：3 passed / 1 failed（Judge route 与冻结价格 profile 不一致）；`pg-second`：4 passed。现在预检冻结 profile/价格和实际 bundle route，拒绝不一致配置。
- `regression-first`：194 passed / 3 failed；其中两个旧 M5 用例依赖 enforce 环境，另一个是新增 CLI 接缝缺静态审计登记。按所需 enforce 配置及审计登记修复后完整回归通过。
- `final-local`、`verified` 为阶段复跑；`release` 增加源码执行时摘要及运行期间不变断言，为本交付权威执行。所有失败日志/XML和失败批次保留，不以收集成功当执行通过。

## 边界与下一步

没有付费模型调用；fixture/simulated 不写作真实费用，缺 usage 的成本保持 null/unknown；mock 耗时不用于声称性能提升。未重跑全仓测试。本轮不修改数据源业务状态、原预算、stable channel 或正式发布阈值。

CLI 的 controlled 模式拒绝自行付费启动；ResearchEvaluationRunner 提供已有明确启动流程可调用的受控接缝，必须有可信 owner、独立 PG evaluation root、当前源授权及冻结配置。本任务书不授权付费运行。下一步由现有评估启动机制展示预计调用数与预算，用独立业务案例取得真实模型证据。

实现提交与证据版本：执行时为基线 `15b4493` 上的工作区增量，逐文件 SHA256 在运行开始记录，结束验证一致；实现与证据作为一个 enclosing commit 提交。提交后只读交付校验记录 enclosing commit，避免自引用提交摘要。
