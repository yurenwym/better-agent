from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import psycopg
from psycopg import sql

from app.behavior import BehaviorBundleService
from app.config import load_llm_ap
from app.costs import PriceSnapshot
from app.embedding import OpenAICompatibleEmbeddingClient, load_embedding_profile_from_env
from app.embedding_worker import EmbeddingWorker
from app.memory_archive import ArchiveUnavailable, ConversationArchiver, LiveEpisodeSummarizer
from app.memory_v2 import MemoryContextProvider
from app.model_admin import ModelAdminService, ROLES
from app.model_control import RoutedModelGateway
from app.startup import build_runtime
from app.token_budget import DEFAULT_TOKEN_COUNTER


BATCH_ID = "M1-LIVE-20260908-005"
MAX_MODEL_ATTEMPTS = 21
MODEL_BUDGET_MICROUSD = 530_000
INVOCATION_BUDGET_MICROUSD = 25_232
MAX_EMBEDDING_REQUESTS = 6
MAX_EMBEDDING_INPUT = 12_288
MAX_EMBEDDING_ITEM_INPUT = 2_048
EMBEDDING_TIMEOUT_SECONDS = 60
MAX_SECONDS = 25 * 60
EXPECTED_HASHES = {
    "app/costs.py": "024ce73af79d286e764fddfe819d115d0b16b443a912d98ddadaec975b3ebecd",
    "app/live_model.py": "c10640c148ef517ac0af8dfb76522fc1fb20ebf633d01b7d3abdd614e26fba4c",
    "app/memory_archive.py": "aa47618ce5fe8d76c938240c58b933fdce3d055feb58e221ef3b65cb906ae0a2",
    "app/memory_v2.py": "857b74803a1f06b9fddd6a67aca50b85c3f9aa749a5825cef65fd26000e6cddb",
    "app/conversation.py": "9e51691df04da066af9a00813140316ef5de5e2eddc1aaacbc66c51724c9953d",
}


class AcceptanceFailure(RuntimeError):
    pass


@dataclass
class BatchBudget:
    started: float = field(default_factory=time.monotonic)
    model_attempts: list[dict[str, object]] = field(default_factory=list)
    embedding_requests: list[dict[str, object]] = field(default_factory=list)
    embedding_input_upper_bound: int = 0

    def remaining(self) -> float:
        value = MAX_SECONDS - (time.monotonic() - self.started)
        if value <= 0:
            raise AcceptanceFailure("batch duration exceeded 25 minutes")
        return value

    async def model_attempt(self, profile, request, **kwargs):
        self.remaining()
        if len(self.model_attempts) >= MAX_MODEL_ATTEMPTS:
            raise AcceptanceFailure("model attempt hard limit reached before network request")
        record: dict[str, object] = {
            "ordinal": len(self.model_attempts) + 1,
            "round": os.environ.get("M1_LIVE_ROUND", "unknown"),
            "role": request.role,
            "purpose": request.purpose,
            "status": "started",
            "started_at": time.time(),
        }
        self.model_attempts.append(record)
        try:
            response = await asyncio.wait_for(
                RoutedModelGateway._execute_http_attempt(profile, request, **kwargs),
                timeout=min(float(profile.timeout_seconds), self.remaining()),
            )
        except asyncio.TimeoutError as exc:
            record["status"] = "batch_timeout"
            raise AcceptanceFailure("model request exceeded the remaining batch duration") from exc
        except Exception as exc:
            record["status"] = "failed"
            record["error_kind"] = getattr(exc, "kind", type(exc).__name__)
            raise
        usage = response.usage
        record.update({
            "status": "succeeded",
            "uncached_input_tokens": usage.uncached_input_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "latency_seconds": round(response.timing.finished_at - response.timing.started_at, 6),
        })
        return response

    def embedding_request(self, client, texts):
        self.remaining()
        costs = [DEFAULT_TOKEN_COUNTER.count_text(text) for text in texts]
        if any(cost > MAX_EMBEDDING_ITEM_INPUT for cost in costs):
            raise AcceptanceFailure("embedding item input exceeds 2,048-token upper bound")
        if len(self.embedding_requests) >= MAX_EMBEDDING_REQUESTS:
            raise AcceptanceFailure("embedding request hard limit reached before network request")
        if self.embedding_input_upper_bound + sum(costs) > MAX_EMBEDDING_INPUT:
            raise AcceptanceFailure("embedding batch input hard limit reached before network request")
        self.embedding_input_upper_bound += sum(costs)
        record: dict[str, object] = {
            "ordinal": len(self.embedding_requests) + 1,
            "round": os.environ.get("M1_LIVE_ROUND", "unknown"),
            "input_upper_bound": sum(costs),
            "status": "started",
            "started_at": time.time(),
        }
        self.embedding_requests.append(record)
        try:
            response = client.embed(texts)
        except Exception as exc:
            record["status"] = "failed"
            record["error_kind"] = getattr(exc, "kind", type(exc).__name__)
            raise
        record.update({
            "status": "succeeded",
            "provider_prompt_tokens": response.prompt_tokens,
            "provider_total_tokens": response.total_tokens,
        })
        return response


class BudgetedEmbeddingProvider:
    def __init__(self, budget: BatchBudget, client: OpenAICompatibleEmbeddingClient) -> None:
        self.budget = budget
        self.client = client

    def embed(self, texts):
        return self.budget.embedding_request(self.client, texts)


class CapturingMemoryContext:
    def __init__(self, provider: MemoryContextProvider) -> None:
        self.provider = provider
        self.last = None

    def select(self, request):
        self.last = self.provider.select(request)
        return self.last


class UnavailableArchiver:
    keep_tokens = 1

    def enqueue(self, *_args, **_kwargs):
        raise ArchiveUnavailable("injected isolated acceptance outage")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_frozen_inputs(backend_root: Path) -> None:
    for relative, expected in EXPECTED_HASHES.items():
        actual = sha256(backend_root / relative)
        require(actual == expected, f"frozen input changed: {relative} ({actual})")


def run_command(command: list[str], *, cwd: Path, env=None, timeout=90) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, check=True, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return result.stdout.strip()


def create_round_database(admin_url: str, name: str) -> str:
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    return admin_url.rsplit("/", 1)[0] + "/" + name


def migrate(backend_root: Path, database_url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    run_command(
        [sys.executable, "-m", "alembic", "-c", str(backend_root / "alembic.ini"), "upgrade", "head"],
        cwd=backend_root, env=env, timeout=60,
    )


def configure_owner_runtime(runtime, owner: str, profile, attempt_budget: BatchBudget):
    admin = ModelAdminService(runtime.db, owner_id=owner)
    registered = admin.ensure_profile(profile)
    policy = admin.ensure_policy("M1 isolated acceptance", {
        role: {"primary": registered.registered_profile_version_id, "fallback": []}
        for role in ROLES
    })
    base = runtime.behavior.active("stable").manifest
    manifest = {
        **base,
        "fallback_models": [],
        "model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]},
        "model_role_bindings": policy["roles"],
        "model_price_snapshot_id": f"{BATCH_ID}-deepseek-peak",
        "acceptance_batch": BATCH_ID,
        "acceptance_owner": owner,
    }
    bundle = BehaviorBundleService(runtime.db).ensure(manifest)
    runtime.behavior.activate("stable", bundle.id, f"{BATCH_ID}:{owner}:stable")
    runtime.costs.register_price(
        registered.registered_profile_version_id,
        PriceSnapshot(
            f"{BATCH_ID}-deepseek-peak",
            uncached_input_rate=440_000,
            cache_read_rate=14_000,
            cache_write_rate=0,
            output_rate=1_320_000,
            reasoning_rate=0,
        ),
        "2026-09-08T00:00:00+00:00",
    )
    runtime.costs.set_budget(owner, "INVOCATION", "default", INVOCATION_BUDGET_MICROUSD)
    runtime.costs.set_budget(owner, "DAILY", runtime.costs.today_period(), MODEL_BUDGET_MICROUSD)
    runtime.conversation.route_model.gateway._execute_attempt = attempt_budget.model_attempt
    # These acceptance prompts are intentionally self-contained. Skipping the
    # generic research-intent pre-classifier keeps the frozen six-call contract.
    runtime.conversation.route_model.gateway.supports_intent_classification = False
    # M1 validates memory, conversation degradation, and Episode archival only.
    # Safety judging and evolution exposure are separate unapproved workflows.
    runtime.safety_judge = None
    runtime.evolution = None
    return bundle, registered


def seed_memory(runtime, owner: str, candidate_thread_id: str, embedding_provider) -> dict[str, object]:
    store = runtime.memory_store
    cycling = store.remember(
        owner, "constraint", "user", "", "骑行负荷每周增幅不超过10%", f"{BATCH_ID}:{owner}:cycling",
    )
    worker = EmbeddingWorker(runtime.db, embedding_provider, owner=f"{BATCH_ID}-{owner}-embedding")
    require(worker.run_once(), "cycling embedding job was not processed")
    with runtime.db.connection() as connection:
        embedding_status = connection.execute(
            "SELECT status FROM embedding_jobs WHERE revision_id=?", (cycling.revision_id,),
        ).fetchone()["status"]
    require(embedding_status == "COMPLETED", f"cycling embedding job ended in {embedding_status}")
    recovery = store.remember(
        owner, "fact", "user", "", "recovery_friday：训练恢复日安排在周五", f"{BATCH_ID}:{owner}:recovery",
    )
    database = store.remember(
        owner, "preference", "user", "", "长期默认数据库是PostgreSQL", f"{BATCH_ID}:{owner}:database", pinned=True,
    )
    irrelevant = store.remember(
        owner, "preference", "user", "", "编辑器配色偏好是solarized", f"{BATCH_ID}:{owner}:irrelevant",
    )
    cross_scope = store.remember(
        owner, "constraint", "project", "project-other", "另一个项目固定使用Rust", f"{BATCH_ID}:{owner}:cross",
    )
    expired = store.remember(
        owner, "constraint", "user", "", "过期规则要求骑行负荷增加30%", f"{BATCH_ID}:{owner}:expired",
    )
    deleted = store.remember(
        owner, "fact", "user", "", "已删除规则要求周二恢复", f"{BATCH_ID}:{owner}:deleted",
    )
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE threads SET project_id=? WHERE id=? AND owner_id=?",
            ("project-current", candidate_thread_id, owner),
        )
        connection.execute(
            "UPDATE memory_entries SET valid_until='2020-01-01T00:00:00+00:00' WHERE id=?",
            (expired.id,),
        )
    store.purge(deleted.id, owner, idempotency_key=f"{BATCH_ID}:{owner}:purge")
    return {
        "cycling": cycling, "recovery": recovery, "database": database,
        "irrelevant": irrelevant, "cross_scope": cross_scope,
        "expired": expired, "deleted": deleted,
    }


async def run_turn(runtime, owner: str, thread_id: str, key: str, content: str):
    submitted = runtime.conversation.accept_turn(thread_id, key, content, owner_id=owner)
    require(await runtime.turn_worker.run_once(), f"turn worker did not claim {key}")
    turn = runtime.conversation.turn(submitted.turn_id, owner)
    messages = runtime.conversation.messages(thread_id, owner)
    assistant = next((message for message in reversed(messages) if message.turn_id == turn.id and message.role == "assistant"), None)
    return turn, assistant


def require_completed_turn(runtime, turn, assistant, label: str) -> None:
    if turn.status == "COMPLETED" and assistant is not None:
        return
    with runtime.db.connection() as connection:
        job = connection.execute(
            "SELECT status,last_error_json FROM turn_jobs WHERE turn_id=?", (turn.id,),
        ).fetchone()
    details = dict(job) if job is not None else None
    raise AcceptanceFailure(f"{label} turn failed: status={turn.status}, job={details}")


def seed_archive_exchange(runtime, owner: str, thread_id: str, suffix: str) -> tuple[str, str]:
    now = "2026-09-08T00:00:00+00:00"
    turn_id = f"{BATCH_ID}-{suffix}-archive-turn"
    user_id = f"{BATCH_ID}-{suffix}-archive-user"
    assistant_id = f"{BATCH_ID}-{suffix}-archive-assistant"
    root_budget_id = runtime.costs.create_default_root_budget(
        owner, "turn", turn_id,
    )["id"]
    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,runtime_bundle_id,root_budget_id,created_at,updated_at) "
            "VALUES (?,?,?,'COMPLETED',?,?,?,?)",
            (turn_id, thread_id, turn_id, runtime.behavior.active("stable").id, root_budget_id, now, now),
        )
        connection.execute(
            "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at,completed_at) "
            "VALUES (?,?,?,'user',?,'ready',1,?,1,?,?)",
            (user_id, thread_id, turn_id, "我确认训练计划为每天60分钟。", len("我确认训练计划为每天60分钟。"), now, now),
        )
        connection.execute(
            "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at,completed_at) "
            "VALUES (?,?,?,'assistant',?,'ready',1,?,2,?,?)",
            (assistant_id, thread_id, turn_id, "助手建议每天90分钟，但用户尚未确认。", len("助手建议每天90分钟，但用户尚未确认。"), now, now),
        )
    return user_id, assistant_id


def side_effect_counts(runtime) -> dict[str, int]:
    tables = ("turn_asks", "plan_documents", "research_jobs", "agent_runs", "memory_entries")
    with runtime.db.connection() as connection:
        return {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


async def run_round(
    backend_root: Path, database_url: str, data_root: Path, round_number: int,
    profile, batch_budget: BatchBudget,
) -> dict[str, object]:
    owner = f"{BATCH_ID.lower()}-owner-{round_number}-{uuid.uuid4().hex[:8]}"
    os.environ["M1_LIVE_ROUND"] = str(round_number)
    runtime = build_runtime(data_root, profile=profile, database_url=database_url)
    generated_embedding_client = runtime.embedding_client
    embedding_client = OpenAICompatibleEmbeddingClient(replace(
        load_embedding_profile_from_env(), timeout_seconds=EMBEDDING_TIMEOUT_SECONDS,
    ))
    embedding_provider = BudgetedEmbeddingProvider(batch_budget, embedding_client)
    try:
        bundle, registered = configure_owner_runtime(runtime, owner, profile, batch_budget)
        baseline_thread = runtime.conversation.create_thread(f"{BATCH_ID} baseline {round_number}", owner)
        candidate_thread = runtime.conversation.create_thread(f"{BATCH_ID} candidate {round_number}", owner)
        memory = seed_memory(runtime, owner, candidate_thread.id, embedding_provider)

        prompt = (
            "这是一次信息提取题，请直接回答，不要提问、保存、研究或启动协作。"
            "本次临时改用SQLite；若上下文中能看到长期默认数据库，请同时说明它。"
            "再回答：自行车锻炼强度递增上限是多少，recovery_friday是哪天？"
            "只使用当前消息和系统提供的上下文，不补充一般建议。"
        )
        runtime.memory_context = None
        baseline_turn, baseline = await run_turn(
            runtime, owner, baseline_thread.id, f"{BATCH_ID}-{round_number}-baseline", prompt,
        )
        require_completed_turn(runtime, baseline_turn, baseline, "baseline")

        capturing = CapturingMemoryContext(MemoryContextProvider(runtime.db, embedding_provider=embedding_provider))
        runtime.memory_context = capturing
        before_memory = {entry.id: entry.revision_id for entry in runtime.memory_store.list_entries(owner)}
        candidate_turn, candidate = await run_turn(
            runtime, owner, candidate_thread.id, f"{BATCH_ID}-{round_number}-candidate", prompt,
        )
        require_completed_turn(runtime, candidate_turn, candidate, "candidate")
        require(capturing.last is not None, "candidate did not retrieve memory")
        selected = set(capturing.last.revision_ids)
        require(memory["cycling"].revision_id in selected, "semantic cycling memory was not selected")
        require(memory["recovery"].revision_id in selected, "unembedded lexical memory was not selected")
        require(memory["database"].revision_id in selected, "pinned database memory was not selected")
        excluded = {memory[name].revision_id for name in ("irrelevant", "cross_scope", "expired", "deleted")}
        require(not selected.intersection(excluded), "an unrelated, cross-scope, expired, or deleted memory was selected")
        require(capturing.last.renderer_version == "memory-v5", "candidate renderer is not memory-v5")
        require(capturing.last.trace.get("embedding_coverage", 1) < 1, "partial embedding coverage was not exercised")
        require(capturing.last.trace.get("retrieval_mode") == "hybrid", "semantic and lexical recall did not form a hybrid result")
        candidate_text = candidate.content
        for expected in ("SQLite", "PostgreSQL", "10%", "周五"):
            require(expected.lower() in candidate_text.lower(), f"candidate answer omitted {expected}")
        for forbidden in ("solarized", "Rust", "30%", "周二"):
            require(forbidden.lower() not in candidate_text.lower(), f"candidate answer leaked excluded memory: {forbidden}")
        after_memory = {entry.id: entry.revision_id for entry in runtime.memory_store.list_entries(owner)}
        require(before_memory == after_memory, "current SQLite correction modified long-term memory")

        archive_thread = runtime.conversation.create_thread(f"{BATCH_ID} archive {round_number}", owner)
        archive_sources = seed_archive_exchange(runtime, owner, archive_thread.id, str(round_number))
        archiver = ConversationArchiver(
            runtime.db, runtime.memory_store, LiveEpisodeSummarizer(runtime.conversation.route_model.gateway),
            keep_messages=0, max_attempts=1,
        )
        episode = await archiver.archive_thread(archive_thread.id, owner)
        if episode is None:
            raise AcceptanceFailure(f"real Episode archive failed: {archiver.status(archive_thread.id, owner)}")
        require(await archiver.archive_thread(archive_thread.id, owner) is None, "archive refresh created duplicate work")
        with runtime.db.connection() as connection:
            row = connection.execute("SELECT * FROM memory_episodes WHERE id=?", (episode.id,)).fetchone()
            count = int(connection.execute("SELECT COUNT(*) FROM memory_episodes WHERE thread_id=?", (archive_thread.id,)).fetchone()[0])
            job = connection.execute("SELECT * FROM memory_archive_jobs WHERE thread_id=?", (archive_thread.id,)).fetchone()
        structured = {name: json.loads(row[f"{name}_json"]) for name in ("synopsis", "decisions", "outcomes", "open_loops")}
        require(any("60" in item["text"] for items in structured.values() for item in items), "Episode lost user-confirmed 60 minutes")
        require(not any("90" in item["text"] for name in ("decisions", "outcomes") for item in structured[name]), "Episode promoted unconfirmed 90 minutes to a decision")
        require(set(json.loads(row["source_message_ids_json"])) == set(archive_sources), "Episode source message list is inconsistent")
        valid_refs = all(set(item["source_message_ids"]).issubset(set(archive_sources)) for items in structured.values() for item in items)
        require(valid_refs, "Episode contains an invalid source attribution")
        require(row["prompt_version"] == "episode-v4", "Episode prompt version mismatch")
        require(row["tokenizer_version"] == "utf8-upper-bound-v1", "Episode tokenizer version mismatch")
        require(count == 1 and job["status"] == "COMPLETED", "Episode archive is not idempotently completed")

        runtime.archiver = UnavailableArchiver()
        independent_thread = runtime.conversation.create_thread(f"{BATCH_ID} incomplete independent {round_number}", owner)
        seed_archive_exchange(runtime, owner, independent_thread.id, f"{round_number}-independent")
        independent_turn, independent = await run_turn(
            runtime, owner, independent_thread.id, f"{BATCH_ID}-{round_number}-independent", "什么是哈希表？",
        )
        require_completed_turn(runtime, independent_turn, independent, "independent incomplete-context")
        require(
            "历史上下文不完整" in independent.content and "哈希" in independent.content,
            f"independent answer lacks the incomplete-context notice or answer: {independent.content!r}",
        )

        dependent_thread = runtime.conversation.create_thread(f"{BATCH_ID} incomplete dependent {round_number}", owner)
        seed_archive_exchange(runtime, owner, dependent_thread.id, f"{round_number}-dependent")
        before_effects = side_effect_counts(runtime)
        dependent_turn, dependent = await run_turn(
            runtime, owner, dependent_thread.id, f"{BATCH_ID}-{round_number}-dependent", "继续刚才的计划并保存",
        )
        after_effects = side_effect_counts(runtime)
        require(dependent_turn.status == "FAILED", "dependent incomplete-context request was not blocked")
        require(before_effects == after_effects, "blocked dependent request produced a side effect")

        with runtime.db.connection() as connection:
            attempts = [dict(row) for row in connection.execute(
                "SELECT a.id,a.invocation_id,a.ordinal,a.reason,a.status,a.uncached_input_tokens,a.cache_read_tokens,"
                "a.cache_write_tokens,a.output_tokens,a.cost_status,a.cost_microusd,i.role,i.purpose "
                "FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id "
                "WHERE i.owner_id=? ORDER BY a.started_at,a.id", (owner,),
            )]
            invocations = [dict(row) for row in connection.execute(
                "SELECT id,role,purpose,status,selected_attempt_id,request_digest,route_snapshot_json "
                "FROM model_invocations WHERE owner_id=? ORDER BY created_at,id", (owner,),
            )]
            charged = int(connection.execute(
                "SELECT COALESCE(SUM(amount_microusd),0) FROM cost_ledger WHERE owner_id=? AND period_kind='DAILY' AND entry_type='CHARGE'",
                (owner,),
            ).fetchone()[0])
        require(len(invocations) == 6, f"round {round_number} used {len(invocations)} model invocations instead of 6")
        require(all(item["status"] == "SUCCEEDED" for item in invocations), "round contains a failed model invocation")
        require(all(item["cost_status"] in {"ESTIMATED_COMPLETE", "ESTIMATED_PARTIAL"} for item in attempts), "round contains unexplained model cost")
        return {
            "round": round_number,
            "owner_id": owner,
            "database": database_url.rsplit("/", 1)[-1],
            "runtime_bundle_id": bundle.id,
            "profile_version_id": registered.registered_profile_version_id,
            "threads": {
                "baseline": baseline_thread.id, "candidate": candidate_thread.id,
                "archive": archive_thread.id, "independent": independent_thread.id,
                "dependent": dependent_thread.id,
            },
            "retrieval": {
                "revision_ids": list(capturing.last.revision_ids),
                "episode_ids": list(capturing.last.episode_ids),
                "renderer_version": capturing.last.renderer_version,
                "tokenizer_version": capturing.last.tokenizer_version,
                "bundle_hash": capturing.last.bundle_hash,
                "trace": capturing.last.trace,
            },
            "episode": {
                "id": episode.id, "source_hash": row["source_hash"],
                "source_message_ids": list(archive_sources), "prompt_version": row["prompt_version"],
                "tokenizer_version": row["tokenizer_version"], "structured": structured,
            },
            "model_attempts": attempts,
            "model_invocations": invocations,
            "charged_microusd": charged,
            "answers": {
                "baseline_sha256": hashlib.sha256(baseline.content.encode()).hexdigest(),
                "candidate_sha256": hashlib.sha256(candidate.content.encode()).hexdigest(),
                "independent_sha256": hashlib.sha256(independent.content.encode()).hexdigest(),
            },
            "dependent_terminal_status": dependent_turn.status,
            "side_effect_counts": after_effects,
        }
    finally:
        embedding_client.close()
        if generated_embedding_client is not None:
            generated_embedding_client.close()
        runtime.db.close()


async def main(output: Path) -> int:
    backend_root = Path(__file__).resolve().parents[1]
    verify_frozen_inputs(backend_root)
    os.environ["AGENT_MODEL_CAPABILITIES"] = "streaming,tool_calling,json_object"
    for name in (
        "AGENT_FALLBACK_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_ID",
        "AGENT_FALLBACK_MODEL_API_KEY", "AGENT_FALLBACK_MODEL_API_KEY_ENV",
        "AGENT_FALLBACK_MODEL_CAPABILITIES",
    ):
        os.environ.pop(name, None)
    profile = replace(
        load_llm_ap(os.environ["LLM_AP_PATH"]),
        max_attempts=3, network_retries=2, timeout_seconds=60,
        provider_name="deepseek",
    )
    require(profile.model == "deepseek-v4-flash", "approved DeepSeek model is not configured")
    require(profile.context_window == 32_768 and profile.max_output_tokens == 8_192, "approved model limits changed")
    embedding = replace(load_embedding_profile_from_env(), timeout_seconds=EMBEDDING_TIMEOUT_SECONDS)
    require(embedding.model == "Qwen/Qwen3-Embedding-4B" and embedding.dimensions == 1_024, "approved embedding profile changed")
    require(re.match(r"^https://api\.siliconflow\.cn(?:/|$)", embedding.base_url) is not None, "embedding base URL is not SiliconFlow")

    compose_file = backend_root / "tests" / "integration" / "compose.postgres.yaml"
    project = f"better-m1-live-{uuid.uuid4().hex[:10]}"
    budget = BatchBudget()
    report: dict[str, object] = {
        "batch_id": BATCH_ID,
        "status": "RUNNING",
        "started_at": time.time(),
        "limits": {
            "model_attempts": MAX_MODEL_ATTEMPTS, "model_microusd": MODEL_BUDGET_MICROUSD,
            "embedding_requests": MAX_EMBEDDING_REQUESTS, "embedding_input": MAX_EMBEDDING_INPUT,
            "seconds": MAX_SECONDS,
        },
        "rounds": [],
    }
    try:
        run_command(["docker", "compose", "-f", str(compose_file), "-p", project, "up", "-d", "--wait", "postgres"], cwd=backend_root)
        published = run_command(["docker", "compose", "-f", str(compose_file), "-p", project, "port", "postgres", "5432"], cwd=backend_root)
        match = re.search(r":(\d+)$", published)
        require(match is not None, "could not resolve isolated PostgreSQL port")
        admin_url = f"postgresql://better_agent_test:better_agent_test@127.0.0.1:{match.group(1)}/better_agent_test"
        for round_number in range(1, 4):
            database_name = f"m1_live_{uuid.uuid4().hex[:16]}"
            database_url = create_round_database(admin_url, database_name)
            migrate(backend_root, database_url)
            data_root = output.parent / f"{BATCH_ID.lower()}-round-{round_number}"
            report["rounds"].append(await run_round(
                backend_root, database_url, data_root, round_number, profile, budget,
            ))
        invocation_count = sum(len(item["model_invocations"]) for item in report["rounds"])
        require(invocation_count == 18, "batch did not finish with exactly 18 model invocations")
        require(18 <= len(budget.model_attempts) <= MAX_MODEL_ATTEMPTS, "batch model attempt count is outside 18-21")
        require(len(budget.embedding_requests) == 6, "batch did not finish with exactly 6 embedding requests")
        require(sum(item["charged_microusd"] for item in report["rounds"]) <= MODEL_BUDGET_MICROUSD, "model charges exceeded the approved batch ceiling")
        report["status"] = "PASSED"
    except Exception as exc:
        report["status"] = "FAILED"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        report["finished_at"] = time.time()
        report["elapsed_seconds"] = round(time.monotonic() - budget.started, 6)
        report["model_network_attempts"] = budget.model_attempts
        report["embedding_network_requests"] = budget.embedding_requests
        report["embedding_input_upper_bound"] = budget.embedding_input_upper_bound
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "-p", project, "down"],
            cwd=backend_root, check=False, capture_output=True, encoding="utf-8", errors="replace",
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.output.resolve())))
