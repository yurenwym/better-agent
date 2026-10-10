# T09 对话 Profile 与 loop 开关

状态：已完成（loop 主链路/澄清续写/临时 v2 兼容 5 passed；legacy 接缝 113 passed；默认 legacy）  
依赖：T06、T07、T08  
阻塞：T10、T13

## 目标

增加 `legacy | loop` 开关（默认 `legacy`）。`loop` 模式下对话入口组装真正的 conversation Profile：提示词描述可用能力，不再要求控制头 v3/v4；跳过研究/专家正则和记住、研究分类调用。

## 要做

1. 配置项沿用 `config.py` 现有开关方式，例如环境变量，取值 `legacy | loop`，默认 `legacy`。
2. `loop` 模式：
   - 跳过 `_is_explicit_research_command`、`_explicit_expert_request`、`_classify_explicit_remember`、`_classify_explicit_research_request`
   - 系统提示词去掉控制头协议，改为能力选择原则；**不得**写入 T00 案例原文或 HOLDOUT 特征
   - 能力集包含 T06–T08 与现有 ask / goal / MCP
   - 入口按 `LoopOutcome` 写 `policy/content_shape/reason_code`；`reason_code` 使用 `tool:start_research` 这类前缀
3. 计划文档在本步**仍走控制头 v2**（`force_plan_document`、计划分类、`_is_plain_existing_plan_save` 保留到 T10）。
4. `legacy` 模式行为与 T04 结束时一致。
5. `ask_user` 在 `loop` 模式走 `Suspended`，入口复用 `_finish_ask`。

## 验收

| ID | 断言 |
|---|---|
| AL-T16 | `ask_user` 挂起与续写正常 |
| AL-T17（部分） | `loop` 下研究/专家正则、记住与研究分类调用次数为 0 |
| AL-T18 | 历史控制头仍能读 |

用计数器或 monkeypatch 证明旧函数未被调用，不要只靠提示词文本。

## 禁止

- 不改默认值为 `loop`（T14）。
- 不删除旧函数，只在 `loop` 分支不调用。
- 不在本步实现 `publish_plan_document`。
- 不改前端。
