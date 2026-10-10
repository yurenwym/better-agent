# T08 注册 remember

状态：已完成（能力累计 9 passed；显式记忆使用现有来源/幂等键，缺项目和身份注入拒绝）  
依赖：T04  
阻塞：T09

## 目标

把「记住信息」做成普通低风险写工具，替代循环外的 `_classify_explicit_remember` 直写。本步只注册与执行，不关分类调用。

## 要做

1. 注册 `remember`：
   - 参数：`kind`、`scope ∈ {global, project}`、`content`
   - 注入：owner、project、来源消息 ID、幂等来源键（与现有 `conversation:{hash(...)}` 一致）
2. `scope=project` 但无 `project_id`：观察失败，不写。
3. 沿用当前无审批行为；能力描述写明「仅当用户明确要求记住时调用」。
4. 成功返回已记住条目的公开视图，不含其它 owner 数据。

## 验收（AL-T15、AL-T13）

- 写入带来源证据与幂等键；重复同一来源键不产生第二条。
- project 范围缺项目时观察失败。
- 身份参数拒绝，零写入。

## 禁止

- 不删除 `_classify_explicit_remember`（T09）。
- 不扩大记忆 kind / 高敏感写入。
- 不在本步改系统提示词去引导调用（T09 再改）。
