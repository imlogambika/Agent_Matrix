"""Team-authored evaluation cases.

Builds a small synthetic Northstar dataset (identities, activity, rules, SQL /
JSON / log evidence) in a temporary directory, runs the real pipeline on it with
model calls disabled, and scores the result against hand-written gold answers.

Each case isolates one failure mode that the 32 public samples do not test on
their own:

  C1  replica drift          - profile deleted on the primary, still on a regional replica
  C2  late evidence          - a snapshot exported after audit_at shows the record; it must be ignored
  C3  historical identifier  - request uses an old email; record keyed by the old email hash;
                               a look-alike email in the same index belongs to someone else
  C4  non-deletion request   - Spanish access request, must be NOT_APPLICABLE with []
  C5  tombstone with PII     - deleted-account reference still holds name/email; keep user_id only
  C6  resurrecting connector - active sync re-created a deleted search entry; disable it first
  C7  decoy batch activity   - another subject's profile is processed in the same batch window

    python3 code/evaluation/custom_cases.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(HERE))

from evaluate import score  # noqa: E402
from ghost_audit.data import load_requests  # noqa: E402
from ghost_audit.llm import LLMClient  # noqa: E402
from ghost_audit.pipeline import AuditPipeline  # noqa: E402

RULES = CODE.parent / "dataset" / "deletion_rules.csv"


def h(email: str) -> str:
    return hashlib.sha256(email.lower().encode()).hexdigest()


USERS = [
    # user_id, name, email, phone
    ("user_901", "Mira Sandoval", "mira.sandoval@example.org", "+34 600 100 901"),
    ("user_902", "Tobias Krell", "t.krell@example.org", "+49 151 100 902"),
    ("user_903", "Ines Vartan", "ines.vartan@example.org", "+33 6 10 09 03 00"),   # old: ines.v@example.org
    ("user_904", "Ramon Ochoa", "ramon.ochoa@example.org", "+52 55 1000 9040"),
    ("user_905", "Hana Lindqvist", "hana.lindqvist@example.org", "+46 70 100 90 50"),
    ("user_906", "Kojo Badu", "kojo.badu@example.org", "+233 24 100 9060"),
    ("user_907", "Lea Strom", "lea.strom@example.org", "+46 70 100 90 70"),
    ("user_908", "Lea Stromberg", "lea.stromberg@example.org", "+46 70 100 90 80"),  # C7 decoy owner
]

REQUESTS = [
    # case, email, requested, status, reported, audit, text
    ("case_901", "mira.sandoval@example.org", "2026-05-01T10:00:00Z", "COMPLETE", "2026-05-02T10:00:00Z",
     "2026-05-10T10:00:00Z", "Please delete my account and all personal data."),
    ("case_902", "t.krell@example.org", "2026-05-03T10:00:00Z", "PARTIALLY_COMPLETE", "2026-05-04T10:00:00Z",
     "2026-05-12T10:00:00Z", "Bitte löschen Sie mein Konto vollständig."),
    ("case_903", "ines.v@example.org", "2026-05-05T10:00:00Z", "COMPLETE", "2026-05-06T10:00:00Z",
     "2026-05-14T10:00:00Z", "I changed my email last year. Please erase everything tied to the old address too."),
    ("case_904", "ramon.ochoa@example.org", "2026-05-07T10:00:00Z", "NOT_STARTED", "2026-05-08T10:00:00Z",
     "2026-05-16T10:00:00Z", "¿Pueden enviarme una copia de mis datos? No quiero cerrar la cuenta."),
    ("case_905", "hana.lindqvist@example.org", "2026-05-09T10:00:00Z", "COMPLETE", "2026-05-10T10:00:00Z",
     "2026-05-18T10:00:00Z", "Ta bort mitt konto, tack. Please remove my account."),
    ("case_906", "kojo.badu@example.org", "2026-05-11T10:00:00Z", "COMPLETE", "2026-05-12T10:00:00Z",
     "2026-05-20T10:00:00Z", "I deleted my account but my profile keeps reappearing in search. Please delete it for good."),
    ("case_907", "lea.strom@example.org", "2026-05-13T10:00:00Z", "COMPLETE", "2026-05-14T10:00:00Z",
     "2026-05-22T10:00:00Z", "Please permanently delete my account."),
]

ACTIVITY = [
    # primary-profile deletions ~1h before reported_at (the workflow's claims)
    ("act_1", "2026-05-02T09:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9011", "SUCCESS"),
    ("act_2", "2026-05-04T09:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9021", "PARTIAL"),
    ("act_3", "2026-05-06T09:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9031", "SUCCESS"),
    ("act_4", "2026-05-06T10:50:00Z", "DELETE_RECORD", "deletion_worker", "analytics_platform", "analytics_9032",
     "FAILED"),
    ("act_5", "2026-05-10T09:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9051", "SUCCESS"),
    ("act_6", "2026-05-10T11:00:00Z", "REDACT_RECORD", "deletion_worker", "identity_service", "tombstone_9052",
     "FAILED"),
    ("act_7", "2026-05-12T09:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9061", "SUCCESS"),
    ("act_8", "2026-05-12T09:20:00Z", "DELETE_RECORD", "deletion_worker", "search_service", "search_9062", "SUCCESS"),
    ("act_9", "2026-05-13T07:00:00Z", "SYNC_RECORD", "integration_service", "search_service", "data_flow_9063",
     "SUCCESS"),
    ("act_10", "2026-05-14T09:00:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9071", "SUCCESS"),
    # C7 decoy: same batch window, but the profile belongs to user_908 (look-alike name)
    ("act_11", "2026-05-14T09:05:00Z", "DELETE_RECORD", "deletion_worker", "account_service", "account_9081",
     "FAILED"),
]


def write_dataset(root: Path) -> None:
    ev = root / "evidence"
    for sub in ("databases", "records", "logs"):
        (ev / sub).mkdir(parents=True)
    shutil.copy(RULES, root / "deletion_rules.csv")
    with open(root / "user_identities.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["user_id", "name", "email", "phone", "email_hash", "updated_at"])
        for uid, name, email, phone in USERS:
            w.writerow([uid, name, email, phone, h(email), "2026-01-01T00:00:00Z"])
    with open(root / "user_audit_log.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"])
        w.writerow(["audit_9001", "user_903", "2025-11-02T10:00:00Z", "email", "ines.v@example.org",
                    "ines.vartan@example.org"])
        w.writerow(["audit_9002", "user_903", "2025-11-02T10:00:00Z", "email_hash", h("ines.v@example.org"),
                    h("ines.vartan@example.org")])
    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        with open(root / name, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at",
                        "request_text"])
            if name == "deletion_requests.csv":
                w.writerows(REQUESTS)
    with open(root / "system_activity.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
                    "reported_result", "details"])
        for a in ACTIVITY:
            w.writerow(list(a) + [f"Deletion worker processed {a[5]}."])

    files = []

    # C1 + C3: regional replica capture (primary vs replica drift)
    sql = ["BEGIN TRANSACTION;"]
    for t in ("customer_data_primary", "customer_data_replica_eu_west"):
        sql.append(f'CREATE TABLE "{t}" (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL, '
                   'collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);')

    def row(t, rid, system, coll, rec):
        body = json.dumps({"resource_id": rid, **rec}).replace("'", "''")
        sql.append(f"INSERT INTO \"{t}\" VALUES('{rid}','{system}','{coll}','{body}','2026-05-09T08:00:00Z');")

    row("customer_data_replica_eu_west", "account_9011", "account_service", "customer_profiles",
        {"customer_reference": "user_901", "full_name": "Mira Sandoval", "email": "mira.sandoval@example.org",
         "phone": "+34 600 100 901", "account_status": "ACTIVE"})
    for t in ("customer_data_primary", "customer_data_replica_eu_west"):
        row(t, "audit_record_9012", "security_service", "security_audit_events",
            {"subject_reference": "user_901", "event_name": "account_deletion_reviewed"})
    sql.append("COMMIT;")
    (ev / "databases" / "regional_capture.sql").write_text(
        "-- Captured at: 2026-05-09T08:00:00Z\n" + "\n".join(sql))
    files.append(("evidence_901", "evidence/databases/regional_capture.sql", "replica_capture",
                  "2026-05-09T08:00:00Z"))

    # C2: readback export before audit (404) and a bundle exported after audit (present)
    (ev / "records" / "readback_0511.json").write_text(json.dumps({
        "exported_at": "2026-05-11T09:00:00Z", "region": "global", "service_exports": {
            "account_service": {"profile_lookups": [
                {"observed_at": "2026-05-11T08:00:00Z", "profile": None, "resource_id": "account_9021",
                 "status_code": 404}]},
            "search_service": {"index_queries": [
                {"observed_at": "2026-05-11T08:05:00Z", "query": "t.krell@example.org", "hits": {"documents": []},
                 "resource_id": "search_9022"}]}}}))
    files.append(("evidence_902", "evidence/records/readback_0511.json", "service_resource_export",
                  "2026-05-11T09:00:00Z"))
    (ev / "records" / "bundle_late.json").write_text(json.dumps({
        "exported_at": "2026-05-15T09:00:00Z", "snapshots": [{
            "captured_at": "2026-05-14T09:00:00Z", "data_store": "search_service",
            "collection": "customer_search_documents", "records": [
                {"resource_id": "search_9022", "index_name": "customer_directory_v3",
                 "document": {"display_name": "Tobias Krell", "email": "t.krell@example.org",
                              "customer_reference": "user_902"}}]}]}))
    files.append(("evidence_903", "evidence/records/bundle_late.json", "records_assurance_snapshot_bundle",
                  "2026-05-15T09:00:00Z"))

    # C3: marketing contact keyed by the OLD email hash + look-alike search doc of someone else
    log = [
        "2026-05-13T08:00:00Z level=INFO service=log_router event=export_started",
        "2026-05-13T08:00:01Z level=DEBUG service=account_service trace_id=tr-1 event=profile_lookup_started "
        "lookup_key=\"ines.vartan@example.org\"",
        "2026-05-13T08:00:02Z level=INFO service=account_service trace_id=tr-1 resource_id=account_9031 "
        "event=profile_lookup_completed rows=0 http_status=404",
        f"2026-05-13T08:00:03Z level=DEBUG service=analytics_platform trace_id=tr-2 resource_id=analytics_9032 "
        f"event=contact_lookup_completed rows=1 email_opt_in=true correlation_key=\"{h('ines.v@example.org')}\"",
        "2026-05-13T08:00:04Z level=INFO service=search_service trace_id=tr-3 resource_id=search_9033 "
        "event=index_query_completed index=customer_directory_v3 result=hit correlation_key=\"ines.v.jr@example.org\"",
        # C5 (hana) and C6 (kojo)
        "2026-05-13T08:00:05Z level=INFO service=account_service trace_id=tr-4 resource_id=account_9051 "
        "event=profile_lookup_completed rows=0 http_status=404 correlation_key=\"hana.lindqvist@example.org\"",
    ]
    (ev / "logs" / "service_events.log").write_text("\n".join(log) + "\n")
    files.append(("evidence_904", "evidence/logs/service_events.log", "central_observability_export",
                  "2026-05-13T08:01:00Z"))

    # C5: tombstone still holding PII
    (ev / "records" / "identity_bundle.json").write_text(json.dumps({
        "exported_at": "2026-05-17T09:00:00Z", "snapshots": [{
            "captured_at": "2026-05-17T09:00:00Z", "data_store": "identity_service",
            "collection": "deleted_customer_references", "records": [
                {"resource_id": "tombstone_9052", "subject_reference": "user_905", "name": "Hana Lindqvist",
                 "email": "hana.lindqvist@example.org", "phone": None, "email_hash": None,
                 "account_status": "DELETED"}]}]}))
    files.append(("evidence_905", "evidence/records/identity_bundle.json", "records_assurance_snapshot_bundle",
                  "2026-05-17T09:00:00Z"))

    # C6: connector still enabled, search entry re-created; C7 account 404
    (ev / "records" / "readback_0519.json").write_text(json.dumps({
        "exported_at": "2026-05-21T09:00:00Z", "region": "global", "service_exports": {
            "account_service": {"profile_lookups": [
                {"observed_at": "2026-05-19T08:00:00Z", "profile": None, "resource_id": "account_9061",
                 "status_code": 404}]},
            "integration_service": {"connectors": [
                {"observed_at": "2026-05-19T08:01:00Z", "resource_id": "data_flow_9063",
                 "connector_name": "search_profile_indexer", "customer_email": "kojo.badu@example.org",
                 "customer_reference": None, "enabled": True, "last_delivery_status": "delivered"}]},
            "search_service": {"index_queries": [
                {"observed_at": "2026-05-19T08:02:00Z", "query": "kojo.badu@example.org",
                 "resource_id": "search_9062", "hits": {"documents": [
                     {"resource_id": "search_9062", "display_name": "Kojo Badu", "email": "kojo.badu@example.org",
                      "customer_reference": None, "email_hash": None, "phone": None}]}}]}}}))
    files.append(("evidence_906", "evidence/records/readback_0519.json", "service_resource_export",
                  "2026-05-19T09:00:00Z"))
    (ev / "records" / "readback_0521.json").write_text(json.dumps({
        "exported_at": "2026-05-21T09:30:00Z", "region": "global", "service_exports": {
            "account_service": {"profile_lookups": [
                {"observed_at": "2026-05-21T08:00:00Z", "profile": None, "resource_id": "account_9071",
                 "status_code": 404},
                {"observed_at": "2026-05-21T08:01:00Z", "resource_id": "account_9081", "status_code": 200,
                 "profile": {"email": "lea.stromberg@example.org", "full_name": "Lea Stromberg",
                             "customer_reference": None, "phone": None, "email_hash": None}}]}}}))
    files.append(("evidence_907", "evidence/records/readback_0521.json", "service_resource_export",
                  "2026-05-21T09:30:00Z"))

    with open(root / "evidence_files.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file_id", "file_path", "origin", "captured_at"])
        w.writerows(files)


def step(n, phase, system, rid, op, state, ev=""):
    return {"step_no": n, "phase": phase, "system_id": system, "resource_id": rid, "operation": op, "state": state,
            "evidence_id": ev}


GOLD = {
    "case_901": ("INCOMPLETE", [
        step(1, "FINDING", "account_service", "account_9011", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_901"),
        step(2, "REMEDIATION", "account_service", "account_9011", "DELETE_RECORD", "ABSENT"),
        step(3, "VERIFICATION", "account_service", "account_9011", "VERIFY_RECORD_ABSENT", "ABSENT"),
        step(4, "VERIFICATION", "security_service", "audit_record_9012", "VERIFY_RECORD_PRESENT", "PRESENT")]),
    "case_902": ("COMPLETE", [
        step(1, "VERIFICATION", "account_service", "account_9021", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_902"),
        step(2, "VERIFICATION", "search_service", "search_9022", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_902")]),
    "case_903": ("INCOMPLETE", [
        step(1, "FINDING", "analytics_platform", "analytics_9032", "REPORT_REMAINING_RECORD", "PRESENT",
             "evidence_904"),
        step(2, "REMEDIATION", "analytics_platform", "analytics_9032", "DELETE_RECORD", "ABSENT"),
        step(3, "VERIFICATION", "account_service", "account_9031", "VERIFY_RECORD_ABSENT", "ABSENT"),
        step(4, "VERIFICATION", "analytics_platform", "analytics_9032", "VERIFY_RECORD_ABSENT", "ABSENT")]),
    "case_904": ("NOT_APPLICABLE", []),
    "case_905": ("INCOMPLETE", [
        step(1, "FINDING", "identity_service", "tombstone_9052", "REPORT_EXPOSED_PII", "PRESENT", "evidence_905"),
        step(2, "REMEDIATION", "identity_service", "tombstone_9052", "REDACT_RECORD", "REDACTED"),
        step(3, "VERIFICATION", "account_service", "account_9051", "VERIFY_RECORD_ABSENT", "ABSENT"),
        step(4, "VERIFICATION", "identity_service", "tombstone_9052", "VERIFY_RECORD_REDACTED", "REDACTED")]),
    "case_906": ("INCOMPLETE", [
        step(1, "FINDING", "search_service", "search_9062", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_906"),
        step(2, "FINDING", "integration_service", "data_flow_9063", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE",
             "evidence_906"),
        step(3, "REMEDIATION", "integration_service", "data_flow_9063", "DISABLE_DATA_FLOW", "DISABLED"),
        step(4, "REMEDIATION", "search_service", "search_9062", "DELETE_RECORD", "ABSENT"),
        step(5, "VERIFICATION", "account_service", "account_9061", "VERIFY_RECORD_ABSENT", "ABSENT"),
        step(6, "VERIFICATION", "search_service", "search_9062", "VERIFY_RECORD_ABSENT", "ABSENT"),
        step(7, "VERIFICATION", "integration_service", "data_flow_9063", "VERIFY_DATA_FLOW_DISABLED", "DISABLED")]),
    "case_907": ("COMPLETE", [
        step(1, "VERIFICATION", "account_service", "account_9071", "VERIFY_RECORD_ABSENT", "ABSENT",
             "evidence_907")]),
}

DESCRIPTIONS = {
    "case_901": "C1 replica drift",
    "case_902": "C2 late evidence ignored",
    "case_903": "C3 historical identifier + look-alike decoy",
    "case_904": "C4 non-deletion request (Spanish)",
    "case_905": "C5 tombstone still holds PII",
    "case_906": "C6 resurrecting connector",
    "case_907": "C7 decoy profile in the same batch",
}


def run() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="ghost_custom_"))
    try:
        write_dataset(tmp)
        llm = LLMClient(tmp / "cache.json", tmp / "usage.jsonl", backend="offline")
        pipeline = AuditPipeline(tmp, llm, log=lambda *a: None)
        results = pipeline.run(load_requests(tmp / "deletion_requests.csv"))
        pred = {r.case_id: (r.status, [s.as_dict(i + 1) for i, s in enumerate(r.steps)]) for r in results}
        return {"pred": pred, "metrics": score(pred, GOLD)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    out = run()
    m = out["metrics"]
    print(f"{'case':10s} {'scenario':46s} {'gold':15s} {'pred':15s} exact")
    for cid, info in m["per_case"].items():
        print(f"{cid:10s} {DESCRIPTIONS[cid]:46s} {info['gold']:15s} {info['pred']:15s} {info['exact']}")
    for k in ("status_accuracy", "step_f1", "evidence_accuracy", "sequence_accuracy"):
        print(f"{k:22s} {m[k]:.4f}")
    bad = [c for c, i in m["per_case"].items() if not i["exact"]]
    for c in bad:
        print(f"\n{c} predicted:")
        for s in out["pred"][c][1]:
            print("   ", s)
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
