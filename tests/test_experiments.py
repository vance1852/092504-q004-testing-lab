import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills_workspace.audit import canonical_json, digest
from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.experiments import ExperimentService, normalize_signature
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


def sha(value) -> str:
    return digest(value)


def passed(case_id: str) -> dict:
    return {"case_id": case_id, "outcome": "passed"}


def failed(case_id: str, failure_type: str, message: str,
           log_digest: str | None = None) -> dict:
    return {"case_id": case_id, "outcome": "failed",
            "failure": {"type": failure_type, "message": message},
            "log_digest": log_digest or ("a" * 64)}


class ExperimentFixture:
    def __init__(self, database: Database, clock=None):
        self.base = DomainService(database, clock)
        self.svc = ExperimentService(database, clock or self.base.clock)
        self.db = database
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="测试教研组")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="teacher", actor_id="a1", new_actor_id="t1",
                                 display_name="教师", role="teacher", organization_id="o1")
        self.base.register_actor(request_id="student", actor_id="a1", new_actor_id="s1",
                                 display_name="学生", role="student", organization_id="o1")
        self.base.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="r1",
                                 display_name="复核员", role="reviewer", organization_id="o1")
        connection = database.connection
        self.build_digest = sha({"files": ["main.py"]})
        self.env_declaration = {"os": "linux", "python": "3.11", "image": "runner:2.0"}
        self.env_digest = sha(self.env_declaration)
        self.package_digest = sha({"v": 1})
        self.svc.register_build(request_id="b", actor_id="t1", build_id="build-1",
                                program_name="tax-calculator",
                                build_digest=self.build_digest,
                                manifest={"files": ["main.py"]})
        self.svc.register_case_package(request_id="p", actor_id="t1", package_id="pkg-1",
                                       version="1.0.0", package_digest=self.package_digest,
                                       case_ids=["c1", "c2"])
        self.svc.register_environment(request_id="e", actor_id="t1", environment_id="env-1",
                                      declaration=self.env_declaration)
        self.svc.register_experiment(request_id="x", actor_id="t1", experiment_id="exp-1",
                                     build_id="build-1", package_id="pkg-1",
                                     package_version="1.0.0", environment_id="env-1")

    def shard(self, run_id: str, index: int, cases: list[dict], coverage_digest: str,
              request_id: str, actor_id: str = "t1",
              build_digest: str | None = None, env_digest: str | None = None,
              content_hash: str | None = None):
        build_digest = build_digest or self.build_digest
        env_digest = env_digest or self.env_digest
        content = {"build_digest": build_digest, "environment_digest": env_digest,
                   "coverage_digest": coverage_digest, "cases": cases}
        return self.svc.upload_shard(
            request_id=request_id, actor_id=actor_id, run_id=run_id, shard_index=index,
            content_hash=content_hash or sha(content), build_digest=build_digest,
            environment_digest=env_digest, cases=cases, coverage_digest=coverage_digest)

    def open_run(self, expected_shards: int = 1, request_id: str = "open") -> dict:
        return self.svc.open_run(request_id=request_id, actor_id="t1",
                                 experiment_id="exp-1", expected_shards=expected_shards)


class ExperimentServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.fx = ExperimentFixture(self.database, self.clock)

    def tearDown(self):
        self.database.close()

    def test_build_is_immutable(self):
        with self.assertRaises(ConflictError):
            self.fx.svc.register_build(request_id="b2", actor_id="t1", build_id="build-1",
                                       program_name="tax-calculator",
                                       build_digest="f" * 64, manifest={"files": ["other.py"]})

    def test_environment_digest_is_self_attesting(self):
        row = self.database.connection.execute(
            "SELECT digest FROM environments WHERE environment_id='env-1'").fetchone()
        self.assertEqual(self.fx.env_digest, row["digest"])

    def test_shard_content_hash_mismatch_rejected(self):
        run = self.fx.open_run()
        with self.assertRaises(ValidationError):
            self.fx.shard(run["run_id"], 0, [passed("c1"), passed("c2")], "c" * 64,
                          request_id="sh", content_hash="0" * 64)

    def test_shards_arrive_out_of_order_and_freeze_on_completion(self):
        run = self.fx.open_run(expected_shards=2)
        second = self.fx.shard(run["run_id"], 1, [passed("c2")], "d" * 64, request_id="sh2")
        self.assertFalse(second["frozen"])
        self.assertEqual(1, second["received_shards"])
        first = self.fx.shard(run["run_id"], 0, [passed("c1")], "e" * 64, request_id="sh1")
        self.assertTrue(first["frozen"])
        self.assertEqual("pass", first["conclusion"])

    def test_same_shard_replays_but_different_content_cannot_overwrite(self):
        run = self.fx.open_run()
        cases = [passed("c1"), passed("c2")]
        first = self.fx.shard(run["run_id"], 0, cases, "c" * 64, request_id="sh1")
        replay = self.fx.shard(run["run_id"], 0, cases, "c" * 64, request_id="sh1")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ConflictError):
            self.fx.shard(run["run_id"], 0, cases, "b" * 64, request_id="sh-other")

    def test_repeated_failure_signature_means_stable_fail(self):
        run1 = self.fx.open_run(request_id="open1")
        self.fx.shard(run1["run_id"], 0,
                      [passed("c1"), failed("c2", "AssertionError", "expected 100 got 90")],
                      "1" * 64, request_id="s1")
        run2 = self.fx.open_run(request_id="open2")
        self.fx.shard(run2["run_id"], 0,
                      [passed("c1"), failed("c2", "AssertionError", "expected 100 got 80")],
                      "2" * 64, request_id="s2")
        replay2 = self.fx.svc.get_run(run2["run_id"])
        self.assertEqual("stable_fail", replay2["conclusion"])
        self.assertTrue(replay2["inputs_hash_matches"])
        rules = [item["rule"] for item in replay2["judgement_evidence"]]
        self.assertIn("repeated_failure_signature", rules)

    def test_flaky_when_case_passed_in_prior_attempt(self):
        run1 = self.fx.open_run(request_id="open1")
        self.fx.shard(run1["run_id"], 0,
                      [passed("c1"), failed("c2", "AssertionError", "boom at 0x1234")],
                      "1" * 64, request_id="s1")
        run2 = self.fx.open_run(request_id="open2")
        self.fx.shard(run2["run_id"], 0,
                      [failed("c1", "TimeoutError", "timed out after 5000 ms"),
                       passed("c2")],
                      "2" * 64, request_id="s2")
        replay2 = self.fx.svc.get_run(run2["run_id"])
        self.assertEqual("flaky", replay2["conclusion"])
        rules = [item["rule"] for item in replay2["judgement_evidence"]]
        self.assertIn("flaky_passed_before", rules)

    def test_missing_case_freezes_as_invalid(self):
        run = self.fx.open_run()
        receipt = self.fx.shard(run["run_id"], 0, [passed("c1")], "1" * 64,
                                request_id="s1")
        self.assertTrue(receipt["frozen"])
        self.assertEqual("invalid", receipt["conclusion"])
        replay = self.fx.svc.get_run(run["run_id"])
        reasons = {item["reason"] for item in replay["invalid_reasons"]}
        self.assertIn("missing_case", reasons)

    def test_duplicate_case_across_shards_freezes_invalid_without_crash(self):
        run = self.fx.open_run(expected_shards=2)
        self.fx.shard(run["run_id"], 0, [passed("c1")], "1" * 64, request_id="d1")
        receipt = self.fx.shard(run["run_id"], 1, [passed("c1")], "2" * 64, request_id="d2")
        self.assertTrue(receipt["frozen"])
        self.assertEqual("invalid", receipt["conclusion"])
        replay = self.fx.svc.get_run(run["run_id"])
        reasons = {item["reason"] for item in replay["invalid_reasons"]}
        self.assertIn("duplicate_case", reasons)
        self.assertIn("missing_case", reasons)

    def test_normalize_signature_ignores_addresses_and_numbers(self):
        sig1 = normalize_signature("AssertionError", "NPE at 0xabcd line 12")
        sig2 = normalize_signature("AssertionError", "NPE at 0x9999 line 88")
        self.assertEqual(sig1, sig2)

    def test_review_flow_with_deadline_and_single_explanation(self):
        run = self.fx.open_run()
        self.fx.shard(run["run_id"], 0,
                      [passed("c1"), failed("c2", "AssertionError", "bad result")],
                      "1" * 64, request_id="s1")
        deadline = (self.clock.now() + timedelta(days=2)).isoformat().replace("+00:00", "Z")
        review = self.fx.svc.open_review(request_id="rv", actor_id="t1", run_id=run["run_id"],
                                         student_actor_id="s1", deadline=deadline)
        review_id = review["review_id"]
        self.fx.svc.submit_explanation(request_id="ex", actor_id="s1", review_id=review_id,
                                       explanation="环境偶发抖动导致")
        with self.assertRaises(ConflictError):
            self.fx.svc.submit_explanation(request_id="ex2", actor_id="s1", review_id=review_id,
                                           explanation="再次补充")
        with self.assertRaises(PermissionDenied):
            self.fx.svc.decide_review(request_id="d0", actor_id="t1", review_id=review_id,
                                      decision="accept")
        decision = self.fx.svc.decide_review(request_id="d1", actor_id="r1",
                                             review_id=review_id, decision="rerun")
        self.assertEqual(2, self.fx.svc.get_experiment("exp-1")["runs"][-1]["attempt"])
        self.assertTrue(decision["rerun_run_id"])

    def test_late_explanation_is_rejected(self):
        run = self.fx.open_run(request_id="open1")
        self.fx.shard(run["run_id"], 0,
                      [passed("c1"), failed("c2", "AssertionError", "bad result")],
                      "1" * 64, request_id="s1")
        deadline = (self.clock.now() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        review = self.fx.svc.open_review(request_id="rv", actor_id="t1", run_id=run["run_id"],
                                         student_actor_id="s1", deadline=deadline)
        self.fx.svc.clock = FixedClock(self.clock.now() + timedelta(hours=2))
        with self.assertRaises(ConflictError):
            self.fx.svc.submit_explanation(request_id="ex", actor_id="s1",
                                           review_id=review["review_id"],
                                           explanation="逾期说明")

    def test_stats_only_reference_frozen_runs(self):
        run1 = self.fx.open_run(request_id="open1")
        self.fx.shard(run1["run_id"], 0,
                      [passed("c1"), failed("c2", "AssertionError", "bad result")],
                      "1" * 64, request_id="s1")
        # 未齐套的收集态运行不得进入统计。
        collecting = self.fx.open_run(expected_shards=2, request_id="open2")
        self.fx.shard(collecting["run_id"], 0, [passed("c1")], "2" * 64, request_id="s2a")
        snapshot = self.fx.svc.stats_snapshot(request_id="stat1", actor_id="t1",
                                              experiment_id="exp-1")
        self.assertEqual(1, snapshot["frozen_run_count"])
        self.assertEqual(1, snapshot["conclusion_counts"]["stable_fail"])
        self.assertEqual(run1["run_id"], snapshot["frozen_inputs"][0]["run_id"])
        self.assertEqual(64, len(snapshot["content_hash"]))
        # 幂等重放返回同一快照编号。
        again = self.fx.svc.stats_snapshot(request_id="stat1", actor_id="t1",
                                           experiment_id="exp-1")
        self.assertTrue(again["replayed"])

    def test_recover_interrupted_merge_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            fx = ExperimentFixture(database, self.clock)
            run = fx.open_run()
            # 模拟冻结事务中断：分片已落库但运行仍停留在 collecting。
            content = {"build_digest": fx.build_digest, "environment_digest": fx.env_digest,
                       "coverage_digest": "9" * 64,
                       "cases": [passed("c1"), passed("c2")]}
            database.connection.execute(
                "INSERT INTO shards(run_id,shard_index,content_hash,build_digest,"
                "environment_digest,payload_json,uploaded_by,received_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (run["run_id"], 0, sha(content), fx.build_digest, fx.env_digest,
                 canonical_json(content), "t1", "2026-09-25T08:00:00Z"))
            database.close()

            restarted = Database(path)
            service = ExperimentService(restarted)
            recovered = service.recover_interrupted()
            self.assertEqual(1, len(recovered))
            self.assertEqual(run["run_id"], recovered[0]["run_id"])
            self.assertEqual("pass", recovered[0]["conclusion"])
            # 恢复是幂等的：再次执行没有待冻结运行。
            self.assertEqual([], service.recover_interrupted())
            replay = service.get_run(run["run_id"])
            self.assertEqual("frozen", replay["status"])
            restarted.close()

    def test_student_cannot_register_build(self):
        with self.assertRaises(PermissionDenied):
            self.fx.svc.register_build(request_id="bx", actor_id="s1", build_id="build-x",
                                       program_name="p", build_digest="a" * 64,
                                       manifest={"x": 1})

    def test_run_not_found(self):
        with self.assertRaises(NotFoundError):
            self.fx.svc.get_run("missing-run")


if __name__ == "__main__":
    unittest.main()
