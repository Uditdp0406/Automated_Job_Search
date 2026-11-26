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
-- Company boards the watcher polls (from config + web discovery).
CREATE TABLE IF NOT EXISTS boards (
    source TEXT NOT NULL, slug TEXT NOT NULL,
    origin TEXT,                 -- config | discovered
    added_at TEXT, baselined_at TEXT, last_polled_at TEXT,
    last_error TEXT, disabled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, slug)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- Every posting key the watcher has ever seen, relevant or not (so old jobs never look "new").
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, first_seen TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tracker:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "applied_at" not in cols:  # migrate databases created by older versions
            self.conn.execute("ALTER TABLE jobs ADD COLUMN applied_at TEXT")
            self.conn.commit()

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
        if fields.get("status") == "applied":
            fields.setdefault("applied_at", fields["updated_at"])
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

    # ------------------------------------------------------------ watcher state --
    def add_board(self, source: str, slug: str, origin: str) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO boards(source,slug,origin,added_at) VALUES(?,?,?,?)",
                                (source, slug.lower(), origin, _now()))
        self.conn.commit()
        return cur.rowcount > 0

    def boards(self, include_disabled: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM boards" + ("" if include_disabled else " WHERE disabled=0") + " ORDER BY source, slug"
        return self.conn.execute(q).fetchall()

    def board_update(self, source: str, slug: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE boards SET {cols} WHERE source=? AND slug=?", (*fields.values(), source, slug))
        self.conn.commit()

    def meta_get(self, k: str) -> Optional[str]:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def meta_set(self, k: str, v: str) -> None:
        self.conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.conn.commit()

    def applied_since(self, iso_ts: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs WHERE status='applied' AND applied_at >= ?",
                                 (iso_ts,)).fetchone()[0]

    def add_seen(self, job: JobPosting, status: str) -> None:
        """Record a posting as already seen (e.g. it existed before watching started) without scoring it."""
        if self.add(job):
            self.update(job.key, status=status)

    def unseen(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        have = set()
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = ",".join("?" * len(chunk))
            have |= {r[0] for r in self.conn.execute(f"SELECT key FROM seen WHERE key IN ({q})", chunk)}
        return set(keys) - have

    def mark_seen(self, keys: list[str]) -> None:
        now = _now()
        self.conn.executemany("INSERT OR IGNORE INTO seen(key, first_seen) VALUES(?,?)", [(k, now) for k in keys])
        self.conn.commit()
