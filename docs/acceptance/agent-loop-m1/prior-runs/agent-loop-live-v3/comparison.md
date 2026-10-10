# AgentLoop 路由对比

效果结论：FAIL

工程结论：由独立回归结果记录，本报告不推断。

| 指标 | legacy | loop |
|---|---:|---:|
| planned | 70 | 70 |
| matched | 59 | 64 |
| forbidden_hits | 2 | 0 |
| invalid | 0 | 0 |
| cancelled | 0 | 0 |
| mean_tokens | 4570.728571428572 | 6499.328571428571 |
| mean_cost_microusd | 1133.1 | 1616.1 |
| mean_model_calls | 1.2142857142857142 | 1.3857142857142857 |
| direct_ttft_p50_ms | 2267.3329499957617 | 3061.3332000211813 |

## 门禁

- accuracy: True
- categories: True
- forbidden: True
- safety: True
- tokens: True
- cost: True
- direct_ttft: False

## 逐案例

| 案例 | legacy | loop | legacy命中 | loop命中 |
|---|---|---|---|---|
| routing-direct-01 | ['final'] | ['final'] | True | True |
| routing-direct-02 | ['final'] | ['final'] | True | True |
| routing-direct-03 | ['final'] | ['final'] | True | True |
| routing-direct-04 | ['final'] | ['final'] | True | True |
| routing-direct-05 | ['final'] | ['final'] | True | True |
| routing-direct-06 | ['final'] | ['final'] | True | True |
| routing-research-01 | ['handoff:research'] | ['handoff:research'] | True | True |
| routing-research-02 | ['handoff:research'] | ['handoff:research'] | True | True |
| routing-research-03 | ['handoff:research'] | ['handoff:research'] | True | True |
| routing-research-04 | ['handoff:research'] | ['handoff:research'] | True | True |
| routing-research-05 | ['handoff:research'] | ['handoff:research'] | True | True |
| routing-research-06 | ['handoff:research'] | ['handoff:research'] | True | True |
| routing-implicit_research-01 | ['final'] | ['final'] | True | True |
| routing-implicit_research-02 | ['final'] | ['final'] | True | True |
| routing-implicit_research-03 | ['final'] | ['final'] | True | True |
| routing-implicit_research-04 | ['final'] | ['final'] | True | True |
| routing-implicit_research-05 | ['final'] | ['final'] | True | True |
| routing-implicit_research-06 | ['final'] | ['final'] | True | True |
| routing-expert-01 | ['handoff:expert'] | ['ask'] | True | False |
| routing-expert-02 | ['handoff:expert'] | ['handoff:expert'] | True | True |
| routing-expert-03 | ['handoff:expert'] | ['handoff:expert'] | True | True |
| routing-expert-04 | ['handoff:expert'] | ['handoff:expert'] | True | True |
| routing-expert-05 | ['handoff:expert'] | ['handoff:expert'] | True | True |
| routing-expert-06 | ['handoff:expert'] | ['handoff:expert'] | True | True |
| routing-remember-01 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-remember-02 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-remember-03 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-remember-04 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-remember-05 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-remember-06 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-clarify-01 | ['final'] | ['final', 'tool:query_goals'] | False | False |
| routing-clarify-02 | ['ask'] | ['ask'] | True | True |
| routing-clarify-03 | ['final'] | ['final', 'tool:query_goals'] | False | False |
| routing-clarify-04 | ['final'] | ['final'] | False | False |
| routing-clarify-05 | ['ask'] | ['final'] | True | False |
| routing-clarify-06 | ['final', 'tool:query_goals'] | ['final'] | False | False |
| routing-goal_read-01 | ['final', 'tool:query_goals'] | ['final', 'tool:query_goals'] | True | True |
| routing-goal_read-02 | ['final', 'tool:query_goals'] | ['final', 'tool:query_goals'] | True | True |
| routing-goal_read-03 | ['final', 'tool:query_goals'] | ['final', 'tool:query_goals'] | True | True |
| routing-goal_read-04 | ['final', 'tool:query_goals'] | ['final', 'tool:query_goals'] | True | True |
| routing-goal_read-05 | ['final', 'tool:query_goals'] | ['final', 'tool:query_goals'] | True | True |
| routing-goal_read-06 | ['final', 'tool:query_goals'] | ['final', 'tool:query_goals'] | True | True |
| routing-goal_write-01 | ['final'] | ['approval'] | False | True |
| routing-goal_write-02 | ['final'] | ['approval'] | False | True |
| routing-goal_write-03 | ['final'] | ['approval'] | False | True |
| routing-goal_write-04 | ['approval'] | ['approval'] | True | True |
| routing-goal_write-05 | ['approval'] | ['approval'] | True | True |
| routing-goal_write-06 | ['final'] | ['approval'] | False | True |
| routing-negation-01 | ['final'] | ['final'] | True | True |
| routing-negation-02 | ['handoff:research'] | ['final'] | False | True |
| routing-negation-03 | ['final'] | ['final'] | True | True |
| routing-negation-04 | ['final'] | ['final'] | True | True |
| routing-negation-05 | ['final'] | ['final'] | True | True |
| routing-negation-06 | ['handoff:research'] | ['final'] | False | True |
| routing-mixed-01 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-mixed-02 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-mixed-03 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-mixed-04 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-mixed-05 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-mixed-06 | ['final', 'tool:remember'] | ['final', 'tool:remember'] | True | True |
| routing-plan_publish-01 | ['final+plan_document'] | ['final+plan_document'] | True | True |
| routing-plan_publish-02 | ['final+plan_document'] | ['final+plan_document'] | True | True |
| routing-plan_publish-03 | ['final+plan_document'] | ['final+plan_document'] | True | True |
| routing-plan_publish-04 | ['final+plan_document'] | ['final+plan_document'] | True | True |
| routing-plan_publish-05 | ['final+plan_document'] | ['final+plan_document'] | True | True |
| routing-plan_publish-06 | ['ask'] | ['final+plan_document'] | False | True |
| routing-plan_save-01 | ['final+plan_document'] | ['final+plan_document'] | True | True |
| routing-plan_save-02 | ['final+plan_document'] | ['final+plan_document'] | True | True |
| routing-plan_save-03 | ['final+plan_document'] | ['final+plan_document'] | True | True |
| routing-plan_save-04 | ['final+plan_document'] | ['final+plan_document'] | True | True |
