# 跟进与复盘意图评测集 v1

42 个合成中文场景，2026-09-20 在首次模型调用前固定标签。覆盖旅游、健身、学习、工作、生活，以及跨轮、跨目标和歧义场景。未保留盲测集，本轮只建立开发基线。

## 文件

- `cases.json`：输入、历史、预期标签和说明。
- `live-01/cases.snapshot.json`：本次实际输入快照。
- `live-01/results.json`：严格计分、每例返回值、模型配置和 token 用量。
- `live-01/*-call-*.json`：87 次真实 API 调用的请求和响应，无密钥。
- `live-01/production-prompt.txt`、`probe-instruction.txt`：实际使用的提示词。
- `live-01/semantic-diagnostics.json`：仅解析违规输出开头 JSON 的语义诊断，不替换严格分数。
- `behavior-review.md`：42 场景实际聊天行为的逐例审阅。
- 验收报告：`docs/acceptance/followup-intent-live-2026-09-20.md`。
- 开发文档：`docs/superpowers/plans/2026-09-20-followup-intent-tools-evaluation.md`。

## 标签约定与争议

review 是本次服务请求：none / once / daily / weekly / stop / clarify。缺少时区等执行参数不等于服务意图不明确。none 不表示关闭已有服务；生成复盘表不表示要求系统每日复盘。

tracking 是本次跟踪变更：none / enable / disable / clarify。文档维度 answer / save / modify 区分生成回答、明确保存、修改现有内容。modify 包括修改聊天中的计划内容，不必然是数据库文档修改授权。

本轮保留三个测量限制，不事后改金标：

1. B08 原本已有每周跟踪，改为每日；金标 tracking=enable 与“保持既有跟踪用 none”的约定有冲突。这一 tracking 错误不能直接归责模型。
2. B06 同时提到健身和露营，金标只针对新露营请求；扁平 schema 没有 target_id，不能表达两个对象。聊天实际正确交付露营攻略，探针的 daily 不足以证明真实产品发生跨目标写入。
3. L06 提醒与跟踪在金标中分离，但探针 tracking=enable 的“持续服务”定义较宽。后续需要独立 reminder 维度，不能仅凭本轮断言模型理解错误。

下一版先补全上述契约，再新增独立盲测；不能用修正过的标签覆盖 v1 后宣称模型准确率提升。

## 运行

```powershell
python backend/scripts/followup_intent_eval.py --execute --out docs/evaluation/followup-intent-v1/live-02
```

使用当前配置的真实模型，将产生 API 费用。输出目录必须不存在。`--mode probe` 或 `--mode conversation` 可单独运行；`--ids T01 F04` 可选择场景，完整基线必须跑全量。

`--execute --recover --out <已有目录>` 只从保存的响应重建缺失行，逐调用检查请求一致性，不调用网络，不重新收费。本轮一次汇总序列化故障通过该方式恢复；原始输入输出和严格失败均保留。
