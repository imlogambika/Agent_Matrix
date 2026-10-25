"""Regression tests for the safety scenarios (see code/evaluation/safety_scenarios.py).

Each test runs the real component on real or clearly synthetic input:
non-deletion requests, malformed model output, a dead model server, an identifier
shared by two people, a corrupted request file, and forged, late or fabricated
proof of a COMPLETE deletion. The last group runs the full pipeline offline on the
518 evaluation requests (cached model replies, a few seconds).
"""
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "evaluation"))

import safety_scenarios as sc  # noqa: E402
from ghost_audit import guard  # noqa: E402
from ghost_audit.data import load_requests  # noqa: E402
from ghost_audit.pipeline import AuditPipeline  # noqa: E402

HAS_DATA = (sc.DATASET / "deletion_requests.csv").exists()


class IsolatedScenarios(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, r):
        self.assertTrue(r["passed"], f"{r['name']}: expected {r['expected']}, observed {r['observed']} {r['detail']}")

    def test_malformed_model_output_degrades_to_rules_and_review(self):
        self.check(sc.malformed_model_output(self.tmp))

    def test_model_server_down_is_bounded_and_degrades(self):
        self.check(sc.model_server_unavailable(self.tmp))

    def test_shared_identifier_is_never_merged(self):
        self.check(sc.identity_conflict(self.tmp))

    @unittest.skipUnless(HAS_DATA, "dataset/ not present")
    def test_corrupted_request_file_is_refused(self):
        self.check(sc.corrupted_input(self.tmp))

    @unittest.skipUnless(HAS_DATA, "dataset/ not present")
    def test_non_deletion_requests_in_seven_languages(self):
        self.check(sc.non_deletion_requests(self.tmp))


@unittest.skipUnless(HAS_DATA, "dataset/ not present")
class GuardOnRealDecisions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        llm = sc._offline_llm(Path(cls._tmp.name))
        cls.results = AuditPipeline(sc.DATASET, llm, log=lambda *a: None).run(
            load_requests(sc.DATASET / "deletion_requests.csv"))
        cls.req = {r.case_id: r for r in load_requests(sc.DATASET / "deletion_requests.csv")}
        cls.evidence = guard.evidence_index(sc.DATASET)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def check(self, r):
        self.assertTrue(r["passed"], f"{r['name']}: {r['observed']} {r['detail']}")

    def test_forged_complete_citing_the_workflow_claim_is_rejected(self):
        self.check(sc.forged_complete_claim(self.results, self.req, self.evidence))

    def test_complete_backed_by_late_evidence_is_rejected(self):
        self.check(sc.late_evidence(self.results, self.req, self.evidence))

    def test_complete_citing_nonexistent_evidence_is_rejected(self):
        self.check(sc.fabricated_evidence(self.results, self.req, self.evidence))

    def test_every_real_decision_passes_the_guard(self):
        self.check(sc.real_run_guard(self.results, self.req, self.evidence))

    def test_claimed_complete_is_never_taken_as_proof(self):
        ghost = [r for r in self.results if self.req[r.case_id].reported_status == "COMPLETE"
                 and r.status == "INCOMPLETE"]
        self.assertGreater(len(ghost), 0)
        for r in self.results:   # every COMPLETE verification cites an evidence file, never a workflow record
            if r.status == "COMPLETE":
                self.assertTrue(all(s.evidence_id.startswith("evidence_") for s in r.steps), r.case_id)

    def test_not_applicable_only_for_non_deletion_requests(self):
        for r in self.results:
            if r.status == "NOT_APPLICABLE":
                self.assertEqual(r.steps, [])
                self.assertEqual(r.trace["intent"]["intent"], "NOT_DELETE", r.case_id)


if __name__ == "__main__":
    unittest.main()
