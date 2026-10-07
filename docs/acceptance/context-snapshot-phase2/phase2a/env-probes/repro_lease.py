"""Repro for test_active_turn_lease_is_renewed_during_long_model_call.

Runs the same steps with per-step timeouts and dumps every asyncio task stack
when a step stalls, so the exact await point is visible.
"""

import asyncio
import pathlib
import sys
import tempfile
import traceback

BACKEND = r"D:\RAG\better\backend"
sys.path.insert(0, BACKEND)
sys.path.insert(0, BACKEND + r"\tests")

from test_runtime import make_runtime  # noqa: E402
from app.conversation import ManagedTurnWorker  # noqa: E402


class LongRunningConversationModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def route_and_respond(self, *, on_text_delta, **kwargs):
        self.calls += 1
        print(f"   [model] call #{self.calls} entered", flush=True)
        self.started.set()
        await self.release.wait()
        print("   [model] released", flush=True)
        on_text_delta(
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\nlong-running answer'
        )
        return None


def dump_tasks(label: str) -> None:
    print(f"\n=== TASK DUMP: {label} ===", flush=True)
    for task in asyncio.all_tasks():
        print(f"--- task {task.get_name()} done={task.done()}", flush=True)
        stack = task.get_stack()
        if not stack:
            print("    (no python frames)", flush=True)
        for frame in stack:
            print(
                f"    {frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}",
                flush=True,
            )
    print("=== END DUMP ===\n", flush=True)


async def step(label, coro, timeout=15.0):
    print(f"-> {label}", flush=True)
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        print(f"   ok: {result!r}", flush=True)
        return result
    except asyncio.TimeoutError:
        print(f"   HANG after {timeout}s at: {label}", flush=True)
        dump_tasks(label)
        raise SystemExit(2)


CONV_FILE = BACKEND + r"\app\conversation.py"
TRACE: list[int] = []


def _tracer(frame, event, arg):
    if frame.f_code.co_filename == CONV_FILE:
        if event == "line":
            TRACE.append(frame.f_lineno)
        return _tracer
    return None


async def main() -> None:
    sys.settrace(_tracer)
    tmp = pathlib.Path(tempfile.mkdtemp(dir="C:/tmp", prefix="repro-lease-"))
    print(f"tmp={tmp}", flush=True)
    model = LongRunningConversationModel()
    runtime = make_runtime(tmp, model)
    worker_one = ManagedTurnWorker(
        runtime.conversation, owner="worker-one", lease_seconds=0.2, poll_interval=0.005
    )
    worker_two = ManagedTurnWorker(
        runtime.conversation, owner="worker-two", lease_seconds=0.2, poll_interval=0.005
    )
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-long", "Long call", [])
    print(f"accepted turn={accepted.turn_id}", flush=True)

    first_task = asyncio.create_task(worker_one.run_once())
    started_wait = asyncio.ensure_future(model.started.wait())
    done, _pending = await asyncio.wait(
        [first_task, started_wait], timeout=15, return_when=asyncio.FIRST_COMPLETED
    )
    if first_task in done:
        print("!! first_task finished before the model was called", flush=True)
        exc = first_task.exception()
        if exc is not None:
            print("!! worker_one.run_once() raised:", flush=True)
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        else:
            print(f"!! run_once() returned {first_task.result()!r}", flush=True)
        with runtime.db.connection() as connection:
            print("-- turns --", flush=True)
            for row in connection.execute(
                "SELECT id,status,policy,reason_code FROM turns"
            ).fetchall():
                print("   ", dict(row), flush=True)
            print("-- turn_jobs --", flush=True)
            for row in connection.execute(
                "SELECT turn_id,status,attempts,lease_owner FROM turn_jobs"
            ).fetchall():
                print("   ", dict(row), flush=True)
        print("-- events --", flush=True)
        for event in runtime.conversation.events.list(thread.id):
            print(f"    {event.type}", flush=True)
        sys.settrace(None)
        print("-- last 80 conversation.py lines executed --", flush=True)
        for line in TRACE[-80:]:
            print(f"    {line}", flush=True)
        raise SystemExit(4)
    if started_wait not in done:
        print("   HANG: neither the model nor the worker task progressed", flush=True)
        dump_tasks("model.started.wait()")
        raise SystemExit(2)
    print("   ok: model started", flush=True)
    await step("asyncio.sleep(0.45)", asyncio.sleep(0.45))

    print("-> worker_two.claim_next()", flush=True)
    claimed = worker_two.claim_next()
    print(f"   ok: {claimed!r}", flush=True)

    print("-> read turn_jobs", flush=True)
    with runtime.db.connection() as connection:
        job = connection.execute(
            "SELECT lease_owner, lease_until FROM turn_jobs WHERE turn_id = ?",
            (accepted.turn_id,),
        ).fetchone()
    print(f"   ok: owner={job['lease_owner']!r} until={job['lease_until']!r}", flush=True)

    model.release.set()
    print("-> await first_task", flush=True)
    try:
        done = await asyncio.wait_for(first_task, timeout=20)
        print(f"   ok: {done!r} calls={model.calls}", flush=True)
    except asyncio.TimeoutError:
        print("   HANG awaiting first_task", flush=True)
        dump_tasks("first_task")
        raise SystemExit(3)

    print("ALL STEPS OK", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        raise SystemExit(9)
