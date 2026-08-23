from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .db import Database


@dataclass(frozen=True)
class AppSettings:
    human_mode: bool
    updated_at: str


class SettingsService:
    def __init__(self, db: Database) -> None: self.db = db

    def get(self) -> AppSettings:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM app_settings WHERE id=1").fetchone()
        return AppSettings(bool(row["human_mode"]), row["updated_at"])

    def set_human_mode(self, enabled: bool) -> AppSettings:
        if type(enabled) is not bool: raise ValueError("enabled must be boolean")
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as connection:
            connection.execute("UPDATE app_settings SET human_mode=?,updated_at=? WHERE id=1", (int(enabled), now))
        return self.get()
