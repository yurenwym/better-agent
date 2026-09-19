# Personal Planning Acceptance V1

这套评测包含40个固定中文场景，覆盖澄清、保存、计划编译、执行、复盘、调整、记忆及安全。
它评估实际应用输出与状态快照，不把模型连通性测试当作业务质量通过。
DEV用于开发；HOLDOUT和SAFETY仅在冻结版本后验收。文件在仓库内可见，不构成密码学意义的隐藏集；看过后用它调参必须另建新版本留出集。

## 运行

在 backend 目录运行（不会调用网络或付费模型）：

```powershell
python -m app.business_eval template --output ../evals/results/planning-capture.json
python -m app.business_eval score ../evals/results/planning-capture.json --ratings ../evals/results/planning-ratings.json --output ../evals/results/planning-report.json
python -m app.business_eval compare ../evals/results/baseline.json ../evals/results/planning-report.json
```

模板中的 inputs 不包含评分规则。通过实际应用执行每个场景，在隔离测试库准备 context 指定的历史、任务、故障条件。
填写 outputs 的 text（用户实际收到的文本）、trace_ref（脱敏轨迹路径或调用ID）、observed（由API/数据库检查得到的字段）。
不得让被测模型自己声明 observed 中的状态或执行效果；状态字段必须有对应轨迹证据。程序只验证提交的快照，不自动证明证据真实性，也不是自动端到端驱动器。
计划编译记录 observed.program，每日/周期复盘记录 observed.review，格式采用应用真实schema。
其余字段依 cases 中 expected.state 从实际状态读取，未支持能力应如实失败，不能补造输出。
metadata 必须记录代码版本（未提交版本应记录工作区快照标识）、模型版本和prompt版本。
可填写 latency_seconds、cost_microusd、model_calls；未知留空，不按零计费。报告给出P50/P95和缺失数。

## 人工评分

先不带 --ratings 运行 score，报告会提供 capture_digest。然后按以下格式录入人工评分：

```json
{"capture_digest":"报告中的capture_digest","ratings":[{"id":"clarify-01","reviewer":"评审人标识","rationale":"引用输出中的具体证据与该用例rubric","actionability":4,"faithfulness":4,"personalization":4}]}
```

三项分别是可执行性、证据忠实度、对用户约束的适配。1=严重错误/不可用；2=需大改；3=基本可用有小缺口；4=满足要求；5=有证据支撑的优秀结果。
每项至少3分且硬校验通过才算PASS。未评分为NEEDS_REVIEW，缺输出/硬校验失败为FAIL；这些状态退出码均为1。
评分人需结合每条rubric，不把“说了好听的话”当作证据。输出改变后旧评分失效，必须重新评分。
基线和候选采用同一组场景、相同上下文；评分时先隐藏版本身份，保存各自评分和捕获文件，再比较报告。
本套件不替代既有60例Evolution发布门禁，不应将其通过率直接用作自动晋升依据。

## 周期复盘恢复接口

GET /api/programs/{id}/period-review 查询 NOT_STARTED/PENDING/COMPLETED/FAILED/UNKNOWN、错误码和 retryable。
POST /api/programs/{id}/period-review/retry，JSON正文{}，携带本地CSRF与新的Idempotency-Key。
同一重试键只派发一次；成功结果复用；生成中的请求不重复派发。
旧PENDING或租约过期显示UNKNOWN，需显式重试。未知请求可能已在供应商计费，重试可能再付费，但沿用原根预算，不重新发放额度。
当前提供API恢复入口，尚未新增前端重试按钮，也不会自动重做UNKNOWN请求。
