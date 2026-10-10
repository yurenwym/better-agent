# T10 publish_plan_document（方案 B）

状态：已完成（能力、主链路及核心合计 33 passed；含同回合已提交版本恢复；已有文档走观察后审批修改）  
依赖：T09  
阻塞：T13

## 目标

计划文档改为：模型先流式输出正文，再调用 `publish_plan_document`。Harness 绑定已展示正文并保存。`loop` 模式关闭控制头 v2 写入路径。

## 要做

1. 注册绑定正文型能力 `publish_plan_document`：
   - 模型可见参数：`title`；可选 `source ∈ {this_response, latest_assistant_plan}`，默认 `this_response`
   - **参数中不得出现 markdown / content**
   - `reject_identity_params=True`
2. 循环（T03 已留例外）：响应里全部 tool call 都属于 `bind_text` 时保留已流出文本，传入 `bound_text`。
3. 绑定：
   - `this_response`：使用 `bound_text`；空或已重置 → `NEED_BOUND_TEXT`，零写入
   - `latest_assistant_plan`：使用 `_latest_assistant_plan(history)`；找不到 → 观察，零写入
4. `content_hash` = canonical markdown 的 SHA256。写入的文件/库正文必须与绑定正文一致。
5. 无已有文档：`save_model_revision(..., create_only=True, actor="model")`，消息上写 `plan_document_version_id`。成功则 `Final(text, artifact=plan_document)`。
6. 已有文档：不覆盖；转为挂起的 `modify_plan_document`（绑定同一正文与 hash），或返回观察后由模型再调 `modify_plan_document`。优先与当前 `_finish_success` 冲突语义一致（转为审批）。
7. `loop` 模式不再调用：`_classify_explicit_plan_document_request`、`force_plan_document`、`_is_plain_existing_plan_save`，不再要求模型输出控制头 v2。
8. 入口把 `Final.artifact` 写成与现状相同的 `content_shape=plan_document`。前端不改。
9. 提示词补充：交付计划时同一响应输出正文并调用本工具；保存已有回答时用 `source=latest_assistant_plan`。不写案例原文。

## 验收

| ID | 断言 |
|---|---|
| AL-T23 | 同响应正文+工具：不重置；保存 digest 与展示一致；`content_shape=plan_document` |
| AL-T24 | 无正文或与其它工具混用：`NEED_BOUND_TEXT`，零写入；混用时文本按默认重置 |
| AL-T25 | `latest_assistant_plan` 绑定历史计划；无计划则失败；「保存成计划」不再走正则短路 |
| AL-T26 | 已有文档不覆盖，转审批或观察 |
| AL-T17（齐） | 计划分类 / `force_plan_document` / `_is_plain_existing_plan_save` 调用次数为 0 |

## 禁止

- 不采用方案 A（markdown 放进工具参数、前端流式展示工具参数）。
- 不改 `modify_plan_document` 的 WRITE/审批语义。
- 不改前端。
- 不在供应商不返回 content 时回退到方案 A；该情况记为案例失败。
