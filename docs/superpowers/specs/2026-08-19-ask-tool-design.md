# Conversation Ask Tool V1 Design

## Goal

让模型在确实缺少关键信息时，通过结构化 `ask_user` 工具向用户提问，并在用户选择或输入答案后继续同一对话目标。用户不需要输入“继续”，也不会看到工具调用 JSON。

## Scope

本次只改 Conversation V2 路径：

- `LiveConversationModel` 可以收到一个 `ask_user` 工具调用；
- Agent 持久化问题、问题状态和答案；
- 当前 Turn 进入 `AWAITING_INPUT`，等待用户操作；
- 用户答案创建一个带 `parent_turn_id` 的续接 Turn，继续同一 Thread；
- 前端显示选项按钮、多选、自定义输入和停止询问；
- Thread SSE 和轨迹显示“等待用户回答”；
- 不增加 reasoning 展示；
- 不把 `ask_user` 注册为普通可执行工具，不经过 WRITE 审批，不产生外部副作用；
- 不创建新的 Goal/Run，直到用户后续明确选择执行。

## Tool contract

模型只可调用一个会话工具：

```json
{
  "type": "function",
  "function": {
    "name": "ask_user",
    "description": "Ask the user for the minimum information needed to continue the current goal.",
    "parameters": {
      "type": "object",
      "required": ["questions"],
      "additionalProperties": false,
      "properties": {
        "questions": {
          "type": "array",
          "minItems": 1,
          "maxItems": 4,
          "items": {
            "type": "object",
            "required": ["id", "header", "question", "options", "multi_select", "allow_free_text"],
            "additionalProperties": false,
            "properties": {
              "id": {"type": "string"},
              "header": {"type": "string"},
              "question": {"type": "string"},
              "options": {"type": "array"},
              "multi_select": {"type": "boolean"},
              "allow_free_text": {"type": "boolean"}
            }
          }
        }
      }
    }
  }
}
```

后端再次校验模型输出：问题数量为 1–4；问题 ID 唯一；选项数量为 0 或 2–4；选项为空时必须允许自由输入；文本长度受限；不接受未知字段。普通模型正文不得在工具调用前输出控制头或原始 JSON。

用户答案使用：

```json
{
  "answers": [
    {
      "question_id": "training_level",
      "selected_options": ["有基础"],
      "free_text": ""
    }
  ]
}
```

服务端校验答案 ID、选项归属、单选数量、自由输入开关和每个问题是否已回答，并将规范化答案作为后续模型上下文中的工具结果，同时以可读中文消息显示给用户。

## Persistence and state

新增 `turn_asks` 表，保存 `ask_id`、`turn_id`、模型 `call_id`、问题 JSON、状态、答案 JSON、答案幂等键、续接 Turn 和时间戳。`call_id` 和答案幂等键都必须唯一绑定，重复提交返回原续接 Turn，不创建第二个任务。

Turn 状态新增 `AWAITING_INPUT`。模型产生有效 ask 后：

1. 写入一条固定的用户可读助手消息“为了更准确地完成这个目标，请先补充以下信息。”；
2. 写入 `ask.requested` 和 `turn.awaiting_input`；
3. 将 Job 标记为已完成，释放 Worker；
4. 不创建 Goal、Run、PlanVersion 或 ToolCall。

用户回答后，在同一事务中：

1. 校验当前 Turn 版本和答案；
2. 将 ask 标记为 `ANSWERED`；
3. 原 Turn 标记为 `COMPLETED`；
4. 创建带 `parent_turn_id` 的子 Turn、用户可读答案消息和 QUEUED Job；
5. 将 Thread 的 active Turn 指向子 Turn；
6. 写入 `ask.answered`、原 Turn 完成和子 Turn 接收事件。

停止询问会将 ask 标记为 `CANCELLED`，并将当前 Turn 标记为 `CANCELLED`，不创建子 Turn。

## API and SSE

- `POST /api/turns/{turn_id}/ask/answer`
  - 请求：`expected_version`、`idempotency_key`、`answers`；
  - 返回：`ask_id`、续接 `turn`；
  - 使用 CSRF 和现有本地 Origin 校验。
- `GET /api/turns/{turn_id}/ask`
  - 返回当前 PENDING ask；没有待回答问题返回 404。
- 复用 `/api/threads/{thread_id}/events` 和 SSE；新增事件类型：
  - `ask.requested`；
  - `ask.answered`；
  - `ask.cancelled`；
  - `turn.awaiting_input`。

事件只携带 UI 所需的规范化问题和状态，不携带模型原始响应或控制协议头。

## Frontend behavior

`AskCard` 显示在助手消息下方：

- 单选使用互斥选项按钮；
- 多选允许选择多个选项；
- `allow_free_text` 显示文本输入；
- 所有问题回答完成后才能提交；
- 提交期间按钮禁用；
- 提供“停止询问”按钮；
- 主对话输入框在等待回答时禁用；
- 不显示“继续推动目标”通用按钮。

Thread 重连或刷新后从事件/REST 恢复待回答 AskCard；事件有缺口时沿用现有快照恢复逻辑。

## Tests and acceptance

后端测试覆盖：工具 schema、问题/答案边界校验、模型工具调用解析、`AWAITING_INPUT` 状态、事件顺序、答案幂等、版本冲突、取消、重启后待回答恢复、续接 Turn 上下文以及不创建 Goal/Run。

前端测试覆盖：单选、多选、自由输入、未完成问题不可提交、答案提交调用、停止询问、刷新事件恢复、等待状态禁用普通输入和轨迹可读文案。

验收标准：模型调用 `ask_user` 后，用户在页面可直接点击选项或输入答案；提交后无需手动输入“继续”，同一 Thread 自动继续；原始工具 JSON 不出现在对话或轨迹正文中；重复提交不会产生重复 Turn。
