import pytest
from app.research_judge_checkpoint import JudgeCheckpoint, JudgeCheckpointError


def test_committed_response_survives_restart_and_copy(tmp_path):
    path = tmp_path / "journal.db"
    journal = JudgeCheckpoint(path)
    key, identity, result = journal.begin({"answer": "A", "profile": "v1"}, "call1")
    assert result is None
    journal.received(key, identity, {"message": "{}", "finish_reason": "stop"})
    journal.copy_to(tmp_path / "continued.db")
    resumed = JudgeCheckpoint(tmp_path / "continued.db")
    assert resumed.begin({"answer": "A", "profile": "v1"}, "new-call")[1:] == ("call1", {"message": "{}", "finish_reason": "stop"})
    assert resumed.begin({"answer": "A", "profile": "v2"}, "changed")[2] is None


def test_unknown_send_never_automatically_repeats(tmp_path):
    path = tmp_path / "journal.db"
    JudgeCheckpoint(path).begin({"answer": "A"}, "call1")
    with pytest.raises(JudgeCheckpointError, match="UNRESOLVED_CALL"):
        JudgeCheckpoint(path).begin({"answer": "A"}, "call2")


def test_corrupt_response_is_not_reused(tmp_path):
    journal = JudgeCheckpoint(tmp_path / "journal.db")
    key, identity, _ = journal.begin({}, "call")
    journal.received(key, identity, {"message": "{}"})
    with journal.connect() as db:
        db.execute("UPDATE calls SET response='bad'")
    with pytest.raises(JudgeCheckpointError, match="CORRUPT"):
        journal.begin({}, "new")


def test_explicit_invalid_case_retry_preserves_other_saved_response(tmp_path):
    journal = JudgeCheckpoint(tmp_path / "journal.db")
    for slot in ("left", "right"):
        key, identity, _ = journal.begin({"case_id": "c", "slot": slot}, slot)
        journal.received(key, identity, {"message": slot})
    journal.retry_invalid_case("c")
    assert journal.begin({"case_id": "c", "slot": "left"}, "new")[2] == {"message": "left"}
    assert journal.begin({"case_id": "c", "slot": "right"}, "retry")[2] is None
    with journal.connect() as db:
        assert db.execute("SELECT identity FROM retired_calls").fetchone()[0] == "right"


@pytest.mark.parametrize("status,cost,allowed", [("STARTED", None, False), ("SUCCEEDED", 2, False), ("FAILED", None, False), ("FAILED", 2, True)])
def test_only_explicit_settled_failure_can_be_retried(tmp_path, status, cost, allowed):
    import sqlite3
    from contextlib import contextmanager
    class Ledger:
        @contextmanager
        def connection(self):
            with sqlite3.connect(tmp_path / "ledger.db") as db:
                yield db
    ledger = Ledger()
    with ledger.connection() as db:
        db.execute("CREATE TABLE model_invocations(id,owner_id,status)")
        db.execute("CREATE TABLE model_attempts(invocation_id,status,cost_microusd)")
        db.execute("INSERT INTO model_invocations VALUES ('call','owner',?)", (status,))
        db.execute("INSERT INTO model_attempts VALUES ('call',?,?)", (status, cost))
    journal = JudgeCheckpoint(tmp_path / "journal.db")
    request = {"owner": "owner", "answer": "A"}
    journal.begin(request, "call")
    if not allowed:
        with pytest.raises(JudgeCheckpointError, match="NOT_SETTLED"):
            journal.retry_failed("call", ledger, "owner")
    else:
        journal.retry_failed("call", ledger, "owner")
        assert journal.begin(request, "replacement")[1:] == ("replacement", None)
        with journal.connect() as db:
            assert db.execute("SELECT identity FROM retired_calls").fetchone()[0] == "call"
