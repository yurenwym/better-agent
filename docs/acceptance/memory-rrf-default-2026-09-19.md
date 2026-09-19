# 默认启用 RRF

用户确认后，`MemoryContextProvider` 默认 `ranking_strategy` 从 `legacy` 改为 `rrf`。RRF 两路等权，k=60；向量与关键词候选上限继续各 50。应用 `build_runtime` 使用 provider 默认值，新启动的应用会启用 RRF。

保留显式 `ranking_strategy="legacy"` 以回退。v1/v2 评测 seed 显式传入 legacy，防止历史基线随生产默认变更。已有实验 JSON、数据和哈希保持不变。

快照约束保持严格：RRF binding 含排序策略，旧 legacy binding 不含；同一 invocation ID 从 legacy 切到 RRF 时抛出 MemoryConflict，不静默复用或重排旧快照。新 invocation 正常使用 RRF，RRF 重试继续命中同一 pin。若进程重启后仍需重试旧 invocation，应显式使用 legacy 路径完成旧调用，不能把旧 pin 当作 RRF 结果。

## 验证

- 记忆、候选数量、评测、对话、归档摘要、checkpoint、runtime、conversation worker 共 146 项：首次 145 通过，1 项评测 fixture 在 Windows 原子替换文件时出现 PermissionError；随后整个评测测试文件 4/4 通过，包括该项及 legacy 基线断言。
- 启动契约 13 项：11 通过，2 失败。已通过的启动测试实际构建 runtime，验证默认 RRF 和两路各 50。
- 两个失败分别为模型 profile 重复 digest 的唯一约束错误、缺失价格校验预期不符。在独立测试进程将 provider 默认还原 legacy 后，两项均同样失败，确认不是本次排序切换造成。未顺带修改模型配置或计费逻辑。
- 默认 RRF、新旧 pin 隔离、RRF pin 重试和显式 legacy 兼容性测试通过。

本次修改代码默认值，未重启/部署现有应用进程，因此不宣称已运行的进程自动切换。效果依据见 [v3 真实评测报告](memory-recall-v3-topk-2026-09-19.md)：新留出集 RRF 50/50 为 49/52，原排序为 43/52。
