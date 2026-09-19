# M5 P0-D / P1 补齐与验收边界

日期：2026-09-10。范围：补齐本轮代码核查发现的评测统计、真实提示词命中、灰度预算、晋升与并发保护缺口。

## 当前结论

本轮实现和离线验证已完成；**真实效果验证、生产灰度和生产回滚验收未完成**。不得将本报告或模拟测试的 PASS 解释为生产提示词质量提升。

当前 PostgreSQL `better_agent` 中，按 production / DISCOVERY / research / failure 或 partial / ACTIVE 口径统计，仍只有 **1 个独立生产失败谱系**，不足候选生成所需的 3 个。没有使用验收记录或合成数据补足门槛，没有新建生产候选或发起付费评测。

原 `m5-acceptance-final-2026-09-10.md` 保留为历史记录；其中“开发与离线控制门禁已完成”和 `stage_complete=true` 不能代替本轮补齐后的发布验收。

## 实现

### P0-D

- 发布契约升级为 `researcher-prompt-release-v2`，旧 prompt 冒烟报告、旧契约审批不能进入新的发布流程；policy 也必须提供版本匹配的权威配对评测。
- 质量目标采用预声明的保守门槛：最小净收益 0.05，95% 差值置信区间下界至少达到该门槛。区间使用两项 97.5% Wilson 区间构造保守差值区间。1 胜、29 平、0 负不通过。
- 该版本聚焦研究章节写作的质量改善，不宣称已实现成本/延迟优化目标。门槛未使用本轮候选留出结果调参；真实使用前仍需完成基线分析与人工校准。修改门槛必须另开契约版本，不能改写历史结果。
- 固定 20 DEV / 30 HOLDOUT / 10 SAFETY，检查谱系独立、重复正文、确定性断言、双臂安全、已知费用和模型配置一致性。
- 套件冻结只存储摘要、分区、谱系和契约元数据，不持久化源正文；评测入口验证冻结时间早于候选创建时间，不接受调用方单独声明 `frozen=true`。
- 完整发布回放在模型请求前持久化套件使用记录。失败、崩溃或中断后，同一套件不能再次自动发起回放，也不能复用于另一候选后继续宣称独立留出。
- 两臂都校验实际 system prompt 摘要与生产构造器一致，并核对模型配置；保留不含输出正文的逐例提示词、输入、模型及费用摘要。
- 真实脚本使用外部授权回放套件，不再把重复合成模板当发布证据；裁判使用 left/right 匿名排序，记录实际模型与 provider，而不把不同 profile ID 当作独立模型。
- 每次回放调用前重新验证候选来源和正文授权；正文授权不能由批次脚本自动授予。
- 真实脚本在根预算创建后立即记录预算 ID；失败报告保持批次费用范围。证据不足返回退出码 2、`stage_complete=false`，不会显示已验收。

### P1

- 灰度启动明确绑定目标角色、用途、费用、调用上限、期限、候选和审批。启动时校验 stable 与候选基线一致；不同 owner 的研究任务不进入该灰度。
- 分配任务只生成曝光记录，`prompt_hit=0`；冻结 bundle 对应的真实章节写作请求成功后，才写入 `prompt_hit=1` 及 system prompt digest。
- 每个网络 attempt 前，根据当前价格和配置预留最坏费用，并在部署行上串行化预算检查；保留崩溃请求的预留，不能重启后当作零费用。
- 同时限制调用次数，禁止灰度重试和 fallback。预算拒绝持久化 STOPPED；观察器及启动恢复也会处理到期、撤销、来源失效和预算耗尽。
- 新任务在停止灰度后走 stable；既有曝光保留 bundle。网络调用仍必须满足硬预算与有效期，不能以“在途任务”为由突破上限。
- 生产晋升至少 20 challenger + 20 champion，不能通过服务构造参数降低。流程 completed 不再自动当作质量正确；需要独立人工质量评定、真实 prompt 命中、已知成本、安全和质量置信区间收益。
- 人工质量评定接口：`POST /api/evolution/canaries/{deployment_id}/quality`，提交 `run_id`、`passed`、`prompt_digest`、`reason`，绑定已完成实际曝光；评定不可覆盖。
- 状态展示使用与晋升相同的 M5 门禁，返回 `promotable=false`、`gate_reason` 和停止原因，样本按真实命中计数。
- 安全或执行失败可触发自动回滚；质量退化停止新分配。回滚不覆盖 stable 的后续合法更新。正式晋升使用 bundle + version 比较更新，防止并发覆盖。

## 验证证据

最终组合回归：**90 passed in 13.09s**。

```powershell
$env:PYTHONPATH='.;tests'
python -m pytest -p test_m5_controlled_evolution tests/test_m5_controlled_evolution.py tests/test_m5_release_gates.py tests/test_m4_m5_live_preflight.py tests/test_postgres_authority.py tests/test_model_gateway.py tests/test_model_control.py tests/test_routed_model_gateway.py tests/test_experience_observer.py --junitxml=../docs/acceptance/m5-release-gates-2026-09-10.xml -q
```

运行目录为 `backend`。M5 测试插件将数据库连接替换为内存 SQLite；未创建其他 PostgreSQL 数据库或容器，测试中的模型输出为 mock。代码编译与针对修改文件的 `git diff --check` 通过。

机器可读测试报告：`m5-release-gates-2026-09-10.xml`。覆盖统计不足、明确收益、零收益、未知费用、安全退化、确定性断言、谱系重复、正文重复、冻结时间伪造、中断不重发、旧报告拒绝、实际请求命中、预算前置拒绝、重启停止、并发保护、自动回滚和质量评定缺失拒绝。

## 当前数据库核对

通过 Alembic 在当前数据库应用增量迁移 `20260909_0005 → 20260910_0006`，只新增字段、元数据表、预算预留表和审计保护，不清理历史数据。

迁移后只读核对：

| 项目 | 结果 |
| --- | --- |
| 数据库 | better_agent / better-postgres-1 |
| Alembic | 20260910_0006 |
| stable bundle | bundle_8040b4cc18e6bf2282d15992 |
| stable version | 36，迁移前后不变 |
| 灰度部署 | 0 |
| 新发布套件记录 | 0 |
| 新灰度预算预留 | 0 |
| 合格生产失败谱系 | 1 |
| 本轮模型网络调用 | 0 |

## 尚不能完成的真实验收

需自然生产流量积累至少 3 个独立、可信的行为失败谱系，明确选择 experience ID；随后准备与真实故障对应、独立冻结的 DEV/HOLDOUT/SAFETY 套件及正文授权。真实基线和裁判人工校准、候选完整配对评测、单独批准的灰度预算与有效期、生产实际曝光与回滚证据仍需完成。

真实脚本使用 `M5_EXPERIENCE_IDS`、`M5_RELEASE_SUITE_PATH`、`M5_CONTENT_AUTHORIZATION_ID` 及既有批次配置；环境变量本身不代替授权记录。新灰度启动请求还需要 `max_calls`，不能沿用只传版本号的旧请求。

在上述条件满足前，不应启动候选生成、Canary 或正式晋升，也不能把本轮标为“真实效果与灰度验收完成”。
