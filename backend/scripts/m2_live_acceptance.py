"""Run the bounded M2 live acceptance batch in the current PostgreSQL database."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import psycopg

from app.behavior import BehaviorBundleService
from app.config import load_llm_ap, load_model_profile_from_env
from app.costs import PriceSnapshot
from app.db import POSTGRES_SCHEMA_HEAD
from app.model_admin import ModelAdminService, ROLES
from app.model_control import RoutedModelGateway
from app.startup import build_runtime


BATCH_ID = "M2-LIVE-20260909-008"
MAX_ROUNDS = 3
MAX_ATTEMPTS_PER_ROUND = 10
MAX_TOTAL_ATTEMPTS = 30
MAX_SECONDS = 20 * 60
WORST_ATTEMPT_MICROUSD = 25_232
MAX_COST_MICROUSD = WORST_ATTEMPT_MICROUSD * MAX_TOTAL_ATTEMPTS


class AcceptanceFailure(RuntimeError):
    pass


def require(value: bool, message: str) -> None:
    if not value:
        raise AcceptanceFailure(message)


def digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def source_manifest() -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    paths = [*root.joinpath("app").rglob("*.py"), *root.joinpath("alembic").rglob("*.py"), Path(__file__).resolve()]
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }
    return {"files": files, "digest": digest(files)}


@dataclass
class BatchBudget:
    started: float = field(default_factory=time.monotonic)
    attempts: list[dict[str, object]] = field(default_factory=list)

    def remaining(self) -> float:
        remaining = MAX_SECONDS - (time.monotonic() - self.started)
        if remaining <= 0:
            raise AcceptanceFailure("batch duration exceeded")
        return remaining

    async def execute(self, round_number: int, profile, request, **kwargs):
        self.remaining()
        round_attempts = sum(item["round"] == round_number for item in self.attempts)
        if round_attempts >= MAX_ATTEMPTS_PER_ROUND:
            raise AcceptanceFailure("round attempt hard limit reached before network request")
        if len(self.attempts) >= MAX_TOTAL_ATTEMPTS:
            raise AcceptanceFailure("batch attempt hard limit reached before network request")
        record: dict[str, object] = {
            "ordinal": len(self.attempts) + 1,
            "round": round_number,
            "role": request.role,
            "purpose": request.purpose,
            "request_digest": digest({
                "messages": request.messages,
                "tools": request.tools or [],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "thinking": request.thinking,
            }),
            "status": "started",
        }
        self.attempts.append(record)
        try:
            response = await asyncio.wait_for(
                RoutedModelGateway._execute_http_attempt(profile, request, **kwargs),
                timeout=min(float(profile.timeout_seconds), self.remaining()),
            )
        except Exception as exc:
            record.update(status="failed", error_kind=getattr(exc, "kind", type(exc).__name__))
            raise
        record.update(
            status="succeeded",
            finish_reason=response.finish_reason,
            output_digest=digest(response.message),
            visible_output_bytes=len(response.message.encode("utf-8")),
            usage={
                "uncached_input_tokens": response.usage.uncached_input_tokens,
                "cache_read_tokens": response.usage.cache_read_tokens,
                "cache_write_tokens": response.usage.cache_write_tokens,
                "output_tokens": response.usage.output_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
            },
        )
        require(response.finish_reason != "length", "model output was truncated")
        return response


def verify_current_database(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    require(name == "better_agent", f"expected current better_agent database, got {name}")
    require(head is not None and head[0] == POSTGRES_SCHEMA_HEAD, "current PostgreSQL schema is not at head")


def stable_bundle_id(database_url: str) -> str:
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "SELECT bundle_id FROM runtime_channels WHERE name='stable'",
        ).fetchone()
    require(row is not None and bool(row[0]), "stable runtime bundle is missing")
    return str(row[0])


def cleanup_batch(database_url: str, batch_id: str, restore_bundle_id: str) -> dict[str, object]:
    owner_pattern = batch_id.lower() + "-%"
    removed = 0
    with psycopg.connect(database_url) as connection:
        def execute(statement: str, parameters=()) -> None:
            nonlocal removed
            cursor = connection.execute(statement, parameters)
            if cursor.rowcount > 0:
                removed += cursor.rowcount

        connection.execute(
            "UPDATE runtime_channels SET bundle_id=%s,version=version+1,updated_at=clock_timestamp() "
            "WHERE name='stable' AND bundle_id IS DISTINCT FROM %s",
            (restore_bundle_id, restore_bundle_id),
        )
        thread_ids = "SELECT id FROM threads WHERE owner_id LIKE %s"
        turn_ids = f"SELECT id FROM turns WHERE thread_id IN ({thread_ids})"
        document_ids = f"SELECT id FROM plan_documents WHERE thread_id IN ({thread_ids})"
        version_ids = f"SELECT id FROM plan_document_versions WHERE plan_document_id IN ({document_ids})"
        invocation_ids = "SELECT id FROM model_invocations WHERE owner_id LIKE %s"
        profile_ids = "SELECT id FROM model_profiles WHERE owner_id LIKE %s"
        profile_version_ids = f"SELECT id FROM model_profile_versions WHERE profile_id IN ({profile_ids})"
        bundle_ids = (
            "SELECT id FROM runtime_bundles "
            "WHERE manifest_json::jsonb ->> 'acceptance_batch' = %s"
        )

        # These two audit tables reject ordinary DELETEs.  The acceptance
        # harness owns the rows selected below, so temporarily disable only
        # their append-only user triggers inside this transaction.  PostgreSQL
        # rolls the ALTER TABLE statements back as well if any cleanup fails.
        connection.execute(
            "ALTER TABLE thread_events DISABLE TRIGGER thread_events_append_only"
        )
        connection.execute(
            "ALTER TABLE cost_ledger DISABLE TRIGGER cost_ledger_append_only"
        )

        execute(f"DELETE FROM turn_metrics WHERE turn_id IN ({turn_ids})", (owner_pattern,))
        execute(f"DELETE FROM turn_asks WHERE turn_id IN ({turn_ids})", (owner_pattern,))
        execute(f"DELETE FROM turn_jobs WHERE thread_id IN ({thread_ids})", (owner_pattern,))
        execute(f"DELETE FROM thread_events WHERE thread_id IN ({thread_ids})", (owner_pattern,))
        execute(f"DELETE FROM thread_messages WHERE thread_id IN ({thread_ids})", (owner_pattern,))
        execute(
            f"DELETE FROM plan_write_intents WHERE plan_document_id IN ({document_ids}) "
            f"OR version_id IN ({version_ids})",
            (owner_pattern, owner_pattern),
        )
        execute(f"DELETE FROM plan_document_versions WHERE plan_document_id IN ({document_ids})", (owner_pattern,))
        execute(f"DELETE FROM plan_documents WHERE thread_id IN ({thread_ids})", (owner_pattern,))
        execute(f"DELETE FROM turns WHERE thread_id IN ({thread_ids})", (owner_pattern,))
        execute("DELETE FROM threads WHERE owner_id LIKE %s", (owner_pattern,))

        execute("DELETE FROM cost_ledger WHERE owner_id LIKE %s", (owner_pattern,))
        execute("DELETE FROM cost_budgets WHERE owner_id LIKE %s", (owner_pattern,))
        execute("DELETE FROM memory_context_pins WHERE owner_id LIKE %s", (owner_pattern,))
        execute(f"DELETE FROM model_attempts WHERE invocation_id IN ({invocation_ids})", (owner_pattern,))
        execute("DELETE FROM model_invocations WHERE owner_id LIKE %s", (owner_pattern,))
        execute("DELETE FROM task_budget_roots WHERE owner_id LIKE %s", (owner_pattern,))
        execute(f"DELETE FROM model_price_snapshots WHERE profile_version_id IN ({profile_version_ids})", (owner_pattern,))
        execute(f"DELETE FROM model_profile_versions WHERE profile_id IN ({profile_ids})", (owner_pattern,))
        execute("DELETE FROM model_profiles WHERE owner_id LIKE %s", (owner_pattern,))
        execute("DELETE FROM model_routing_policies WHERE owner_id LIKE %s", (owner_pattern,))
        execute(
            f"DELETE FROM runtime_channel_events WHERE from_bundle_id IN ({bundle_ids}) "
            f"OR to_bundle_id IN ({bundle_ids})",
            (batch_id, batch_id),
        )
        execute(f"DELETE FROM runtime_bundles WHERE id IN ({bundle_ids})", (batch_id,))

        connection.execute(
            "ALTER TABLE cost_ledger ENABLE TRIGGER cost_ledger_append_only"
        )
        connection.execute(
            "ALTER TABLE thread_events ENABLE TRIGGER thread_events_append_only"
        )

        remaining = connection.execute(
            "SELECT COUNT(*) FROM threads WHERE owner_id LIKE %s",
            (owner_pattern,),
        ).fetchone()[0]
        stable = connection.execute(
            "SELECT bundle_id FROM runtime_channels WHERE name='stable'",
        ).fetchone()[0]
        require(remaining == 0, "M2 test rows remain after cleanup")
        require(stable == restore_bundle_id, "stable runtime bundle was not restored")
    return {"test_rows_removed": removed, "stable_bundle_restored": True}


def load_profile():
    path = os.getenv("LLM_AP_PATH")
    profile = load_llm_ap(path) if path else load_model_profile_from_env()
    require(profile.model == "deepseek-v4-flash", "M2 acceptance requires deepseek-v4-flash")
    require(profile.context_window == 32_768, "M2 acceptance requires a 32,768-token context window")
    require(profile.max_output_tokens == 8_192, "M2 acceptance requires an 8,192-token output limit")
    return replace(
        profile, max_attempts=1, network_retries=0, timeout_seconds=120,
        provider_name="deepseek",
    )


def configure_runtime(runtime, owner: str, round_number: int):
    admin = ModelAdminService(runtime.db, owner_id=owner)
    registered = admin.ensure_profile(load_profile())
    policy = admin.ensure_policy(
        f"{BATCH_ID} round {round_number}",
        {
            role: {"primary": registered.registered_profile_version_id, "fallback": []}
            for role in ROLES
        },
    )
    price_id = f"{BATCH_ID}-price-{round_number}-{uuid.uuid4().hex[:8]}"
    runtime.costs.register_price(
        registered.registered_profile_version_id,
        PriceSnapshot(price_id, 440_000, 14_000, 0, 1_320_000, 0),
        "2026-09-09T00:00:00+00:00",
    )
    runtime.costs.set_budget(owner, "INVOCATION", "default", WORST_ATTEMPT_MICROUSD)
    runtime.costs.set_budget(owner, "DAILY", runtime.costs.today_period(), MAX_COST_MICROUSD)
    base = runtime.behavior.active("stable").manifest
    common = {
        **base,
        "fallback_models": [],
        "model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]},
        "model_role_bindings": policy["roles"],
        "model_price_snapshot_id": price_id,
        "acceptance_batch": BATCH_ID,
        "acceptance_round": round_number,
    }
    first = BehaviorBundleService(runtime.db).ensure({**common, "acceptance_generation": "A"})
    second = BehaviorBundleService(runtime.db).ensure({**common, "acceptance_generation": "B"})
    runtime.behavior.activate("stable", first.id, f"{BATCH_ID}:{round_number}:A")
    return first, second


async def run_turn(runtime, owner: str, thread_id: str, key: str, content: str):
    submitted = runtime.conversation.accept_turn(thread_id, key, content, owner_id=owner)
    require(await runtime.turn_worker.run_once(), f"worker did not claim {key}")
    return runtime.conversation.turn(submitted.turn_id, owner)


def require_turn_status(runtime, turn, expected: str, label: str) -> None:
    if turn.status == expected:
        return
    with runtime.db.connection() as connection:
        job = connection.execute(
            "SELECT status,last_error_json FROM turn_jobs WHERE turn_id=?", (turn.id,),
        ).fetchone()
    details = dict(job) if job is not None else None
    raise AcceptanceFailure(f"{label} turn failed: status={turn.status}, job={details}")


def visible_answer(runtime, owner: str, thread_id: str, turn_id: str) -> str:
    messages = runtime.conversation.messages(thread_id, owner)
    message = next((item for item in reversed(messages) if item.turn_id == turn_id and item.role == "assistant"), None)
    return message.content if message is not None else ""


def answer_payload(ask) -> tuple[list[dict[str, object]], list[str]]:
    answers = []
    visible = []
    for question in ask.questions:
        matching = next((item["label"] for item in question.options if "3" in item["label"]), None)
        if matching is not None:
            answers.append({"question_id": question.id, "selected_options": [matching]})
            visible.append(matching)
        elif question.allow_free_text:
            text = "每周3天，每次30分钟，无伤病，目标是在14天内建立稳定习惯"
            answers.append({"question_id": question.id, "free_text": text})
            visible.append(text)
        else:
            require(bool(question.options), "ask question has no answerable option")
            label = question.options[0]["label"]
            answers.append({"question_id": question.id, "selected_options": [label]})
            visible.append(label)
    return answers, visible


async def run_round(database_url: str, root: Path, profile, round_number: int, budget: BatchBudget):
    owner = f"{BATCH_ID.lower()}-{round_number}-{uuid.uuid4().hex[:8]}"
    runtime = build_runtime(root, profile=profile, database_url=database_url)
    try:
        first_bundle, second_bundle = configure_runtime(runtime, owner, round_number)
        gateway = runtime.conversation.route_model.gateway
        gateway._execute_attempt = lambda request_profile, request, **kwargs: budget.execute(
            round_number, request_profile, request, **kwargs,
        )
        runtime.safety_judge = None
        thread = runtime.conversation.create_thread(f"{BATCH_ID} round {round_number}", owner)

        initial = await run_turn(
            runtime, owner, thread.id, f"{BATCH_ID}:{round_number}:ask",
            "请创建并保存一份14天训练计划文档。开始前必须先通过 ask_user 询问我每周可训练几天；不要先生成计划。",
        )
        require_turn_status(runtime, initial, "AWAITING_INPUT", "initial ask")
        ask = runtime.conversation.pending_ask(initial.id, owner)
        require(ask is not None and 1 <= len(ask.questions) <= 4, "bounded ask was not persisted")

        runtime.behavior.activate("stable", second_bundle.id, f"{BATCH_ID}:{round_number}:B")
        answers, answer_text = answer_payload(ask)
        continuation = runtime.conversation.answer_ask(
            initial.id, initial.version, f"{BATCH_ID}:{round_number}:answer", answers, owner,
        ).turn
        require(continuation.runtime_bundle_id == first_bundle.id, "ask continuation changed its pinned bundle")
        require(continuation.root_budget_id == initial.root_budget_id, "ask continuation reset its root budget")
        require(await runtime.turn_worker.run_once(), "worker did not claim ask continuation")
        continuation = runtime.conversation.turn(continuation.id, owner)
        require_turn_status(runtime, continuation, "COMPLETED", "ask continuation")
        document = runtime.plan_documents.get_by_thread(thread.id)
        version_one = runtime.plan_documents.current_version(document.id)
        require(version_one.source_turn_id == continuation.id, "plan was not created by the ask continuation")

        target = await run_turn(
            runtime, owner, thread.id, f"{BATCH_ID}:{round_number}:target",
            "现在只更新对话目标：改为每周2天。不要修改、创建或保存计划文档，只确认这个新约束。",
        )
        require_turn_status(runtime, target, "COMPLETED", "target-only")
        require(target.runtime_bundle_id == second_bundle.id, "new root did not use the new stable bundle")
        require(target.root_budget_id != initial.root_budget_id, "independent target update reused the prior root")
        unchanged = runtime.plan_documents.current_version(document.id)
        require(unchanged.id == version_one.id, "target-only update silently revised the plan")
        require("2" in visible_answer(runtime, owner, thread.id, target.id), "latest constraint was omitted")

        revision = await run_turn(
            runtime, owner, thread.id, f"{BATCH_ID}:{round_number}:revision",
            "现在明确修改并保存现有计划文档：按每周2天调整，仍保持14天目标。不要提问。",
        )
        require_turn_status(runtime, revision, "COMPLETED", "plan revision")
        version_two = runtime.plan_documents.current_version(document.id)
        require(version_two.id != version_one.id, "explicit plan revision did not create a new version")
        require(version_two.base_version_id == version_one.id, "plan revision lost its base version")
        require("2" in version_two.markdown_content and "14" in version_two.markdown_content, "revised plan omitted the new constraint")

        with runtime.db.connection() as connection:
            invocations = [dict(row) for row in connection.execute(
                "SELECT id,turn_id,role,purpose,status,runtime_bundle_id,root_budget_id,request_digest "
                "FROM model_invocations WHERE owner_id=? ORDER BY created_at,id", (owner,),
            )]
            root = dict(connection.execute(
                "SELECT id,max_attempts,attempts_started FROM task_budget_roots WHERE id=?",
                (initial.root_budget_id,),
            ).fetchone())
            initial_invocations = [item for item in invocations if item["turn_id"] in {initial.id, continuation.id}]
            charged = int(connection.execute(
                "SELECT COALESCE(SUM(amount_microusd),0) FROM cost_ledger "
                "WHERE owner_id=? AND period_kind='DAILY' AND entry_type='CHARGE'", (owner,),
            ).fetchone()[0])
        require(initial_invocations, "ask flow recorded no model invocations")
        require(all(item["root_budget_id"] == initial.root_budget_id for item in initial_invocations), "ask flow escaped its root budget")
        require(all(item["runtime_bundle_id"] == first_bundle.id for item in initial_invocations), "ask flow escaped its pinned bundle")
        require(all(item["status"] == "SUCCEEDED" for item in invocations), "round contains a failed invocation")
        require(root["attempts_started"] <= root["max_attempts"], "root attempt budget was exceeded")
        return {
            "round": round_number,
            "status": "PASSED",
            "owner_id": owner,
            "thread_id": thread.id,
            "ask": {"turn_id": initial.id, "question_count": len(ask.questions), "answers": answer_text},
            "continuation": {"turn_id": continuation.id, "root_budget_id": continuation.root_budget_id},
            "target_update": {"turn_id": target.id, "plan_version_unchanged": version_one.id},
            "plan_revision": {"turn_id": revision.id, "from": version_one.id, "to": version_two.id},
            "bundles": {"initial": first_bundle.id, "replacement": second_bundle.id},
            "root_budget": root,
            "invocations": invocations,
            "charged_microusd": charged,
        }
    finally:
        runtime.db.close()


async def execute(output: Path) -> int:
    database_url = os.getenv("DATABASE_URL", "").strip()
    require(bool(database_url), "DATABASE_URL must identify the current PostgreSQL database")
    verify_current_database(database_url)
    require(os.getenv("M2_LIVE_APPROVED") == "1", "M2_LIVE_APPROVED=1 is required")
    os.environ["AGENT_MODEL_CAPABILITIES"] = "streaming,tool_calling,json_object"
    profile = load_profile()
    restore_bundle_id = stable_bundle_id(database_url)
    budget = BatchBudget()
    report: dict[str, object] = {
        "batch_id": BATCH_ID,
        "status": "RUNNING",
        "database": "better_agent",
        "storage": "current_database_with_batch_scoped_cleanup",
        "limits": {
            "rounds": MAX_ROUNDS,
            "attempts_per_round": MAX_ATTEMPTS_PER_ROUND,
            "total_attempts": MAX_TOTAL_ATTEMPTS,
            "max_seconds": MAX_SECONDS,
            "worst_cost_microusd": MAX_COST_MICROUSD,
            "fallback_models": 0,
        },
        "source_manifest": source_manifest(),
        "rounds": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="better-m2-live-") as directory:
            for round_number in range(1, MAX_ROUNDS + 1):
                result = await run_round(database_url, Path(directory) / str(round_number), profile, round_number, budget)
                report["rounds"].append(result)
        report["status"] = "PASSED"
    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        try:
            report["cleanup"] = cleanup_batch(database_url, BATCH_ID, restore_bundle_id)
        except Exception as cleanup_error:
            report["cleanup"] = {"test_rows_removed": False, "error": str(cleanup_error)[:500]}
            report["status"] = "FAILED"
        report["attempts"] = budget.attempts
        report["elapsed_seconds"] = round(time.monotonic() - budget.started, 3)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "batch_id": BATCH_ID,
        "status": report["status"],
        "attempts": len(budget.attempts),
        "cleanup": report.get("cleanup"),
        "output": str(output),
    }, ensure_ascii=False))
    return 0 if report["status"] == "PASSED" else 1


def preflight() -> dict[str, object]:
    return {
        "batch_id": BATCH_ID,
        "database": "better_agent",
        "storage": "current_database_with_batch_scoped_cleanup",
        "status": "READY" if os.getenv("M2_LIVE_APPROVED") == "1" else "NOT_AUTHORISED",
        "network_started": False,
        "limits": {
            "rounds": MAX_ROUNDS,
            "attempts_per_round": MAX_ATTEMPTS_PER_ROUND,
            "total_attempts": MAX_TOTAL_ATTEMPTS,
            "max_seconds": MAX_SECONDS,
            "worst_cost_microusd": MAX_COST_MICROUSD,
            "fallback_models": 0,
        },
        "source_manifest": source_manifest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("../docs/acceptance/m2-live-results-2026-09-09-008.json"))
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(preflight(), ensure_ascii=False))
        return 0
    return asyncio.run(execute(args.output.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
