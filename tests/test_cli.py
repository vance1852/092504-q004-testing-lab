import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from skills_workspace.audit import canonical_json, digest
from skills_workspace.clock import FixedClock
from skills_workspace.experiments import ExperimentService
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


def passed(case_id):
    return {"case_id": case_id, "outcome": "passed"}


def failed(case_id, message):
    return {"case_id": case_id, "outcome": "failed",
            "failure": {"type": "AssertionError", "message": message},
            "log_digest": "a" * 64}


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "cli.sqlite3")
        database = Database(self.db_path)
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        svc = ExperimentService(database, clock)
        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="o1", name="教研组")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                            display_name="管理员", role="admin", organization_id="o1")
        base.register_actor(request_id="teacher", actor_id="a1", new_actor_id="t1",
                            display_name="教师", role="teacher", organization_id="o1")
        build_digest = digest({"files": ["m.py"]})
        env_digest = digest({"os": "linux"})
        svc.register_build(request_id="b", actor_id="t1", build_id="build-1",
                           program_name="calc", build_digest=build_digest,
                           manifest={"files": ["m.py"]})
        svc.register_case_package(request_id="p", actor_id="t1", package_id="pkg",
                                  version="1.0.0", package_digest=digest({"v": 1}),
                                  case_ids=["c1", "c2"])
        svc.register_environment(request_id="e", actor_id="t1", environment_id="env",
                                 declaration={"os": "linux"})
        svc.register_experiment(request_id="x", actor_id="t1", experiment_id="exp",
                                build_id="build-1", package_id="pkg",
                                package_version="1.0.0", environment_id="env")
        run = svc.open_run(request_id="open", actor_id="t1", experiment_id="exp",
                           expected_shards=1)
        content = {"build_digest": build_digest, "environment_digest": env_digest,
                   "coverage_digest": "c" * 64,
                   "cases": [passed("c1"), failed("c2", "expected 100 got 90")]}
        svc.upload_shard(request_id="sh", actor_id="t1", run_id=run["run_id"], shard_index=0,
                         content_hash=digest(content), build_digest=build_digest,
                         environment_digest=env_digest, cases=content["cases"],
                         coverage_digest="c" * 64)
        self.run_id = run["run_id"]
        database.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def _run_cli(self, *args) -> dict:
        env = {**os.environ, "PYTHONPATH": "src"}
        completed = subprocess.run(
            [sys.executable, "-m", "skills_workspace.cli", "--database", self.db_path, *args],
            capture_output=True, text=True, env=env, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_replay_run_command(self):
        result = self._run_cli("replay-run", "--run-id", self.run_id)
        self.assertEqual("stable_fail", result["conclusion"])
        self.assertTrue(result["inputs_hash_matches"])
        rules = [item["rule"] for item in result["judgement_evidence"]]
        self.assertIn("first_observation_failure", rules)

    def test_stats_command_is_idempotent(self):
        first = self._run_cli("stats", "--experiment-id", "exp", "--actor-id", "t1",
                              "--request-id", "stat")
        second = self._run_cli("stats", "--experiment-id", "exp", "--actor-id", "t1",
                               "--request-id", "stat")
        self.assertEqual(1, first["frozen_run_count"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

    def test_experiment_command(self):
        result = self._run_cli("experiment", "--experiment-id", "exp")
        self.assertEqual(self.run_id, result["runs"][0]["run_id"])


if __name__ == "__main__":
    unittest.main()
