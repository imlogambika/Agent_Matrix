"""Unit tests for the deterministic core (standard library only).

    python3 -m unittest discover -s code/tests -v
"""
from __future__ import annotations

import csv
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from ghost_audit.data import IdentityResolver  # noqa: E402
from ghost_audit.evidence.logs import parse_log, tokenize  # noqa: E402
from ghost_audit.evidence.multimodal import merge_records, snap_id  # noqa: E402
from ghost_audit.evidence.sql_snapshots import parse_sql  # noqa: E402
from ghost_audit.evidence.transcript import parse_transcript  # noqa: E402
from ghost_audit.intent import rule_intent, vote  # noqa: E402
from ghost_audit.linker import claim_matches, plausibility  # noqa: E402
from ghost_audit.models import Activity, Observation, Step  # noqa: E402
from ghost_audit.policy import Policy, order_steps  # noqa: E402
from ghost_audit.state import ResourceState, reconstruct  # noqa: E402


def act(event_type, target, rid, result="SUCCESS", t="2026-09-02T11:00:00Z"):
    return Activity("a1", t, event_type, "deletion_worker", target, rid, result, "")


class LogFormats(unittest.TestCase):
    LINES = {
        "logfmt": '2026-09-15T08:51:57Z level=INFO service=search_service trace_id=tr-1 resource_id=search_1 '
                  'event=index_query_completed result=hit',
        "json": '2026-09-10T08:56:20Z {"service":"account_service","trace_id":"tr-2","resource_id":"account_1",'
                '"event":"profile_lookup_completed","rows":"1"}',
        "syslog": '2026-08-22T10:51:17Z <15>1 node-1 account_service tr-3 - - level=DEBUG resource_id="account_2" '
                  'event="profile_lookup_completed" rows="1"',
        "bracketed": '2026-07-28T05:51:21Z [INFO] [account_service] [tr-4] profile_lookup_completed: '
                     'resource_id="account_3" rows="0"',
        "pipe": '2026-07-03T07:52:15Z | INFO | account_service | tr-5 | profile_lookup_completed | '
                'resource_id="account_4" rows="0"',
        "cloud": '2026-06-08T06:52:09Z requestId=tr-6 level=INFO function=search_service message="resource_id=\\"search_2\\" '
                 'event=\\"index_query_completed\\" result=\\"hit\\""',
    }

    def test_every_syntax_yields_service_trace_event_and_resource(self):
        for name, line in self.LINES.items():
            ts, service, trace, event, kv = tokenize(line)
            with self.subTest(name):
                self.assertTrue(service.endswith("_service"))
                self.assertTrue(trace.startswith("tr-"))
                self.assertTrue(event.endswith("_completed"))
                self.assertTrue(kv["resource_id"].split("_")[0] in ("search", "account"))

    def test_lookup_key_on_start_event_is_stitched_to_the_completion(self):
        text = ("2026-09-15T11:51:09Z level=DEBUG service=account_service trace_id=tr-9 event=profile_lookup_started "
                'lookup_key="old.mail@example.org"\n'
                "2026-09-15T11:51:19Z level=INFO service=account_service trace_id=tr-9 resource_id=account_9 "
                "event=profile_lookup_completed rows=0 http_status=404\n")
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write(text)
        obs, _ = parse_log(fh.name, "evidence_x", "2026-09-15T12:00:00Z")
        self.assertEqual(len(obs), 1)
        self.assertFalse(obs[0].present)
        self.assertIn("old.mail@example.org", obs[0].lookup_keys)


class SqlDrift(unittest.TestCase):
    def test_copy_on_a_replica_keeps_the_record_present(self):
        sql = """-- Captured at: 2026-05-09T08:00:00Z
BEGIN TRANSACTION;
CREATE TABLE customer_data_primary (resource_id TEXT, origin_system TEXT, collection_name TEXT, record_json TEXT, updated_at TEXT);
CREATE TABLE customer_data_replica_eu_west (resource_id TEXT, origin_system TEXT, collection_name TEXT, record_json TEXT, updated_at TEXT);
INSERT INTO customer_data_replica_eu_west VALUES('account_1','account_service','customer_profiles','{"customer_reference":"user_1","email":"a@example.org"}','2026-05-09T08:00:00Z');
COMMIT;"""
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as fh:
            fh.write(sql)
        obs, scopes = parse_sql(fh.name, "evidence_y", "2026-05-09T08:00:00Z")
        self.assertEqual([o.resource_id for o in obs], ["account_1"])
        self.assertTrue(obs[0].present)
        self.assertEqual(obs[0].location, "customer_data_replica_eu_west")
        self.assertEqual(obs[0].pii_fields, {"email"})


class Identity(unittest.TestCase):
    def test_historical_email_and_derived_hash_resolve_to_the_same_user(self):
        tmp = Path(tempfile.mkdtemp())
        with open(tmp / "ids.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["user_id", "name", "email", "phone", "email_hash", "updated_at"])
            w.writerow(["user_1", "Ana Ruiz", "ana.new@example.org", "+34 600 000 001",
                        hashlib.sha256(b"ana.new@example.org").hexdigest(), "2026-01-01T00:00:00Z"])
        with open(tmp / "audit.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"])
            w.writerow(["a1", "user_1", "2025-01-01T00:00:00Z", "email", "ana.old@example.org", "ana.new@example.org"])
        r = IdentityResolver(tmp / "ids.csv", tmp / "audit.csv")
        self.assertEqual(r.resolve_email("ANA.OLD@example.org"), "user_1")
        old_hash = hashlib.sha256(b"ana.old@example.org").hexdigest()
        self.assertEqual(r.owners(Observation("f", "t", "s", "r", True, "x", hashes={old_hash})), {"user_1"})


class PolicyAndOrder(unittest.TestCase):
    policy = Policy({})

    def treat(self, **kw):
        base = dict(resource_id="x_1", system="s", category="USER_PROFILE", evidence_id="evidence_1")
        base.update(kw)
        return self.policy.treat(ResourceState(**base))

    def test_present_profile_is_found_deleted_and_verified(self):
        t = self.treat(present=True)
        self.assertEqual([f for f, _ in t.findings], ["REPORT_REMAINING_RECORD"])
        self.assertEqual(t.remediation, ["DELETE_RECORD"])
        self.assertEqual([v for v, _ in t.verification], ["VERIFY_RECORD_ABSENT"])

    def test_tombstone_with_pii_is_redacted_not_deleted(self):
        t = self.treat(category="ACCOUNT_TOMBSTONE", present=True, pii_exposed=True)
        self.assertEqual(t.remediation, ["REDACT_RECORD"])

    def test_retained_records_are_only_verified_present(self):
        for cat in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
            t = self.treat(category=cat, present=True)
            self.assertEqual((t.findings, t.remediation), ([], []))
            self.assertEqual([v for v, _ in t.verification], ["VERIFY_RECORD_PRESENT"])

    def test_restorable_backup_and_active_flow_are_blocked_and_disabled(self):
        self.assertEqual(self.treat(category="BACKUP_ARCHIVE", restore_allowed=True).remediation, ["BLOCK_RESTORE"])
        self.assertEqual(self.treat(category="DATA_FLOW", enabled=True).remediation, ["DISABLE_DATA_FLOW"])

    def test_contract_order_phase_then_operation_then_system_then_resource(self):
        steps = [Step("VERIFICATION", "search_service", "search_1", "VERIFY_RECORD_ABSENT", "ABSENT"),
                 Step("REMEDIATION", "search_service", "search_1", "DELETE_RECORD", "ABSENT"),
                 Step("REMEDIATION", "integration_service", "data_flow_1", "DISABLE_DATA_FLOW", "DISABLED"),
                 Step("FINDING", "support_platform", "support_1", "REPORT_EXPOSED_PII", "PRESENT"),
                 Step("FINDING", "search_service", "search_1", "REPORT_REMAINING_RECORD", "PRESENT"),
                 Step("VERIFICATION", "account_service", "account_1", "VERIFY_RECORD_ABSENT", "ABSENT")]
        got = [(s.phase, s.resource_id) for s in order_steps(steps)]
        self.assertEqual(got, [("FINDING", "search_1"), ("FINDING", "support_1"), ("REMEDIATION", "data_flow_1"),
                               ("REMEDIATION", "search_1"), ("VERIFICATION", "account_1"),
                               ("VERIFICATION", "search_1")])


class Admissibility(unittest.TestCase):
    def test_evidence_exported_after_audit_is_ignored(self):
        class Store:
            by_resource = {"search_1": [
                Observation("evidence_early", "2026-05-11T08:00:00Z", "search_service", "search_1", False, "x"),
                Observation("evidence_late", "2026-05-14T09:00:00Z", "search_service", "search_1", True, "x")]}
            scopes = []

            @staticmethod
            def captured_at(fid):
                return {"evidence_early": "2026-05-11T09:00:00Z", "evidence_late": "2026-05-15T09:00:00Z"}[fid]

        st = reconstruct("search_1", Store, "2026-05-12T10:00:00Z", set(), set(), "2026-05-03T10:00:00Z")
        self.assertFalse(st.present)
        self.assertEqual(st.evidence_id, "evidence_early")


class Intent(unittest.TestCase):
    def test_negations_override_deletion_words(self):
        self.assertEqual(rule_intent("Remove me from marketing lists only, keep the account."), "NOT_DELETE")
        self.assertEqual(rule_intent("This is not a request to erase my data."), "NOT_DELETE")
        self.assertEqual(rule_intent("Please delete the export file that still downloads."), "DELETE")
        self.assertEqual(rule_intent("このメールアドレスに関連するアカウントの完全な削除を依頼します。"), "DELETE")

    def test_vote_needs_a_strict_majority_for_non_deletion(self):
        self.assertEqual(vote(["NOT_DELETE", "NOT_DELETE", "DELETE"]), "NOT_DELETE")
        self.assertEqual(vote(["NOT_DELETE", "DELETE", "DELETE"]), "DELETE")


class Transcript(unittest.TestCase):
    def test_merged_table_rows_keep_each_email_with_its_own_id(self):
        recs = parse_transcript("Archives | backup_7 | a@example.org | eu | backup_8 | b@example.org | us | "
                                "backup_9 | broken-email | us")
        by_id = {r["resource_id"]: r for r in recs}
        self.assertEqual(by_id["backup_7"]["email"], "a@example.org")
        self.assertEqual(by_id["backup_8"]["email"], "b@example.org")
        self.assertIsNone(by_id["backup_9"]["email"])
        self.assertEqual(by_id["backup_7"]["state"], "RESTORABLE")

    def test_reference_page_without_contact_details_is_a_tombstone(self):
        recs = parse_transcript("Customer Reference Registry\nuser_17 | Retained reference\nCustomer key")
        self.assertEqual(recs[0]["record_type"], "ACCOUNT_TOMBSTONE")
        self.assertEqual(recs[0]["customer_reference"], "user_17")

    def test_stopped_connector_is_disabled(self):
        recs = parse_transcript("data_flow_5 | warehouse | x@example.org | Paused")
        self.assertEqual(recs[0]["state"], "DISABLED")


class Ensembles(unittest.TestCase):
    def test_union_keeps_records_either_model_found_and_fills_gaps(self):
        a = [{"resource_id": "support_1", "email": None, "state": "PRESENT"}]
        b = [{"resource_id": "support_1", "email": "k@example.org", "state": "PRESENT"},
             {"resource_id": None, "business_reference": "CS-77", "email": None}]
        merged = merge_records(a, b)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["email"], "k@example.org")


class Matching(unittest.TestCase):
    def test_a_single_known_id_one_edit_away_is_accepted(self):
        self.assertEqual(snap_id("account_5782", {"account_5762", "search_5782"}), "account_5762")
        self.assertEqual(snap_id("account_5782", {"account_5762", "account_5781"}), "account_5782")  # ambiguous

    def test_batch_offsets(self):
        primary = act("DELETE_RECORD", "account_service", "account_1")
        self.assertEqual(plausibility(primary, 55), 2)          # 55 min before reported_at
        self.assertEqual(plausibility(primary, -60), 0)
        failed = act("REDACT_RECORD", "support_platform", "support_1", "FAILED")
        self.assertEqual(plausibility(failed, -90), 2)          # failures land after reported_at
        self.assertTrue(claim_matches([act("DELETE_RECORD", "account_service", "account_1", "PARTIAL")],
                                      "PARTIALLY_COMPLETE"))


if __name__ == "__main__":
    unittest.main()
