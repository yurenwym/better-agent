# 首次保存保护、v3 冻结基线与日期回归

分支：front0919。工作区未提交，保留原有 D2/D3/D4 和前端改动。本文件更新冻结契约中的实施状态，不修改已冻结的标签语义或历史结果。

## 实施

1. `conversation.py::_finish_success` 首次 artifact 保存显式传 `create_only=True`。普通并发创建原本会被版本检查挡住；修复的关键窗口是其他入口在空检查后创建并删除文档，旧路径可能恢复已删除文档并写新版本。现在拒绝覆盖/复活并记录冲突。
2. 增加 SQLite 与 PostgreSQL 共用的交错故障注入，覆盖并发创建、创建后删除：版本仍只有一份、当前版本和删除标记不变、无越权目标或审批副作用。本测试为确定性交错验证，不是所有并发情况的证明。
3. 新增独立 v3 分类器协议、严格评分器与 60 个案例（开发/留出各 30）。保存冻结哈希、每次真实调用和全部失败，不更改 v1/v2。四维意图独立，增加目标数组和复盘发起方式；生产分类逻辑未替换成 v3。
4. 检查工具 E2E 原始数据发现日期漏检：原执行管理例把“明天”解析成 2026-01-01，原断言却通过。补充 `live_model.py` 聊天上下文中的 UTC 当前时间及用户时区换算规则，共用预检查和发送构造；增加 E2E 日期、时区与每日预算断言。

## 确定性测试

在 backend 执行：

```powershell
python -m pytest tests/test_chat_goal_tools.py tests/test_plan_document_service.py tests/test_v2_acceptance.py tests/integration/test_chat_goal_tools_postgres.py -q
# 37 passed，含 5 个 PostgreSQL 集成测试
python -m pytest tests/test_followup_intent_v3.py tests/test_followup_intent_eval.py -q
# 18 passed
python -m pytest tests/test_live_model.py tests/test_chat_goal_tools.py -q
# 日期补丁后 80 passed
python -m pytest tests/test_context_budget_contract.py -q
# 21 passed，1 failed
```

预算测试失败：`test_model_admin_round_trips_the_budget_contract` 读取 `legacy['validation_tier']` 抛 KeyError；路径为未修改的 ModelAdminService，不经过本次聊天时间上下文。未为本轮扩大范围修改该模块。各组存在重叠，不应把通过数相加当作独立用例总数。误用不存在的 `test_context_budget.py` 的初次命令未收集测试，随后使用上述实际文件名。

## 真实模型结果

分类证据：`docs/evaluation/followup-intent-v3/{dev-01,holdout-01}`，模型 deepseek-flash，60 次真实请求。

| 指标 | 开发集 | 留出集 |
| --- | --- | --- |
| 完全匹配 | 29/30 | 29/30 |
| 复盘类型 | 34/34 | 34/34 |
| daily FP / FN | 0 / 0 | 0 / 0 |
| 主动升级错误 | 0 | 1 |
| 门槛 | 通过 | **未通过** |

D19 将不记录任务判为 tracking=disable（预期 none）。H12 将烘焙失败后的重复复盘判为 proactive（预期 clarify）。不修改标签，不用已公开留出题调优。其他维度、用量与限制见 v3 README。此结果只证明独立分类探针，未对 60 例逐一运行生产工具链。

工具 E2E 第一轮 `docs/evaluation/chat-tool-e2e-create-only-2026-09-20`：原断言 7/7，24 次调用。包含攻略零写入、保存、激活、修改后目标来源不变、拒绝、取消、恢复。但是执行管理日期错误，**不能将原 7/7 视作语义验收全部通过**。

补充日期上下文后定向运行：

```powershell
python -X utf8 scripts/chat_tool_e2e.py --execute --ids execution_management --out ../docs/evaluation/chat-tool-e2e-clock-2026-09-20
```

5 次真实调用：日期 2026-09-21 至 2026-09-23、Asia/Shanghai、每日 30 分钟均正确；但最终 **0/1**，目标未激活。编译第一次输出截断，现有修复调用成功；预览回答虽然没有控制头，但已有包装逻辑使该轮正常完成。真正失败发生在确认轮请求发出之前：`turn_jobs.last_error_json` 保存 `required model messages exceed the input budget`，事件中的 model_attempt_count=0，用户看到“暂时无法生成可用回答”。因此不能归因于控制头协议。未伪造成功、未删失败、未重复跑到偶然成功。本轮修复了时间信息缺失，未扩大范围重构既有上下文容量机制。

不同阶段源码哈希保存在各自结果文件。冻结分类结果对应日期补丁之前的生产文件哈希；探针提示词不依赖生产文件，因此未重跑或覆盖冻结分类结果。最新日期修改只定向复测了执行管理，不能把之前另六例当作最终版本的全量重跑。

## 交付结论

首次保存保护、测试、v3 冻结案例与真实模型基线已落地；日期信息缺失已修复并通过真实日期断言。**尚不能宣布整体验收通过**：留出集存在主动服务升级错误，工具确认链触发必需上下文容量限制，预算测试另有字段契约失败。本轮所有失败原样可查；继续修复应独立保留新旧证据，不能降低冻结门槛或重标失败题。
