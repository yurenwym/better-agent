"""A01-A09: approval, restart recovery and refusal paths.

A WRITE tool call pauses the turn.  Everything the resume needs must already be
durable at that moment - the original trace, span, turn, run, budget root and
bundle - and restoring the context must never restore an old *authorization*.

The chain is the production one (``ManagedTurnWorker`` -> ``ChatToolRunner`` ->
``ToolRegistry`` -> ``GoalProgramService``) driven by a fixed-response routed
gateway, so the records under test are the ones a real run would write.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from test_harness_context_flow import (
    FixedGateway,
    _count,
    _owner_bundle,
    _program_fixture,
    _versioned,
    answer,
    build_runtime,
    reopen_runtime,
    stored_context,
    tool_call,
    turn_context,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _preview_script(planner: list | None = None) -> FixedGateway:
    return FixedGateway(
        conversation=[
            {"tool_calls": [tool_call("activate_goal_plan", {"mode": "preview"})]},
            answer("预览已经生成，请确认后激活。"),
        ],
        planner=planner if planner is not None else [
            {"message": json.dumps(_program_fixture(), ensure_ascii=False)},
        ],
    )


def _approval_runtime(tmp_path, monkeypatch, script, *, runtime=None, tag="a"):
    """Wire a WRITE ``activate_goal_plan`` call onto a real thread and document."""
    runtime = runtime if runtime is not None else _versioned(
        build_runtime(tmp_path, monkeypatch, script)
    )
    thread = runtime.conversation.create_thread(f"审批链路 {tag}")
    document = runtime.plan_documents.save_model_revision(
        thread_id=thread.id, title="五周训练计划",
        markdown_content="# 五周训练计划\n\n- 每周三次力量训练",
        source_turn_id=None, source_message_id=None, actor="model",
    )
    script.conversation[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        "mode": "preview",
        "document_id": document.plan_document_id,
        "expected_document_version_id": document.id,
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
        "timezone": "Asia/Shanghai",
        "daily_minutes": 60,
    }, ensure_ascii=False)
    return runtime, thread


async def _pause_on_write(runtime, thread, *, client_turn_id="a-turn"):
    accepted = runtime.conversation.accept_turn(thread.id, client_turn_id, "生成执行预览", [])
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    assert pending is not None and pending.tool_name == "activate_goal_plan"
    return accepted, paused, pending


def _tool_row(runtime, call_id: str):
    with runtime.db.connection() as connection:
        return connection.execute("SELECT * FROM turn_tool_calls WHERE id=?", (call_id,)).fetchone()


def _identity(runtime, call_id: str) -> tuple[str, str]:
    row = _tool_row(runtime, call_id)
    return row["execution_context_json"], row["execution_context_digest"]


def _planner_rows(runtime):
    with runtime.db.connection() as connection:
        return connection.execute(
            "SELECT * FROM model_invocations WHERE role='planner' ORDER BY created_at,id"
        ).fetchall()


def _resume_events(runtime, thread_id: str):
    return [
        event for event in runtime.conversation.events.list(thread_id)
        if event.type == "chat_tool.context_resumed"
    ]


# --------------------------------------------------------------------------- #
# A01: pause, restart, approve, resume, then call an internal LLM
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a01_restart_restores_the_original_context_and_span(tmp_path, monkeypatch) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)

    original_turn = turn_context(runtime, accepted.turn_id)
    original_payload, original_digest = _identity(runtime, pending.id)
    original_tool = stored_context(_tool_row(runtime, pending.id))
    assert original_tool["run_id"] == f"chat-turn:{accepted.turn_id}"
    assert original_tool["parent_span_id"] is not None

    # Everything the resume needs is already durable: the worker, the tool runner
    # and the model gateway are all rebuilt before the user decides.
    reopened = reopen_runtime(tmp_path, script)
    continuation = reopened.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a01-approve",
    )
    assert await reopened.turn_worker.run_once() is True
    assert reopened.conversation.turn(continuation.id).status == "COMPLETED"

    # The stored identity is byte-for-byte what it was before the restart.
    assert _identity(reopened, pending.id) == (original_payload, original_digest)
    resumed_tool = stored_context(_tool_row(reopened, pending.id))
    assert resumed_tool == original_tool
    assert resumed_tool["trace_id"] == original_turn["trace_id"]
    assert resumed_tool["turn_id"] == accepted.turn_id
    assert resumed_tool["run_id"] == f"chat-turn:{accepted.turn_id}"

    # The continuation turn has its own trace root; it did not replace the tool's.
    continuation_root = turn_context(reopened, continuation.id)
    assert continuation_root["trace_id"] != original_turn["trace_id"]

    # The resume is audited against the original call and the original span.
    events = _resume_events(reopened, thread.id)
    assert len(events) == 1
    assert events[0].data["call_id"] == pending.id
    assert events[0].data["trace_id"] == original_tool["trace_id"]
    assert events[0].data["span_id"] == original_tool["span_id"]
    assert events[0].data["parent_span_id"] == original_tool["parent_span_id"]
    # The event does not rewrite the context.
    assert _identity(reopened, pending.id)[1] == original_digest

    # The tool-internal LLM call hangs off the tool span, not the continuation.
    planners = _planner_rows(reopened)
    assert len(planners) == 1
    planner = stored_context(planners[0])
    assert planner["parent_span_id"] == original_tool["span_id"]
    assert planner["trace_id"] == original_tool["trace_id"]
    assert planner["turn_id"] == accepted.turn_id
    assert planner["root_budget_id"] == original_tool["root_budget_id"]
    assert planner["runtime_bundle_id"] == original_tool["runtime_bundle_id"]

    # Exactly one business side effect.
    assert _count(reopened, "goal_programs") == 1
    with reopened.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goal_program_versions").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# A02: the continuation's environment must not overwrite the original
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a02_continuation_budget_bundle_and_trace_do_not_replace_the_original(
    tmp_path, monkeypatch,
) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)

    # Give the original turn a real budget root *before* it runs, so "the
    # original root survived" is not vacuous.
    accepted = runtime.conversation.accept_turn(thread.id, "a02-turn", "生成执行预览", [])
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turns SET root_budget_id=? WHERE id=?", ("budget-original", accepted.turn_id),
        )
    await runtime.turn_worker.run_once()
    paused = runtime.conversation.turn(accepted.turn_id)
    assert paused.status == "AWAITING_TOOL_APPROVAL"
    pending = runtime.conversation.pending_tool_call(accepted.turn_id)
    original_tool = stored_context(_tool_row(runtime, pending.id))
    assert original_tool["root_budget_id"] == "budget-original"
    payload, digest = _identity(runtime, pending.id)

    # The continuation environment carries another bundle and another budget root.
    other_bundle = _owner_bundle(
        runtime, monkeypatch, "local-user", env_prefix="ALT", profile_suffix="-alt",
    )
    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a02-approve",
    )
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turns SET runtime_bundle_id=?, root_budget_id=? WHERE id=?",
            (other_bundle, "budget-continuation", continuation.id),
        )

    assert await runtime.turn_worker.run_once() is True
    assert runtime.conversation.turn(continuation.id).status == "COMPLETED"

    resumed_tool = stored_context(_tool_row(runtime, pending.id))
    assert resumed_tool == original_tool
    assert resumed_tool["runtime_bundle_id"] == runtime.bundle_id != other_bundle
    assert resumed_tool["root_budget_id"] == "budget-original" != "budget-continuation"
    assert resumed_tool["run_id"] == f"chat-turn:{accepted.turn_id}"
    assert _identity(runtime, pending.id) == (payload, digest)

    # The continuation turn really did carry the other environment...
    continuation_root = turn_context(runtime, continuation.id)
    assert continuation_root["runtime_bundle_id"] == other_bundle
    assert continuation_root["root_budget_id"] == "budget-continuation"
    assert continuation_root["trace_id"] != resumed_tool["trace_id"]
    # ...and the tool-internal call still used the original binding.
    planners = _planner_rows(runtime)
    assert planners
    for row in planners:
        assert stored_context(row)["runtime_bundle_id"] == runtime.bundle_id


# --------------------------------------------------------------------------- #
# A03: the tool is revoked while the approval waits
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a03_revoked_tool_is_refused_without_side_effects(tmp_path, monkeypatch) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)
    before = _identity(runtime, pending.id)

    # The turn's allowance no longer names the tool: the *current* permission set
    # decides, and restoring the context is not restoring the authorization.
    runtime.conversation_tool_allowance = lambda turn_id, skill_names: {"get_today_tasks"}

    runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a03-approve",
    )
    await runtime.turn_worker.run_once()

    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "FAILED" and stored.error_code == "TOOL_NOT_ALLOWED"
    assert _count(runtime, "goal_programs") == 0
    assert _planner_rows(runtime) == []
    assert _identity(runtime, pending.id) == before


# --------------------------------------------------------------------------- #
# A04: changed parameters, changed association, changed binding
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["params", "association", "binding"])
async def test_a04_tampered_call_is_refused(tmp_path, monkeypatch, tamper) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script, tag=tamper)
    accepted, paused, pending = await _pause_on_write(runtime, thread)
    before = _identity(runtime, pending.id)

    runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, f"a04-{tamper}",
    )

    if tamper == "params":
        # The stored parameters no longer hash to the approved ``params_hash``.
        with runtime.db.transaction() as connection:
            connection.execute(
                "UPDATE turn_tool_calls SET params_json=? WHERE id=?",
                (json.dumps({"mode": "activate", "program_id": "other"}, ensure_ascii=False), pending.id),
            )
        expected = "ACTION_NOT_ELIGIBLE"
    elif tamper == "association":
        # The call now claims a thread it does not belong to.
        with runtime.db.transaction() as connection:
            connection.execute(
                "UPDATE turn_tool_calls SET thread_id=? WHERE id=?", ("thread-elsewhere", pending.id),
            )
        expected = "CONTEXT_IDENTITY_CONFLICT"
    else:
        # The trusted bundle binding changed underneath the stored context.
        rotated = _owner_bundle(
            runtime, monkeypatch, "local-user", env_prefix="ROT", profile_suffix="-rot",
        )
        with runtime.db.transaction() as connection:
            connection.execute(
                "UPDATE turns SET runtime_bundle_id=? WHERE id=?", (rotated, accepted.turn_id),
            )
        expected = "CONTEXT_IDENTITY_CONFLICT"

    await runtime.turn_worker.run_once()

    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "FAILED" and stored.error_code == expected
    assert _count(runtime, "goal_programs") == 0
    assert _planner_rows(runtime) == []
    assert _identity(runtime, pending.id) == before


# --------------------------------------------------------------------------- #
# A05: another owner cannot read or resume the call
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a05_another_owner_cannot_resume_the_call(tmp_path, monkeypatch) -> None:
    from app.chat_tools import ChatToolRunner

    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)
    before = _identity(runtime, pending.id)

    # The other owner cannot even read the turn.
    with pytest.raises(KeyError):
        runtime.conversation.turn(accepted.turn_id, "owner-b")

    # The legitimate owner approves it; the other owner then tries to resume.
    runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a05-approve",
    )
    call = runtime.conversation.chat_tool_calls.get(pending.id)
    assert call.status == "APPROVED"

    runner = ChatToolRunner(
        runtime.tools, runtime.approvals, runtime.conversation.chat_tool_calls,
        turn_id=accepted.turn_id, thread_id=thread.id, owner_id="owner-b", project_id=None,
        skill_names=(), goal_programs=runtime.goal_programs, plan_documents=runtime.plan_documents,
    )
    result = await runner.resume(call)
    assert result.ok is False and result.error == "CONTEXT_IDENTITY_CONFLICT"
    assert _count(runtime, "goal_programs") == 0
    assert _planner_rows(runtime) == []
    assert _identity(runtime, pending.id) == before


# --------------------------------------------------------------------------- #
# A06: the original bundle becomes unusable - stop, do not fall back
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a06_unusable_original_bundle_does_not_fall_back_to_stable(
    tmp_path, monkeypatch,
) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)
    original_tool = stored_context(_tool_row(runtime, pending.id))
    before = _identity(runtime, pending.id)

    # A working replacement bundle becomes the stable one, and the *original*
    # bundle's planner route is made unavailable.  A fallback would therefore
    # succeed - so "no planner call at all" is the proof that none happened.
    other = _owner_bundle(
        runtime, monkeypatch, "local-user", env_prefix="NEXT", profile_suffix="-next",
    )
    runtime.control.bundles.activate("stable", other, "a06-switch")
    with runtime.db.connection() as connection:
        profile_id = connection.execute(
            "SELECT profile_id FROM model_profile_versions WHERE id=?",
            (runtime.control.versions["planner"],),
        ).fetchone()["profile_id"]
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE model_profiles SET status='DISABLED' WHERE id=?", (profile_id,))

    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a06-approve",
    )
    await runtime.turn_worker.run_once()

    # The compile did not succeed, and it did not re-route through the newly
    # stable bundle: no planner call was dispatched under either bundle.
    assert _count(runtime, "goal_programs") == 1
    with runtime.db.connection() as connection:
        program = connection.execute("SELECT * FROM goal_programs").fetchone()
        versions = connection.execute("SELECT COUNT(*) FROM goal_program_versions").fetchone()[0]
    assert versions == 0
    assert program["compile_status"] != "READY"
    assert _planner_rows(runtime) == []
    assert runtime.conversation.chat_tool_calls.get(pending.id).status != "EXECUTED"

    # The persisted identity is untouched: no substitute bundle, no new root.
    resumed_tool = stored_context(_tool_row(runtime, pending.id))
    assert resumed_tool == original_tool
    assert resumed_tool["runtime_bundle_id"] == runtime.bundle_id
    assert _identity(runtime, pending.id) == before
    assert runtime.conversation.turn(continuation.id).status in {"COMPLETED", "FAILED"}


# --------------------------------------------------------------------------- #
# A07: legacy, corrupt and unknown-version records
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a07_legacy_missing_context_is_refused_and_stays_readable(
    tmp_path, monkeypatch,
) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)

    # A pending approval from before this feature: both columns empty.
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turn_tool_calls SET execution_context_json=NULL, execution_context_digest=NULL "
            "WHERE id=?", (pending.id,),
        )
    runtime.conversation.decide_tool_call(accepted.turn_id, "approve", paused.version, "a07-legacy")
    await runtime.turn_worker.run_once()

    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "FAILED" and stored.error_code == "LEGACY_CONTEXT_MISSING"
    assert _planner_rows(runtime) == []
    assert _count(runtime, "goal_programs") == 0
    # A legacy record stays readable, and the thread's own turn context is intact.
    assert stored.params["mode"] == "preview"
    assert turn_context(runtime, accepted.turn_id)["turn_id"] == accepted.turn_id


@pytest.mark.asyncio
async def test_a07_corrupt_and_unknown_context_fail_closed(tmp_path, monkeypatch) -> None:
    from app.execution_context import execution_context_digest

    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)

    # A digest that does not match its own JSON.
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE turn_tool_calls SET execution_context_digest=? WHERE id=?", ("0" * 64, pending.id),
        )
    runtime.conversation.decide_tool_call(accepted.turn_id, "approve", paused.version, "a07-corrupt")
    await runtime.turn_worker.run_once()
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "FAILED" and stored.error_code == "CONTEXT_IDENTITY_CONFLICT"
    assert _planner_rows(runtime) == []

    # A well-formed envelope under an unknown schema version.
    script2 = _preview_script()
    reopened = reopen_runtime(tmp_path, script2)
    runtime2, thread2 = _approval_runtime(
        tmp_path, monkeypatch, script2, runtime=reopened, tag="version",
    )
    accepted2, paused2, pending2 = await _pause_on_write(runtime2, thread2, client_turn_id="a07-version")
    envelope = json.loads(_tool_row(runtime2, pending2.id)["execution_context_json"])
    envelope["schema_version"] = "harness-execution-context-v99"
    payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # ``execution_context_digest`` validates the schema before hashing, so the
    # documented canonical form is hashed directly: the row's digest then matches
    # its JSON, and only the *version* is wrong.
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with runtime2.db.transaction() as connection:
        connection.execute(
            "UPDATE turn_tool_calls SET execution_context_json=?, execution_context_digest=? WHERE id=?",
            (payload, digest, pending2.id),
        )
    runtime2.conversation.decide_tool_call(
        accepted2.turn_id, "approve", paused2.version, "a07-version-approve",
    )
    await runtime2.turn_worker.run_once()
    stored2 = runtime2.conversation.chat_tool_calls.get(pending2.id)
    assert stored2.status == "FAILED" and stored2.error_code == "UNKNOWN_SCHEMA_VERSION"
    assert _planner_rows(runtime2) == []


# --------------------------------------------------------------------------- #
# A08: repeated resume reuses the result, a crash before persistence recovers
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a08_repeated_resume_reuses_the_result_without_rewriting(tmp_path, monkeypatch) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)
    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a08-approve",
    )
    assert await runtime.turn_worker.run_once() is True

    first = runtime.conversation.chat_tool_calls.get(pending.id)
    identity = _identity(runtime, pending.id)
    assert first.status == "EXECUTED"

    # Replaying the decision returns the same continuation and executes nothing.
    replay = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a08-approve",
    )
    assert replay.id == continuation.id
    assert await runtime.turn_worker.run_once() in {True, False}
    assert runtime.conversation.chat_tool_calls.get(pending.id).status == "EXECUTED"
    assert _identity(runtime, pending.id) == identity
    assert _count(runtime, "goal_program_versions") == 1
    assert len(_planner_rows(runtime)) == 1


@pytest.mark.asyncio
async def test_a08_crash_before_result_persistence_recovers_one_side_effect(
    tmp_path, monkeypatch,
) -> None:
    from app.chat_tools import ChatToolCallStore, ChatToolRunner

    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)
    continuation = runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a08-crash",
    )
    call = runtime.conversation.chat_tool_calls.get(pending.id)
    runner = ChatToolRunner(
        runtime.tools, runtime.approvals, runtime.conversation.chat_tool_calls,
        turn_id=continuation.id, thread_id=thread.id, owner_id="local-user", project_id=None,
        skill_names=(), goal_programs=runtime.goal_programs, plan_documents=runtime.plan_documents,
    )

    original = ChatToolCallStore.record_result

    def flaky(self, call_id, *, result, error_code, status):
        if call_id == pending.id:
            raise RuntimeError("simulated crash before the result was persisted")
        return original(self, call_id, result=result, error_code=error_code, status=status)

    monkeypatch.setattr(ChatToolCallStore, "record_result", flaky)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await runner.resume(call)

    # The compile committed, but the chat row still has no result.
    assert _count(runtime, "goal_programs") == 1
    assert runtime.conversation.chat_tool_calls.get(pending.id).result is None
    identity = _identity(runtime, pending.id)

    monkeypatch.setattr(ChatToolCallStore, "record_result", original)
    recovered = await runner.resume(call)
    assert recovered.ok is True

    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "EXECUTED" and stored.result["ok"] is True
    # One program, one version, one planner call - the recovery did not redo work.
    assert _count(runtime, "goal_programs") == 1
    assert _count(runtime, "goal_program_versions") == 1
    assert len(_planner_rows(runtime)) == 1
    assert _identity(runtime, pending.id) == identity


# --------------------------------------------------------------------------- #
# A09: an unclear WRITE outcome enters reconciliation and is not retried
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a09_unclear_write_outcome_is_not_retried(tmp_path, monkeypatch) -> None:
    from app.tools import ToolReconciliationRequired, ToolRegistry

    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)
    before = _identity(runtime, pending.id)

    original = ToolRegistry.execute_async
    calls = []

    async def unclear(self, call, **kwargs):
        calls.append(call.name)
        raise ToolReconciliationRequired("remote outcome unknown")

    monkeypatch.setattr(ToolRegistry, "execute_async", unclear)
    try:
        continuation = runtime.conversation.decide_tool_call(
            accepted.turn_id, "approve", paused.version, "a09-approve",
        )
        await runtime.turn_worker.run_once()
    finally:
        monkeypatch.setattr(ToolRegistry, "execute_async", original)

    assert calls == ["activate_goal_plan"]  # exactly one attempt, no automatic retry
    # An unclear outcome is neither success nor failure: the chat row keeps the
    # approved state and the *registry claim* is what blocks a re-run, so the
    # operation can be reconciled instead of silently retried.
    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "APPROVED" and stored.result is None
    resumed = [event for event in runtime.conversation.events.list(thread.id)
               if event.type == "chat_tool.resumed"]
    assert resumed and resumed[-1].data["error"] == "TOOL_RECONCILIATION_REQUIRED"
    assert resumed[-1].data["ok"] is False
    assert _count(runtime, "goal_programs") == 0
    # The identity is untouched by the unclear outcome.
    assert _identity(runtime, pending.id) == before
    assert runtime.conversation.turn(continuation.id).status in {"COMPLETED", "FAILED"}
