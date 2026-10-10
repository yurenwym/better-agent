# Loop-M1 分步执行记录

## T00 — 完成

日期：2026-10-09。工程验收：PASS。路由效果：INSUFFICIENT_EVIDENCE。

- 70 个合成案例，46 DEV / 24 HOLDOUT；12 类均满足任务要求最低数量。
- 作者检查了意图、期望与禁止结果，材料不含真实用户正文或凭据；尚无独立人工复核。
- HOLDOUT 每类 2 例，完整案例按 case_id 排序后，以 UTF-8、排序键、紧凑 JSON 计算 SHA256，冻结在 `holdout-freeze.json`。正常运行只校验，不重写冻结文件。
- DEV 可修改；HOLDOUT 不可修改。后续提示词开发不读取 HOLDOUT，只使用 DEV 和聚合结果。当前作者参与案例编写，因此本集不应声称对当前作者完全盲测；真正独立盲测需由未接触案例的评审者执行。
- mock 固定返回 `final`，与 expected 无关。仅验证报告统计，不实例化生产路由、不访问模型或搜索。不用 mock 命中比例评价业务路由。
- 无效、缺失和取消响应保留在计划总数中；误触发单独统计；mock 的 token、成本、首字延迟为 null。

仓库根目录运行：

```powershell
python backend/scripts/eval_agent_loop_routing.py --mode legacy --provider mock --output docs/acceptance/agent-loop-m1/mock-legacy.json
```

结果：退出码 0，70 个案例全部输出，见 `mock-legacy.json`。

在 backend 目录运行：

```powershell
python -m pytest tests/test_agent_loop_routing_eval.py -q --tb=short
```

结果：退出码 0，5 passed。覆盖禁止网络、完整案例输出、DEV 改动不影响 HOLDOUT、HOLDOUT 篡改拒绝、无效/取消/缺失分母、禁止结果、mock 不抄期望及 CLI。

本步没有修改业务路由、提示词或前端，没有付费调用，没有重跑全量回归。

## 当前交付

已完成真实授权评测及默认 loop 切换。最终结果、成本、回归及 T14 回滚兼容保留限制见 [acceptance-report.md](acceptance-report.md)。逐案比较见 [comparison.md](comparison.md)，测试映射见 [test-mapping.md](test-mapping.md)。
