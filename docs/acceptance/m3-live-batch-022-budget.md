# M3-LIVE-20260909-022

021 的第 1 轮通过全部冻结门禁；第 2 轮三个专家和 coordinator 调用成功，角色审计全部通过，但 coordinator 在方案 A 段漏写 `S1`，冻结质量门禁以 `a_budget`、`source_support` 失败并在该轮单 Agent 基线前停止。021 共 9 次调用、记账 `227,088 microusd`，精确清理 259 行并恢复执行前 stable；原失败报告不追写为通过。

022 的修复与冻结证据：

- coordinator 明确要求每个关键事实、数字、依赖和冲突在对应句段标注专家提供的 `source_ref`，不得仅在文末笼统罗列来源。
- worker 将专家结构化 findings 中已有的事实文本与 `source_refs` 确定性汇入综合结果；不增加模型调用、不虚构来源、不放宽冻结门禁。
- 021 第 2 轮原始产物离线回放：原综合答案 3/5；确定性汇入来源证据后 5/5。021 仍保持失败历史。
- 未来失败轮的本地诊断将嵌入主结果 JSON，避免主报告只记录错误名而丢失失败产物。
- child 失败仍在 coordinator 前停止；角色或多 Agent 质量失败仍在该轮单 Agent 基线前停止。
- 每轮最多 5 次：三个专家、coordinator、同题单 Agent 基线；三轮总上限 15 次。
- `130分钟在/未超过两小时预算`、C=150、未知 `source_ref`、角色文本相似度 `>=0.8`、关键来源缺失均为硬失败。

执行边界：

- 唯一当前 PostgreSQL 数据库 `better_agent`，不创建隔离数据库。
- 三轮冻结案例，每轮最多 5 次 DeepSeek `deepseek-v4-flash` 调用，总计最多 15 次。
- 单次最坏费用 `25,232 microusd`；批次费用硬上限 `378,480 microusd`（US$0.378480）。
- 最长 15 分钟；无 fallback、无网络重试；任一传输、模型、状态、互补性、来源、算术、质量、预算或清理错误立即停止。
- 批次结束后按 `m3-live-20260909-022-*` owner、`acceptance_batch=M3-LIVE-20260909-022` 和 manifest 关联精确删除测试数据并恢复执行前 stable。
- 不执行 Canary、候选发布、M4、M5、embedding 或其他后台付费任务。

022 必须先通过无网络预检和完整相关回归，记录冻结源码/案例摘要，再单独获得明确批准。

执行前冻结证据：

- M3 相关单元/服务回归：`313 passed`。
- `py_compile`：通过。
- `git diff --check`（本批改动范围）：通过。
- 无网络预检：`NOT_AUTHORISED`，`network_started=false`。
- 冻结源码摘要：`7b81109efe5edeeb29a6899fa784c8c40308e05c4d439dfdec0cebae79f1a305`。
- 冻结案例摘要：`bc94ad19d9e4c57b1bc70668bb52dc00f466a292bf66ae01e381814693488254`。
- 冻结规则摘要：`e509266f4cfd884c3e1092d7a744d6c1a725dc3672e2fd13a1040292fcf2bf99`。
- 预检记录：`m3-022-preflight.json`。
- 用户于 2026-09-09 明确要求继续验收，批准执行 `M3-LIVE-20260909-022`。

执行结果：`PASSED`。连续三轮均完成 3 个专家、coordinator 和同题单 Agent 基线，各轮多/单 Agent 均为 5/5；共 15 次调用，记账 `378,480 microusd`。精确删除 486 行后批次关键表残留为 0，stable 恢复为 `bundle_8040b4cc18e6bf2282d15992`。正式阶段结论见 `m3-acceptance-final-2026-09-09.md`。
