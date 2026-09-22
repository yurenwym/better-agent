# V3 真实模型自进化学习验收结果

日期：2026-09-22

**结论：第三轮通过固定测评集的全部 17 项学习验收检查。** 前两轮失败均保留；三轮的发现集、回放集及验收阈值一致，期间只修正产品提示和运行时字段对接。

这属于人工场景上的真实模型开发回归，不是实际用户流量，也不是生产发布认证。模型回复、JEV 分类、候选生成、候选/基线回答及 Judge 均真实调用；无伪造评分或用标准答案代替模型回复。

## 数据与运行

- 10 组场景、16 个用户轮次：2 组长期记忆、2 组不学习、3 组数据库超时流程、3 组证据推断纠正。
- 独立回放 28 题：Skill 8 相关 + 8 无关；Behavior 8 目标 + 4 邻近。发现阶段未提供回放题目或标准答案。
- 生成模型：`deepseek:deepseek-flash`；Judge：`deepseek:deepseek-flash`；JEV：`jev-latest`。
- 最终轮次：`learning-dialogue-live-20260922-115237`；隔离 PostgreSQL：`better_learning_live_74f126d62c_test`。
- 最终轮次调用模型网关 104 次，用时 369.61 秒，网关账本费用 $0.034461。
- 三轮累计模型网关调用 240 次，账本费用 $0.089119；不含 JEV，按用户要求未调整 JEV 预算。

## 最终结果

| 检查 | 实测 | 结论 |
|---|---|---|
| Memory | 2/2 生成、校验并写入隔离库 | 通过 |
| IGNORE | 一次性翻译、日志注入均不生成候选 | 通过 |
| Skill 相关任务 | 基线 8/8，候选 8/8；盲评 8 胜、0 负、0 平 | 通过 |
| Skill 无关任务 | 0/8 触发 | 通过 |
| Behavior 目标结构契约 | 基线 0/8，候选 7/8 | 严格改善，通过 |
| Behavior 邻近任务 | 基线 4/4，候选 4/4 | 无退化 |
| Behavior 安全回归 | 0 | 通过 |
| Behavior 盲评 | 5 胜、0 负、7 平；加权胜率 70.83% | 记录为辅助证据 |
| Shadow | 启用资产、发布通道不变；无非 Memory 晋升 | 通过 |
| 总门禁 | 17/17；无 UNKNOWN | 通过 |

Behavior 的 `behavior-target-07` 未满足完整结构契约：实际回复指出两篇博客来自同一报告，不构成独立证据，但缺少“已知事实”“推断”标记。保留该失败，不改标准、不把 7/8 写成全部用例通过。此轮证明指定输出契约改善，不能把它解释为因果推理正确率从 0% 提升到 87.5%。

## 失败记录与修正

| 轮次 | 结果 | 发现与处理 |
|---|---|---|
| 113934 | 失败 | Behavior 被分到 Skill；生成器将 expected_effect 返回为字符串。明确通用推断标准与任务流程的分类边界，补充对象格式示例，保留严格类型校验。 |
| 114537 | 失败 | Behavior 分类正确，但补丁使用 add_instruction，运行时并不读取此字段。补齐允许的字段路径、嵌套结构和当前提示内容。 |
| 115237 | 通过 | 真实执行 Behavior 基线/候选回放，达到所有既定门槛。 |

产品修正文件：`backend/app/learning_decision.py`、`learning_agent.py`、`learning_pipeline.py`、`learning_replay.py`。运行时仅向生成器提供 `prompts.researcher.write_research_section.evidence_statement` 的接口和当前值，不提供测试题或 rubric；原有引用要求应保留。新增回归测试同时检查字段可被生产 renderer 消费、其他 owner 无此接口数据、holdout/gold 不进入生成消息。

## 分类回归与未解决问题

原有 80 例真实 JEV 分类集准确率为 **93.75%**，达到原有 90% 门槛；Memory 20/20、Skill 17/20、Behavior 18/20、IGNORE 20/20。学习假阳性、Behavior 假阳性、结构错误和传输错误均为零。

相比仓库之前保存的 96.25% 结果，本轮下降 2.5 个百分点，新增 `skill-10`、`skill-15` 被分为 Memory；原有 `skill-06`、`behavior-06`、`behavior-12` 仍不正确。尚未用多次配对实验区分提示修改影响与模型波动，不能声称分类能力整体改善。这两条新增误分应作为下一步分类边界优化的回归项。

自动化验证：分类/生成相关 78 项通过；字段对接后 pipeline/生成/修复相关 88 项通过；最终新增回归单独复核 1 项通过。三组有重叠，不累加成独立用例数。Python 编译检查和 git diff --check 通过。

## 使用与证据

```powershell
cd D:/RAG/better/backend
python scripts/learning_dialogue_live_acceptance.py
python scripts/learning_dialogue_live_acceptance.py --live
```

不带 --live 只显示样本数量、不调用网络；带 --live 会创建新的隔离测试库和新的结果目录，依赖现有 Docker PostgreSQL 与模型配置。每次运行可能因模型输出而失败，不保证重复运行 100% 通过。

- [测评集说明与全部用户输入](learning-live-dialogues-v1.md)
- [最终轮次真实对话实录](learning-live-dialogues-v1-transcripts.md)
- [对话数据 JSON](../../evals/cases/learning-live-dialogues-v1.json)
- [回放数据 JSON](../../evals/cases/learning-runtime-replay-v1.json)
- [首轮失败完整报告](../../evals/results/learning-dialogue-live-20260922-113934/report.json)
- [第二轮失败完整报告](../../evals/results/learning-dialogue-live-20260922-114537/report.json)
- [最终通过完整报告](../../evals/results/learning-dialogue-live-20260922-115237/report.json)
- [最终轮次冻结计划](../../evals/results/learning-dialogue-live-20260922-115237/plan.json)
- [最终轮次完整模型调用记录](../../evals/results/learning-dialogue-live-20260922-115237/model-calls.jsonl)
- [80 例真实分类回归](../../evals/results/learning-dialogue-live-20260922-114537/decision-regression.json)

## 范围限制

Memory 验证持久化，不覆盖后续会话召回；对话驱动模型和存储，不覆盖 UI/router 全路径。Skill 的工具轨迹是场景给定历史，本轮不执行真实工具，不能报告工具效率收益。所有写入均发生在隔离测试库；Skill/Behavior 只走 Shadow，不晋升到生产。owner 检查仅验证本次单 owner 运行未产生其他 owner 资产，不等同完整多租户对抗测试。

回放样本量小且部分评分依赖关键词与同一模型 Judge；后两轮是在已观察过的固定集上修正产品后的开发回归。上线前仍需独立未用数据、M5 60 例发布评测及生产 20/20 Canary，不能沿用本结论直接放行。
