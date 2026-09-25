import unittest
from datetime import datetime, timedelta, timezone

from skills_workspace.api import route
from skills_workspace.clock import FixedClock
from skills_workspace.experiments import ExperimentService
from skills_workspace.storage import Database
from skills_workspace.audit import digest


def case(case_id, outcomes, signature="SIG"):
    attempts = []
    for outcome in outcomes:
        attempt = {"outcome": outcome}
        if outcome == "fail":
            attempt["failure_signature"] = signature
            attempt["log_summary"] = "日志"
        attempts.append(attempt)
    return {"case_id": case_id, "attempts": attempts,
            "coverage": {"evidence_ref": f"cov/{case_id}.lcov"}}


class ExperimentApiTest(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.database = Database()
        self.service = ExperimentService(self.database, self.clock)
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
                                package_digest=digest({"v": 1}), case_ids=["c1"],
                                manifest={"v": 1})
        s.register_environment(request_id="env-req", actor_id="tea", environment_id="e1",
                               environment_digest=self.env_digest, manifest={"os": "linux"})

    def tearDown(self):
        self.database.close()

    def _headers(self, actor="tea"):
        return {"X-Actor-Id": actor}

    def test_full_run_lifecycle_over_http(self):
        status, body = route(self.service, "POST", "/runs",
                             {"request_id": "run-req", "run_id": "run-1", "site_id": "lab",
                              "build_id": "b1", "package_id": "p1", "environment_id": "e1",
                              "expected_shards": 1}, self._headers())
        self.assertEqual(201, status)

        status, body = route(self.service, "POST", "/runs/run-1/shards",
                             {"shard_index": 0,
                              "shard": {"executor": {"observed_environment_digest": self.env_digest},
                                        "cases": [case("c1", ["fail", "fail"], "BUG-9")]}},
                             self._headers())
        self.assertEqual(200, status)
        self.assertTrue(body["frozen"])
        self.assertEqual("stable_failure", body["verdict"])

        status, body = route(self.service, "GET", "/runs/run-1/replay", None, self._headers())
        self.assertEqual(200, status)
        self.assertTrue(body["matches"])

        status, body = route(self.service, "GET", "/statistics", None, self._headers())
        self.assertEqual(200, status)
        self.assertEqual(1, body["frozen_run_count"])

        status, body = route(self.service, "GET", "/signatures?signature=BUG-9", None,
                             self._headers())
        self.assertEqual(200, status)
        self.assertEqual(1, body["match_count"])

    def test_review_decision_over_http(self):
        route(self.service, "POST", "/runs",
              {"request_id": "run-req", "run_id": "run-1", "site_id": "lab",
               "build_id": "b1", "package_id": "p1", "environment_id": "e1",
               "expected_shards": 1}, self._headers())
        route(self.service, "POST", "/runs/run-1/shards",
              {"shard_index": 0,
               "shard": {"executor": {"observed_environment_digest": self.env_digest},
                         "cases": [case("c1", ["fail", "pass"], "BUG")]}},
              self._headers())
        deadline = (self.clock.now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        status, body = route(self.service, "POST", "/reviews",
                             {"request_id": "rv-req", "run_id": "run-1",
                              "deadline": deadline, "note": "请复核"}, self._headers())
        self.assertEqual(201, status)
        review_id = body["resource_id"]
        status, body = route(self.service, "POST", f"/reviews/{review_id}/supplement",
                             {"request_id": "sup-req", "content": "已复现"}, self._headers("stu"))
        self.assertEqual(201, status)
        status, body = route(self.service, "POST", f"/reviews/{review_id}/decision",
                             {"request_id": "dec-req", "decision": "accept"},
                             self._headers("rev"))
        self.assertEqual(201, status)
        status, body = route(self.service, "GET", f"/reviews/{review_id}", None, self._headers())
        self.assertEqual(200, status)
        self.assertEqual("accepted", body["status"])

    def test_shard_overwrite_conflict(self):
        route(self.service, "POST", "/runs",
              {"request_id": "run-req", "run_id": "run-1", "site_id": "lab",
               "build_id": "b1", "package_id": "p1", "environment_id": "e1",
               "expected_shards": 2}, self._headers())
        status, _ = route(self.service, "POST", "/runs/run-1/shards",
                          {"shard_index": 0,
                           "shard": {"executor": {"observed_environment_digest": self.env_digest},
                                     "cases": [case("c1", ["pass"])]}},
                          self._headers())
        self.assertEqual(200, status)
        status, body = route(self.service, "POST", "/runs/run-1/shards",
                             {"shard_index": 0,
                              "shard": {"executor": {"observed_environment_digest": self.env_digest},
                                        "cases": [case("c1", ["fail"], "X")]}},
                             self._headers())
        self.assertEqual(409, status)
        self.assertEqual("conflict", body["error"])


if __name__ == "__main__":
    unittest.main()
