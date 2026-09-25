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
    program_name TEXT NOT NULL,
    digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(program_name, digest)
);
CREATE TABLE IF NOT EXISTS case_packages (
    package_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(package_id, version)
);
CREATE TABLE IF NOT EXISTS environments (
    environment_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    declaration_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    package_id TEXT NOT NULL,
    package_version TEXT NOT NULL,
    environment_id TEXT NOT NULL REFERENCES environments(environment_id),
    created_at TEXT NOT NULL,
    UNIQUE(build_id, package_id, package_version, environment_id),
    FOREIGN KEY(package_id, package_version) REFERENCES case_packages(package_id, version)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    attempt INTEGER NOT NULL CHECK(attempt >= 1),
    expected_shards INTEGER NOT NULL CHECK(expected_shards >= 1),
    status TEXT NOT NULL CHECK(status IN ('collecting', 'frozen')),
    conclusion TEXT CHECK(conclusion IN ('pass', 'stable_fail', 'flaky', 'invalid')),
    invalid_reasons_json TEXT NOT NULL DEFAULT '[]',
    inputs_hash TEXT,
    opened_by TEXT NOT NULL REFERENCES actors(actor_id),
    opened_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE(experiment_id, attempt)
);
CREATE TABLE IF NOT EXISTS shards (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    shard_index INTEGER NOT NULL CHECK(shard_index >= 0),
    content_hash TEXT NOT NULL,
    build_digest TEXT NOT NULL,
    environment_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    uploaded_by TEXT NOT NULL REFERENCES actors(actor_id),
    received_at TEXT NOT NULL,
    PRIMARY KEY(run_id, shard_index)
);
CREATE TABLE IF NOT EXISTS frozen_cases (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    case_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('passed', 'failed', 'errored')),
    failure_signature TEXT,
    log_digest TEXT,
    coverage_digest TEXT,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(run_id, case_id)
);
CREATE TABLE IF NOT EXISTS signature_occurrences (
    signature TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    attempt INTEGER NOT NULL,
    case_id TEXT NOT NULL,
    failure_type TEXT NOT NULL,
    PRIMARY KEY(run_id, case_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    student_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    opened_by TEXT NOT NULL REFERENCES actors(actor_id),
    opened_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'decided')),
    explanation TEXT,
    explanation_at TEXT,
    decision TEXT CHECK(decision IN ('accept', 'rerun')),
    decided_by TEXT REFERENCES actors(actor_id),
    decided_at TEXT,
    rerun_run_id TEXT REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS stats_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
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
