import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skills_workspace.audit import digest
from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, PermissionDenied, ValidationError
from skills_workspace.experiments import ExperimentService
from skills_workspace.storage import Database


def case(case_id, outcomes, signature="SIG"):
    attempts = []
    for outcome in outcomes:
        attempt = {"outcome": outcome}
        if outcome == "fail":
            attempt["failure_signature"] = signature
            attempt["log_summary"] = f"{case_id} 日志"
        attempts.append(attempt)
    return {"case_id": case_id, "attempts": attempts,
            "coverage": {"evidence_ref": f"cov/{case_id}.lcov"}}


def shard(index, cases, env_digest):
    return {"executor": {"executor_id": f"e{index}",
                         "observed_environment_digest": env_digest}, "cases": cases}


class ExperimentServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite3"
        self.clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.database = Database(self.db_path)
        self.service = ExperimentService(self.database, self.clock)
        self._bootstrap()

    def tearDown(self):
        self.database.close()
        self.tmp.cleanup()

    def _bootstrap(self):
        s = self.service
        s.register_organization(request_id="org-req", actor_id="bootstrap",
                                organization_id="o1", name="教研组")
        s.register_actor(request_id="adm-req", actor_id="bootstrap", new_actor_id="adm",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="tea-req", actor_id="adm", new_actor_id="tea",
                         display_name="教师", role="operator", organization_id="o1")
        s.register_actor(request_id="rev-req", actor_id="adm", new_actor_id="rev",
                         display_name="复核员", role="reviewer", organization_id="o1")
        s.register_actor(request_id="stu-req", actor_id="adm", new_actor_id="stu",
                         display_name="学生", role="student", organization_id="o1")
        s.register_site(request_id="site-req", actor_id="tea", site_id="lab",
                        organization_id="o1", name="集训室", timezone_name="Asia/Shanghai")
        self.env_digest = digest({"os": "linux"})
        s.register_build(request_id="build-req", actor_id="tea", build_id="b1", source_ref="r@1",
                         build_digest=digest({"commit": "1"}), manifest={"commit": "1"})
        s.register_case_package(request_id="pkg-req", actor_id="tea", package_id="p1",
                                package_digest=digest({"v": 1}),
                                case_ids=["c1", "c2"], manifest={"v": 1})
        s.register_environment(request_id="env-req", actor_id="tea", environment_id="e1",
                               environment_digest=self.env_digest, manifest={"os": "linux"})

    def _create_run(self, run_id="run-1", shards=2):
        self.service.create_run(request_id=f"req-{run_id}", actor_id="tea", run_id=run_id,
                                site_id="lab", build_id="b1", package_id="p1",
                                environment_id="e1", expected_shards=shards)

    def test_immutable_build_rejects_changed_digest(self):
        with self.assertRaises(ConflictError):
            self.service.register_build(request_id="b2", actor_id="tea", build_id="b1",
                                        source_ref="r@1", build_digest="a" * 64,
                                        manifest={"commit": "2"})

    def test_shards_arrive_out_of_order_and_freeze_on_completion(self):
        self._create_run()
        second = self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=1,
            shard=shard(1, [case("c2", ["pass"])], self.env_digest))
        self.assertFalse(second["frozen"])
        first = self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["fail", "fail"], "BUG-1")], self.env_digest))
        self.assertTrue(first["frozen"])
        self.assertEqual("stable_failure", first["verdict"])
        stored = self.service.get_run("run-1")
        self.assertEqual("frozen", stored["status"])
        self.assertTrue(stored["frozen_inputs_digest"])

    def test_same_shard_content_is_idempotent_other_content_rejected(self):
        self._create_run()
        body = shard(0, [case("c1", ["pass"])], self.env_digest)
        self.service.upload_shard(actor_id="tea", run_id="run-1", shard_index=0, shard=body)
        again = self.service.upload_shard(actor_id="tea", run_id="run-1", shard_index=0, shard=body)
        self.assertTrue(again["replayed"])
        with self.assertRaises(ConflictError):
            self.service.upload_shard(
                actor_id="tea", run_id="run-1", shard_index=0,
                shard=shard(0, [case("c1", ["fail"], "X")], self.env_digest))

    def test_client_content_hash_mismatch_rejected(self):
        self._create_run()
        body = shard(0, [case("c1", ["pass"])], self.env_digest)
        body["content_hash"] = "0" * 64
        with self.assertRaises(ValidationError):
            self.service.upload_shard(actor_id="tea", run_id="run-1",
                                      shard_index=0, shard=body)

    def test_resume_freezes_complete_run_after_restart(self):
        self._create_run()
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["pass"])], self.env_digest))
        # 模拟中断：第二个分片已落库，但冻结步骤从未执行（手动关闭后用新实例）。
        self.database.close()
        database = Database(self.db_path)
        service = ExperimentService(database, self.clock)
        # 新实例先确认仍是 collecting。
        self.assertEqual("collecting", service.get_run("run-1")["status"])
        # 直接通过恢复路径前，先补入最后一个分片，再调用 resume。
        service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=1,
            shard=shard(1, [case("c2", ["pass"])], self.env_digest))
        self.assertEqual("frozen", service.get_run("run-1")["status"])
        # 再次恢复应当幂等无操作。
        self.assertEqual([], service.resume_pending())
        database.close()

    def test_resume_picks_up_already_complete_but_unfrozen_run(self):
        # 构造齐套但未冻结的状态：临时替换 _freeze_if_complete 为空操作。
        self._create_run()
        original = ExperimentService._freeze_if_complete
        ExperimentService._freeze_if_complete = lambda self, conn, run_id: None
        try:
            self.service.upload_shard(
                actor_id="tea", run_id="run-1", shard_index=0,
                shard=shard(0, [case("c1", ["pass"])], self.env_digest))
            self.service.upload_shard(
                actor_id="tea", run_id="run-1", shard_index=1,
                shard=shard(1, [case("c2", ["pass"])], self.env_digest))
        finally:
            ExperimentService._freeze_if_complete = original
        self.assertEqual("collecting", self.service.get_run("run-1")["status"])
        resumed = self.service.resume_pending()
        self.assertEqual([{"run_id": "run-1", "verdict": "stable_pass"}], resumed)
        self.assertEqual("frozen", self.service.get_run("run-1")["status"])

    def test_invalid_run_freezes_with_invalid_verdict(self):
        self._create_run(shards=1)
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["pass"]), case("c2", ["pass"])], "other-env-digest"))
        stored = self.service.get_run("run-1")
        self.assertEqual("invalid", stored["verdict"])
        self.assertEqual("environment_mismatch", stored["reason_code"])

    def test_replay_recomputes_and_matches_frozen(self):
        self._create_run(shards=1)
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["fail", "pass"], "FL"), case("c2", ["pass"])],
                        self.env_digest))
        replayed = self.service.replay_run("run-1")
        self.assertTrue(replayed["matches"])
        self.assertEqual("flaky", replayed["recomputed_verdict"])
        self.assertIn("偶发", replayed["explanation"]["summary"])

    def test_review_workflow_accept_with_single_supplement(self):
        self._create_run(shards=1)
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["fail", "fail"], "B"), case("c2", ["pass"])],
                        self.env_digest))
        deadline = (self.clock.now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        self.service.open_review(request_id="rv", actor_id="tea", run_id="run-1",
                                 deadline=deadline, note="请复核")
        review_id = self.service.get_run("run-1")["review_id"]
        self.service.submit_supplement(request_id="sup", actor_id="stu",
                                       review_id=review_id, content="已本地复现")
        with self.assertRaises(ConflictError):
            self.service.submit_supplement(request_id="sup2", actor_id="stu",
                                           review_id=review_id, content="再次补充")
        self.service.decide_review(request_id="dec", actor_id="rev",
                                   review_id=review_id, decision="accept")
        self.assertEqual("accepted", self.service.get_review(review_id)["status"])

    def test_student_cannot_open_review(self):
        self._create_run(shards=1)
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["pass"]), case("c2", ["pass"])], self.env_digest))
        deadline = (self.clock.now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        with self.assertRaises(PermissionDenied):
            self.service.open_review(request_id="rv2", actor_id="stu", run_id="run-1",
                                     deadline=deadline, note="x")

    def test_supplement_after_deadline_rejected(self):
        self._create_run(shards=1)
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["pass"]), case("c2", ["pass"])], self.env_digest))
        deadline = (self.clock.now() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        self.service.open_review(request_id="rv", actor_id="tea", run_id="run-1",
                                 deadline=deadline, note="x")
        review_id = self.service.get_run("run-1")["review_id"]
        self.clock._value = self.clock.now() + timedelta(hours=2)
        with self.assertRaises(ConflictError):
            self.service.submit_supplement(request_id="sup", actor_id="stu",
                                           review_id=review_id, content="迟到的说明")
        expired = self.service.expire_due_reviews()
        self.assertEqual(review_id, expired[0]["review_id"])

    def test_rerun_creates_new_run_and_supersedes_old(self):
        self._create_run(shards=1)
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["fail", "pass"], "F"), case("c2", ["pass"])],
                        self.env_digest))
        deadline = (self.clock.now() + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        self.service.open_review(request_id="rv", actor_id="tea", run_id="run-1",
                                 deadline=deadline, note="重跑")
        review_id = self.service.get_run("run-1")["review_id"]
        self.service.decide_review(request_id="dec", actor_id="rev",
                                   review_id=review_id, decision="rerun")
        old = self.service.get_run("run-1")
        self.assertEqual("superseded", old["status"])
        new_run = self.service.get_run(old["superseded_by_run_id"])
        self.assertEqual("collecting", new_run["status"])
        self.assertEqual(0, self.service.statistics()["frozen_run_count"])
        self.assertEqual(1, self.service.statistics(include_superseded=True)["frozen_run_count"])

    def test_replay_detects_tampered_shard_payload(self):
        self._create_run(shards=1)
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["fail", "fail"], "B"), case("c2", ["pass"])],
                        self.env_digest))
        self.assertTrue(self.service.replay_run("run-1")["matches"])
        # 直接篡改已冻结分片的载荷，绕过领域层。
        row = self.database.connection.execute(
            "SELECT payload_json FROM run_shards WHERE run_id='run-1' AND shard_index=0"
        ).fetchone()
        tampered = row[0].replace("日志", "被篡改的日志")
        self.database.connection.execute(
            "UPDATE run_shards SET payload_json=? WHERE run_id='run-1' AND shard_index=0",
            (tampered,),
        )
        replayed = self.service.replay_run("run-1")
        self.assertFalse(replayed["matches"])
        self.assertEqual(0, replayed["tampered_shards"][0]["shard_index"])

    def test_open_review_rejects_past_deadline(self):
        self._create_run(shards=1)
        self.service.upload_shard(
            actor_id="tea", run_id="run-1", shard_index=0,
            shard=shard(0, [case("c1", ["pass"]), case("c2", ["pass"])], self.env_digest))
        past = (self.clock.now() - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        with self.assertRaises(ValidationError):
            self.service.open_review(request_id="rv", actor_id="tea", run_id="run-1",
                                     deadline=past, note="x")


if __name__ == "__main__":
    unittest.main()
