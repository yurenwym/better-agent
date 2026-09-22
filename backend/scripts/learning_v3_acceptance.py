"""End-to-end V3 acceptance: shadow gate plus the three real cases (V3 §41-§51).

    cd backend
    RUN_EVOLUTION_V3_LIVE=1 python scripts/learning_v3_acceptance.py

Before the first network call the script freezes `evolution-v3-acceptance-plan.json`
(V3 §59). Afterwards it writes `evolution-v3-acceptance-report.json` (V3 §60).

What is real and what is simulated — stated here rather than buried in the report:

* **Real**: the JEV decision layer (TypeSafe System One), the Learning LLM, the
  LLM-as-a-Judge, the harness rules, the target adapters, the promotion gate,
  leases, dispatch, the audit tables.
* **Simulated**: the replay runners for Case B and Case C. There is no way to run
  60 research-role cases end to end from this script, so the runners are
  deterministic models of the behaviour a candidate claims to improve. They are
  derived from the candidate's own content (a skill loads on the tasks its
  `适用：` line matches) so the gate is not fed a constant. V3 §55 anticipates
  exactly this split in `test_learning_v3_simulated.py`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN = ROOT / "evals" / "results" / "evolution-v3-acceptance-plan.json"
DEFAULT_REPORT = ROOT / "evals" / "results" / "evolution-v3-acceptance-report.json"

MAX_CALLS = 400
BUDGET_MICROUSD = 5_000_000  # $5 of head-room for one acceptance run


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- replay models

def skill_replay_suite(candidate):
    """A 30-case replay suite for a skill: 20 relevant, 10 unrelated.

    Relevant tasks quote the candidate's own `适用：` declaration, so a skill that
    declares a narrow trigger is exercised on exactly that trigger and a skill
    that declares "everything" is caught by the unrelated cases. The unrelated
    tasks share no vocabulary with the trigger, so the unrelated-trigger gate
    still measures something real.

    What this runner *cannot* test is whether the declaration is the right one —
    a simulated runner only models "the declared trigger removes trial and error".
    That limitation is repeated in the report rather than left implicit.
    """
    content = str(candidate.get("content", ""))
    trigger = ""
    for line in content.splitlines():
        if line.startswith("适用："):
            trigger = line.replace("适用：", "").strip().rstrip("。").strip()
            break
    steps = sum(1 for line in content.splitlines() if line.strip()[:2] in {f"{n}." for n in range(1, 10)})

    def runner(case, version_id):
        loaded = bool(version_id) and bool(trigger) and trigger[:6] in str(case["task"])
        # Guidance with an ordered procedure removes the trial-and-error calls.
        calls = max(3, 8 - min(steps, 4)) if loaded else 8
        # The answer itself has to differ, not just its label: a judge given two
        # near-identical strings returns 20 ties and the win-rate gate measures
        # nothing. The candidate answers with the declared order; the baseline
        # flails. That is the behaviour the skill claims to improve.
        text = (f"按 部署变更 → 慢查询 → 连接池 的顺序定位：{case['task']}。"
                f"先确认连接池上限与当前占用，再核对最近发布与慢查询日志，最后验证连接泄漏。"
                if loaded else
                f"逐个尝试各种可能原因：{case['task']}。"
                f"先看日志，再重启服务，还不行就调整数据库参数，逐个排除。")
        return {"success": True, "tool_calls": calls, "loaded_skill": loaded, "text": text}

    stem = trigger[:24] or "数据库超时定位"
    cases = [{"id": f"skill-relevant-{i:02d}", "relevant": True,
              "task": f"{stem}：第 {i} 个现场，请定位。"} for i in range(20)]
    cases += [{"id": f"skill-unrelated-{i:02d}", "relevant": False,
               "task": f"帮我把第 {i} 段文案改成更口语的表达。"} for i in range(10)]
    return cases, runner


def behavior_replay_suite(candidate):
    """A 60-case frozen suite for behavior: 30 target, 30 neighbour.

    The candidate's own change decides how much the target behaviour improves,
    so an empty change cannot pass the improvement gate.
    """
    change = json.dumps(candidate.get("diff") or {}, ensure_ascii=False, sort_keys=True)
    strength = 3 + min(len(change) // 40, 5)

    def runner(case, bundle_id):
        treated = bundle_id == candidate.get("target_bundle_id")
        is_target = bool(case.get("target_behavior", True))
        base_pass = case["index"] % 3 == 0  # ~1/3 baseline pass rate
        passed = (base_pass or (treated and is_target and case["index"] % 10 < strength)) if is_target else base_pass
        return {"passed": passed, "safety_pass": True,
                "text": ("要求两条独立证据后确认根因。" if treated else "直接确认根因。") + str(case["id"])}

    cases = [{"id": f"behavior-target-{i:02d}", "target_behavior": True, "task": "确认故障根因。", "index": i}
             for i in range(30)]
    cases += [{"id": f"behavior-neighbour-{i:02d}", "target_behavior": False, "task": "总结这段文档。", "index": i}
              for i in range(30)]
    return cases, runner


def replay_for(cases_by_target):
    """Bind a per-target replay suite into the pipeline's `replay` hook."""
    def replay(target, candidate):
        builder = cases_by_target.get(target)
        return builder(candidate) if builder else None
    return replay


# ------------------------------------------------------------------- fixtures

MEMORY_CASE = "以后生产数据库不能由 Agent 自动重启，必须人工批准。"
MEMORY_CORRECTION = "生产数据库允许自动执行只读检查，但重启仍需要人工批准。"
# V3 §45's Case B: three independent failures that all end at the connection
# pool. The notes record the *observed path* — what actually happened — and
# never name the target. Naming "procedure" or "skill" in the input would be
# leading the witness, and the point of the case is that JEV classifies it.
SKILL_FAILURES = [
    "线上 HTTP 500，按 部署变更 → 慢查询 → 连接池 的顺序排查，最后停在连接池。",
    "又一次 database timeout，走同一套排查顺序，最后还是停在连接池。",
    "database timeout 再次出现，第三次走同样的排查顺序，结果仍然在连接池。",
    "第四次 database timeout，同样的排查顺序，结论依旧在连接池。",
    "再次遇到 database timeout，按同一套顺序排查，还是停在连接池。",
]
# The observed *procedure*, not the conclusion. `build_state` takes the tool
# trace as its own input; leaving it empty hides the ordered steps that §45
# says the three failures converged on, which is a construction defect rather
# than a model error.
SKILL_FAILURE_TRACES = [
    [{"step": "check_recent_deploy", "result": "no_recent_change"},
     {"step": "check_slow_queries", "result": "no_slow_query"},
     {"step": "check_connection_pool", "result": "pool_exhausted"}],
    [{"step": "check_recent_deploy", "result": "no_recent_change"},
     {"step": "check_slow_queries", "result": "no_slow_query"},
     {"step": "check_connection_pool", "result": "pool_too_small"}],
    [{"step": "check_recent_deploy", "result": "no_recent_change"},
     {"step": "check_slow_queries", "result": "no_slow_query"},
     {"step": "check_connection_pool", "result": "pool_exhausted"}],
    [{"step": "check_recent_deploy", "result": "no_recent_change"},
     {"step": "check_slow_queries", "result": "no_slow_query"},
     {"step": "check_connection_pool", "result": "pool_too_small"}],
    [{"step": "check_recent_deploy", "result": "no_recent_change"},
     {"step": "check_slow_queries", "result": "no_slow_query"},
     {"step": "check_connection_pool", "result": "pool_exhausted"}],
]
BEHAVIOR_FAILURES = [
    "只有一个日志片段就确认了根因，结果判断错了。",
    "又一次只凭一条证据就下结论，用户指出了错误。",
    "再次在证据不足时直接确认根因，导致修复方向错误。",
]
BEHAVIOR_FAILURE_TRACES = [
    [{"step": "read_logs", "evidence_count": 1},
     {"step": "confirm_root_cause", "evidence_count": 1, "result": "wrong"}],
    [{"step": "read_logs", "evidence_count": 1},
     {"step": "confirm_root_cause", "evidence_count": 1, "result": "wrong"}],
    [{"step": "read_logs", "evidence_count": 1},
     {"step": "confirm_root_cause", "evidence_count": 1, "result": "wrong"}],
]


# ---------------------------------------------------------------------- plan

def build_plan(*, llm_profile: str, runtime_bundle: str, jev_version: str, budget_microusd: int) -> dict:
    return {
        "suite_digest": _digest({"memory": [MEMORY_CASE, MEMORY_CORRECTION],
                                 "skill": SKILL_FAILURES, "behavior": BEHAVIOR_FAILURES}),
        "jev_version": jev_version,
        "llm_profile": llm_profile,
        "runtime_bundle": runtime_bundle,
        "max_calls": MAX_CALLS,
        "budget_microusd": budget_microusd,
        "cases": {"memory": 2, "skill": len(SKILL_FAILURES), "behavior": len(BEHAVIOR_FAILURES),
                  "skill_replay": 30, "behavior_replay": 60},
        "created_at": _now(),
    }


def freeze_plan(path: Path, plan: dict) -> None:
    if path.exists():
        raise SystemExit(f"{path} already exists; a frozen acceptance plan must not be rewritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------- harness

class CallCounter:
    """Stops the run at the frozen call budget instead of silently overspending."""

    def __init__(self, limit: int) -> None:
        self.limit, self.calls = limit, 0

    def charge(self, what: str) -> None:
        self.calls += 1
        if self.calls > self.limit:
            raise RuntimeError(f"acceptance call budget of {self.limit} exhausted at {what}")


class CountingDecisions:
    """The real JEV service, with the frozen call budget enforced."""

    def __init__(self, service, counter: CallCounter) -> None:
        self.service, self.counter = service, counter

    def decide(self, **kwargs):
        self.counter.charge("jev")
        return self.service.decide(**kwargs)

    def get(self, owner_id, job_id):
        return self.service.get(owner_id, job_id)


class CountingAgent:
    """The real Learning LLM, with the frozen call budget enforced."""

    def __init__(self, agent, counter: CallCounter) -> None:
        self.agent, self.counter = agent, counter

    def generate(self, **kwargs):
        self.counter.charge("learning_llm")
        return self.agent.generate(**kwargs)


class CountingJudge:
    """The real judge, with the frozen call budget enforced."""

    def __init__(self, judge, counter: CallCounter) -> None:
        self.judge, self.counter = judge, counter

    def compare(self, **kwargs):
        self.counter.charge("judge")
        return self.judge.compare(**kwargs)


def local_budget_dispatch(runtime, counter: CallCounter):
    """The dispatch seam for a SQLite acceptance run.

    `LearningService.dispatch` reserves a root task budget, and root budgets are
    PostgreSQL-authoritative by design (`CostService.create_root_budget` refuses
    SQLite). This acceptance run therefore substitutes the *ledger* only: it
    still writes `dispatched_at` and `root_budget_id`, so the UNKNOWN semantics
    and `assert_learning_call_allowed` behave exactly as in production, and the
    frozen call budget is enforced by `counter`. The monetary reservation itself
    is not exercised here and the report says so.
    """
    def dispatch(job):
        root = f"local-budget:{job['id']}"
        with runtime.db.transaction() as connection:
            connection.execute(
                "UPDATE learning_jobs SET dispatched_at=?,root_budget_id=?,version=version+1,updated_at=? WHERE id=?",
                (_now(), root, _now(), job["id"]))
        return root
    return dispatch


def asset_snapshot(runtime) -> dict:
    """The *production* surface: what the running agent actually reads.

    V3 §41 runs the whole chain in shadow — `Experience → JEV → Learning LLM →
    Candidate → Eval` — and forbids only the *promotion*. So a staged candidate
    is expected: a Memory proposal, an INSTALLED-but-not-ENABLED skill version,
    an `evolution_candidates` row. Counting those as "assets" made the first
    version of this check pass while every cycle died with `DraftError`, and then
    fail once the Skill classifier started working. What must not move is what
    production reads: enabled skill versions, the live channels, and the bundle
    the runtime is on.
    """
    with runtime.db.connection() as connection:
        snapshot = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("memory_entries", "memory_revisions", "runtime_channels", "canary_deployments",
                          "learning_promotions")
        }
        snapshot["enabled_skills"] = connection.execute(
            "SELECT COUNT(*) FROM skills WHERE status='ENABLED'").fetchone()[0]
        snapshot["skill_defaults"] = connection.execute(
            "SELECT COUNT(*) FROM skills WHERE default_version_id IS NOT NULL").fetchone()[0]
        # Staging is the chain working; it is snapshotted so the run can prove it
        # reached the candidate stage instead of passing vacuously.
        snapshot["staged_candidates"] = connection.execute(
            "SELECT (SELECT COUNT(*) FROM skill_versions) + (SELECT COUNT(*) FROM evolution_candidates) "
            "+ (SELECT COUNT(*) FROM memory_proposals)").fetchone()[0]
    snapshot["active_bundle"] = runtime.behavior.active("stable").id
    return snapshot


def run_cycle(pipeline, runtime, *, source_kind, source_id, source_hash, root_id, key):
    job_id = runtime.learning.enqueue("local-user", source_kind, source_id, source_hash, root_id)
    job = runtime.learning.claim("local-user", lease_seconds=3600)
    if job is None or job["id"] != job_id:
        raise RuntimeError(f"could not claim the job for {key}")
    return pipeline.run_job(job)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply-memory", action="store_true",
                        help="let the gate actually apply the Memory case (V3 §62 stage 2)")
    args = parser.parse_args()

    from app.config import load_env_file

    load_env_file(ROOT / ".env")

    if os.getenv("RUN_EVOLUTION_V3_LIVE", "").strip() not in {"1", "true", "TRUE", "yes"}:
        print("refusing to call live services: set RUN_EVOLUTION_V3_LIVE=1", file=sys.stderr)
        return 2
    if not os.getenv("TYPESAFE_API_KEY"):
        print("refusing to start: TYPESAFE_API_KEY is not configured", file=sys.stderr)
        return 2
    if not any(os.getenv(name) for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
        print("refusing to start: no LLM provider credential is configured", file=sys.stderr)
        return 2

    os.environ.setdefault("BETTER_AGENT_TEST_ALLOW_SQLITE", "1")
    import tempfile

    from app.learning_agent import LearningAgent
    from app.learning_decision import DEFAULT_MODEL, JevDecisionService, TypesafeClient
    from app.learning_eval import LearningJudge
    from app.learning_pipeline import MODE_ACTIVE, MODE_SHADOW, build_pipeline
    from app.startup import build_runtime

    data_root = Path(tempfile.mkdtemp(prefix="learning-v3-acceptance-"))
    runtime = build_runtime(data_root)
    runtime.learning.configure("local-user", expected_version=0, paused=False,
                               allowed_assets=["memory", "skill", "prompt"])

    counter = CallCounter(MAX_CALLS)
    runtime.learning.dispatch = local_budget_dispatch(runtime, counter)
    decisions = CountingDecisions(
        JevDecisionService(db=runtime.db, client=TypesafeClient(model=os.getenv("TYPESAFE_MODEL", DEFAULT_MODEL))),
        counter)
    agent = CountingAgent(LearningAgent(runtime.model.gateway), counter)
    judge = CountingJudge(LearningJudge(runtime.model.gateway), counter)

    plan = build_plan(llm_profile=judge.judge.identity(), runtime_bundle=runtime.behavior.active("stable").id,
                      jev_version=os.getenv("TYPESAFE_MODEL", DEFAULT_MODEL), budget_microusd=BUDGET_MICROUSD)
    plan["budget_backend"] = ("local stand-in: PostgreSQL root budgets are unavailable in this environment; "
                              "dispatched_at/root_budget_id semantics are preserved, monetary reservation is not")
    freeze_plan(args.plan, plan)
    print(f"frozen plan: {args.plan}")

    started = time.monotonic()
    report = {"suite": "evolution-v3-acceptance", "mode": "live", "plan": plan,
              "created_at": _now(), "parts": {},
              "construction": {
                  "memory_source": "thread_message (the user's own utterance is the evidence for an explicit constraint)",
                  "skill_source": (f"{len(SKILL_FAILURES)} independent DISCOVERY Experiences, one learning cycle per "
                                   "run, one run per Experience"),
                  "behavior_source": (f"{len(BEHAVIOR_FAILURES)} independent DISCOVERY Experiences, one learning cycle "
                                      "per run, one run per Experience"),
                  "experience_tool_trace": ("the seeded Experiences carry the observed step sequence, because "
                                            "`build_state` takes `tool_trace` as its own input and an empty trace "
                                            "hides the repeatable procedure JEV §45 asks it to classify"),
                  "replay_runner": "deterministic model of the candidate's own claim; see the module docstring",
                  "skill_replay_limit": ("the relevant cases quote the candidate's declared 适用： trigger, so the "
                                         "suite measures whether a declaration is precise, not whether it is the "
                                         "right declaration"),
                  "target_stability": ("every Case runs its learning cycle once per Experience; "
                                       "`target_is_stable` requires all of them to agree, and "
                                       "`scripts/learning_target_stability_probe.py` re-measures the same state "
                                       "off the pipeline at a chosen sample size"),
                  "budget_backend": plan.get("budget_backend", ""),
              }}

    # ------------------------------------------------ Part 1: shadow (§41/§42)
    shadow_before = asset_snapshot(runtime)
    shadow = build_pipeline(runtime, mode=MODE_SHADOW, decisions=decisions, agent=agent, judge=judge)
    shadow_cycles = []
    for index, text in enumerate(SKILL_FAILURES + BEHAVIOR_FAILURES, 1):
        thread = runtime.conversation.create_thread("v3-shadow")
        runtime.conversation.accept_turn(thread.id, f"shadow-{index}", text)
        with runtime.db.connection() as connection:
            message_id = connection.execute(
                "SELECT id FROM thread_messages WHERE thread_id=? AND role='user'", (thread.id,)).fetchone()[0]
        from app.learning import digest

        result = run_cycle(shadow, runtime, source_kind="thread_message", source_id=message_id,
                           source_hash=digest(text), root_id=message_id, key=f"shadow-{index}")
        shadow_cycles.append(result.to_dict())
        print(f"shadow [{index}] target={result.target} outcome={result.outcome}", flush=True)
    shadow_after = asset_snapshot(runtime)

    # V3 §41 runs the whole chain in shadow and forbids only the promotion, so a
    # staged candidate is the chain working. What production reads must not move:
    # enabled skill versions, the live channels, and the active bundle.
    production_surfaces = ("memory_entries", "memory_revisions", "runtime_channels", "canary_deployments",
                           "enabled_skills", "skill_defaults")
    changed = {name: shadow_after[name] - shadow_before[name]
               for name in shadow_before
               if name != "active_bundle" and shadow_after[name] != shadow_before[name]}
    production_delta = {name: delta for name, delta in changed.items() if name in production_surfaces}
    staged = shadow_after["staged_candidates"] - shadow_before["staged_candidates"]
    with runtime.db.connection() as connection:
        promoted_in_shadow = connection.execute(
            "SELECT COUNT(*) FROM learning_promotions WHERE outcome='PROMOTED'").fetchone()[0]
    report["parts"]["shadow"] = {
        "cycles": len(shadow_cycles),
        "targets": sorted({item["target"] for item in shadow_cycles}),
        "outcomes": sorted({item["outcome"] for item in shadow_cycles}),
        "asset_delta": changed,
        "production_delta": production_delta,
        "staged_candidates": staged,
        "cycles_detail": shadow_cycles,
        "checks": {
            "no_production_asset_modified": not production_delta,
            "active_bundle_unchanged": shadow_after["active_bundle"] == shadow_before["active_bundle"],
            "no_promotion_applied": int(promoted_in_shadow) == 0,
            # Without this the shadow check passes vacuously: its first version
            # reported "pass" while every cycle died before reaching an adapter,
            # then failed only once the Skill classifier started working.
            "shadow_chain_reached_the_candidate_stage": staged > 0,
        },
    }

    # ---------------------------------------------------- Part 2: Case A (§43)
    memory_part = {"case": "A", "requirement": "V3 §43/§44"}
    apply_memory = args.apply_memory
    active = build_pipeline(runtime, mode=MODE_ACTIVE if apply_memory else MODE_SHADOW,
                            decisions=decisions, agent=agent, judge=judge)
    entries_before = len(runtime.memory_store.list_entries())
    thread = runtime.conversation.create_thread("v3-memory")
    runtime.conversation.accept_turn(thread.id, "memory-1", MEMORY_CASE)
    with runtime.db.connection() as connection:
        first_message = connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=? AND role='user'", (thread.id,)).fetchone()[0]
    from app.learning import digest as _digest

    first = run_cycle(active, runtime, source_kind="thread_message", source_id=first_message,
                      source_hash=_digest(MEMORY_CASE), root_id=first_message, key="memory-1")
    entries_after = runtime.memory_store.list_entries()
    memory_part.update({
        "decision_target": first.target, "outcome": first.outcome,
        "candidate_id": first.candidate_id,
        "would_be": ((first.promotion or {}).get("detail") or {}).get("would_be"),
        "gate": (first.promotion or {}).get("gate"),
        "applied": first.status == "APPLIED",
        "entries_created": len(entries_after) - entries_before,
        "proposal_status": (runtime.memory_store.list_proposals()[-1].status
                            if runtime.memory_store.list_proposals() else None),
    })
    if entries_after:
        entry = entries_after[-1]
        with runtime.db.connection() as connection:
            revision = connection.execute(
                "SELECT content FROM memory_revisions WHERE id=?", (entry.revision_id,)).fetchone()
        memory_part["entry_id"] = entry.id
        memory_part["revision_no"] = entry.revision_no
        memory_part["content_digest"] = _digest(revision["content"] if revision else "")
        # Another owner must never see it.
        with runtime.db.connection() as connection:
            leaked = connection.execute("SELECT COUNT(*) FROM memory_entries WHERE owner_id<>'local-user'").fetchone()[0]
        memory_part["cross_owner_entries"] = int(leaked)

    # Correction: a new revision, not a second conflicting entry.
    runtime.conversation.accept_turn(thread.id, "memory-2", MEMORY_CORRECTION)
    with runtime.db.connection() as connection:
        second_message = connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=? AND role='user' ORDER BY created_at DESC",
            (thread.id,)).fetchone()[0]
    correction = run_cycle(active, runtime, source_kind="thread_message", source_id=second_message,
                           source_hash=_digest(MEMORY_CORRECTION), root_id=second_message, key="memory-2")
    final_entries = runtime.memory_store.list_entries()
    memory_part["correction_outcome"] = correction.outcome
    memory_part["entries_after_correction"] = len(final_entries)
    if memory_part["applied"]:
        memory_part["correction_is_a_new_revision"] = (
            len(final_entries) == len(entries_after) and final_entries
            and final_entries[-1].revision_no > entries_after[-1].revision_no)
        memory_part["entry_count_unchanged"] = len(final_entries) == len(entries_after)
    else:
        memory_part["correction_is_a_new_revision"] = None
        memory_part["entry_count_unchanged"] = None
    report["parts"]["memory"] = memory_part
    print(f"memory: outcome={first.outcome} entries={memory_part['entries_created']} "
          f"correction={correction.outcome}", flush=True)

    # --------------------------------------------------- Part 3: Case B (§45)
    skill_suite_holder = {}

    def skill_replay(target, candidate):
        suite = skill_replay_suite(candidate)
        skill_suite_holder[candidate["candidate_id"]] = suite[0]
        return suite

    skill_pipeline = build_pipeline(runtime, mode=MODE_SHADOW, decisions=decisions, agent=agent,
                                    judge=judge, replay=skill_replay)
    # Three independent failures are the *evidence*; they are one learning cycle,
    # not three (V3 §45: "连续三个独立故障 ... JEV target = SKILL").
    skill_bundle = runtime.behavior.active("stable")
    skill_experiences = [
        runtime.evolution.record_experience(
            task_type="database_timeout_diagnosis", outcome="failure",
            lineage_group_hash=f"v3-skill-lineage-{index}", root_task_id=f"v3-skill-root-{index}",
            source_content_hash=f"v3-skill-source-{index}", runtime_bundle_id=skill_bundle.id,
            dataset_partition="DISCOVERY", source_kind="manual", source_id=f"v3-skill-job-{index}",
            source_event_id=f"v3-skill-event-{index}", signal_type="run_failed", severity="error",
            provenance="production", idempotency_key=f"v3-acceptance-skill-{index}",
            evidence={"note": text, "tool_trace": SKILL_FAILURE_TRACES[index - 1]},
        ) for index, text in enumerate(SKILL_FAILURES, 1)
    ]
    # V3 §45 asks for one classification over three independent failures. The
    # same case is run three times so the *stability* of that classification is
    # visible: the first cycle is the case result, and disagreement between the
    # three is reported rather than papered over by picking a lucky draw.
    skill_cycles = []
    for index, experience in enumerate(skill_experiences, 1):
        result = run_cycle(skill_pipeline, runtime, source_kind="experience",
                           source_id=experience["id"], source_hash=experience["source_content_hash"],
                           root_id=experience["id"], key=f"skill-{index}")
        skill_cycles.append(result)
        print(f"skill [{index}] target={result.target} outcome={result.outcome}", flush=True)
    skill_result = skill_cycles[0]
    skill_metrics = (skill_result.evaluation or {}).get("metrics") if skill_result else None
    with runtime.db.connection() as connection:
        enabled_skills = connection.execute("SELECT COUNT(*) FROM skill_versions WHERE status='ENABLED'").fetchone()[0]
    report["parts"]["skill"] = {
        "case": "B", "requirement": "V3 §45",
        "targets": [item.target for item in skill_cycles],
        "outcomes": [item.outcome for item in skill_cycles],
        "cycles_detail": [item.to_dict() for item in skill_cycles],
        "target_is_stable": len({item.target for item in skill_cycles}) == 1,
        "candidate_id": skill_result.candidate_id,
        "evaluation": skill_result.evaluation,
        "promotion": skill_result.promotion,
        "replay_cases": len(skill_suite_holder.get(skill_result.candidate_id, [])),
        "replay_metrics": skill_metrics,
        "enabled_versions_after": int(enabled_skills),
    }

    # --------------------------------------------------- Part 4: Case C (§46)
    behavior_suite_holder = {}

    def behavior_replay(target, candidate):
        suite = behavior_replay_suite(candidate)
        behavior_suite_holder[candidate["candidate_id"]] = suite[0]
        return suite

    behavior_pipeline = build_pipeline(runtime, mode=MODE_SHADOW, decisions=decisions, agent=agent,
                                       judge=judge, replay=behavior_replay)
    # A Behavior candidate needs at least three independent DISCOVERY experiences,
    # so the three failures are recorded as Experiences first (V3 §46).
    bundle = runtime.behavior.active("stable")
    behavior_experiences = [
        runtime.evolution.record_experience(
            task_type="root_cause_confirmation", outcome="failure", lineage_group_hash=f"v3-behavior-lineage-{index}",
            root_task_id=f"v3-behavior-root-{index}", source_content_hash=f"v3-behavior-source-{index}",
            runtime_bundle_id=bundle.id, dataset_partition="DISCOVERY", source_kind="manual",
            source_id=f"v3-behavior-job-{index}", source_event_id=f"v3-behavior-event-{index}",
            signal_type="run_failed", severity="error", failure_tags=["premature_root_cause"],
            provenance="production", idempotency_key=f"v3-acceptance-behavior-{index}",
            evidence={"note": text, "tool_trace": BEHAVIOR_FAILURE_TRACES[index - 1]},
        ) for index, text in enumerate(BEHAVIOR_FAILURES, 1)
    ]
    behavior_cycles = []
    for index, experience in enumerate(behavior_experiences, 1):
        result = run_cycle(behavior_pipeline, runtime, source_kind="experience",
                           source_id=experience["id"], source_hash=experience["source_content_hash"],
                           root_id=experience["id"], key=f"behavior-{index}")
        behavior_cycles.append(result)
        print(f"behavior [{index}] target={result.target} outcome={result.outcome}", flush=True)
    behavior_result = behavior_cycles[0]
    report["parts"]["behavior"] = {
        "case": "C", "requirement": "V3 §46",
        "targets": [item.target for item in behavior_cycles],
        "outcomes": [item.outcome for item in behavior_cycles],
        "cycles_detail": [item.to_dict() for item in behavior_cycles],
        "target_is_stable": len({item.target for item in behavior_cycles}) == 1,
        "candidate_id": behavior_result.candidate_id,
        "evaluation": behavior_result.evaluation,
        "promotion": behavior_result.promotion,
        "replay_cases": len(behavior_suite_holder.get(behavior_result.candidate_id, [])),
    }

    # ------------------------------------------- Harness gates (§42/§51/§61)
    with runtime.db.connection() as connection:
        unknown_jobs = connection.execute(
            "SELECT COUNT(*) FROM learning_jobs WHERE status='UNKNOWN'").fetchone()[0]
        # A dispatched request must never re-enter the claimable set (V3 §48).
        requeued = connection.execute(
            "SELECT COUNT(*) FROM learning_jobs WHERE status IN ('QUEUED','RUNNING') AND dispatched_at IS NOT NULL"
        ).fetchone()[0]
        cross_owner = connection.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE owner_id<>'local-user'").fetchone()[0]
        expansions = connection.execute(
            "SELECT COUNT(*) FROM learning_promotions WHERE outcome='PROMOTED'").fetchone()[0]

    # A shadow run that never reaches the adapters is not a passing shadow run.
    # The first version of this gate only checked that nothing was modified,
    # which reported "pass" while all thirteen cycles died with `DraftError`.
    # The per-case checks below are what actually catch that; this one only
    # insists that a failed cycle says why it failed.
    unknown_cycles = [{"target": item["target"], "reason": item["reason"]}
                      for part in report["parts"].values() for item in part.get("cycles_detail", [])
                      if item["status"] == "UNKNOWN"]
    case_checks = {
        "memory_case_reached_memory": report["parts"]["memory"]["decision_target"] == "MEMORY",
        "memory_case_produced_a_candidate": bool(report["parts"]["memory"]["candidate_id"]),
        "memory_case_would_promote": report["parts"]["memory"]["would_be"] == "PROMOTED",
        "skill_case_reached_skill": report["parts"]["skill"]["targets"][0] == "SKILL",
        "skill_case_target_is_stable": report["parts"]["skill"]["target_is_stable"],
        "skill_case_produced_a_candidate": bool(report["parts"]["skill"]["candidate_id"]),
        "skill_case_replay_ran": report["parts"]["skill"]["replay_cases"] > 0,
        "behavior_case_reached_behavior": report["parts"]["behavior"]["targets"][0] == "BEHAVIOR",
        "behavior_case_target_is_stable": report["parts"]["behavior"]["target_is_stable"],
        "behavior_case_produced_a_candidate": bool(report["parts"]["behavior"]["candidate_id"]),
        "behavior_case_replay_ran": report["parts"]["behavior"]["replay_cases"] > 0,
        "no_unexplained_cycles": all(item["reason"] for item in unknown_cycles),
    }
    # The acceptance has to fail if the routing contract loses its precedence
    # rules: without them JEV was measured to flip between SKILL and IGNORE on
    # the same input (1/3 stable before the fix, 15/15 after), because a lesson
    # that is both a reusable procedure and a general defect was genuinely
    # ambiguous. `scripts/learning_target_stability_probe.py` re-measures it.
    from app.learning_decision import build_questions

    target_question = build_questions()["target"]["instructions"]
    contract_checks = {
        "target_contract_states_precedence": "(1) SKILL" in target_question and "(4) IGNORE" in target_question,
        "target_contract_states_tie_break": "is a SKILL" in target_question,
    }
    report["harness"] = {
        "unknown_jobs": int(unknown_jobs),
        "requeued_after_dispatch": int(requeued),
        "cross_owner_rows": int(cross_owner),
        "real_promotions": int(expansions),
        "unknown_cycles": unknown_cycles,
        "checks": {
            "no_production_asset_modified_in_shadow": report["parts"]["shadow"]["checks"]["no_production_asset_modified"],
            "active_bundle_unchanged_in_shadow": report["parts"]["shadow"]["checks"]["active_bundle_unchanged"],
            "no_promotion_applied_in_shadow": report["parts"]["shadow"]["checks"]["no_promotion_applied"],
            "shadow_chain_reached_the_candidate_stage": report["parts"]["shadow"]["checks"][
                "shadow_chain_reached_the_candidate_stage"],
            "cross_owner_mutation_zero": int(cross_owner) == 0,
            "unknown_replay_zero": int(requeued) == 0,
            "calls_within_frozen_budget": counter.calls <= plan["max_calls"],
            **case_checks,
            **contract_checks,
        },
    }
    report["calls_used"] = counter.calls
    report["duration_seconds"] = round(time.monotonic() - started, 1)
    report["gate"] = {"pass": all(report["harness"]["checks"].values()),
                      "checks": report["harness"]["checks"]}

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    failed = sorted(name for name, passed in report["gate"]["checks"].items() if not passed)
    print(json.dumps({"calls_used": report["calls_used"], "gate": report["gate"],
                      "failed_checks": failed}, ensure_ascii=False, indent=2))
    print(f"report: {args.report}")
    return 0 if report["gate"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
