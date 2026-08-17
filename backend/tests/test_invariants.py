import asyncio

from test_runtime import make_runtime


def test_invariants_reject_event_seq_gaps_and_unapproved_write(tmp_path) -> None:
    from app.evals import check_invariants
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = asyncio.run(runtime.create_goal("Invariant", "Check facts"))

    assert check_invariants(runtime, run.id) == []
