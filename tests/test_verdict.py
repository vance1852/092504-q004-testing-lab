import unittest

from skills_workspace.verdict import (
    VERDICT_FLAKY,
    VERDICT_INVALID,
    VERDICT_STABLE_FAILURE,
    VERDICT_STABLE_PASS,
    classify,
    frozen_inputs_digest,
)


def case(case_id, outcomes, signature="SIG", coverage=True):
    attempts = []
    for outcome in outcomes:
        attempt = {"outcome": outcome}
        if outcome == "fail":
            attempt["failure_signature"] = signature
            attempt["log_summary"] = "日志摘要"
        attempts.append(attempt)
    return {
        "case_id": case_id,
        "attempts": attempts,
        "coverage": {"evidence_ref": f"cov/{case_id}.lcov"} if coverage else {},
    }


def shard(index, cases, env_digest="env-1"):
    return {
        "shard_index": index,
        "executor": {"executor_id": f"e{index}", "observed_environment_digest": env_digest},
        "cases": cases,
    }


class ClassifyTest(unittest.TestCase):
    def test_all_pass_is_stable_pass(self):
        result = classify(["c1", "c2"], [shard(0, [case("c1", ["pass"]), case("c2", ["pass", "pass"])])],
                          expected_shards=1, declared_environment_digest="env-1")
        self.assertEqual(VERDICT_STABLE_PASS, result["verdict"])

    def test_deterministic_failures_are_stable_failure(self):
        result = classify(["c1"], [shard(0, [case("c1", ["fail", "fail"], "SIG-X")])],
                          expected_shards=1, declared_environment_digest="env-1")
        self.assertEqual(VERDICT_STABLE_FAILURE, result["verdict"])
        self.assertEqual("deterministic_failures", result["reason_code"])

    def test_mixed_outcomes_are_flaky(self):
        result = classify(["c1"], [shard(0, [case("c1", ["fail", "pass"], "SIG-X")])],
                          expected_shards=1, declared_environment_digest="env-1")
        self.assertEqual(VERDICT_FLAKY, result["verdict"])
        self.assertEqual(1, result["counts"]["flaky_cases"])

    def test_repeated_failure_signature_clusters_cases(self):
        result = classify(
            ["c1", "c2"],
            [shard(0, [case("c1", ["fail"], "SAME"), case("c2", ["fail"], "SAME")])],
            expected_shards=1, declared_environment_digest="env-1",
        )
        self.assertEqual(VERDICT_STABLE_FAILURE, result["verdict"])
        self.assertEqual(1, len(result["repeated_signatures"]))
        self.assertEqual(["c1", "c2"], result["repeated_signatures"][0]["cases"])

    def test_missing_shard_is_invalid(self):
        result = classify(["c1"], [shard(1, [case("c1", ["pass"])])],
                          expected_shards=2, declared_environment_digest="env-1")
        self.assertEqual(VERDICT_INVALID, result["verdict"])
        self.assertEqual("shards_incomplete", result["reason_code"])

    def test_case_set_mismatch_is_invalid(self):
        result = classify(["c1", "c2"], [shard(0, [case("c1", ["pass"]), case("c3", ["pass"])])],
                          expected_shards=1, declared_environment_digest="env-1")
        self.assertEqual(VERDICT_INVALID, result["verdict"])
        self.assertEqual("case_set_mismatch", result["reason_code"])

    def test_missing_coverage_is_invalid(self):
        result = classify(["c1"], [shard(0, [case("c1", ["pass"], coverage=False)])],
                          expected_shards=1, declared_environment_digest="env-1")
        self.assertEqual(VERDICT_INVALID, result["verdict"])
        self.assertEqual("coverage_evidence_missing", result["reason_code"])

    def test_environment_mismatch_is_invalid(self):
        result = classify(["c1"], [shard(0, [case("c1", ["pass"])], env_digest="other-env")],
                          expected_shards=1, declared_environment_digest="env-1")
        self.assertEqual(VERDICT_INVALID, result["verdict"])
        self.assertEqual("environment_mismatch", result["reason_code"])

    def test_failure_without_signature_is_invalid(self):
        bad = {"case_id": "c1", "attempts": [{"outcome": "fail", "log_summary": "x"}],
               "coverage": {"evidence_ref": "cov/c1.lcov"}}
        result = classify(["c1"], [shard(0, [bad])],
                          expected_shards=1, declared_environment_digest="env-1")
        self.assertEqual(VERDICT_INVALID, result["verdict"])
        self.assertEqual("malformed_attempt_evidence", result["reason_code"])

    def test_frozen_digest_is_order_independent_and_deterministic(self):
        first = frozen_inputs_digest(build_digest="b" * 64, package_digest="p" * 64,
                                     environment_digest="e" * 64,
                                     shard_hashes=[(0, "h0"), (1, "h1")])
        second = frozen_inputs_digest(build_digest="b" * 64, package_digest="p" * 64,
                                      environment_digest="e" * 64,
                                      shard_hashes=[(1, "h1"), (0, "h0")])
        self.assertEqual(first, second)
        third = frozen_inputs_digest(build_digest="a" * 64, package_digest="p" * 64,
                                     environment_digest="e" * 64,
                                     shard_hashes=[(0, "h0"), (1, "h1")])
        self.assertNotEqual(first, third)


if __name__ == "__main__":
    unittest.main()
