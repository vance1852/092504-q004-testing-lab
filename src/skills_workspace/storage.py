"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS builds (
    build_id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    build_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(build_digest)
);
CREATE TABLE IF NOT EXISTS case_packages (
    package_id TEXT PRIMARY KEY,
    package_digest TEXT NOT NULL,
    case_count INTEGER NOT NULL CHECK(case_count >= 0),
    case_ids_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(package_digest)
);
CREATE TABLE IF NOT EXISTS environments (
    environment_id TEXT PRIMARY KEY,
    environment_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(environment_digest)
);
CREATE TABLE IF NOT EXISTS experiments (
    run_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    package_id TEXT NOT NULL REFERENCES case_packages(package_id),
    environment_id TEXT NOT NULL REFERENCES environments(environment_id),
    expected_shards INTEGER NOT NULL CHECK(expected_shards >= 1),
    status TEXT NOT NULL CHECK(status IN ('collecting','frozen','superseded')),
    frozen_inputs_digest TEXT,
    verdict TEXT,
    reason_code TEXT,
    decision_json TEXT,
    frozen_at TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    superseded_by_run_id TEXT,
    review_id TEXT
);
CREATE TABLE IF NOT EXISTS run_shards (
    run_id TEXT NOT NULL REFERENCES experiments(run_id),
    shard_index INTEGER NOT NULL CHECK(shard_index >= 0),
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    PRIMARY KEY(run_id, shard_index)
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES experiments(run_id),
    opened_by TEXT NOT NULL REFERENCES actors(actor_id),
    opened_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    note TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','accepted','rerun','expired','cancelled')),
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT,
    rerun_run_id TEXT,
    UNIQUE(run_id)
);
CREATE TABLE IF NOT EXISTS review_supplements (
    review_id TEXT NOT NULL REFERENCES reviews(review_id),
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    content TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY(review_id, submitted_by)
);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
