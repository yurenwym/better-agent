import sqlite3


def test_goal_program_migration_is_repeatable_for_new_and_existing_databases(tmp_path) -> None:
    from app.db import Database

    path = tmp_path / "agent.db"
    Database(path)
    Database(path)
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert {"goal_programs", "goal_program_versions", "goal_actions", "goal_program_events", "goal_adjustment_proposals", "goal_daily_reviews", "goal_review_action_snapshots"} <= tables
    assert versions[-1] == (4,)
