"""SQLite tracker so the agent never applies to the same job twice and you can see status."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import FitAssessment, JobPosting

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    posting TEXT NOT NULL,
    company TEXT, title TEXT, location TEXT, url TEXT,
    score INTEGER, recommendation TEXT, assessment TEXT,
    status TEXT NOT NULL DEFAULT 'found',   -- found|scored|skipped|tailored|filled|applied|manual|failed
    resume_path TEXT, cover_letter_path TEXT,
    notes TEXT,
    created_at TEXT, updated_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tracker:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def has(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM jobs WHERE key=?", (key,)).fetchone() is not None

    def add(self, job: JobPosting) -> bool:
        if self.has(job.key):
            return False
        self.conn.execute(
            "INSERT INTO jobs(key,posting,company,title,location,url,status,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'found',?,?)",
            (job.key, job.model_dump_json(), job.company, job.title, job.location, job.url, _now(), _now()),
        )
        self.conn.commit()
        return True

    def set_assessment(self, key: str, a: FitAssessment, status: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET score=?, recommendation=?, assessment=?, status=?, updated_at=? WHERE key=?",
            (a.score, a.recommendation, a.model_dump_json(), status, _now(), key),
        )
        self.conn.commit()

    def update(self, key: str, **fields) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE key=?", (*fields.values(), key))
        self.conn.commit()

    def get(self, key: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()

    def posting(self, key: str) -> JobPosting:
        return JobPosting.model_validate_json(self.get(key)["posting"])

    def assessment(self, key: str) -> Optional[FitAssessment]:
        row = self.get(key)
        return FitAssessment.model_validate_json(row["assessment"]) if row and row["assessment"] else None

    def by_status(self, *statuses: str, min_score: int = 0) -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({q}) AND COALESCE(score,0) >= ? ORDER BY score DESC",
            (*statuses, min_score),
        ).fetchall()

    def all(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs ORDER BY COALESCE(score,-1) DESC, created_at DESC").fetchall()

    def export_json(self) -> str:
        return json.dumps([dict(r) for r in self.all()], indent=2)
