from app.db import _postgres_sql


def test_qmark_translation_preserves_literals_and_token_boundaries() -> None:
    assert _postgres_sql("SELECT '?' value WHERE id=?ORDER BY id") == (
        "SELECT '?' value WHERE id=%s ORDER BY id"
    )


def test_qmark_translation_preserves_postgres_dollar_quotes_and_json_operators() -> None:
    assert _postgres_sql(
        "SELECT $$body ? untouched$$, payload ? 'key', payload ?| array['a'], "
        "payload ?& array['b'] FROM records WHERE id=?"
    ) == (
        "SELECT $$body ? untouched$$, payload ? 'key', payload ?| array['a'], "
        "payload ?& array['b'] FROM records WHERE id=%s"
    )


def test_qmark_translation_preserves_tagged_dollar_quotes() -> None:
    assert _postgres_sql("SELECT $fn$begin ?; end$fn$ WHERE id=?") == (
        "SELECT $fn$begin ?; end$fn$ WHERE id=%s"
    )
