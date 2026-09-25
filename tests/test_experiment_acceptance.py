import unittest

from skills_workspace.experiment_acceptance import run


class ExperimentAcceptanceTest(unittest.TestCase):
    def test_offline_experiment_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["replay_matches"])
        self.assertEqual("flaky", result["flaky_verdict"])
        self.assertEqual("stable_failure", result["stable_verdict"])
        self.assertEqual(2, result["repeated_signature_cases"])
        self.assertEqual(2, result["signature_cluster_size"])
        self.assertTrue(result["rerun_run_id"].startswith("run-flaky-r"))
        self.assertEqual(1, result["frozen_runs_in_stats"])


if __name__ == "__main__":
    unittest.main()
