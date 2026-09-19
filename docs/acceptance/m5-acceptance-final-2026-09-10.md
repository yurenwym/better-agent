# M5 Controlled Evolution 最终开发与验收记录（2026-09-10）

## 结论

M5 开发与离线控制门禁已完成，最终验收状态为：

- `stage_complete = true`
- `status = EVIDENCE_INSUFFICIENT`
- `release_decision = NO_CANDIDATE`
- `canary_started = false`
- stable bundle 未变化：`bundle_8040b4cc18e6bf2282d15992`

这是设计允许的有效研究终态，不是发布成功，也不证明新提示词优于 stable。当前只有 1 个符合口径的独立生产研究失败谱系，未达到生成候选所需的至少 3 个独立生产谱系；因此系统在任何新模型调用前停止，没有生成候选、启动 Canary 或执行发布。

## 已交付能力

- 将生产经验观察与付费候选生成分离；验收、测试和合成记录不能作为生产 `DISCOVERY` 证据。
- 候选生成要求至少 3 个独立、有效、生产来源的问题谱系，并要求调用方显式选择 experience ID。
- 候选权限限制为 researcher 角色的 `prompts.researcher.write_research_section.evidence_statement`，禁止扩大权限或修改其他路径。
- 支持真实角色、固定 `DEV/HOLDOUT/SAFETY` 分区的 champion/challenger paired replay。
- 真实评测冻结模型 profile、价格快照、根预算和 stable bundle；禁止 fallback 与网络重试，首个错误停止批次。
- 批次 owner 贯穿候选 proposer、预算与模型调用，不再错误固定为 `local-user`。
- `ExperienceObserver` 能将研究重试归并到根任务，并将 `[ACCEPT-...]` 研究记录标记为 `acceptance`。
- `ModelGateway` 支持复用可选 HTTP client，同时保持默认调用兼容。
- 执行器不自动启动 Canary、不自动晋升；证据不足时返回零网络、零费用的 `NO_CANDIDATE`。
- 当前批次费用证据只统计其根预算，不再混入同 owner 的历史模型调用。

## 验证结果

2026-09-10 最终复验：

```text
python -m py_compile app/experience_observer.py app/model_gateway.py app/evolution.py scripts/m5_live_acceptance.py
pytest 相关组合回归：72 passed in 21.07s
git diff --check：通过（仅有 Git 的 LF/CRLF 工作树提示）
Alembic：20260909_0005 (head)
```

此前已通过的基线未重复消耗资源：M5 专项测试 `8 passed`，核心兼容回归 `62 passed`。最终的 72 项组合回归覆盖新增的 owner 传播、验收 provenance、研究重试谱系归并、执行器门禁及费用范围修复。

## 真实付费批次证据

| 批次 | 结果 | 模型 attempts | 计费（microusd） | 关键证据 |
| --- | --- | ---: | ---: | --- |
| `001` | `FAILED` | 0 | 0 | 暴露 owner/运行时路由绑定错误；未发出模型网络请求，随后完成修复。 |
| `002` | `FAILED` | 54 | 1,362,528 | 候选生成成功并进入 paired replay；约第 55 个请求处 TLS/连接建立失败。零重试、首错停止、费用记账和 stable 保护生效。 |
| `003` | `FAILED` | 1 | 25,232 | proposer 首次请求即 `provider_unavailable`；首错停止，stable 未变化。 |
| `004` | `EVIDENCE_INSUFFICIENT` | 0 | 0 | 只存在 1 个合格生产研究失败谱系；在网络启动前返回 `NO_CANDIDATE`。 |

付费批次 `002 + 003` 合计 55 次 attempt、`1,387,760 microusd`，低于批准上限 300 次 / `7,600,000 microusd`。Windows 主机侧 DNS 与 TCP 443 可达，但 HTTPS/TLS 请求超时；Docker 内可以建立到 `api.deepseek.com:443` 的 TLS。由于随后确认生产证据不足，没有通过 Docker 继续发起付费评测。

## 数据库与证据治理

- PostgreSQL 容器：`better-postgres-1`
- 数据库：`better_agent`
- Alembic：`20260909_0005 (head)`
- stable：`bundle_8040b4cc18e6bf2282d15992`
- provenance：`acceptance=22`、`production=34`
- 符合 M5 选择口径的独立生产研究失败谱系：1

已将 22 条被误标为 production 的历史验收记录精确修正为 `acceptance`，包括 `[ACCEPT-...]` 研究任务和本轮 `m5-live-*` 验收记录；没有删除历史数据。旧生产记录创建时尚无 `target_role` 值，因此谱系统计采用执行器的实际口径：`source_kind=research`、`provenance=production`、`dataset_partition=DISCOVERY`、失败/部分成功且状态有效。

## 发布判定

本次不得启动 Canary、不得发布、不得宣称质量改善。后续只有在自然生产流量积累至少 3 个独立且可信的研究失败谱系，并由人工明确选择对应 experience ID 后，才可新建一次受控付费批次；不得复用验收故障、合成故障或网络故障补足证据门槛。

## 证据文件

- `m5-live-results-2026-09-10-001.json`
- `m5-live-results-2026-09-10-002.json`
- `m5-live-results-2026-09-10-003.json`
- `m5-live-results-2026-09-10-004.json`
- `m5-live-batch-budget-2026-09-08.md`
- `../architecture/m5-evolution-development-review-2026-09-09.md`
