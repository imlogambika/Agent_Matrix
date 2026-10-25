"""Escalation layer: say where the audit is unsure, and what resolves it.

The audit never guesses silently.  After a case is decided, ``assess`` checks it
for the situations in which the evidence cannot settle the answer on its own and
tags each one with how it is resolved:

* ``auto``   - already resolved by a second opinion inside the pipeline (intent
               vote, a misread id snapped to the single known id);
* ``review`` - a person (or a stronger model with the evidence pack) should look:
               an unowned record that two cases could equally own, a COMPLETE
               verification backed only by a workflow or catalog record, data that
               reappears in evidence captured after the audit;
* ``info``   - context for the reviewer (a record no visible request can own).

``output.csv`` is never changed by this layer; ``escalation_report.md`` lists it.
"""
from __future__ import annotations

from collections import Counter

COMPLIANT_FOR = {  # verification operation -> what a later observation must show to undo it
    "VERIFY_RECORD_ABSENT": lambda o: o.present,
    "VERIFY_RECORD_REDACTED": lambda o: bool(o.pii_fields),
    "VERIFY_RECORD_ANONYMIZED": lambda o: bool(o.pii_fields),
    "VERIFY_DATA_FLOW_DISABLED": lambda o: o.enabled is True,
    "VERIFY_RESTORE_BLOCKED": lambda o: o.restore_allowed is True,
}
RESOLUTION = {
    "BATCH_TIE": ("review", "Ask the deletion service for the worker job id of the batch, or confirm ownership "
                            "manually; the evidence names no owner and both cases fit the timing equally."),
    "WEAK_VERIFICATION": ("review", "Re-capture the system (lookup or snapshot) before closing the case: the "
                                    "check is backed only by a workflow or catalog record."),
    "POST_AUDIT_REAPPEARANCE": ("review", "Ghost data: the record is compliant at audit_at but evidence captured "
                                          "later shows it again. Re-open the case and find the recreating flow."),
    "GUARD_REJECTED": ("review", "The completion guard found the decision unsupported by admissible evidence; the "
                                 "run refuses to publish output.csv until the case is fixed or reviewed."),
    "INTENT_FALLBACK": ("review", "A model gave no valid answer (server down or malformed output), so the rule "
                                  "classifier decided alone; confirm the request really asks for deletion."),
    "IDENTITY_UNRESOLVED": ("review", "The request asks for deletion but its email matches no known identity; "
                                      "verify the requester before closing the request as not applicable."),
    "INTENT_SPLIT": ("auto", "Resolved by the majority of two models and the rule classifier."),
    "SCREEN_READ_REPAIRED": ("auto", "A misread identifier was snapped to the only known id one character away."),
    "UNOWNED_BATCH_RECORD": ("info", "A worker touched this record in the case's batch, but no visible request can "
                                     "own it; it most likely belongs to a request outside the files."),
}


def assess(result, cl, store, linker, intent_info) -> list[dict]:
    req = cl.request
    out = []

    def flag(signal, resource="", detail=""):
        severity, resolution = RESOLUTION[signal]
        out.append({"signal": signal, "severity": severity, "resource": resource, "detail": detail,
                    "resolution": resolution})

    for rid, rivals in sorted(cl.ties.items()):
        flag("BATCH_TIE", rid, f"equally plausible for {', '.join(rivals)}")
    votes = intent_info.get("votes") or {}
    if intent_info.get("degraded"):
        flag("INTENT_FALLBACK", "", intent_info.get("explanation", ""))
    elif len(set(votes.values())) > 1:
        flag("INTENT_SPLIT", "", ", ".join(f"{k}={v}" for k, v in votes.items()))
    for step in result.steps:
        if result.status == "COMPLETE" and not step.evidence_id.startswith("evidence_"):
            flag("WEAK_VERIFICATION", step.resource_id, f"{step.operation} cites {step.evidence_id or 'nothing'}")
        undo = COMPLIANT_FOR.get(step.operation)
        if step.phase == "VERIFICATION" and undo and result.status == "COMPLETE":
            later = [o for o in store.by_resource.get(step.resource_id, [])
                     if (o.ts > req.audit_at or store.captured_at(o.file_id) > req.audit_at) and undo(o)]
            if later:
                o = max(later, key=lambda x: x.ts)
                flag("POST_AUDIT_REAPPEARANCE", step.resource_id, f"{o.file_id} at {o.ts}")
    for rid in sorted(cl.resources()):
        for o in store.by_resource.get(rid, []):
            if o.extra.get("read_as"):
                flag("SCREEN_READ_REPAIRED", rid, f"read as {o.extra['read_as']} in {o.file_id}")
                break
    for rid, cands in sorted(linker.leftovers.items()):
        if req.case_id in cands:
            flag("UNOWNED_BATCH_RECORD", rid, f"candidates {', '.join(cands)}")
    return out


def assess_unaudited(intent_info: dict, identity_resolved: bool = True) -> list[dict]:
    """Flags for a case that never reached the audit (not a deletion, or no identity)."""
    out = []
    for signal, cond, detail in (("IDENTITY_UNRESOLVED", intent_info.get("intent") == "DELETE" and
                                  not identity_resolved, "request email resolves to no user"),
                                 ("INTENT_FALLBACK", intent_info.get("degraded"), intent_info.get("explanation", ""))):
        if cond:
            severity, resolution = RESOLUTION[signal]
            out.append({"signal": signal, "severity": severity, "resource": "", "detail": detail,
                        "resolution": resolution})
    return out


def render(results: list, n_requests: int) -> str:
    flags = [(r.case_id, f) for r in results for f in r.trace.get("review", [])]
    by_signal = Counter(f["signal"] for _, f in flags)
    review_cases = sorted({c for c, f in flags if f["severity"] == "review"}, key=lambda c: int(c.split("_")[1]))
    lines = ["# Escalation Report", "",
             f"Generated by the audit run over {n_requests} requests. This layer never changes `output.csv`; it "
             "lists the decisions the evidence could not settle on its own and how each is resolved.", "",
             f"**{len(review_cases)} of {n_requests} cases need a reviewer**; every other flag was resolved "
             "automatically or is context.", "",
             "| Signal | Severity | Cases | How it is resolved |", "| --- | --- | ---: | --- |"]
    for signal, (severity, resolution) in RESOLUTION.items():
        n = len({c for c, f in flags if f["signal"] == signal})
        lines.append(f"| `{signal}` | {severity} | {n} | {resolution} |")
    lines += ["", "## Cases for review", "", "| Case | Status | Signal | Resource | Detail |",
              "| --- | --- | --- | --- | --- |"]
    status = {r.case_id: r.status for r in results}
    for c, f in flags:
        if f["severity"] == "review":
            lines.append(f"| {c} | {status[c]} | `{f['signal']}` | `{f['resource']}` | {f['detail']} |")
    lines += ["", f"Total flags: {sum(by_signal.values())} ({', '.join(f'{k}: {v}' for k, v in by_signal.items())}).",
              ""]
    return "\n".join(lines)
