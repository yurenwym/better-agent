# T03 AgentLoop 循环与守卫

状态：已完成（AL-T01–T07 及绑定例外，13 passed；尚未接入业务）  
依赖：T02  
阻塞：T04、T11

## 目标

实现 `AgentLoop.run`：模型调用 → 解析 tool call → 执行 → 观察 → 下一轮，加上统一守卫。只用 fake model 与 fake executor。

## 要做

实现总览第 4.2 节语义：

1. 每轮新子 span / 新 invocation；purpose 为首轮 `{prefix}`，后续 `{prefix}_tool_{n}`。
2. 无 tool call：`allow_text_final=True` 则 `Final`；否则追加「必须调用终止型能力」后继续，计入迭代。
3. 多 tool call 按序执行；挂起 / 移交 / 终止立即返回，后续不执行、不写 tool 消息。
4. 默认：响应同时有文本和**非** `bind_text` 工具时，调用 `on_text_reset`，文本不当作回答。
5. 例外：该响应全部 tool call 都属于 `profile.bind_text` 时，保留文本，把正文传给 `executor.execute(..., bound_text=text)`。
6. `bind_text` 与其它工具混用：重置文本；绑定类调用不传 `bound_text`。
7. 守卫：迭代、墙钟、相同动作、连续错误、同一失败重复两次后去工具再答一次。
8. 运行中取消：取消当前模型调用，不再执行后续工具。

## 验收（AL-T01–T07）

| ID | 断言 |
|---|---|
| AL-T01 | `Final`，一次模型调用，零工具 |
| AL-T02 | 工具顺序与 tool 消息正确；每轮新 invocation |
| AL-T03 | 第二个 pending 时第三个不执行 |
| AL-T04 | 非法参数观察；同一失败两次后 `Final` |
| AL-T05 | 四种守卫各自 `Exhausted`，无额外模型调用 |
| AL-T06 | 取消后无后续工具 |
| AL-T07 | 非绑定工具带文本 → 重置，不作为回答 |

本步可用 fake 覆盖「仅 bind_text 工具时保留文本」的单元断言，正式编号 AL-T23 在 T10 补业务保存。

## 禁止

- 不接入 `ChatToolRunner`、不改 `live_model.py`。
- 不在循环内写业务 SQL 或识别 `start_research` 等名字（只认 Profile 里的集合）。
