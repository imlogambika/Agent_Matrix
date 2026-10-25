"""End-to-end audit: requests in, ordered steps out.

Each case is worked by five agents under one orchestrator:

    intent-classifier      does the request authorise deletion?          (open models + rules vote)
    identity-resolver      every identifier the subject ever had          (identity snapshot + audit log)
    evidence-investigator  which records in which sources are theirs      (parsed evidence + worker batch)
    historical-auditor     the state of each record at audit_at           (admissible evidence only)
    decision-verifier      policy, steps, completion guard, escalation    (deterministic)

The decision itself is deterministic code; models only read language and pixels.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import guard
from .data import IdentityResolver, load_activities, load_requests, load_rules
from .evidence.store import EvidenceStore
from .intent import classify
from .linker import Linker
from .llm import LLMClient
from .models import Request, Step
from .policy import OP_STATE, Policy, order_steps
from .review import assess, assess_unaudited
from .state import reconstruct
from .telemetry import Telemetry, text_ref
from .util import norm_email


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list
    user_id: str = ""
    trace: dict = field(default_factory=dict)

    def steps_json(self) -> str:
        return json.dumps([s.as_dict(i + 1) for i, s in enumerate(self.steps)], ensure_ascii=False,
                          separators=(",", ":"))

    def step_dicts(self) -> list[dict]:
        return [s.as_dict(i + 1) for i, s in enumerate(self.steps)]


class AuditPipeline:
    def __init__(self, dataset_dir: Path, llm: LLMClient, log=print, extractions: dict | None = None,
                 telemetry: Telemetry | None = None):
        self.dataset_dir = dataset_dir
        self.log = log
        self.llm = llm
        self.tel = telemetry or Telemetry(enabled=False)
        if self.tel.active:
            llm.telemetry = self.tel
        self.activities = load_activities(dataset_dir / "system_activity.csv")
        self.resolver = IdentityResolver(dataset_dir / "user_identities.csv", dataset_dir / "user_audit_log.csv")
        self.policy = Policy(load_rules(dataset_dir / "deletion_rules.csv"))
        self.evidence = guard.evidence_index(dataset_dir)
        with self.tel.session("ingest", "audit-orchestrator", "Read every evidence file once for all cases"):
            with self.tel.agent("evidence-investigator", "Parse SQL, JSON, logs, text, PDFs and screenshots") as a:
                self.store = EvidenceStore(dataset_dir, self.activities, llm, log, extractions)
                kinds = Counter(Path(m["file_path"]).suffix.lstrip(".") for m in self.store.manifest.values())
                a.summary = (f"{len(self.store.manifest)} files, {len(self.store.observations)} observations, "
                             f"{len(self.store.scopes)} full captures")
                self.tel.tool("parse_evidence_files", {"files": len(self.store.manifest), "by_type": dict(kinds)},
                              {"observations": len(self.store.observations), "full_captures": len(self.store.scopes),
                               "unreadable_or_unlinkable": self.store.skipped})
        self.linker = Linker(self.resolver, self.store, self.activities)
        # every request the platform knows about takes part in batch matching,
        # so sample and evaluation cases never steal each other's resources
        self.all_requests = (load_requests(dataset_dir / "sample_deletion_requests.csv") +
                             load_requests(dataset_dir / "deletion_requests.csv"))
        self.log(f"[evidence] {len(self.store.observations)} observations, {len(self.store.scopes)} full captures")

    def run(self, requests: list[Request]) -> list[CaseResult]:
        with self.tel.session("intent", "audit-orchestrator", "Classify every unique request text once"):
            with self.tel.agent("intent-classifier", "Does each request authorise deletion?") as a:
                intents = classify(self.all_requests, self.llm, self.log)
                a.summary = f"{len({r.request_text for r in self.all_requests})} unique texts classified"
        authorised = []
        for r in self.all_requests:
            uid = self.resolver.resolve_email(r.email)
            if intents[r.case_id]["intent"] == "DELETE" and uid:
                authorised.append((r, uid))
        links = self.linker.link(authorised, has_findings=self._has_explicit_findings)
        results = []
        for r in requests:
            if r.case_id not in links:
                results.append(self.not_applicable(r, intents[r.case_id]))
            else:
                results.append(self.audit_case(links[r.case_id], intents[r.case_id]))
        return results

    # ------------------------------------------------------------------ cases
    def not_applicable(self, req: Request, intent: dict) -> CaseResult:
        uid = self.resolver.resolve_email(req.email) or ""
        result = CaseResult(req.case_id, "NOT_APPLICABLE", [], uid, trace={"intent": intent, "user_id": uid})
        with self.tel.session(req.case_id, "audit-orchestrator", f"Verify the deletion claimed for {req.case_id}",
                              claimed_status=req.reported_status) as root:
            with self.tel.agent("intent-classifier", "Does the request authorise deletion?") as a:
                self.tel.tool("classify_request", {"request": text_ref(req.request_text)}, self._intent_out(intent))
                a.summary = f"{intent['intent']} ({intent['confidence']} confidence)"
            reason = ("the request does not authorise deletion" if intent["intent"] != "DELETE"
                      else "the request email resolves to no known identity")
            result.trace["review"] = assess_unaudited(intent, identity_resolved=bool(uid))
            self._decide(req, result, intent, root, reason)
        return result

    def audit_case(self, cl, intent) -> CaseResult:
        req = cl.request
        tel = self.tel
        trace = {"intent": intent, "user_id": cl.user_id, "resources": {}}
        with tel.session(req.case_id, "audit-orchestrator", f"Verify the deletion claimed for {req.case_id}",
                         claimed_status=req.reported_status) as root:
            with tel.agent("intent-classifier", "Does the request authorise deletion?") as a:
                tel.tool("classify_request", {"request": text_ref(req.request_text)}, self._intent_out(intent))
                a.summary = f"{intent['intent']} ({intent['confidence']} confidence)"
            with tel.agent("identity-resolver", "Every identifier the subject ever held") as a:
                ident = self._identity_out(req, cl.user_id)
                trace["identity"] = ident
                tel.tool("resolve_identity", {"request_email": "masked", "case_id": req.case_id}, ident)
                a.summary = f"{cl.user_id} via {ident['matched_on']}"
            with tel.agent("evidence-investigator", "Find every record of the subject in every source") as a:
                found = self._evidence_out(cl)
                trace["evidence"] = found
                tel.tool("link_evidence", {"user_id": cl.user_id, "window": [req.requested_at, req.audit_at]}, found)
                a.summary = f"{len(cl.resources())} records in {len(found['files_cited'])} files"
            findings, remediation, verification = [], [], []
            primary = self._primary_file(cl)
            trace["primary_file"] = primary
            with tel.agent("historical-auditor", f"State of each record at audit_at {req.audit_at}") as a:
                for rid in sorted(cl.resources()):
                    acts = [x for x in cl.batch.get(rid, []) if x.event_time <= req.audit_at]
                    attempted = {x.event_type for x in acts}
                    st = reconstruct(rid, self.store, req.audit_at, attempted, cl.footprint, req.requested_at, primary)
                    if not st.evidence_id and acts:
                        # nothing but the workflow's own record mentions it: cite that activity
                        st.evidence_id = max(acts, key=lambda x: x.event_time).activity_id
                    if not st.evidence_id:
                        links = [x for t, r, u, x in self.linker.catalog_links if r == rid and u == cl.user_id
                                 and t <= req.audit_at]
                        st.evidence_id = links[-1] if links else ""
                    late = sum(1 for o in self.store.by_resource.get(rid, [])
                               if o.ts > req.audit_at or self.store.captured_at(o.file_id) > req.audit_at)
                    trace["resources"][rid] = {"category": st.category, "present": st.present,
                                               "pii": st.pii_exposed, "enabled": st.enabled,
                                               "restore": st.restore_allowed, "evidence": st.evidence_id,
                                               "evidence_at": self.evidence.get(st.evidence_id, ""),
                                               "explicit": rid in cl.explicit, "batch": sorted(attempted),
                                               "late_ignored": late}
                    tel.tool("reconstruct_state", {"resource_id": rid, "audit_at": req.audit_at},
                             trace["resources"][rid])
                    t = self.policy.treat(st)
                    for op, ev in t.findings:
                        findings.append(Step("FINDING", st.system, rid, op, OP_STATE[op], ev))
                    for op in t.remediation:
                        remediation.append(Step("REMEDIATION", st.system, rid, op, OP_STATE[op], ""))
                    for op, ev in t.verification:
                        verification.append(Step("VERIFICATION", st.system, rid, op, OP_STATE[op], ev))
                a.summary = f"{len(trace['resources'])} records reconstructed, {len(findings)} violations"
            if findings:
                status = "INCOMPLETE"
                for s in verification:
                    s.evidence_id = ""
                steps = findings + remediation + verification
            else:
                status = "COMPLETE"
                steps = verification
            result = CaseResult(req.case_id, status, order_steps(steps), cl.user_id, trace)
            trace["review"] = assess(result, cl, self.store, self.linker, intent)
            self._decide(req, result, intent, root, f"{len(findings)} policy violations at audit_at")
        return result

    def _decide(self, req: Request, result: CaseResult, intent: dict, root, basis: str) -> None:
        """decision-verifier: completion guard, escalation review, human hand-off."""
        tel = self.tel
        with tel.agent("decision-verifier", "Apply the policy, guard the decision, escalate doubt") as a:
            steps = result.step_dicts()
            phases = Counter(s["phase"] for s in steps)
            tel.tool("apply_policy", {"case_id": req.case_id, "basis": basis},
                     {"status": result.status, "findings": phases["FINDING"],
                      "remediation": phases["REMEDIATION"], "verification": phases["VERIFICATION"]})
            resolved = bool(result.user_id) or intent["intent"] != "DELETE"
            violations = guard.check(result.status, steps, req.audit_at,
                                     intent["intent"] if resolved else "UNRESOLVED", self.evidence)
            result.trace["guard"] = violations
            tel.hook("completion-guard", "pre_publish",
                     {"status": result.status, "claimed": req.reported_status, "steps": len(steps)},
                     "rejected" if violations else "success",
                     {"violations": violations, "checked": "evidence exists, predates audit_at, "
                                                            "COMPLETE rests on evidence files only"})
            if violations:
                result.trace.setdefault("review", []).append(
                    {"signal": "GUARD_REJECTED", "severity": "review", "resource": "",
                     "detail": "; ".join(violations)[:300], "resolution": "fix or review before publishing"})
            review = [f for f in result.trace.get("review", []) if f["severity"] == "review"]
            tel.hook("escalation-review", "post_decision", {"status": result.status},
                     "success", {"flags": [f["signal"] for f in result.trace.get("review", [])]})
            if review:
                tel.human_review(req.case_id, sorted({f["signal"] for f in review}),
                                 review[0]["resolution"])
            a.summary = f"{result.status}" + (" - needs review" if review else "")
        root.summary = (f"{result.status} (workflow claimed {req.reported_status}); "
                        f"{len(result.steps)} steps" + ("; NEEDS_REVIEW" if review else ""))

    # ------------------------------------------------------------------ agent outputs (no personal data)
    @staticmethod
    def _intent_out(intent: dict) -> dict:
        return {k: intent.get(k) for k in ("intent", "confidence", "explanation", "source", "degraded")}

    def _identity_out(self, req: Request, uid: str) -> dict:
        ident = self.resolver.users[uid]
        current = ident.current_email == norm_email(req.email)
        return {"user_id": uid,
                "matched_on": "current email" if current else "historical email (user_audit_log.csv)",
                "emails_known": len(ident.emails), "historical_emails": max(len(ident.emails) - 1, 0),
                "phones_known": len(ident.phones), "email_hashes": len(ident.hashes),
                "sources": ["user_identities.csv", "user_audit_log.csv", "sha256(lower(email)) derived"],
                "conflicting_identifiers_excluded": sum(1 for k in self.resolver.conflicts
                                                        if k[1] in ident.emails | ident.phones | ident.hashes)}

    def _evidence_out(self, cl) -> dict:
        files = sorted({o.file_id for rid in cl.resources() for o in self.store.by_resource.get(rid, [])},
                       key=lambda f: int(f.split("_")[1]))
        kinds = Counter(Path(self.store.manifest[f]["file_path"]).suffix.lstrip(".") for f in files
                        if f in self.store.manifest)
        return {"records": sorted(cl.resources()), "by_identifier": sorted(cl.explicit),
                "by_deletion_batch": sorted(set(cl.batch) - cl.explicit), "files_cited": files,
                "source_types": dict(kinds), "ties": {r: v for r, v in cl.ties.items()}}

    # ------------------------------------------------------------------ helpers (unchanged logic)
    def _primary_file(self, cl) -> str:
        """The admissible file that documents most of this case's resources."""
        req = cl.request
        counts: dict[str, int] = {}
        for rid in cl.resources():
            for o in self.store.by_resource.get(rid, []):
                cap = self.store.captured_at(o.file_id)
                if o.ts <= req.audit_at and cap <= req.audit_at and o.source != "multimodal":
                    counts[o.file_id] = counts.get(o.file_id, 0) + 1
        if not counts:
            return ""
        return max(counts, key=lambda f: (counts[f], self.store.captured_at(f), f))

    def _has_explicit_findings(self, cl) -> bool:
        req = cl.request
        for rid in cl.explicit:
            attempted = {a.event_type for a in cl.batch.get(rid, []) if a.event_time <= req.audit_at}
            st = reconstruct(rid, self.store, req.audit_at, attempted, cl.footprint, req.requested_at)
            if self.policy.treat(st).findings:
                return True
        return False
