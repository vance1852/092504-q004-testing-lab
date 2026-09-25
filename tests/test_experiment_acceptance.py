import unittest

from skills_workspace.experiment_acceptance import run


class ExperimentAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["checks"]["audit_chain_valid"])
        self.assertTrue(result["checks"]["stable_fail_confirmed"])
        self.assertTrue(result["checks"]["flaky_detected"])
        self.assertTrue(result["checks"]["restart_resumes_merge"])
        self.assertTrue(result["checks"]["stats_reference_only_frozen"])


if __name__ == "__main__":
    unittest.main()
