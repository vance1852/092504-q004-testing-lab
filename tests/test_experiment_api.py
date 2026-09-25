import unittest
from datetime import datetime, timedelta, timezone

from skills_workspace.api import route
from skills_workspace.audit import digest
from skills_workspace.clock import FixedClock
from skills_workspace.experiments import ExperimentService
from skills_workspace.service import DomainService
from skills_workspace.storage import Database


def passed(case_id):
    return {"case_id": case_id, "outcome": "passed"}


def failed(case_id, failure_type, message):
    return {"case_id": case_id, "outcome": "failed",
            "failure": {"type": failure_type, "message": message},
            "log_digest": "a" * 64}


class ExperimentApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.svc = ExperimentService(self.database, clock)
        self.headers = {"X-Actor-Id": "t1"}
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="教研组")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        for request_id, actor_id, name, role in [
                ("t", "t1", "教师", "teacher"),
                ("s", "s1", "学生", "student"),
                ("r", "r1", "复核员", "reviewer")]:
            self.base.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                     display_name=name, role=role, organization_id="o1")
        self.build_digest = digest({"files": ["m.py"]})
        self.env_digest = digest({"os": "linux"})
        self._call("POST", "/builds", {
            "request_id": "b", "build_id": "build-1", "program_name": "calc",
            "build_digest": self.build_digest, "manifest": {"files": ["m.py"]}})
        self._call("POST", "/case-packages", {
            "request_id": "p", "package_id": "pkg", "version": "1.0.0",
            "package_digest": digest({"v": 1}), "case_ids": ["c1", "c2"]})
        self._call("POST", "/environments", {
            "request_id": "e", "environment_id": "env",
            "declaration": {"os": "linux"}})
        self._call("POST", "/experiments", {
            "request_id": "x", "experiment_id": "exp", "build_id": "build-1",
            "package_id": "pkg", "package_version": "1.0.0", "environment_id": "env"})

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="t1"):
        return route(self.base, method, path, body or {}, {"X-Actor-Id": actor},
                     experiment_service=self.svc)

    def _shard_body(self, request_id, run_id, index, cases, coverage_digest="c" * 64):
        content = {"build_digest": self.build_digest, "environment_digest": self.env_digest,
                   "coverage_digest": coverage_digest, "cases": cases}
        return {"request_id": request_id, "run_id": run_id, "shard_index": index,
                "content_hash": digest(content), "build_digest": self.build_digest,
                "environment_digest": self.env_digest, "cases": cases,
                "coverage_digest": coverage_digest}

    def test_full_lifecycle_via_http(self):
        status, opened = self._call("POST", "/runs/open", {
            "request_id": "open", "experiment_id": "exp", "expected_shards": 2})
        self.assertEqual(201, status)
        run_id = opened["run_id"]

        # 乱序到达：先传分片 1。
        status, second = self._call("POST", "/runs/shards",
                                    self._shard_body("sh2", run_id, 1, [passed("c2")], "d" * 64))
        self.assertEqual(201, status)
        self.assertFalse(second["frozen"])

        # 齐套分片 0 触发冻结。
        status, first = self._call("POST", "/runs/shards",
                                   self._shard_body("sh1", run_id, 0,
                                                    [failed("c1", "AssertionError", "bad 1")],
                                                    "e" * 64))
        self.assertTrue(first["frozen"])
        self.assertIn(first["conclusion"], {"stable_fail", "flaky", "invalid", "pass"})

        # 同内容重传幂等回放。
        status, replay = self._call("POST", "/runs/shards",
                                    self._shard_body("sh2", run_id, 1, [passed("c2")], "d" * 64))
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        # 异内容覆盖被拒绝。
        status, conflict = self._call("POST", "/runs/shards",
                                      self._shard_body("shx", run_id, 1,
                                                       [passed("c2")], "f" * 64))
        self.assertEqual(409, status)
        self.assertEqual("conflict", conflict["error"])

        # 判定重放。
        status, replay_body = self._call("GET", f"/runs/{run_id}/replay")
        self.assertEqual(200, status)
        self.assertEqual(first["conclusion"], replay_body["conclusion"])
        self.assertTrue(replay_body["inputs_hash_matches"])
        self.assertEqual(2, replay_body["received_shards"])

        # 复核流程。
        deadline = (datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc) + timedelta(days=1)) \
            .isoformat().replace("+00:00", "Z")
        status, review = self._call("POST", "/reviews", {
            "request_id": "rv", "run_id": run_id, "student_actor_id": "s1",
            "deadline": deadline})
        self.assertEqual(201, status)
        status, _ = self._call("POST", "/reviews/explanation", {
            "request_id": "ex", "review_id": review["review_id"],
            "explanation": "网络抖动"}, actor="s1")
        self.assertEqual(201, status)
        status, decision = self._call("POST", "/reviews/decision", {
            "request_id": "dec", "review_id": review["review_id"], "decision": "accept"},
            actor="r1")
        self.assertEqual(201, status)
        self.assertEqual("accept", decision["decision"])

        # 统计快照。
        status, stats = self._call("POST", "/stats/snapshots", {
            "request_id": "stats", "experiment_id": "exp"})
        self.assertEqual(201, status)
        self.assertEqual(1, stats["frozen_run_count"])
        self.assertEqual(run_id, stats["frozen_inputs"][0]["run_id"])
        self.assertEqual(64, len(stats["content_hash"]))

        # 实验视图。
        status, experiment = self._call("GET", "/experiments/exp")
        self.assertEqual(200, status)
        self.assertEqual(1, len(experiment["runs"]))

    def test_student_forbidden_from_build_registration(self):
        status, payload = self._call("POST", "/builds", {
            "request_id": "bx", "build_id": "bx", "program_name": "p",
            "build_digest": "a" * 64, "manifest": {"x": 1}}, actor="s1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_recover_route(self):
        status, payload = self._call("POST", "/runs/recover", {})
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])

    def test_unknown_experiment_route_still_404(self):
        status, payload = route(self.base, "GET", "/nope", {}, {},
                                experiment_service=self.svc)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
