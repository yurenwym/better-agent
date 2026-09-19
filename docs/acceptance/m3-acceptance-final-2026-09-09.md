# M3 多 Agent 正式验收（2026-09-09）

状态：`PASSED`。`M3-LIVE-20260909-022` 在冻结源码、案例、规则和预算下连续三轮通过，满足架构计划第 6 节及第 9 节的 M3 阶段出口，允许进入 M4。该结论不覆盖或改写 018–021 的失败历史。

## 实现与关键决定

- 通过持久化 conversation → turn → `agent_context_snapshots` → expert run 自动装配当前请求、计划版本、会话上下文与固定 runtime bundle。
- researcher、planner、critic 使用不同的持久化 objective、system contract 和最终角色指令；专家只读、无工具，返回结构化 JSON。
- coordinator 保留分歧、未决条件和权限边界，并把专家 findings 的事实文本与 `source_refs` 确定性汇入最终综合结果，避免来源在自然语言改写中丢失；不新增模型调用。
- 显式协作请求走确定性 V4 `start_expert`；默认仍为单 Agent。部分成功、全部失败、取消、租约接管、晚到结果和根预算均有机器可读状态及离线回归。
- 所有角色使用所属 turn/run 固定的 bundle、profile 与价格快照；本批无 fallback、无网络重试。

## 冻结版本

- 批次：`M3-LIVE-20260909-022`。
- 源码摘要：`7b81109efe5edeeb29a6899fa784c8c40308e05c4d439dfdec0cebae79f1a305`。
- 案例摘要：`bc94ad19d9e4c57b1bc70668bb52dc00f466a292bf66ae01e381814693488254`。
- 规则摘要：`e509266f4cfd884c3e1092d7a744d6c1a725dc3672e2fd13a1040292fcf2bf99`。
- 模型：DeepSeek `deepseek-v4-flash`，32,768 token 上下文，8,192 token 输出上限。
- 执行前 M3 相关单元/服务回归：`313 passed`；`py_compile` 与本批改动范围 `git diff --check` 通过。

## 连续三轮证据

| 轮次 | 调用 | 角色审计 | 最高角色相似度 | 多 Agent | 单 Agent | 观察到质量增益 | 费用 |
| --- | ---: | --- | ---: | ---: | ---: | --- | ---: |
| 1 | 5 | 通过 | 0.463068 | 5/5 | 5/5 | 否 | 126,160 microusd |
| 2 | 5 | 通过 | 0.520900 | 5/5 | 5/5 | 否 | 126,160 microusd |
| 3 | 5 | 通过 | 0.348837 | 5/5 | 5/5 | 否 | 126,160 microusd |

三轮都正确覆盖 A=130>120、B=100 但批准未知、C 在 S3/S4 下为 90/130 且来源冲突、S1–S4 可追溯和只读权限边界。三个角色每轮均成功，角色相似度低于冻结硬上限 0.8。

## 费用、清理与恢复

- 共 15 次成功调用，全部有价格记录；总记账 `378,480 microusd`（US$0.378480），等于但未超过冻结硬上限。
- 总耗时 68.903902 秒；无 fallback、无网络重试。
- 在唯一当前 PostgreSQL `better_agent` 上执行；结束后按批次 owner、manifest 与关联行精确删除 486 行。
- 独立核验本批 threads、agent runs、model invocations、runtime bundles、cost budgets 均为 0。
- stable 恢复为执行前 `bundle_8040b4cc18e6bf2282d15992`。
- 本轮未新增数据库迁移；复用 M2 的根预算和已有 M3 状态表。应用回滚可停用专家触发并继续读取现有记录，无破坏性 schema 降级。

## 失败历史与剩余边界

- 018：critic provider unavailable；019：researcher 门禁与角色合同冲突；020：跨句预算正则误判；021：coordinator 漏写 S1。失败报告、费用和清理证据均保留，未追写为成功。
- 单 Agent 三轮也均为 5/5，因此本验收只证明显式多 Agent 协作流程可靠且结果达标，不证明默认启用多 Agent 有质量收益。默认单 Agent 策略保持不变。
- 三轮是流程稳定性最低证据，不是统计显著性或可靠 P95；相同模型的不同角色不算独立外部证据。
- usage token 分桶不可用时费用按冻结单次最坏值记为 `ESTIMATED_PARTIAL`，没有把缺失费用记为 0。

## 阶段出口

M3 已满足进入 M4 的条件。M4 仍须按独立冻结案例、回归、真实多主题研究三轮、费用上限和单独批准执行；本次授权不覆盖 M4，也不覆盖 M5、候选发布或 Canary。

证据文件：`m3-live-batch-022-budget.md`、`m3-022-preflight.json`、`m3-live-results-2026-09-09-022.json` 及三份 round blind-review JSON。
