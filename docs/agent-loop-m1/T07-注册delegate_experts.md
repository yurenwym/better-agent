# T07 注册 delegate_experts

状态：已完成（研究与专家合计 8 passed；含归档历史、bundle、幂等和角色/身份拒绝）  
依赖：T04、T05  
阻塞：T09

## 目标

把「找专家」做成移交型工具。执行体调用 T05 的 `handoff_start_expert`。

## 要做

1. 注册 `delegate_experts`：
   - 参数：`objective`（1–2000 字符）、`roles` 为 `{researcher, planner, critic}` 的非空子集，最多 3 个、去重
   - 非法角色在授权前拒绝
   - 注入：owner、thread、计划来源、已打包历史、预算根、bundle、幂等键 `conversation-expert:{turn_id}`
2. `goal_action_id` 存在时返回观察，不创建任务（AL-T12）。
3. 成功返回 `handoff`，回合收尾与旧 `start_expert` 一致。

## 验收

| ID | 断言 |
|---|---|
| AL-T11 | 专家任务带计划来源、历史、预算根、幂等键；非法角色授权前拒绝 |
| AL-T12 | 行动帮助回合拒绝，零任务 |
| AL-T13 | 身份参数拒绝 |
| AL-T14 | 同一回合只产生一个专家运行 |

## 禁止

- 不删除 `_explicit_expert_request`（T09/T14）。
- 不把专家内部改成子 agent 循环。
- 不在本步改对话系统提示词。
