# M2-LIVE-20260909-007

006 在 ask 续接中收到有效 `plan_document` artifact，但随后错误地用当前回答文本重新分类文档意图，结果将 artifact 降级并导致线程计划不存在。四次网络请求均成功；批次第一轮立即停止，失败报告保留在 `m2-live-results-2026-09-09-006.json`，临时 schema 已删除。

007 让通过 `turn_asks.continuation_turn_id` 确认的父 ask 原请求直接授权续接返回的计划 artifact，不再用回答文本重复分类。普通 Turn 仍无此权限；继承关系、bundle 与根预算均有回归断言。

边界：只使用当前 PostgreSQL 数据库 `better_agent`；每轮在临时 schema 中执行，结束后删除 schema，不创建第二数据库。连续三轮，每轮最多 10 次、总计最多 30 次 DeepSeek `deepseek-v4-flash` 请求；无备用模型；最长 20 分钟；首个失败停止。007 最坏费用 `756,960 microusd`（US$0.756960）；连同此前 30 次请求按逐次最坏值保守累计为 `1,513,920 microusd`（US$1.513920）。
