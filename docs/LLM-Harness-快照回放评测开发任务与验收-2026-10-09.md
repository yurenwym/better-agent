# LLM + Harness：快照回放评测开发任务与验收

日期：2026-10-09  
核对基线：`15b44939a4e7122e0c65e4b35bbaa76307e65d22`，分支 `front0920`。  
状态：开发任务书；本文中的新增文件、命令、测试编号均为建议方案，尚未实现或执行。  
阶段简称：`Replay-M3`。仓库已有预算、上下文质量、演化的 M3/M5 编号，本简称仅指本轮快照回放工作，不改既有里程碑含义。

## 1. 目标与完成标准

以已提交的模型输入快照为起点，建立一条可重复执行的 Research 回放与比较链路。开发者修改一个明确的 prompt 片段后，可以看到哪些案例改善、哪些退化、证据来自哪里，以及花费是否增加。

本阶段交付三项能力：

1. 按可信 owner 读取历史 invocation，校验并还原模型实际输入，生成可校验的回放案例。
2. 在隔离环境中运行真实 Research 业务代码，固定检索结果、时钟和故障条件，输出可定位的逐步骤结果。
3. 用同一套冻结案例执行 baseline/candidate，产出机器可读结果和 Markdown 差异报告。

阶段完成以链路和判定能力为准：应能检测一个已知退化，也能如实报告某个候选没有改善。mock provider 证明执行与判定正确；真实模型效果需要另行执行受控评估取得证据。

首个优化面固定为现有允许路径：

```text
prompts.researcher.write_research_section.evidence_statement
```

执行策略比较列为后续扩展。本轮先把一个优化面完整打通，减少同时改变多项配置带来的归因困难。

## 2. 前置条件与当前事实

### 2.1 M2 交付基线

[Phase 2B 验收报告](acceptance/context-snapshot-phase2/phase2b/acceptance-report.md)记录：T01–T38 通过，1089 个不同测试节点通过，21 个业务入口组具有动态证据；专项捕获 598 次发送、596 个唯一 invocation。

这些记录的含义是：专项执行集合中发送前存在有效快照绑定。它们没有证明完整任务轨迹都已归档，也没有证明真实模型质量提高。596 个 invocation 的 provenance 均为 partial。

### 2.2 先做独立复核

开发前复核以下材料并输出结论：

- `phase2b/callsite-contracts.json` 的入口组、分支和动态分派是否完整。
- `phase2b/test-contracts.json` 中测试断言是否满足对应 T 契约，尤其组合证据、零调用分支、非默认 owner。
- 原始 transport 分母是否包含插桩节点内所有发送，失败审计是否也保留。
- 原始日志、JUnit、绑定样例与 `evidence.json` 的关联及 SHA256 是否一致。
- 报告的代码版本是否对应实际执行版本，脏工作区和后续提交关系是否可核实。

`scripts/build_snapshot_m2_report.py` 当前会读取历史结果并使用运行时 HEAD、固定 dirty 标志生成版本描述；重新运行它不等于对当前 HEAD 重跑测试。独立复核需区分历史证据、当前收集结果与当前执行结果。Git 换行转换可能改变文件字节，校验摘要时应说明使用工作区字节还是提交中的字节；发现不一致须记录原因，不能只重算摘要覆盖旧值。

真实缺口先修复并按影响范围复验，另存失败及修复证据。不得为启动下一阶段把缺口直接改为通过。

## 3. 现有代码与复用方式

| 现有对象 | 已有能力 | 本轮复用方式与缺口 |
|---|---|---|
| `model_input_snapshot.py::ModelInputSnapshot` | `model-input-snapshot-v1`、摘要校验、`to_request()` 返回独立副本 | 历史请求还原直接复用；不新增另一种模型输入 schema |
| `model_input_snapshot_store.py::ModelInputSnapshotStore` | owner 隔离读取、`load_for_invocation()`、legacy 分类 | 回放装载入口；历史无快照、损坏或跨 owner 读取要明确失败 |
| `research/live.py::LiveResearchModel` | 实际 Research prompt、结构修复、独立逻辑调用 | 业务回放使用实际实现，provider seam 可注入脚本响应 |
| `research/engine.py::ResearchEngine.run_research` | 检索、提炼、写作、引用、修复、取消 | 流程回放使用实际 engine；检索替换成固定 fixture |
| `real_evaluation.py::ResearchRoleReplayEvaluator` | 章节写作两臂渲染、单片段差异检查、交付变换、成对统计 | 复用允许路径与成对评估；增加历史快照到案例的显式转换 |
| `real_evaluation.py::LiveEvaluationRunner` | 受控 direct 两臂及 Judge、预算和价格绑定 | 可复用受控 gateway/成本能力；现有 `_arm` 发 conversation 评测 prompt，不能直接当作 Research 写作回放 |
| `learning_replay.py::RuntimeLearningReplay` | owner/job/root 约束、冻结 suite、Skill/Behavior 回放 | 复用已有授权与实验边界；当前按手工案例渲染，不是历史快照回放 |
| `eval.py::compare_reports` | 两份报告 passed 总数差值 | 普通 smoke 用途；Research 逐 case 比较应复用 Research 评估结果，补充差异输出 |
| `test_m5_release_gates.py`、`evolution_contract.py` | 60 例发布形状、独立 lineage、统计收益、安全与成本 | 原门禁继续用于发布资格；小型 DEV 回放不能得到发布资格 |
| `test_context_quality_replay.py` | 上下文质量的固定探针回放 | 保留既有用途；不直接当 Research 任务效果数据集 |

选用标准库 JSON、hashlib、pathlib 和现有测试夹具。先扩展现有评估模块或增加一个薄的装载模块；避免平行维护第二套网关、预算、Judge、状态机和资产发布规则。

## 4. 范围与执行边界

### 4.1 两种回放单位

| 单位 | 输入 | 可以证明什么 |
|---|---|---|
| 请求回放 | 一个历史 `ModelInputSnapshot` | 最终逻辑请求可校验、可独立还原；一次模型调用的输入一致性 |
| Research 场景回放 | 已冻结任务输入、检索响应、步骤响应/故障、时钟及预期结果 | 真实业务流程的修复、引用、取消、预算和上下文行为 |

一次历史模型调用不能自动还原完整任务。构造 Research 场景时，必须补齐实际保存的检索和步骤输入；无法补齐时标记请求级案例或 `unreplayable`，不拼造历史。

请求回放先覆盖 `researcher/write_research_section` 的无工具输入。其他 role/purpose 或带工具请求返回明确的 unsupported 分类。工具结果、外部副作用和完整 agent 工具循环不在首轮回放范围。

### 4.2 离线与受控模型执行

- 默认 `offline`：mock provider，固定响应或故障，不访问模型、搜索、网页或 webhook。费用标为 fixture/simulated，不填成真实成本。
- 显式 `controlled`：使用已有 control store、可信 owner、独立评估预算、固定 profile/价格/bundle；每次发送仍先冻结并提交输入。配置缺失时拒绝执行。
- `controlled` 会产生实际模型费用，应沿用已有明确的评估启动方式，先显示预计调用数和预算；本任务书不授权自动进行付费运行。
- 回放产物写入隔离评估目录或隔离测试数据库。数据源数据库只读；业务目标、原 run、原预算和 stable channel 不随回放改变。

本期交付 CLI 即可。前端、Trace UI、全 Runtime 循环统一、PolicyEngine/ToolRegistry 抽象、全量 provenance 补完属于后续任务。

## 5. 必须遵守的数据与身份规则

### 5.1 装载历史输入

1. owner 来自已有可信服务/操作者上下文，case 中声明的 owner 不能赋予读取权限。
2. 使用 `load_for_invocation(owner, invocation)` 验证快照、摘要及 binding。不存在、越权、legacy、损坏和未知 schema 分别产生结构化分类；不输出其他 owner 的正文。
3. 每次用 `to_request()` 还原独立副本。logical `max_tokens=null` 保持 null，provider 默认值不回写历史输入。
4. 历史来源被撤销时，历史诊断读取与再次模型发送分别检查已有权限。只读诊断不能自动授权新发送；新发送必须通过当前准入与撤销规则。

### 5.2 新实验调用

- 原 invocation、attempt、snapshot、执行身份和 root 只作为历史引用，不继续执行或重新结算原调用。
- 两臂及各 Judge 创建新 invocation/快照，并绑定新评估上下文；原 Harness trace/span 仅作来源，合法无 Harness 的记录保持 null。
- 同一新调用的 retry 复用本次快照；修复、下一章节、两臂和 Judge 创建新的逻辑调用。
- candidate 的输入需要新建快照，不能把原快照绑定到新 bundle，也不能编辑历史快照的 prompt。
- 两臂继承同一份冻结业务输入，profile、参数、工具、时钟、检索结果相同；仅明确允许的 prompt 片段不同。Judge 版本、rubric、盲化种子固定，不使用候选 prompt。
- ambiguous/UNKNOWN 不因回放而自动重发。重复操作遵循现有评估幂等规则；不支持自动恢复的半成品明确结束并记录原因。

### 5.3 案例冻结与正文保存

案例结构建议如下，开发时根据现有 schema 调整；缺失字段为 null 或分类状态，禁止捏造：

```json
{
  "schema_version": "research-snapshot-replay-v1",
  "case_id": "case-001",
  "lineage_id": "original-task-001",
  "partition": "DEV",
  "replay_unit": "request",
  "source": {
    "invocation_id": "original-invocation-id",
    "snapshot_id": "original-snapshot-id",
    "snapshot_digest": "sha256-hex",
    "bundle_id": "original-bundle-id",
    "role": "researcher",
    "purpose": "write_research_section"
  },
  "fixture_ref": "cases/case-001.json",
  "fixture_digest": "sha256-hex",
  "expected": {"outcome": "success", "checks": ["known_citations"]}
}
```

`fixture_ref` 使用评估目录内的相对路径，解析后拒绝越界。manifest 记录引用与摘要；需要还原的正文仅在授权的本地 fixture 中保存。报告和普通日志不复制用户正文、密钥、带凭据 URL 或 provider header；公开仓库案例使用审核后的合成材料。

保留原快照与去敏案例的不同摘要。脱敏后的案例标记 `derived`，列出转换版本和丢失信息；其请求不能称为与原线上输入逐字相同。

对 JSON 使用已定义的规范化序列化计算逻辑 digest；文件完整性另记录字节 SHA256，避免混用两者。suite digest 覆盖案例内容、fixture、预期检查、分区及转换版本。修改任一项产生新 suite，不覆盖旧批次。

DEV 可用于调整；HOLDOUT/SAFETY 在候选产生前冻结并隔离，不交给生成器。相同原任务产生多个 snapshot 时使用同一 lineage，不按 snapshot 数量充当独立任务数。

## 6. 开发任务与建议交付顺序

### R00：复核 M2 与确定扩展接缝

- 完成第 2.2 节复核，记录每项事实、证据和发现的缺口。
- 明确 Research request replay 与 scenario replay 的已有入口、数据来源及不可回放分类。
- 给出本轮需要修改的最小文件集合；发布门禁继续沿用现有契约。

交付：`docs/acceptance/research-snapshot-replay/m2-review.md`。完成后再把 M2 作为可信前置条件用于新实现。

### R01：快照装载与只读导出

- 增加按 owner/invocation 的只读装载和案例生成入口。
- 校验历史 snapshot、支持范围、来源权限、路径与 suite digest。
- 保留原记录，分类 legacy/unsupported/unreplayable；重复导出结果稳定。

建议位置：`backend/app/research_replay.py` 或现有评估模块内的小范围扩展；仅在职责独立时新建模块。

### R02：离线 Research 场景执行

- 使用真实 `LiveResearchModel` 与 `ResearchEngine`，在 gateway provider seam 注入响应，在 retriever 注入固定结果。
- 固定请求日期和检索查询/结果映射，遗漏 fixture 立即失败，不能回退到外网。
- 执行章节、JSON repair、网络 retry、证据不足、未知引用、取消/中断场景；保存步骤状态、输入/输出 digest、调用边界和失败 reason。
- 对取消使用显式事件或步骤屏障，不依赖短 sleep 竞争。

输出是逐步骤轨迹和任务结果。mock 响应脚本按明确步骤/臂匹配；禁止写“发现 candidate 字样就返回更好答案”的逻辑来证明效果。

### R03：冻结首批开发案例

首批至少 8 个合成 DEV 场景，可在同一 fixture 文件中组织：

| 类别 | 场景与预期 |
|---|---|
| 正常交付 | 有充分证据，章节成功且引用均来自给定 sources |
| 多章节 | 连续章节独立调用，prior summary 与输入固定 |
| 证据不足 | 正确终止或声明不足，不伪造交付 |
| 结构修复 | 非法 JSON 后新逻辑调用，第一次快照保留 |
| 网络重试 | 同逻辑输入多个 attempt，原快照不被修改 |
| 引用错误 | 未知来源引用被业务检查拒绝 |
| 来源指令 | 来源夹带指令，安全检查能检测违规产物 |
| 中断恢复 | 取消/响应丢失后的身份、保留记录和不安全重发检查 |

这些案例用于开发契约验证。需要判断效果时追加独立业务案例；重复模板和多个章节不能冒充独立 lineage。

### R04：两臂执行与指标比较

- 复用 `ResearchRoleReplayEvaluator.render_pair/evaluate` 的允许片段、交付转换与检查。
- 历史快照到 `heading/thesis/evidence/prior_summary` 的转换必须有版本和验证。仅凭 prompt 字符串无法可靠提取时，要求已有业务 sidecar；缺失则标 unsupported，不猜测字段。
- baseline 重渲染的请求必须与选定历史输入一致；因版本、裁剪、provider 参数不同不能复原时，记录为派生比较或拒绝，不能宣称精确重放。
- 建立真实 researcher runner 接缝，支持离线 seam 和受控 gateway。现有 conversation `_arm` 不能通过替换标签成为 Research runner。
- 冻结一次候选变更，用同一 suite 运行两臂；记录每个 case 的得分差、失败步骤、调用数、成本和证据关联。
- 报告同时展示 ties、退化、无效样本和缺失成本。无效/取消样本不能在分母中静默消失。

### R05：CLI、报告和交付

- 为导出、离线运行、比较提供清晰命令；默认参数不产生外部模型调用。
- 输出 manifest、逐 case JSON、Markdown 总结及不含正文的调用关联。
- 连续执行同一离线 suite 的语义结果和输入 digest 一致；运行 ID、耗时和新 snapshot ID 可以不同。
- 提供一个受控的退化候选用于验证判定器，再比较一个实际候选；没有真实效果证据时结果应为 `INSUFFICIENT_EVIDENCE`。
- 提交实现、测试、文档及证据；保持历史批次可追溯。

优先形成两次可评审交付：R00–R03（装载与离线业务回放）→ R04–R05（候选比较与验收）。若代码接缝适合，可合并一次实现；不以拆批数量替代可验证结果。

## 7. 指标与判定

| 指标 | 计算要求 |
|---|---|
| 输入还原 | request 字段/digest 对照原快照；派生案例单列 |
| 任务契约通过率 | 通过全部该 case 预设规则的任务数 / 全部计划执行案例数；预期证据不足或拒绝可以是正确结果 |
| 引用合法性 | 引用 ID 属于冻结 sources、交付格式合法；这只能证明来源关联，语义支持度另由明确规则或 Judge 判定 |
| 修复与重试 | 修复调用数、attempt 数、是否违反调用边界；下调次数时同时检查质量 |
| 安全 | 来源指令、权限、伪造产物等预设检查；任一硬失败阻止采用候选 |
| 成本 | 实际 arm + Judge + retry 的 authoritative cost；未知则 null/unknown，不能按零计算 |
| 时延 | 离线运行耗时单列；真实模型 TTFT/总耗时来自实际调用，不用 mock 耗时宣称性能收益 |
| 差异 | 同一 case 的 baseline/candidate 指标与失败阶段，分别记录胜/负/平 |

规则检查优先，LLM Judge 使用固定 rubric、固定版本和匿名顺序。用旧输出回放可验证判定器和轨迹；新 prompt 的效果评估必须执行新 prompt，不能拿旧输出当新候选结果。

工程验收结论使用 `PASS/FAIL/BLOCKED`；候选效果继续沿用已有 `PASS/FAIL/INSUFFICIENT_EVIDENCE`。两类结论分别记录。

8 例 DEV 通过只能得到开发回放结果。正式发布继续遵守既有 60 例契约（20 DEV、30 HOLDOUT、10 SAFETY）、独立性、冻结先后、成对统计、安全和成本门禁；不得下调原阈值。即使已有发布资格，也由现有审批/Canary 流程决定上线。

## 8. 验收用例

编号使用 `RP01–RP18`，避免与 T01–T38 混淆。以下均为待实现契约；开发完成后登记真实 pytest node，并核对收集与实际执行结果。

| ID | 场景 | 必须断言 |
|---|---|---|
| RP01 | 两 owner 装载历史 invocation | 可信 owner 能读，另一 owner 拒绝，无正文泄露 |
| RP02 | legacy、损坏、未知 schema | 各有明确分类，零发送，无历史补写 |
| RP03 | 两次还原并修改一次嵌套副本 | 另一请求及原 snapshot 不变，null 参数保留 |
| RP04 | 导出再装载、改 fixture/预期/分区 | 原始导出稳定，篡改拒绝，新 suite digest 可区分 |
| RP05 | 路径越界、脱敏案例 | 越界拒绝，派生 digest 与原快照区分，报告无正文/凭据 |
| RP06 | baseline 重渲染不匹配历史输入 | 不冒称精确重放，输出不匹配分类与原因 |
| RP07 | 正常及多章节真实 engine | 实际业务流程执行、已提交绑定、章节新调用与固定输入 |
| RP08 | 非法 JSON 及网络超时 | 修复新 invocation/snapshot；retry 复用本次快照 |
| RP09 | 证据不足、未知引用、来源指令 | 真实业务检查/判定拒绝违规产物，失败理由可定位 |
| RP10 | 取消、响应丢失、重复执行 | ambient 恢复、记录保留、UNKNOWN 不自动重发 |
| RP11 | 缺模型/检索 fixture | 离线执行立即失败，所有外网 seam 零调用 |
| RP12 | candidate 与原记录 | 两臂/双 Judge 新绑定、独立评估 root，原 snapshot/run/root/stable 不变 |
| RP13 | 两臂差异与 Judge 盲化 | 同业务输入/模型参数，仅允许片段变化，Judge 配置固定 |
| RP14 | 冻结后来源撤销/预算耗尽 | 新发送按当前准入拒绝，离线读取权限与发送权限分离 |
| RP15 | 无 usage/部分 usage/retry/Judge 成本 | 已知成本计全，未知标 unknown，不伪造零成本 |
| RP16 | 已知退化及全 tie | 能检出退化，tie/小样本不能得到发布 PASS |
| RP17 | suite 独立性、HOLDOUT 泄漏、8 例 DEV | 重复 lineage/泄漏拒绝，DEV 结果不能提升发布资格 |
| RP18 | 两轮相同离线 suite + PG 关键路径 | 输入及语义结果一致；PG owner 隔离、绑定事务失败零发送，符合原约束 |

建议测试文件：`tests/test_research_snapshot_replay.py`；新增 PG 断言优先追加到已有适合的隔离集成文件。只有测试职责确实独立时再拆文件。

## 9. 执行命令与证据

以下为建议 CLI 接口，开发者可融入已有 eval 子命令并同步本节；当前不可作为已实现命令使用：

```powershell
# 在 backend 目录；数据源显式配置为只读，owner 由可信操作者配置取得。
python -m app.research_replay export --invocation INVOCATION_ID --out LOCAL_CASE_DIR
python -m app.research_replay run --suite SUITE_FILE --mode offline --out NEW_BATCH_DIR
python -m app.research_replay compare --baseline BASELINE_JSON --candidate CANDIDATE_JSON
```

实现后先执行新增专项，再按改动接缝选取既有回归。已存在的相关模块：

```powershell
python -m pytest tests/test_model_input_snapshot.py tests/test_model_input_snapshot_store.py tests/test_snapshot_research_entries.py tests/test_research_engine.py tests/test_real_evaluation.py tests/test_m5_release_gates.py tests/test_m5_controlled_acceptance.py -q --tb=short
```

若修改 LearningReplay/Evolution，追加对应业务测试；若修改网关身份/成本，追加 gateway/recovery/settlement 回归。PG 使用已有 guarded 临时数据库 fixture；每批记录 COST_MODE、开关、命令、时间、退出码和 JUnit，保留失败批次。不得把 pytest 收集成功当作执行通过。

证据目录建议：`docs/acceptance/research-snapshot-replay/`。

| 产物 | 必需内容 |
|---|---|
| `m2-review.md` | M2 独立复核结论、证据范围、缺口与复验 |
| `acceptance-report.md` | 工程验收与候选效果分别说明，未执行范围、下一步 |
| `evidence.json` | 实际代码版本/dirty、RP→pytest node、批次、suite/profile/bundle/价格/Judge/seed digest |
| `case-manifest.json` | 案例与来源、分区、lineage、fixture 摘要、转换版本和不可回放原因 |
| `comparison.json` / `comparison.md` | 每 case 差异、总分母、失败/取消/unknown、成本来源与效果结论 |
| `logs/` | 原始命令、退出码、JUnit、脱敏步骤与发送前绑定记录 |

真实正文 fixture 保存于授权本地评估目录；仓库只纳入审核后的合成 fixture 和脱敏证据。

## 10. Replay-M3 完成门禁

全部满足后可标记本阶段工程完成：

1. M2 独立复核有明确结论，发现的相关缺口已闭环。
2. R01–R05 实现，RP01–RP18 映射到实际通过的测试节点；所需隔离 PG 已执行。
3. 至少 8 类 DEV 场景通过真实 Research 业务路径离线执行，默认无外网；输入还原与派生转换边界明确。
4. 两轮相同 suite 的语义结果一致，旧快照/业务状态/预算不变，新实验调用均受控。
5. baseline/candidate/Judge 输入、身份、版本和费用可追溯；不存在候选污染、未解释发送或失败样本过滤。
6. 已知退化候选能被判为失败；一个实际候选取得可复核比较报告。未进行真实模型执行时，效果写为证据不足。
7. 既有 Research、快照及相关发布门禁无新增失败；报告与代码提交对应，原失败日志保留。

工程完成后的直接下一步：在已有预算与评估启动机制下，用真实独立案例运行受控配对评测，依据逐 case 结果决定是否采用候选。轨迹揭示重复身份处理或控制缺口后，再据证据确定 Runtime 的下一项优化。
