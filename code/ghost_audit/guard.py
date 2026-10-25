"""Completion guard: the last check before a decision may leave the pipeline.

A deletion is only reported COMPLETE when the evidence proves it. This module
re-checks every decided case against invariants that do not depend on how the
decision was reached (rules, a model, or a tampered result):

* COMPLETE   - verification steps only, each citing an evidence *file* that
               exists and was captured at or before ``audit_at``. A workflow
               activity or audit-log record is the claim being audited, so it is
               never accepted as proof of a compliant state.
* INCOMPLETE - at least one finding; every finding cites a participant-visible
               id (file, activity or audit record) available at ``audit_at``;
               remediation and post-remediation checks carry no evidence; every
               remediated resource has a verification step.
* NOT_APPLICABLE - only when the request does not authorise deletion; no steps.

``check`` returns the violated invariants (empty = the case may be published).
``main.py`` refuses to write ``output.csv`` if any case violates one.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .policy import FINDING_OPS, OP_STATE, REMEDIATION_OPS, VERIFICATION_OPS

PHASE_OPS = {"FINDING": FINDING_OPS, "REMEDIATION": REMEDIATION_OPS, "VERIFICATION": VERIFICATION_OPS}
PHASE_RANK = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}


def evidence_index(dataset: Path) -> dict[str, str]:
    """Every citable id -> the instant it became available."""
    idx = {r["file_id"]: r["captured_at"] for r in csv.DictReader(open(dataset / "evidence_files.csv"))}
    idx.update({r["activity_id"]: r["event_time"] for r in csv.DictReader(open(dataset / "system_activity.csv"))})
    idx.update({r["audit_id"]: r["event_time"] for r in csv.DictReader(open(dataset / "user_audit_log.csv"))})
    return idx


def check(status: str, steps: list[dict], audit_at: str, intent: str, evidence: dict[str, str]) -> list[str]:
    """Violated invariants for one decided case (``steps`` as written to output.csv)."""
    v = []
    if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
        return [f"unknown status {status!r}"]
    if status == "NOT_APPLICABLE":
        if steps:
            v.append("NOT_APPLICABLE case carries steps")
        if intent == "DELETE":
            v.append("request authorises deletion but the case was not audited")
        return v
    if intent != "DELETE":
        v.append(f"{status} decision for a request that does not authorise deletion")
    if not steps:
        v.append(f"{status} decision without any step")
    last_rank = 0
    for i, s in enumerate(steps, 1):
        phase, op, ev = s.get("phase"), s.get("operation"), s.get("evidence_id", "")
        if s.get("step_no") != i:
            v.append(f"step {i}: numbering is not consecutive")
        if op not in PHASE_OPS.get(phase, []):
            v.append(f"step {i}: {op} is not allowed in phase {phase}")
            continue
        if OP_STATE[op] != s.get("state"):
            v.append(f"step {i}: {op} must have state {OP_STATE[op]}")
        if PHASE_RANK[phase] < last_rank:
            v.append(f"step {i}: phase {phase} after a later phase")
        last_rank = max(last_rank, PHASE_RANK[phase])
        cited = phase == "FINDING" or (status == "COMPLETE" and phase == "VERIFICATION")
        if cited:
            if not ev:
                v.append(f"step {i}: {op} on {s.get('resource_id')} cites no evidence")
            elif ev not in evidence:
                v.append(f"step {i}: cites {ev}, which does not exist")
            elif evidence[ev] > audit_at:
                v.append(f"step {i}: cites {ev}, captured {evidence[ev]} after audit_at {audit_at}")
            elif status == "COMPLETE" and not ev.startswith("evidence_"):
                v.append(f"step {i}: {op} rests on {ev}, a workflow/audit record - a claim, not proof")
        elif ev:
            v.append(f"step {i}: future {phase.lower()} step carries evidence {ev}")
    if status == "COMPLETE" and any(s.get("phase") != "VERIFICATION" for s in steps):
        v.append("COMPLETE decision contains findings or remediation")
    if status == "INCOMPLETE":
        if not any(s.get("phase") == "FINDING" for s in steps):
            v.append("INCOMPLETE decision without a finding")
        fixed = {s["resource_id"] for s in steps if s.get("phase") == "REMEDIATION"}
        checked = {s["resource_id"] for s in steps if s.get("phase") == "VERIFICATION"}
        for rid in sorted(fixed - checked):
            v.append(f"remediation of {rid} has no verification check")
    return v
