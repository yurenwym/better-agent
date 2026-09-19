import pytest

from app.db import Database
from app.memory_v2 import MemoryContextProvider, MemoryContextRequest, MemoryStore, MemoryConflict


def test_lexical_sql_cap_does_not_hide_python_fallback(tmp_path):
    db = Database(tmp_path / "agent.db")
    with db.transaction() as c:
        c.execute("INSERT INTO threads(id,title,owner_id,created_at,updated_at) VALUES ('t','t','u','now','now')")
    store = MemoryStore(db, tmp_path / "memory")
    for i in range(8):
        store.remember("u", "fact", "user", "", f"PostgreSQL maintenance service {i}", f"m{i}")
    request = MemoryContextRequest("u", "t", None, "PostgreSQL", model_invocation_id="pin")
    provider = MemoryContextProvider(db, lexical_candidate_limit=3)
    selected = provider.select(request)
    assert selected.trace["lexical_raw_candidate_count"] == 3
    assert selected.trace["python_fallback_candidate_count"] == 5
    assert len(selected.revision_ids) == 8
    assert provider.select(request).revision_ids == selected.revision_ids
    for kwargs in ({"lexical_candidate_limit": 4}, {"lexical_candidate_limit": 3, "semantic_candidate_limit": 20}):
        with pytest.raises(MemoryConflict):
            MemoryContextProvider(db, **kwargs).select(request)
    scope = provider.scopes.resolve_scope("t", "u")
    binding = MemoryContextProvider(db)._binding(request, scope)
    assert "semantic_candidate_limit" not in binding and "lexical_candidate_limit" not in binding


@pytest.mark.parametrize("value", [0, -1, 201, 2.5, True, "20"])
def test_invalid_candidate_limits(tmp_path, value):
    db = Database(tmp_path / "agent.db")
    for name in ("semantic_candidate_limit", "lexical_candidate_limit"):
        with pytest.raises(ValueError, match="candidate limit"):
            MemoryContextProvider(db, **{name: value})
