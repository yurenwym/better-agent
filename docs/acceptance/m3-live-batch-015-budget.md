# M3-LIVE-20260909-015

014 的三轮模型调用全部完成，但第三轮 researcher 与 critic 输出完全相同，触发角色互补硬门禁，因此 014 为失败历史，不能作为 M3 阶段出口。015 只针对该失败点复验：协调器现在为 researcher、planner、critic 持久化不同的角色限定 objective；运行器会在门禁判断前保存三个规范化专家结果和逐项 `role_audit.checks`，失败信息会直接列出未通过项。

执行范围：

- 唯一当前 PostgreSQL 数据库 `better_agent`，不创建隔离数据库。
- 三轮冻结案例，每轮固定最多 7 次 DeepSeek `deepseek-v4-flash` 调用，总计最多 21 次。
- 单次最坏费用 `25,232 microusd`；批次费用硬上限 `529,872 microusd`（US$0.529872）。
- 最长 15 分钟；无 fallback；任一传输、模型、状态、角色互补、来源、算术、质量、预算或清理错误立即停止。
- C 的合法结论必须保留 `90/130` 两种可能，任何专家或最终答案出现错误值 `150` 均失败；`source_refs` 只能引用 S1–S4；仅添加角色标签不能满足互补门禁。
- 批次结束后按 `m3-live-20260909-015-*` owner、`acceptance_batch=M3-LIVE-20260909-015` 和 manifest 关联精确删除测试数据，恢复执行前 stable；不清理非本批次应用数据。
- 不执行 Canary、候选发布、M4、M5、embedding 或其他后台付费任务。

执行前证据：角色目标、运行器、模型/网关、成本控制、对话交接、PostgreSQL fencing 与根预算相关回归 `279 passed`。015 源码摘要由无网络预检现场生成并写入预检报告。
