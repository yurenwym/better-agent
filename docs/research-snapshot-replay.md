# Research 快照回放 CLI

默认模式是 offline；只注入固定 provider 和检索结果，执行真实 ResearchEngine、LiveResearchModel 和受控网关。模型、检索、日期、来源和预期结果都纳入冻结 fixture/suite 摘要。报告只保存身份、摘要和判定；正文只在授权本地 fixture 或隔离评估数据库内。

## 合成 DEV 回放

在 `backend` 目录执行，每次使用新的输出目录：

```powershell
python -m app.research_replay run --suite tests/fixtures/research-snapshot-replay-v3/case-manifest.json --out ../outputs/replay/base-001
python -m app.research_replay run --suite tests/fixtures/research-snapshot-replay-v3/case-manifest.json --out ../outputs/replay/candidate-001 --arm candidate --fragment-file tests/fixtures/research-snapshot-replay-v3/candidate.txt
python -m app.research_replay compare --baseline ../outputs/replay/base-001/report.json --candidate ../outputs/replay/candidate-001/report.json --out ../outputs/replay/comparison-001
```

允许变更路径仅为 `prompts.researcher.write_research_section.evidence_statement`。candidate.txt 是待评估的真实 prompt 候选；离线脚本固定且不根据请求出现“candidate”等字样决定质量。本轮新 prompt 确实经过 Research 写作路径，但固定输出仅证明输入命中、执行边界和判定能力，没有模型效果证据。

regression.txt 与 fixture 中明确冻结的 `regression_manifest_digest`、`scripts.regression` 配对，回放故意缺失引用的旧输出，验证判定器能检出退化。它不能证明该 prompt 在真实模型上必然导致退化。正常 candidate 与 baseline 的脚本相同，未假造改善。

8 类 DEV：充分证据、多章节、证据不足、JSON 修复、网络 timeout retry、未知引用、来源指令、中断/响应丢失。来源指令 fixture 的正常输出遵守安全规则；专项另外注入违规输出验证拒绝。UNKNOWN、取消、缺 fixture 的结果保留在比较分母；原始结果中的预期拒绝仍可能是工程契约通过。

输出 `case-*.json`、`report.json` 和 `batch-state.json`；目录已存在时拒绝执行，STOPPED/UNKNOWN 不自动恢复。旧 suite 不覆盖，改案例后冻结到新目录。`fixture_digest` 是规范化 JSON 的逻辑摘要，`fixture_sha256` 是 UTF-8/LF 文件字节摘要；suite 摘要覆盖完整 manifest 和每个 fixture 摘要。

## 历史只读导出

owner 和数据库来自可信操作者环境，case 内 owner 不授予权限。SQLite 以 mode=ro + query_only 打开；PG 使用 READ ONLY 事务；不会初始化/迁移数据源。

```powershell
$env:REPLAY_OPERATOR_OWNER = 'authorized-owner'
$env:REPLAY_SOURCE_DATABASE = 'D:/authorized-local-history/agent.db'
python -m app.research_replay export --invocation INVOCATION_ID --lineage ORIGINAL_TASK_ID --out ../outputs/replay/export-001
```

支持 researcher/write_research_section 无工具请求；missing（含跨 owner）、legacy、corrupt、unknown schema 和 unsupported 分别分类，拒绝结果不含正文。未提供原任务 lineage 时标 `unknown_task_request_only`，不能用于独立任务效果统计。

有业务 sidecar 时可加 `--sidecar SIDECAR.json --baseline-manifest ORIGINAL_MANIFEST.json`：

```json
{"version":"research-write-sidecar-v1","heading":"标题","thesis":"论点","evidence":[["事实","source_id"]],"prior_summary":""}
```

转换检查所有逻辑请求字段；任一字段不一致得到 `BASELINE_INPUT_MISMATCH` 和字段名。max_tokens=null 保持 null，不改为 provider 默认值。没有 sidecar 不猜测 prompt 文本，分类 `UNSUPPORTED_MISSING_BUSINESS_SIDECAR`。导出不生成历史响应、检索或时钟；导出日期注明诊断默认值，不能当历史时钟。空 scripts 表示还需要显式保存的响应 fixture 才能发送。

需要去敏时，Python `export_case(..., replacements=...)` 生成带转换版本/丢失信息的 derived 案例，保留原 snapshot digest，与新 fixture 摘要分别记录；derived 不能用于精确两臂比较。真实 fixture 不纳入公开仓库。

## 受控执行接缝

`ResearchEvaluationRunner` 复用已有 LiveEvaluationRunner 的网关、价格 reservation、context 和 attempt 成本；研究臂实际调用 LiveResearchModel.write。输入配置须有可信 owner、独立 evaluation root、两臂 bundle、同一 researcher profile/价格、两个固定 Judge profile/价格和 evaluator bundle。预检整份 manifest 仅允许一个片段变化，并核验 bundle 当前路由与 profile 无 fallback。

`runner(config)` 和 `judge(config)` 可传给 `ResearchRoleReplayEvaluator.evaluate`。受控模式必须使用 PostgreSQL 的独立 evaluation root 与 CostService；当前来源 snapshot digest、权限和撤销在新发送前再检查。Judge 只有匿名 left/right，固定 prompt、rubric 版本与盲化 seed；所有调用创建新 invocation/snapshot，成本汇总包含 arm/Judge/retry，部分 usage 或缺价格保持 unknown。

CLI `--mode controlled` 拒绝自行启动付费运行，返回 `CONTROLLED_REQUIRES_EXISTING_AUTHORIZED_EVALUATION_START`。应由现有明确评估启动方式先展示预计调用数、独立预算并取得启动授权后，在授权服务上下文调用此接缝。本轮没有付费启动授权，也未执行真实模型。

HOLDOUT/SAFETY 不经过 DEV CLI，返回 `SEALED_PARTITION_REQUIRES_MANAGED_EVALUATION`；同 lineage 多案例直接拒绝。8 例 DEV 不获发布资格，正式发布仍采用既有 60 例、冻结先后、独立性、统计、安全、成本与审批/Canary 门禁。

## 验收

从仓库根目录：

```powershell
python scripts/run_research_replay_acceptance.py UNIQUE_BATCH_NAME
python scripts/review_snapshot_m2.py
```

验收脚本记录命令、UTC 开始/结束、COST_MODE=enforce、执行版本/dirty、退出码、JUnit 和失败数量；PG 沿用 guarded 临时 Docker fixture。历史失败日志保留，不重算摘要覆盖旧证据。验收结果见 `docs/acceptance/research-snapshot-replay/acceptance-report.md`。
