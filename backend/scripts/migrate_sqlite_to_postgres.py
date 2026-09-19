from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


EXCLUDED_TABLES = {
    "alembic_version",
    "embedding_profiles",
    "embedding_jobs",
    "memory_embeddings",
    "memory_fts",
    "schema_migrations",
    "sqlite_sequence",
}


@dataclass(frozen=True)
class TableManifest:
    count: int
    sha256: str


def _manifest_payload(manifest: dict[str, TableManifest]) -> dict[str, dict[str, Any]]:
    return {
        table: {"count": item.count, "sha256": item.sha256}
        for table, item in manifest.items()
    }


def _digest(rows: list[dict[str, Any]], column_types: dict[str, str]) -> str:
    normalized = [
        {
            column: _normalize_value(value, column_types.get(column, ""))
            for column, value in row.items()
        }
        for row in rows
    ]
    encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_value(value: Any, target_type: str) -> Any:
    if value is None or target_type != "timestamp with time zone":
        return value
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.isoformat()


def _source_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY rowid"
        )
        if row[0] not in EXCLUDED_TABLES and not row[0].startswith("memory_fts_")
    ]


def _validate_source_foreign_keys(connection: sqlite3.Connection) -> None:
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if not violations:
        return
    details = "; ".join(
        f"table={row[0]}, rowid={row[1]}, referenced_table={row[2]}, fk_index={row[3]}"
        for row in violations
    )
    raise RuntimeError(f"foreign key orphan detected in SQLite source: {details}")


def _target_columns(connection, table: str) -> list[str]:
    return [
        row["column_name"]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s ORDER BY ordinal_position",
            (table,),
        )
    ]


def _target_column_types(connection, table: str) -> dict[str, str]:
    return {
        row["column_name"]: row["data_type"]
        for row in connection.execute(
            "SELECT column_name,data_type FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        )
    }


def _ordered_tables(connection, tables: list[str]) -> list[str]:
    dependencies = {table: set() for table in tables}
    rows = connection.execute(
        """
        SELECT tc.table_name,ccu.table_name referenced_table
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_schema=tc.constraint_schema AND ccu.constraint_name=tc.constraint_name
        WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema=current_schema()
        """
    ).fetchall()
    for row in rows:
        table, parent = row["table_name"], row["referenced_table"]
        if table in dependencies and parent in dependencies and parent != table:
            dependencies[table].add(parent)
    ordered: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(table for table in remaining if not (dependencies[table] & remaining))
        if not ready:
            raise RuntimeError(f"cyclic foreign keys require manual migration: {sorted(remaining)}")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _rebuild_memory_indexes(connection) -> None:
    memories = connection.execute(
        "SELECT e.id entry_id,e.owner_id,r.id revision_id,r.content,r.content_hash "
        "FROM memory_entries e JOIN memory_revisions r ON r.id=e.current_revision_id "
        "WHERE e.status='ACTIVE'"
    ).fetchall()
    if not memories:
        return
    connection.cursor().executemany(
        "INSERT INTO memory_fts(entry_id,owner_id,content) VALUES (%s,%s,%s)",
        [(row["entry_id"], row["owner_id"], row["content"]) for row in memories],
    )
    profile = connection.execute(
        "SELECT id FROM embedding_profiles WHERE active=true"
    ).fetchone()
    if profile is None:
        raise RuntimeError("active embedding profile is required for memory backfill")
    connection.cursor().executemany(
        "INSERT INTO embedding_jobs(revision_id,profile_id,content_hash,status) "
        "VALUES (%s,%s,%s,'QUEUED')",
        [
            (row["revision_id"], profile["id"], row["content_hash"])
            for row in memories
        ],
    )


def _advance_identity_sequences(connection, tables: list[str]) -> None:
    identities = connection.execute(
        "SELECT table_name,column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND identity_generation IS NOT NULL "
        "AND table_name=ANY(%s)",
        (tables,),
    ).fetchall()
    for identity in identities:
        table = identity["table_name"]
        column = identity["column_name"]
        maximum = connection.execute(
            sql.SQL("SELECT MAX({}) maximum FROM {}").format(
                sql.Identifier(column), sql.Identifier(table)
            )
        ).fetchone()["maximum"]
        if maximum is None:
            continue
        connection.execute(
            "SELECT setval(pg_get_serial_sequence(%s,%s),%s,true)",
            (table, column, maximum),
        )


def _record_completion(connection, manifest: dict[str, TableManifest]) -> None:
    payload = _manifest_payload(manifest)
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    connection.execute(
        "INSERT INTO sqlite_import_completions(id,source_manifest_sha256,manifest_json) "
        "VALUES (1,%s,%s::jsonb)",
        (hashlib.sha256(encoded.encode("utf-8")).hexdigest(), encoded),
    )


def migrate(sqlite_path: Path, database_url: str) -> dict[str, TableManifest]:
    if not sqlite_path.is_file():
        raise FileNotFoundError(sqlite_path)
    source = sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        _validate_source_foreign_keys(source)
        with psycopg.connect(database_url, row_factory=dict_row) as target:
            completed = target.execute(
                "SELECT 1 FROM sqlite_import_completions WHERE id=1"
            ).fetchone()
            if completed is not None:
                raise RuntimeError(
                    "target must be empty before import: completed SQLite import exists"
                )
            tables = _source_tables(source)
            available = {
                row["table_name"]
                for row in target.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
                )
            }
            missing = sorted(set(tables) - available)
            if missing:
                raise RuntimeError(f"target schema is missing tables: {missing}")
            missing_columns: dict[str, list[str]] = {}
            for table in tables:
                source_columns = {
                    row[1] for row in source.execute(f'PRAGMA table_info("{table}")')
                }
                absent = sorted(source_columns - set(_target_columns(target, table)))
                if absent:
                    missing_columns[table] = absent
            if missing_columns:
                raise RuntimeError(
                    f"target schema is missing source columns: {missing_columns}"
                )
            nonempty = []
            for table in tables:
                count = target.execute(
                    sql.SQL("SELECT COUNT(*) count FROM {}").format(sql.Identifier(table))
                ).fetchone()["count"]
                if count and table != "app_settings":
                    nonempty.append(table)
            if nonempty:
                raise RuntimeError(f"target must be empty before import: {sorted(nonempty)}")

            manifest: dict[str, TableManifest] = {}
            for table in _ordered_tables(target, tables):
                source_columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
                column_types = _target_column_types(target, table)
                columns = [column for column in source_columns if column in column_types]
                order = ",".join(f'"{column}"' for column in columns)
                rows = [dict(row) for row in source.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}')]
                if table == "app_settings":
                    target.execute("DELETE FROM app_settings")
                if rows:
                    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns),
                    )
                    target.cursor().executemany(
                        statement,
                        [tuple(row[column] for column in columns) for row in rows],
                    )
                manifest[table] = TableManifest(len(rows), _digest(rows, column_types))

            for table, expected in manifest.items():
                column_types = _target_column_types(target, table)
                columns = [column for column in column_types if column in {
                    row[1] for row in source.execute(f'PRAGMA table_info("{table}")')
                }]
                order = ",".join(f'"{column}"' for column in columns)
                actual_rows = [dict(row) for row in target.execute(
                    sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                    )
                ).fetchall()]
                actual = TableManifest(len(actual_rows), _digest(actual_rows, column_types))
                if actual != expected:
                    raise RuntimeError(f"manifest mismatch for {table}")
            _advance_identity_sequences(target, tables)
            _rebuild_memory_indexes(target)
            _record_completion(target, manifest)
            return manifest
    finally:
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time stopped-write SQLite to PostgreSQL import")
    parser.add_argument("sqlite_path", type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = migrate(args.sqlite_path, args.database_url)
    args.manifest.write_text(
        json.dumps({key: value.__dict__ for key, value in manifest.items()}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
