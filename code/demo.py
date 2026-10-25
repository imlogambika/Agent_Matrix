"""Agent Matrix demo: "Don't trust a deletion status. Verify the evidence."

    python3 code/demo.py summary            # what the agents found across all 518 requests
    python3 code/demo.py case case_47       # one request, agent by agent, with the evidence
    python3 code/demo.py claims             # cases the workflow called COMPLETE that are not
    python3 code/demo.py safety             # unsafe situations and what the system does about them

Every number is computed from a real run of the pipeline. Model replies come from the
local reply cache, so the demo runs in seconds with no model server; ``main.py`` runs
the same pipeline with the live open-weight models.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path

CODE = Path(__file__).resolve().parent
DATASET = CODE.parent / "dataset"
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(CODE / "evaluation"))

from ghost_audit.data import load_requests  # noqa: E402
from ghost_audit.llm import LLMClient  # noqa: E402
from ghost_audit.pipeline import AuditPipeline  # noqa: E402

BOLD, DIM, RED, GREEN, YELLOW, END = ("\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m") \
    if sys.stdout.isatty() else ("",) * 6
STATUS_COLOR = {"COMPLETE": GREEN, "INCOMPLETE": RED, "NOT_APPLICABLE": DIM}


def run(cases: set | None = None):
    tmp = tempfile.mkdtemp(prefix="agent-matrix-demo-")
    llm = LLMClient(CODE / "cache" / "llm_cache.json", Path(tmp) / "ledger.jsonl", backend="offline",
                    log=lambda *a: None)
    pipe = AuditPipeline(DATASET, llm, log=lambda *a: None)
    reqs = load_requests(DATASET / "deletion_requests.csv")
    if cases:
        reqs = [r for r in reqs if r.case_id in cases]
    return pipe, reqs, pipe.run(reqs)


def mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def h(title: str) -> None:
    print(f"\n{BOLD}{title}{END}")


# ------------------------------------------------------------------ summary
def summary(_args) -> None:
    pipe, reqs, results = run()
    req = {r.case_id: r for r in reqs}
    status = Counter(r.status for r in results)
    review = [r for r in results if any(f["severity"] == "review" for f in r.trace.get("review", []))]
    guard_ok = sum(not r.trace.get("guard") for r in results)
    steps = [s for r in results for s in r.steps]
    ops = Counter(s.operation for s in steps)
    files = Counter(Path(m["file_path"]).suffix.lstrip(".") for m in pipe.store.manifest.values())

    h("Requests")
    print(f"  processed              {len(results)}")
    print(f"  deletion requests      {len(results) - status['NOT_APPLICABLE']}")
    print(f"  not applicable         {status['NOT_APPLICABLE']}   (the request does not authorise deletion)")
    h("Verdicts (official output.csv)")
    for s in ("COMPLETE", "INCOMPLETE"):
        print(f"  {STATUS_COLOR[s]}{s:<22}{END} {status[s]}")
    print(f"  {YELLOW}needs human review{END}     {len(review)}   (review_queue.csv; the official status is kept)")
    print(f"  completion guard       {guard_ok} / {len(results)} decisions backed by admissible evidence")
    h("Claim versus evidence")
    claim = Counter((req[r.case_id].reported_status, r.status) for r in results)
    for (c, s), n in sorted(claim.items(), key=lambda kv: -kv[1]):
        mark = f"  {RED}<- ghost data{END}" if c == "COMPLETE" and s == "INCOMPLETE" else ""
        print(f"  workflow said {c:<19} evidence says {STATUS_COLOR[s]}{s:<15}{END}{n:>4}{mark}")
    h("Evidence searched")
    print(f"  {len(pipe.store.manifest)} files: " + ", ".join(f"{n} {k}" for k, n in files.most_common()))
    print(f"  {len(pipe.store.observations)} observations, {len(pipe.store.scopes)} full captures; "
          f"{len(pipe.activities)} activity rows; unlinkable: {', '.join(pipe.store.skipped) or 'none'}")
    h("Residual data and recreation risks found")
    for op in ("REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"):
        print(f"  {op:<26} {ops[op]}")
    h("Remediation plan (in execution order)")
    for op in ("DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"):
        print(f"  {op:<26} {ops[op]}")
    h("Verification checks")
    planned = sum(1 for s in steps if s.phase == "VERIFICATION" and not s.evidence_id)
    proven = sum(1 for s in steps if s.phase == "VERIFICATION" and s.evidence_id)
    print(f"  proven now (cite evidence)        {proven}")
    print(f"  to run after remediation          {planned}")
    h("Review flags raised")
    for sig, n in Counter(f["signal"] for r in review for f in r.trace["review"]
                          if f["severity"] == "review").most_common():
        print(f"  {sig:<26} {n}")


# ------------------------------------------------------------------ one case
def case(args) -> None:
    pipe, reqs, results = run({args.case_id})
    if not results:
        sys.exit(f"unknown case {args.case_id}")
    r, q = results[0], reqs[0]
    t = r.trace
    print(f"{BOLD}{q.case_id}{END}  {mask(q.email)}  requested {q.requested_at}  audit_at {q.audit_at}")
    print(f'  "{q.request_text[:160]}"')
    print(f"  workflow claims: {BOLD}{q.reported_status}{END} (reported {q.reported_at}) - a claim, not proof")

    it = t["intent"]
    h("1. intent-classifier")
    print(f"  {it['intent']}  ({it['confidence']} confidence) - {it['explanation']}")
    if r.status == "NOT_APPLICABLE":
        h("5. decision-verifier")
        print(f"  {DIM}NOT_APPLICABLE{END}: the request does not authorise deletion; no steps")
        return
    idt = t["identity"]
    h("2. identity-resolver")
    print(f"  {idt['user_id']} matched on {idt['matched_on']}; knows {idt['emails_known']} emails "
          f"({idt['historical_emails']} historical), {idt['phones_known']} phones, {idt['email_hashes']} hashes")
    ev = t["evidence"]
    h("3. evidence-investigator")
    print(f"  {len(ev['records'])} records: by identifier {ev['by_identifier']}, "
          f"by deletion batch {ev['by_deletion_batch']}")
    print(f"  sources: {ev['source_types']}  files: {', '.join(ev['files_cited'][:8])}")
    h(f"4. historical-auditor  (state at {q.audit_at}; later evidence is ignored)")
    for rid, s in t["resources"].items():
        state = ("PRESENT" if s["present"] else "absent" if s["present"] is False else "unknown")
        extra = [x for x in (("PII exposed" if s["pii"] else ""), ("flow ACTIVE" if s["enabled"] else ""),
                             ("restore ALLOWED" if s["restore"] else "")) if x]
        late = f", {s['late_ignored']} later observations ignored" if s["late_ignored"] else ""
        print(f"  {rid:<22} {s['category']:<20} {state:<8} {' '.join(extra):<28} "
              f"proof {s['evidence'] or '-'} @ {s['evidence_at'] or '-'}{DIM}{late}{END}")
    h("5. decision-verifier")
    print(f"  {STATUS_COLOR[r.status]}{BOLD}{r.status}{END}  (workflow claimed {q.reported_status})")
    print(f"  completion guard: {'PASSED' if not t.get('guard') else 'REJECTED: ' + t['guard'][0]}")
    for i, s in enumerate(r.steps, 1):
        print(f"  {i:>2} {s.phase:<12} {s.system_id:<20} {s.resource_id:<22} {s.operation:<26} {s.evidence_id}")
    flags = [f for f in t.get("review", []) if f["severity"] == "review"]
    if flags:
        print(f"  {YELLOW}NEEDS REVIEW{END}: " + "; ".join(f"{f['signal']} {f['resource']} ({f['detail']})"
                                                        for f in flags))
        print(f"  resolution: {flags[0]['resolution']}")


def claims(_args) -> None:
    _, reqs, results = run()
    req = {r.case_id: r for r in reqs}
    ghost = [r for r in results if req[r.case_id].reported_status == "COMPLETE" and r.status == "INCOMPLETE"]
    print(f"{len(ghost)} cases the deletion workflow reported COMPLETE still hold personal data at audit_at:\n")
    for r in ghost[:12]:
        f = [s for s in r.steps if s.phase == "FINDING"]
        print(f"  {r.case_id:<9} {len(f)} findings: " +
              ", ".join(f"{s.operation.replace('REPORT_', '').lower()} {s.resource_id} ({s.evidence_id})" for s in f[:3]))
    print(f"\n  ... python3 code/demo.py case {ghost[0].case_id}")


def safety(_args) -> None:
    import safety_scenarios as sc
    _, _, results = run()
    res = sc.run_all(results)
    for r in res:
        ok = f"{GREEN}PASS{END}" if r["passed"] else f"{RED}FAIL{END}"
        print(f"[{ok}] {BOLD}{r['name']}{END}\n       expected: {r['expected']}\n       observed: {r['observed']}"
              + (f"\n       {DIM}{r['detail']}{END}" if r["detail"] else ""))
    print(f"\n{sum(r['passed'] for r in res)} / {len(res)} scenarios pass")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary").set_defaults(fn=summary)
    c = sub.add_parser("case")
    c.add_argument("case_id")
    c.set_defaults(fn=case)
    sub.add_parser("claims").set_defaults(fn=claims)
    sub.add_parser("safety").set_defaults(fn=safety)
    args = ap.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
