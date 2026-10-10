# T00 评测案例与 mock 框架

状态：已完成（2026-10-09；离线框架验收通过，真实路由效果未评估）  
依赖：无  
阻塞：T01、T13

## 目标

建立路由评测的案例文件和 mock 运行框架，先能量化旧路径，后续才能对比 `loop` 路径。本步不改业务代码。

## 要做

1. 新建 `evals/cases/agent-loop-routing-v1.json`，schema 含：`case_id`、`partition`（DEV/HOLDOUT）、`category`、`input`、`history`（可空）、`expected`、`forbidden`。
2. 类别与最低例数（合计不少于 60，含计划文档类）：

| 类别 | 最少 | expected 示例 |
|---|---|---|
| 直接回答 | 6 | `final` |
| 明确深度研究 | 6 | `handoff:research` |
| 隐含研究但不应开研究 | 6 | `final`；forbidden=`handoff:research` |
| 明确找专家 | 6 | `handoff:expert` |
| 记住信息 | 6 | `tool:remember` |
| 需要澄清 | 6 | `ask` |
| goal 读工具 | 6 | `tool:query_goals` 等 |
| goal 写工具（审批） | 6 | `approval` |
| 否定句（如「不用研究，直接说」） | 6 | `final`；forbidden=`handoff:research` |
| 多意图混合 | 6 | 写明主期望与 forbidden |
| 流式输出计划并发布 | 6 | `final+plan_document` |
| 「保存成计划」 | 4 | `final+plan_document`，source 为历史计划 |

3. 案例用审核过的合成材料。DEV 可改；HOLDOUT 写完即冻结，不提供给提示词编写者，案例正文不得出现在系统提示词里。
4. 评测脚本优先扩展 `backend/app/real_evaluation.py` 或 `backend/app/evals.py`。mock 模式：固定响应，不访问外网、不发真实模型。能在 `legacy` 模式下跑通并输出 JSON 报告骨架。
5. 无效/取消样本留在分母，单独列出。

## 交付

- `evals/cases/agent-loop-routing-v1.json`
- mock 评测入口（命令写进 `docs/acceptance/agent-loop-m1/` 的说明）
- HOLDOUT 冻结记录（案例 digest）

## 验收

- mock 运行退出码 0，报告含全部案例 ID、分区、类别。
- HOLDOUT 案例数 ≥ 计划的 HOLDOUT 数；改 DEV 不改变 HOLDOUT digest。
- 仓库案例无真实用户正文、密钥。

## 禁止

- 不调用真实模型、搜索、网页。
- 不改 `live_model.py` / `conversation.py` 的路由逻辑。
- 不把 HOLDOUT 案例写进提示词或测试夹具注释。
