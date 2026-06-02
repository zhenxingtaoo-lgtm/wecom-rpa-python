from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import TargetGroup, TargetStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS targets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              group_name TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL DEFAULT 'pending',
              batch_no INTEGER,
              retry_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT,
              finished_at TEXT,
              config_json TEXT,
              status TEXT
            );
            """
        )
        self.conn.commit()

    def upsert_targets(self, groups: list[TargetGroup]) -> None:
        now = utc_now()
        self.conn.executemany(
            """
            INSERT INTO targets(group_name, status, updated_at)
            VALUES (?, 'pending', ?)
            ON CONFLICT(group_name) DO NOTHING
            """,
            [(g.group_name, now) for g in groups],
        )
        self.conn.commit()

    def start_run(self, config: Any) -> int:
        def default(obj: Any) -> Any:
            if is_dataclass(obj):
                return asdict(obj)
            raise TypeError(type(obj).__name__)

        cur = self.conn.execute(
            "INSERT INTO runs(started_at, config_json, status) VALUES (?, ?, 'running')",
            (utc_now(), json.dumps(config, ensure_ascii=False, default=default)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, status = ? WHERE id = ?",
            (utc_now(), status, run_id),
        )
        self.conn.commit()

    def get_status(self, group_name: str) -> str | None:
        row = self.conn.execute("SELECT status FROM targets WHERE group_name = ?", (group_name,)).fetchone()
        return None if row is None else str(row["status"])

    def get_statuses(self, group_names: list[str]) -> dict[str, str | None]:
        return {name: self.get_status(name) for name in group_names}

    def set_status(self, group_name: str, status: TargetStatus | str, *, batch_no: int | None = None, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE targets
            SET status = ?, batch_no = COALESCE(?, batch_no), last_error = ?, updated_at = ?
            WHERE group_name = ?
            """,
            (str(status), batch_no, error, utc_now(), group_name),
        )
        self.conn.commit()

    def increment_retry(self, group_name: str, error: str) -> int:
        self.conn.execute(
            """
            UPDATE targets
            SET retry_count = retry_count + 1, last_error = ?, updated_at = ?
            WHERE group_name = ?
            """,
            (error, utc_now(), group_name),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT retry_count FROM targets WHERE group_name = ?", (group_name,)).fetchone()
        return int(row["retry_count"])

    def summary(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM targets GROUP BY status").fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}
