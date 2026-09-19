"""R2-04 acceptance: bounded wait, cancellability, and the local latency budget.

Measures, offline and deterministically, the quantities the plan asks for:

* ``counting_ms``  -- the local context-selection cost R2 added to the answer
  path. Candidate P95 target: **200ms**.
* ``status_hint_ms`` -- from the start of the context phase to the
  ``context.archiving`` status being *committed*. Candidate target: **within
  1s**. Only the *backend* half is measurable here; the rest is transport, and
  the report says so rather than claiming the whole path.
* ``wait_rate`` / ``timeout_rate`` -- how often a turn waits at all, and how
  often the wait runs out. Both with the background worker on and off.
* ``cancel_ms`` -- how quickly a stop request ends the wait.
* restart -- after a timeout, the next turn still works.

What this deliberately does **not** claim: online end-to-end P50/P95. There is
no network here and the summariser is a stub, so these are local-cost numbers,
not user-perceived latency.

How the harness stays honest about *what* it drives
---------------------------------------------------

It calls ``ManagedTurnWorker._archive_history_before_generation`` directly --
the same entry point the real worker calls -- with ``agent_runtime`` wired to
the archiver exactly as ``startup.py`` wires it. An earlier revision set
``agent_runtime = None`` to "drive the selector directly"; that made the method
early-return on ``archiver is None`` and the whole matrix silently measured
nothing. The reachability is therefore part of the fixture, not an accident.

With no ``route_model`` the turn resolves its budget through the documented
unrouted fallback in ``ConversationService._hot_window``: ``H`` is the
archiver's own ``keep_tokens``. That keeps the harness free of model control and
self-consistent -- the same ``H`` drives the trigger, the target, the packing
budget and the wait -- and the case table prints it so the reader can see which
budget each row was measured against.

Default invocation is offline and spends nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BATCH_ID = "R2-04-OFFLINE-001"
TARGET_COUNTING_P95_MS = 200
TARGET_STATUS_HINT_MS = 1_000

# The seeded thread starts far larger than the budget, so the first live turn
# has to compact before it can answer.
TURNS = 24
TURN_BYTES = 900
# The live turns are the ones the foreground actually has to answer. They carry
# real bytes so the thread keeps growing and keeps re-crossing the trigger; a
# turn with no messages is invisible to the transcript builder (it needs at
# least one ``ready`` message), and the matrix would silently measure nothing.
LIVE_TURNS = 6
LIVE_TURN_BYTES = 3_000
# ``H`` on the unrouted path is the archiver's retention, so this is the lever
# that decides how oversized the thread is.
KEEP_TOKENS = 4_000
WAIT_MS = 2_000
POLL_MS = 50


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "p50": round(_percentile(values, 0.50), 2),
        "p95": round(_percentile(values, 0.95), 2),
        "max": round(max(values), 2),
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _StubSummarizer:
    """Deterministic, instant summariser: this harness measures local cost."""

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.calls = 0

    async def __call__(self, payload: dict) -> dict:
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        ids = [
            event["message_id"]
            for turn in payload["turns"] for event in turn["events"]
            if event["message_id"]
        ]
        return {
            "synopsis": [{"text": "stub", "source_message_ids": [ids[0]]}],
            "topics": [], "decisions": [], "outcomes": [], "open_loops": [],
            "sensitivity": "normal",
        }


class _EventTap:
    """Records *when* each thread event is committed, in perf-counter seconds.

    The status hint has to be timed at the moment it is committed, not when the
    wait returns. Measuring it after the call would report a 2s wait as a 2s
    "status hint", which is precisely the kind of number that flatters the
    change instead of describing it.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._original = store.append
        self.marks: list[tuple[str, float, dict]] = []

    def __enter__(self) -> "_EventTap":
        def append(thread_id, turn_id, event_type, actor, data, **kwargs):
            event = self._original(thread_id, turn_id, event_type, actor, data, **kwargs)
            self.marks.append((event_type, time.perf_counter(), dict(data)))
            return event

        self._store.append = append
        return self

    def __exit__(self, *exc) -> bool:
        self._store.append = self._original
        return False

    def drain(self) -> list[tuple[str, float, dict]]:
        marks, self.marks = self.marks, []
        return marks


def _build(
    tmp_path: Path, *, keep_tokens: int, summarizer, wait_ms: int, poll_ms: int,
    lease_seconds: int = 30, route_model=None,
):
    from app.conversation import ConversationService
    from app.db import Database
    from app.memory_archive import ConversationArchiver, ManagedArchiveWorker, foreground_turn_pending
    from app.memory_v2 import MemoryStore

    os.environ["AGENT_ARCHIVE_WAIT_MS"] = str(wait_ms)
    os.environ["AGENT_ARCHIVE_WAIT_POLL_MS"] = str(poll_ms)

    db = Database(tmp_path / "acceptance.db", workspace=tmp_path / "workspace")
    archiver = ConversationArchiver(
        db, MemoryStore(db, tmp_path / "memory"), summarizer, keep_tokens=keep_tokens,
        lease_seconds=lease_seconds,
    )
    worker = ManagedArchiveWorker(
        archiver, foreground_probe=lambda: foreground_turn_pending(db),
    )
    # The same wiring `startup.py` uses. Without it the worker's archival path
    # is unreachable and every case measures nothing.
    conversation = ConversationService(db, agent_runtime=_ArchiverHost(archiver), route_model=route_model)
    return db, conversation, archiver, worker


class _ArchiverHost:
    """The minimum ``agent_runtime`` surface the archival path reads.

    ``_archive_history_before_generation`` and ``_history`` both read
    ``conversation.agent_runtime.archiver``; ``_hot_window``'s unrouted fallback
    reads its ``keep_tokens``. Nothing else on this path touches the runtime, so
    a full runtime would only add unrelated moving parts.
    """

    def __init__(self, archiver) -> None:
        self.archiver = archiver


class _ResolvedProfileStore:
    """A stand-in for the model-control store: one frozen profile, no routing."""

    def __init__(self, profile) -> None:
        self._profile = profile

    def resolved_profile(self, context):
        return self._profile


class _StaticRouteModel:
    """A route model that never routes.

    ``ConversationService`` replaces any ``route_model`` without
    ``route_and_respond`` with its fallback, so the attribute has to exist or
    the frozen profile is silently dropped and the case measures the unrouted
    fallback instead. It is never called: this harness sends no requests.
    """

    def __init__(self, profile) -> None:
        self.control_store = _ResolvedProfileStore(profile)

    async def route_and_respond(self, *args, **kwargs):
        raise AssertionError("the acceptance harness must not send a model request")


def _repository_profile():
    """The repository's legacy profile: ``H`` comes out at 23,348 bytes.

    This is the profile the plan's 200ms candidate target was written against,
    so one case measures on it rather than only on the unrouted fallback.
    """
    from app.model_gateway import ModelProfile

    return ModelProfile(
        base_url="https://api.deepseek.com", model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY", context_window=32768, max_output_tokens=8192,
    )


def _seed(db, conversation, *, turns: int, size: int):
    thread = conversation.create_thread(owner_id="local-user")
    now = "2026-01-01T00:00:00+00:00"
    with db.transaction() as connection:
        for index in range(turns):
            turn_id = f"hist-{index}"
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED',?,?)", (turn_id, thread.id, turn_id, now, now),
            )
            for role, offset in (("user", 1), ("assistant", 2)):
                connection.execute(
                    "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                    "content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                    (f"m-{index}-{role}", thread.id, turn_id, role, "x" * size, size,
                     index * 2 + offset, now, now),
                )
    return thread, turns * 2 + 1


def _insert_live_turn(db, conversation, thread_id: str, turn_id: str, seq: int, size: int):
    """A COMPLETED turn carrying one real user message, then its snapshot.

    Inserted directly rather than through ``accept_turn`` so the harness spends
    no model budget and every row is deterministic. The transcript builder needs
    a ``ready`` message before a turn is visible at all, which is why the
    message is not optional.
    """
    now = "2026-01-01T00:00:00+00:00"
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
            "VALUES (?,?,?,'COMPLETED',?,?)", (turn_id, thread_id, turn_id, now, now),
        )
        connection.execute(
            "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
            "content_length,message_seq,created_at,completed_at) "
            "VALUES (?,?,?,'user',?,'ready',1,?,?,?,?)",
            (f"live-{turn_id}", thread_id, turn_id, "y" * size, size, seq, now, now),
        )
    return conversation.turn(turn_id, "local-user")


def _events(db, thread_id: str, event_type: str) -> list[dict]:
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT data_json FROM thread_events WHERE thread_id=? AND type=? ORDER BY seq",
            (thread_id, event_type),
        ).fetchall()
    return [json.loads(row["data_json"]) for row in rows]


def _cursor(db, thread_id: str) -> int:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
    return int(row["archived_through_seq"]) if row else 0


def _episodes(db, thread_id: str) -> int:
    with db.connection() as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM memory_episodes WHERE thread_id=?", (thread_id,)
        ).fetchone()[0])


async def _run_case(
    tmp_path: Path, *, label: str, background: bool, turns: int = TURNS,
    turn_bytes: int = TURN_BYTES, live_turn_bytes: int = LIVE_TURN_BYTES,
    keep_tokens: int = KEEP_TOKENS, wait_ms: int = WAIT_MS, poll_ms: int = POLL_MS,
    summarizer_delay: float = 0.0, cancel_after: int | None = None, summarizer=None,
    routed_profile: bool = False,
) -> dict:
    from app.conversation import ManagedTurnWorker, TurnJobCancelled
    from app.memory_archive import ArchiveWaitTimeout

    summarizer = summarizer or _StubSummarizer(summarizer_delay)
    db, conversation, archiver, worker = _build(
        tmp_path / label, keep_tokens=keep_tokens, summarizer=summarizer,
        wait_ms=wait_ms, poll_ms=poll_ms,
        route_model=_StaticRouteModel(_repository_profile()) if routed_profile else None,
    )
    # `max_summary_tokens` is deliberately left at its production default. It is
    # what bounds one batch, so it is also what makes a compaction need more
    # than one slice -- and "the cancel is noticed between slices" is only
    # observable when there is more than one slice to notice it between.
    thread, next_seq = _seed(db, conversation, turns=turns, size=turn_bytes)
    turn_worker = ManagedTurnWorker(conversation)

    if background:
        await worker.start()

    counting: list[float] = []
    hints: list[float] = []
    waits = 0
    timeouts = 0
    cancels: list[float] = []
    cancel_in_wait: list[bool] = []
    states: list[str] = []
    details: list[dict] = []
    resolved_input_limit: int | None = None
    live = 0

    try:
        for index in range(LIVE_TURNS):
            turn_id = f"live-{index}"
            snapshot = _insert_live_turn(
                db, conversation, thread.id, turn_id, next_seq + index * 2, live_turn_bytes,
            )
            live += 1

            if cancel_after is not None and index == cancel_after:
                checks = {"n": 0}

                def cancelling(tid, c=checks):
                    # Let the wait start before cancelling, so the case measures
                    # "stop a wait in progress" rather than "never start one".
                    c["n"] += 1
                    return c["n"] > 1

                turn_worker._cancel_requested = cancelling
            else:
                turn_worker._cancel_requested = lambda tid: False

            cancelled = False
            with _EventTap(conversation.events) as tap:
                started = time.perf_counter()
                try:
                    await turn_worker._archive_history_before_generation(snapshot)
                except ArchiveWaitTimeout:
                    timeouts += 1
                except TurnJobCancelled:
                    cancels.append((time.perf_counter() - started) * 1000)
                    cancelled = True
                marks = tap.drain()

            states.extend(
                data.get("state") for kind, _, data in marks if kind == "context.archiving"
            )
            details.extend(
                {"turn": turn_id, **data} for kind, _, data in marks if kind == "context.archiving"
            )
            hint_times = [at for kind, at, _ in marks if kind == "context.archiving"]
            if hint_times:
                waits += 1
                hints.append((hint_times[0] - started) * 1000)
            if cancelled:
                # The cancel is only evidence if the wait had actually started.
                cancel_in_wait.append(bool(hint_times))
                continue

            # The selector's own cost, on the same turn and the same window the
            # wait used. This is the part R2 added to the answer path.
            window = turn_worker._window_for_turn(snapshot)
            if resolved_input_limit is None:
                resolved_input_limit = window.input_limit
            counting_started = time.perf_counter()
            turn_worker._history(thread.id, turn_id, window)
            counting.append((time.perf_counter() - counting_started) * 1000)
    finally:
        if background:
            await worker.stop()

    context_events = _events(db, thread.id, "context.counted")
    return {
        "case": label,
        "background": background,
        "budget_source": "routed profile" if routed_profile else "unrouted fallback (keep_tokens)",
        "input_limit": resolved_input_limit if resolved_input_limit is not None else archiver.keep_tokens,
        "turns_seeded": turns,
        "turns_run": live,
        "counting_ms": _summary(counting),
        "status_hint_ms": _summary(hints),
        "wait_rate": round(waits / live, 3) if live else 0.0,
        "timeout_rate": round(timeouts / live, 3) if live else 0.0,
        "cancel_ms": _summary(cancels),
        "cancel_landed_in_wait": all(cancel_in_wait) if cancel_in_wait else None,
        "archiving_states": states,
        "archiving_details": details,
        "summarizer_calls": summarizer.calls,
        "episodes": _episodes(db, thread.id),
        "archived_through_seq": _cursor(db, thread.id),
        "counted_events": len(context_events),
    }


async def _restart_case(tmp_path: Path, *, wait_ms: int = 3_000) -> dict:
    """After a timeout, the next turn must still work -- not be permanently stuck.

    The first turn's summariser never returns, so the only thing that can end
    the wait is the deadline. The job is deliberately left ``RUNNING`` under its
    lease (that is what ``asyncio.wait_for`` cancelling the attempt produces),
    and the second turn runs with a working summariser. The lease is shortened
    so the reclaim happens inside the case instead of after the production 30s;
    the reclaim path exercised is the real one in ``claim()``.
    """
    from app.conversation import ManagedTurnWorker
    from app.memory_archive import ArchiveWaitTimeout

    class _Hangs:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, payload):
            self.calls += 1
            await asyncio.sleep(30)

    hangs = _Hangs()
    db, conversation, archiver, worker = _build(
        tmp_path / "restart", keep_tokens=KEEP_TOKENS, summarizer=hangs,
        wait_ms=wait_ms, poll_ms=POLL_MS, lease_seconds=1,
    )
    thread, next_seq = _seed(db, conversation, turns=TURNS, size=TURN_BYTES)
    turn_worker = ManagedTurnWorker(conversation)

    first = _insert_live_turn(
        db, conversation, thread.id, "restart-1", next_seq, LIVE_TURN_BYTES,
    )
    timed_out = False
    started = time.perf_counter()
    try:
        await turn_worker._archive_history_before_generation(first)
    except ArchiveWaitTimeout:
        timed_out = True
    first_ms = (time.perf_counter() - started) * 1000

    # Wait for the abandoned lease to lapse -- the same condition the background
    # worker waits on -- rather than reaching in and resetting the row.
    reclaimed = False
    for _ in range(60):
        with db.connection() as connection:
            row = connection.execute(
                "SELECT status,lease_until FROM memory_archive_jobs ORDER BY created_at DESC,id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            break
        if row["status"] != "RUNNING":
            reclaimed = True
            break
        if row["lease_until"] is not None and row["lease_until"] < _utc_now_iso():
            reclaimed = True
            break
        await asyncio.sleep(0.1)

    # Swap in a working summariser: a timeout must not poison later turns.
    archiver.summarizer = _StubSummarizer()
    second = _insert_live_turn(
        db, conversation, thread.id, "restart-2", next_seq + 2, LIVE_TURN_BYTES,
    )
    second_error = None
    try:
        await turn_worker._archive_history_before_generation(second)
    except Exception as exc:  # noqa: BLE001 -- the case is "does it survive"
        second_error = f"{type(exc).__name__}: {exc}"

    with db.connection() as connection:
        jobs = int(connection.execute(
            "SELECT COUNT(*) FROM memory_archive_jobs WHERE thread_id=?", (thread.id,)
        ).fetchone()[0])
    return {
        "case": "restart",
        "deadline_ms": wait_ms,
        "first_turn_timed_out": timed_out,
        "first_turn_ms": round(first_ms, 2),
        "abandoned_lease_reclaimed": reclaimed,
        "second_turn_error": second_error,
        "second_turn_succeeded": second_error is None,
        "summarizer_calls": hangs.calls,
        "jobs": jobs,
        "episodes": _episodes(db, thread.id),
        "archived_through_seq": _cursor(db, thread.id),
    }


def _verdict(report: dict) -> list[str]:
    problems: list[str] = []
    cases = report["cases"]
    for case in cases:
        counting = case["counting_ms"]
        if counting["count"] and counting["p95"] > TARGET_COUNTING_P95_MS:
            problems.append(
                f"{case['case']}: counting p95 {counting['p95']}ms exceeds the "
                f"{TARGET_COUNTING_P95_MS}ms candidate target"
            )
        hint = case["status_hint_ms"]
        if hint["count"] and hint["max"] > TARGET_STATUS_HINT_MS:
            problems.append(
                f"{case['case']}: status hint max {hint['max']}ms exceeds the "
                f"{TARGET_STATUS_HINT_MS}ms candidate target"
            )
        if case["timeout_rate"] > 0:
            problems.append(
                f"{case['case']}: the bounded wait timed out on the happy path "
                f"({case['timeout_rate']}) -- the deadline is too tight for one pass"
            )
        if case["episodes"] <= 0:
            problems.append(f"{case['case']}: no Episode was committed, so nothing was measured")
        if case["archived_through_seq"] <= 0:
            problems.append(f"{case['case']}: the coverage cursor never advanced")
    if not any(case["background"] for case in cases):
        problems.append("matrix: no background-on case was measured")
    if not any(not case["background"] for case in cases):
        problems.append("matrix: no background-off case was measured")
    if not any(case["wait_rate"] > 0 for case in cases):
        problems.append("matrix: the bounded wait was never exercised")
    cancelled = [case for case in cases if case["cancel_ms"]["count"]]
    if not cancelled:
        problems.append("matrix: cancellation was never exercised")
    elif not all(case["cancel_landed_in_wait"] for case in cancelled):
        problems.append(
            "matrix: a cancel was recorded on a turn that never started waiting, "
            "so it does not demonstrate that a wait can be stopped"
        )
    restart = report["restart"]
    if not restart["first_turn_timed_out"]:
        problems.append("restart: the first turn was expected to time out")
    if restart["second_turn_error"] is not None:
        problems.append(
            f"restart: a timeout left the thread unusable ({restart['second_turn_error']})"
        )
    if restart["archived_through_seq"] <= 0:
        problems.append("restart: the recovered turn archived nothing")
    return problems


async def _main_async(output: Path, live: bool) -> int:
    import tempfile

    report: dict = {
        "batch_id": BATCH_ID,
        "mode": "live" if live else "offline",
        "note": (
            "Offline local-cost measurement. The summariser is a stub and there is no network, "
            "so these are not user-perceived latencies. Only the backend half of status_hint_ms "
            "is measured (context phase start -> the context.archiving event being committed); "
            "the remainder is transport. Each case reports where its H came from: most use the "
            "documented unrouted fallback in ConversationService._hot_window (H = the archiver's "
            "retention), and 'routed-profile-h23348' uses a frozen ModelProfile so the 200ms "
            "candidate target is measured on the budget it was written against."
        ),
        "targets": {
            "counting_p95_ms": TARGET_COUNTING_P95_MS,
            "status_hint_ms": TARGET_STATUS_HINT_MS,
        },
        "cases": [],
    }
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        report["cases"].append(await _run_case(
            root, label="background-on", background=True,
        ))
        report["cases"].append(await _run_case(
            root, label="background-off", background=False,
        ))
        report["cases"].append(await _run_case(
            root, label="background-on-slow-summariser", background=True, summarizer_delay=0.15,
        ))
        # The cancel lands on the *first* live turn on purpose: that turn is the
        # one guaranteed to cross the trigger, because the seeded prefix is
        # oversized by construction. A later index can legitimately fall under
        # the line (the thread was just compacted), and then the case would
        # "cancel" a turn that never waited -- no evidence at all.
        report["cases"].append(await _run_case(
            root, label="background-off-cancel", background=False, cancel_after=0,
        ))
        # The same measurement on the profile the 200ms target was written
        # against, so the claim is not only about the unrouted fallback's
        # smaller H. `keep_tokens` is the archiver's production default here.
        report["cases"].append(await _run_case(
            root, label="routed-profile-h23348", background=False,
            keep_tokens=12_000, routed_profile=True,
        ))
        report["restart"] = await _restart_case(root)

    report["problems"] = _verdict(report)
    report["passed"] = not report["problems"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for case in report["cases"]:
        cancel = ""
        if case["cancel_ms"]["count"]:
            cancel = (
                f"  cancel={case['cancel_ms']['max']}ms"
                f"(in_wait={case['cancel_landed_in_wait']})"
            )
        print(
            f"{case['case']:<32} H={case['input_limit']}  counting p50/p95="
            f"{case['counting_ms']['p50']}/{case['counting_ms']['p95']}ms  "
            f"hint max={case['status_hint_ms']['max']}ms  wait_rate={case['wait_rate']}  "
            f"timeout_rate={case['timeout_rate']}  episodes={case['episodes']}  "
            f"cursor={case['archived_through_seq']}{cancel}"
        )
        print(f"{'':<32} states={case['archiving_states']}  ({case['budget_source']})")
    restart = report["restart"]
    print(
        f"{'restart':<32} timed_out={restart['first_turn_timed_out']} "
        f"({restart['first_turn_ms']}ms)  reclaimed={restart['abandoned_lease_reclaimed']}  "
        f"recovered={restart['second_turn_succeeded']}  cursor={restart['archived_through_seq']}"
    )
    print(f"\npassed={report['passed']}  problems={len(report['problems'])}")
    for problem in report["problems"]:
        print(f"  - {problem}")
    print(f"\nreport: {output}")
    return 0 if report["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default=str(Path(__file__).resolve().parents[2] / "outputs" / "r2-04-wait-acceptance.json"),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="reserved for the live variant; the offline matrix is the default and spends nothing",
    )
    args = parser.parse_args(argv)
    if args.execute and os.getenv("R2_LIVE_APPROVED") != "1":
        print("refusing --execute without R2_LIVE_APPROVED=1", file=sys.stderr)
        return 2
    return asyncio.run(_main_async(Path(args.output), live=args.execute))


if __name__ == "__main__":
    raise SystemExit(main())
