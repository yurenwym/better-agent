# M3-LIVE-20260909-018

017 在第一轮专家执行前首错停止：明确要求 researcher、planner、critic 协作的请求仍交给对话模型路由；模型返回一个 `ask_user` tool call，未返回 V4 控制头，因此协议拒绝继续。017 仅使用 2 次调用，实际记账 `50,464 microusd`；清理 65 行后批次 run、invocation、bundle 均为 0，stable 恢复为执行前版本。

018 的修复与冻结门禁：

- 用户明确命令专家协作时，由代码直接生成 V4 `start_expert` 控制头并保留显式角色，不再用模型重复判断已经明确的授权。
- 显式专家路由不暴露 `ask_user` 或其他工具，不可能再以空正文 tool call 破坏控制头协议。
- 每轮调用缩减为 5 次：researcher、planner、critic、coordinator、同题单 Agent 基线。三轮总上限 15 次。
- 冻结任务明确“两小时（120分钟）”；planner 必须统一单位并展示计算，critic 必须复核单位与大小比较。
- 任何“130分钟在/未超过两小时预算”或 C=150 的表述均硬失败。
- researcher 只审计冲突/缺证来源；角色文本任意两项相似度必须 `<0.8`；角色 findings 必须满足 researcher→S3/S4、planner→S1/S2、critic→S3/S4，且所有 source_refs 只能是 S1–S4。
- 成功与失败报告在清理前保存本批次实际调用数、计价状态和 `charged_microusd`。

执行边界：

- 唯一当前 PostgreSQL 数据库 `better_agent`，不创建隔离数据库。
- 三轮冻结案例，每轮最多 5 次 DeepSeek `deepseek-v4-flash` 调用，总计最多 15 次。
- 单次最坏费用 `25,232 microusd`；批次费用硬上限 `378,480 microusd`（US$0.378480）。
- 最长 15 分钟；无 fallback；任一传输、模型、状态、互补性、来源、算术、质量、预算或清理错误立即停止。
- 批次结束后按 `m3-live-20260909-018-*` owner、`acceptance_batch=M3-LIVE-20260909-018` 和 manifest 关联精确删除测试数据并恢复执行前 stable。
- 不执行 Canary、候选发布、M4、M5、embedding 或其他后台付费任务。

执行前结果：无网络预检通过，源码摘要 `63002cd68ae8986a5dce6e0bd917b2b3b06adfb8678a0c7e4f5e519e2ba3c253`，冻结案例摘要 `bc94ad19d9e4c57b1bc70668bb52dc00f466a292bf66ae01e381814693488254`；完整 M3 相关回归 `282 passed`。018 仍需单独获得明确批准。
