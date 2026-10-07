# 脱敏链路样本：invocation → execution digest → snapshot → attempts

由 `dump_chain.py` 驱动一次**真实最小链路对话调用**（对话 → LLM）产生；
调用带真实的 turn 根上下文（`create_root_context`），因此有执行身份摘要。
第一次 attempt 被脚本化的传输层打死（`GatewayError(timeout)`），因此该调用有 2 个 attempt。
只打印标识符、摘要、长度与状态，**不打印冻结的用户正文**。

- 逻辑调用数（invocation）：**1**
- attempt 数：**2**

## invocation `conversation:turn_70beda8dc226440eaa4600…`

| 字段 | 值 |
|---|---|
| id | `conversation:turn_70beda8dc226440eaa460085a4dae002` |
| owner_id | `local-user` |
| role / purpose | `conversation` / `route_and_respond` |
| runtime_bundle_id | `bundle_67fb74736d576397d…` |
| status | `SUCCEEDED` |
| idempotency_key | `conversation:turn_70beda8dc226440eaa460085a4dae002` |
| request_digest | `2880e47a99c98b0aeeb154c1…` |
| **execution_context_digest** | `e0d686774539139c3ba396fe…` |
| execution_context_json 已持久化 | ✅ |
| **context_snapshot_id** | `model_input_snapshot_ec07129b37d546a8966d808e71161e3f` |
| **context_snapshot_digest** | `af5c73bc2ee232be943341a6…` |
| selected_attempt_id | `conversation:turn_70beda8dc226440eaa460085a4dae002_attempt_2` |

### 冻结输入（快照）

| 字段 | 值 |
|---|---|
| snapshot id | `model_input_snapshot_ec07129b37d546a8966d808e71161e3f` |
| schema_version | `model-input-snapshot-v1` |
| content_digest | `af5c73bc2ee232be943341a6…` |
| 规范化 JSON 字节数 | 12202 |
| provenance.status | `partial` |
| 重新计算 SHA-256 == content_digest | ✅ |
| invocation.context_snapshot_digest == snapshot.content_digest | ✅ |
| 快照内含本次用户输入（未打印正文） | ✅ |

### attempts

| ordinal | reason | status | provider_protocol | attempt id | request_digest |
|---|---|---|---|---|---|
| 1 | `primary` | `FAILED` | `openai_compatible` | `conversation:turn_70beda…` | `2880e47a99c98b0aeeb154c1…` |
| 2 | `retry` | `SUCCEEDED` | `openai_compatible` | `conversation:turn_70beda…` | `2880e47a99c98b0aeeb154c1…` |

## 结论

- 两个 attempt 同属一个 invocation：✅
- 执行身份摘要非空：✅
- 快照摘要自校验与绑定一致：✅
- 本文件不含用户正文，仅含摘要与标识符。
