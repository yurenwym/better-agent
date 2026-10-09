"""Local durable journal for explicit Research evaluation continuations.

A started call without a saved response is ambiguous: never resend it implicitly.
No credentials are stored. The response may contain private research material.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class JudgeCheckpointError(ValueError):
    pass


class JudgeCheckpoint:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, request TEXT NOT NULL, "
                       "identity TEXT NOT NULL, state TEXT NOT NULL, response TEXT, response_digest TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS retired_calls (identity TEXT PRIMARY KEY, key TEXT NOT NULL, "
                       "request TEXT NOT NULL, state TEXT NOT NULL, response TEXT, response_digest TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def encoded(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def begin(self, request, identity):
        encoded = self.encoded(request)
        key = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT request,identity,state,response,response_digest FROM calls WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != encoded:
                    raise JudgeCheckpointError("CHECKPOINT_REQUEST_MISMATCH")
                if row[2] != "RECEIVED":
                    raise JudgeCheckpointError("CHECKPOINT_UNRESOLVED_CALL:" + row[1])
                if hashlib.sha256(row[3].encode()).hexdigest() != row[4]:
                    raise JudgeCheckpointError("CHECKPOINT_RESPONSE_CORRUPT")
                return key, row[1], json.loads(row[3])
            db.execute("INSERT INTO calls(key,request,identity,state) VALUES (?,?,?,'STARTED')", (key, encoded, identity))
        return key, identity, None

    def received(self, key, identity, response):
        encoded = self.encoded(response)
        with self.connect() as db:
            changed = db.execute("UPDATE calls SET state='RECEIVED',response=?,response_digest=? "
                "WHERE key=? AND identity=? AND state='STARTED'",
                (encoded, hashlib.sha256(encoded.encode()).hexdigest(), key, identity)).rowcount
            if changed != 1:
                raise JudgeCheckpointError("CHECKPOINT_STATE_CONFLICT")

    def copy_to(self, target):
        # SQLite backup preserves committed rows even after an abrupt exit.
        with self.connect() as source:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()

    def retry_failed(self, identity, control_db, owner):
        """Explicit operator retry only after authoritative failure settlement."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT key,request,state,response,response_digest FROM calls WHERE identity=?", (identity,)).fetchone()
            if row is None or row[2] != "STARTED":
                raise JudgeCheckpointError("CHECKPOINT_NOT_UNRESOLVED")
            if json.loads(row[1])["owner"] != owner:
                raise JudgeCheckpointError("CHECKPOINT_OWNER_MISMATCH")
            with control_db.connection() as ledger:
                invocation = ledger.execute("SELECT status FROM model_invocations WHERE id=? AND owner_id=?", (identity, owner)).fetchone()
                attempts = ledger.execute("SELECT status,cost_microusd FROM model_attempts WHERE invocation_id=?", (identity,)).fetchall()
            if invocation is None or invocation[0] != "FAILED" or not attempts or any(
                    item[0] != "FAILED" or item[1] is None for item in attempts):
                raise JudgeCheckpointError("CHECKPOINT_FAILURE_NOT_SETTLED")
            db.execute("INSERT INTO retired_calls(identity,key,request,state,response,response_digest) VALUES (?,?,?,?,?,?)", (identity, *row))
            db.execute("DELETE FROM calls WHERE key=?", (row[0],))

    def retry_invalid_case(self, case_id):
        """Explicit retry of the last saved invalid response; keep prior responses."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT identity,key,request,state,response,response_digest FROM calls ORDER BY rowid DESC").fetchall()
            row = next((r for r in rows if json.loads(r[2])["case_id"] == case_id), None)
            if row is None or row[3] != "RECEIVED":
                raise JudgeCheckpointError("NO_SAVED_INVALID_RESPONSE")
            db.execute("INSERT INTO retired_calls VALUES (?,?,?,?,?,?)", row)
            db.execute("DELETE FROM calls WHERE key=?", (row[1],))
