# M3-LIVE-20260909-019

018 在第一轮首错停止：planner、researcher 成功，critic 返回 `provider_unavailable`。旧运行器随后仍执行了 coordinator，因而共使用 4 次调用并记账 `100,928 microusd`，之后才以 fan-out 不完整失败。清理 96 行后批次 run、invocation、bundle 均为 0，stable 恢复为执行前版本。该结果保留为失败历史。

019 的修复与冻结门禁：

- 核心验收每完成一个 child 都检查持久化状态；任一 child `FAILED`/`CANCELLED` 时，在 coordinator 与基线调用前立即停止，并报告角色、状态和错误码。
- 部分交付仍由独立的零网络状态案例验收，不再让核心三轮在必需专家失败后额外调用 coordinator。
- 失败诊断保存停止前已成功专家的规范化产物，避免只看到摘要 digest。
- 每个专家在完整上下文 JSON 后再收到一条最后用户指令，重新绑定唯一角色范围：researcher 仅证据冲突/缺口，planner 仅单位/计算/依赖，critic 仅反例/约束/失败模式。
- 用户明确命令专家协作时仍由代码直接生成 V4 `start_expert`，不调用路由模型。
- 每轮最多 5 次：三个专家、coordinator、同题单 Agent 基线；三轮总上限 15 次。
- 冻结任务明确两小时=120分钟；“130分钟在/未超过两小时预算”、C=150、未知 source_ref、角色文本相似度 `>=0.8` 均硬失败。

执行边界：

- 唯一当前 PostgreSQL 数据库 `better_agent`，不创建隔离数据库。
- 三轮冻结案例，每轮最多 5 次 DeepSeek `deepseek-v4-flash` 调用，总计最多 15 次。
- 单次最坏费用 `25,232 microusd`；批次费用硬上限 `378,480 microusd`（US$0.378480）。
- 最长 15 分钟；无 fallback、无网络重试；任一传输、模型、状态、互补性、来源、算术、质量、预算或清理错误立即停止。
- 批次结束后按 `m3-live-20260909-019-*` owner、`acceptance_batch=M3-LIVE-20260909-019` 和 manifest 关联精确删除测试数据并恢复执行前 stable。
- 不执行 Canary、候选发布、M4、M5、embedding 或其他后台付费任务。

执行前结果：无网络预检通过，源码摘要 `f78ba0c2c113bf162d4553ad0fa0041e83e66e0f9331891468a323d9113da33c`，冻结案例摘要 `bc94ad19d9e4c57b1bc70668bb52dc00f466a292bf66ae01e381814693488254`；完整 M3 相关回归 `283 passed`。019 仍需单独获得明确批准。
