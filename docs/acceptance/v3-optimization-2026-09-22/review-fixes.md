# V3 代码复查修复记录

日期：2026-09-22  
范围：修复代码复查发现的运行缺口。用户已明确排除 JEV 预算改造；本轮不修改 JEV 客户端、计价或费用入账。

## 已实现的修复

| 问题 | 修改 | 验证 |
| --- | --- | --- |
| 测试库 URL 查询参数可覆盖已校验目标 | 拒绝 dbname、host、hostaddr、port、service、servicefile、options 路由覆盖；解码路径后检查库名 | 守卫测试覆盖查询参数与编码绕过，无需连接危险目标 |
| 正式启动未接 replay | 新增 `RuntimeLearningReplay`，由 `startup.py` 注入，调用现有模型网关 | Skill 和 Behavior 实际构造执行输入；记录输入/输出摘要；无生产启用操作 |
| 生成/Judge 未显式绑定学习上下文 | cycle 中绑定 owner、原 root budget、runtime bundle，结束后恢复 ContextVar | replay 调用上下文断言及学习回归 |
| Skill 每次使用 1.0.0 | 用确定性内容摘要生成合法 prerelease 版本；保留完整包摘要校验 | 同内容复用、异内容新版本、SQLite/PG 并发用例 |
| 同一 job 可绑定不同候选 | 候选事件绑定 job 和包摘要；PostgreSQL 按 owner/name 事务锁串行创建 | 重复 job 幂等，改变内容明确拒绝 |
| 无写入拒绝被记 UNKNOWN | 新增 `SkillCandidateRejected`，仅用于验证/候选写入前冲突 | 预检失败 REJECTED；模拟写入中断仍 UNKNOWN |
| Skill 回滚丢失上一默认版本 | 启用事件持久化 previous version，事务内切换与恢复；拒绝基线已变化的晋升 | 连续两轮晋升/回滚、重复回滚与过期基线测试 |
| Experience 不能提供 Memory 用户证据 | 通过 thread_message、turn、run、research 的数据库关联解析用户消息 | 不使用 agent 自报 evidence_refs；校验 owner、用户角色、线程有效性 |
| Memory 晋升前来源可能变化 | 保存用户原文摘要，接受 proposal 前重新检查 | 修改原文后拒绝，跨 owner 无来源 |
| 候选等待后不能继续 | 保存 candidate/evaluation 到 checkpoint，新增显式 resume API | 不重新调用 JEV/Generator，不续行 UNKNOWN；版本 CAS 防止重复推进 |
| Canary 推进只有定义无调度 | 接入既有 `LearningService.run_once`；空队列也执行 | 完成/失败/等待分支测试；终结候选不再扫描 |
| Canary 停止与回滚不完整 | 检查 deployment 状态、deadline、完成样本；回滚传 reason；完成重复调用幂等 | 暂停阻止晋升，但仍处理安全回滚；既有 Evolution 测试回归 |
| owner 配置未限制 V3 目标 | JEV 决策后、生成前校验 allowed_assets | 未开放目标不生成候选 |

## 运行时配置

### replay

设置 `BETTER_AGENT_LEARNING_REPLAY_FILE` 为运维管理的 JSON 文件路径，启动时读取并在 `learning_snapshots` 冻结内容摘要。未配置或目标无套件时继续返回 `NEEDS_REPLAY`，不会伪造评估通过。

文件格式示例（仅说明结构，不是有效发布评测集）：

```json
{
  "suites": [
    {
      "owner_id": "local-user",
      "target": "SKILL",
      "cases": [
        {
          "id": "timeout-01",
          "task": "根据给定数据库超时日志说明排查顺序。",
          "relevant": true,
          "rubric": {"deterministic_required": ["连接池"], "deterministic_forbidden": ["已自动重启生产库"]}
        },
        {
          "id": "unrelated-01",
          "task": "写一句春日问候。",
          "relevant": false,
          "rubric": {"deterministic_required": ["春"]}
        }
      ]
    }
  ]
}
```

- 每个 owner/target 一个套件；Skill 必须同时包含 relevant=true/false，Behavior 必须同时包含 target_behavior=true/false。
- 套件须在学习 job 创建之前冻结。新增套件不能反向为旧候选补造 holdout；需要生成新的合法候选。
- Skill 是无工具的 instruction-only 回放，使用独立适用性选择调用，基线为候选创建时记录的上一默认 Skill；不能用它证明真实工具操作的效率收益。
- Behavior 复用 `ResearchRoleReplayEvaluator.render_pair` 和生产 researcher/write_research_section 提示构造器；仅支持允许的 evidence_statement 片段。
- 此处是学习评估，不能替代 Evolution 原有 60 例 release evaluation；发布审批仍执行原门禁。
- 无效套件在启动时拒绝；网络/解析/执行失败不算成功。Judge 无有效比较结果不能通过晋升门禁。

### 候选续行

`POST /api/learning/jobs/{job_id}/resume`，沿用本地 owner、Origin 与 CSRF 校验。

```json
{"expected_version": 3, "approve": false}
```

`expected_version` 从 learning history 读取；`approve=true` 表示对该版本所绑定候选的明确人工批准。服务端只接受已保存候选的 NEEDS_REPLAY、PENDING_APPROVAL、PENDING_RELEASE_EVALUATION；不接受 UNKNOWN、已晋升或版本冲突作业。

续行保留原 root budget，不重置费用、尝试次数或预算截止时间。原预算过期/耗尽仍被现有网关阻断；本文没有增加自动扩额能力。候选基线、用户证据或学习策略变化时不直接应用旧结果。

### Behavior Canary

已知发布目标默认为 `researcher / write_research_section`。在部署配置中显式设置正整数 `BETTER_AGENT_LEARNING_CANARY_BUDGET_MICROUSD`；其他既有默认参数为 10% 流量、200 次调用上限、72 小时 deadline 和至少 20/20 样本。

这些配置不自动开放生产：仍需 ACTIVE、owner 目标权限、有效 release evaluation 和人工审批。此次没有修改本机 `.env`，没有启用生产 Canary。

## 验证证据

| 验证 | 结果 |
| --- | --- |
| 相关模块回归 | **350 passed / 231.60 秒** |
| 最后修改后的专项复验 | **82 passed / 44.65 秒** |
| PostgreSQL 集成复验 | **5 passed / 34.08 秒** |

专项与回归存在重复用例，不将这些数字相加宣称独立测试总数。相关回归不是整个仓库的全量测试。

- `review-fixes-postgres.txt`：专用数据库 `better_v3_review_c90eb788_test` 上的 V3 集成测试，包含并发候选与第二轮版本。
- `review-fixes-regression.txt`：Learning、Skill、Experience observer、Evolution/M5、API 与测试库守卫相关回归。
- `review-fixes-final-targets.txt`：最后调整后的 Skill 基线、续行 API、Behavior 输入构造、Canary 配置等专项复验。
- 语法编译与 `git diff --check` 检查已执行；测试最终数字以各日志末尾的 pytest summary 为准。

## 交付边界

本轮交付代码与离线/数据库验证，不宣称整个 T00–T22 任务书全部完成。尚未执行新的付费模型端到端验收、真实生产 20/20 Canary 收敛或 Legacy 全部迁移。JEV 预算依据用户要求从本轮范围排除，不能再作为此次修复的阻断项。

PostgreSQL 测试使用新建专用库，未对开发库执行迁移或 truncate。该测试库保留供复现。仓库原有未提交修改予以保留。
