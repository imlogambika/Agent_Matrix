"""Check output.csv against every structural rule of the submission contract.

    python3 code/evaluation/validate_output.py [output.csv] [requests.csv]
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "code"))

from ghost_audit.policy import FINDING_OPS, OP_STATE, REMEDIATION_OPS, VERIFICATION_OPS, order_steps  # noqa: E402
from ghost_audit.models import Step  # noqa: E402

PHASE_OPS = {"FINDING": FINDING_OPS, "REMEDIATION": REMEDIATION_OPS, "VERIFICATION": VERIFICATION_OPS}
KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def evidence_index(dataset: Path) -> dict:
    """evidence id -> the instant it became available (file captured_at, activity or audit time)."""
    idx = {r["file_id"]: r["captured_at"] for r in csv.DictReader(open(dataset / "evidence_files.csv"))}
    idx.update({r["activity_id"]: r["event_time"] for r in csv.DictReader(open(dataset / "system_activity.csv"))})
    idx.update({r["audit_id"]: r["event_time"] for r in csv.DictReader(open(dataset / "user_audit_log.csv"))})
    return idx


def validate(out_path: Path, req_path: Path) -> list[str]:
    errors = []
    evidence = evidence_index(req_path.parent)
    audit_at = {r["case_id"]: r["audit_at"] for r in csv.DictReader(open(req_path, encoding="utf-8"))}
    with open(out_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)
    if header != ["case_id", "deletion_status", "steps_json"]:
        errors.append(f"bad header {header}")
    expected = [r["case_id"] for r in csv.DictReader(open(req_path, encoding="utf-8"))]
    ids = [r[0] for r in rows]
    if sorted(ids) != sorted(expected) or len(ids) != len(set(ids)):
        errors.append("case ids differ from the request file or contain duplicates")
    if ids != sorted(ids, key=lambda c: int(c.split("_")[1])):
        errors.append("rows are not sorted by case_id")
    for cid, status, raw in rows:
        try:
            steps = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"{cid}: steps_json is not valid JSON")
            continue
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errors.append(f"{cid}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps != []:
            errors.append(f"{cid}: NOT_APPLICABLE must have []")
        if status != "NOT_APPLICABLE" and not steps:
            errors.append(f"{cid}: {status} without steps")
        for i, s in enumerate(steps, 1):
            if set(s) != KEYS:
                errors.append(f"{cid}: step {i} keys {sorted(s)}")
                continue
            if s["step_no"] != i:
                errors.append(f"{cid}: step numbers not consecutive at {i}")
            if s["operation"] not in PHASE_OPS.get(s["phase"], []):
                errors.append(f"{cid}: {s['operation']} not allowed in {s['phase']}")
            if OP_STATE.get(s["operation"]) != s["state"]:
                errors.append(f"{cid}: {s['operation']} must have state {OP_STATE.get(s['operation'])}")
            ev = s["evidence_id"]
            if ev and ev not in evidence:
                errors.append(f"{cid}: step {i} cites unknown evidence {ev}")
            elif ev and evidence[ev] > audit_at.get(cid, ""):
                errors.append(f"{cid}: step {i} cites {ev} captured {evidence[ev]} after audit_at {audit_at[cid]}")
            if status == "COMPLETE" and (s["phase"] != "VERIFICATION" or not s["evidence_id"]):
                errors.append(f"{cid}: COMPLETE allows only evidence-backed verification")
            if status == "INCOMPLETE":
                if s["phase"] == "FINDING" and not s["evidence_id"]:
                    errors.append(f"{cid}: finding without evidence")
                if s["phase"] != "FINDING" and s["evidence_id"]:
                    errors.append(f"{cid}: future step with evidence")
        if status == "INCOMPLETE" and not any(s.get("phase") == "FINDING" for s in steps):
            errors.append(f"{cid}: INCOMPLETE without findings")
        objs = [Step(s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"], s["evidence_id"])
                for s in steps if set(s) == KEYS]
        if objs != order_steps(objs):
            errors.append(f"{cid}: steps not in contract order")
    return errors


def main(argv) -> int:
    out = Path(argv[1]) if len(argv) > 1 else REPO / "output.csv"
    req = Path(argv[2]) if len(argv) > 2 else REPO / "dataset" / "deletion_requests.csv"
    errors = validate(out, req)
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    cited = sum(1 for r in rows for s in json.loads(r["steps_json"]) if s["evidence_id"])
    print(f"{out.name}: {len(rows)} rows, {cited} evidence citations checked, {len(errors)} contract violations")
    for e in errors[:50]:
        print("  ", e)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
