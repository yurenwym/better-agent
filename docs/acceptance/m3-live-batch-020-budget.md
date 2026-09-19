# M3-LIVE-20260909-020

019 第一轮的 5 次调用全部成功并记账 `126,160 microusd`。三个角色的最高文本相似度为 0.428062，planner 的 120 分钟预算计算、critic 的约束复核、角色来源引用均正确；但旧门禁要求 researcher 计算 C=130，与已冻结的“researcher 只保留冲突来源原始主张、不做预算计算”合同冲突，因此误判 `researcher_focus` 并停止后两轮。清理 162 行后批次 run、invocation、bundle 均为 0，stable 恢复为执行前版本。019 保留为失败历史。

020 的修复与冻结门禁：

- researcher 门禁只要求 S3/S4、原始执行值 50/90 和来源冲突/核实语义，不再要求 planner 的派生总时长 130。
- 对 019 已保存专家产物进行离线回放，新角色门禁全部通过；不改写 019 当时的失败结论。
- coordinator 完成后立即检查角色门禁，并对多 Agent 最终答案执行冻结质量评分；任一失败都在单 Agent 基线调用前停止。
- 失败诊断新增完整 coordinator synthesis，保留最终答案和专家产物，不只保存 digest。
- child 失败仍在 coordinator 前停止；显式专家协作仍由代码直接生成 V4 控制头。
- 每轮最多 5 次：三个专家、coordinator、同题单 Agent 基线；三轮总上限 15 次。
- 冻结任务明确两小时=120分钟；多 Agent 最终答案必须覆盖 A=130>120、B=100但批准未知、C=90/130且 S3/S4 冲突、S1–S4 来源和只读权限边界。
- “130分钟在/未超过两小时预算”、C=150、未知 source_ref、角色文本相似度 `>=0.8` 均硬失败。

执行边界：

- 唯一当前 PostgreSQL 数据库 `better_agent`，不创建隔离数据库。
- 三轮冻结案例，每轮最多 5 次 DeepSeek `deepseek-v4-flash` 调用，总计最多 15 次。
- 单次最坏费用 `25,232 microusd`；批次费用硬上限 `378,480 microusd`（US$0.378480）。
- 最长 15 分钟；无 fallback、无网络重试；任一传输、模型、状态、互补性、来源、算术、质量、预算或清理错误立即停止。
- 批次结束后按 `m3-live-20260909-020-*` owner、`acceptance_batch=M3-LIVE-20260909-020` 和 manifest 关联精确删除测试数据并恢复执行前 stable。
- 不执行 Canary、候选发布、M4、M5、embedding 或其他后台付费任务。

执行前结果：无网络预检通过，源码摘要 `32b18d49826d209b3d25686191cd2a99a6297e8bf93dd5aa30ec1bcacaa6735f`，冻结案例摘要 `bc94ad19d9e4c57b1bc70668bb52dc00f466a292bf66ae01e381814693488254`；完整 M3 相关回归 `284 passed`。020 仍需单独获得明确批准。
