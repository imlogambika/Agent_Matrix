"""Safety scenarios: unsafe or unreliable situations, and what the system does about them.

Every scenario runs the real code path (the real guard, the real classifier, the real
model client, the real identity resolver) on real or clearly synthetic input, and
reports what it expected and what actually happened. ``demo.py safety`` prints them;
``tests/test_safety.py`` asserts them.

    python3 code/evaluation/safety_scenarios.py
"""
from __future__ import annotations

import copy
import csv
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
DATASET = CODE.parent / "dataset"
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(HERE))

from ghost_audit import guard  # noqa: E402
from ghost_audit.data import IdentityResolver, InputError, load_requests  # noqa: E402
from ghost_audit.intent import classify  # noqa: E402
from ghost_audit.llm import LLMClient  # noqa: E402
from ghost_audit.models import Request  # noqa: E402
from ghost_audit.review import assess_unaudited  # noqa: E402


def _result(name, expected, observed, passed, detail=""):
    return {"name": name, "expected": expected, "observed": observed, "passed": bool(passed), "detail": detail}


def _requests(texts):
    return [Request(f"t_{i}", "someone@example.org", "2026-09-01T00:00:00Z", "COMPLETE",
                    "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z", t) for i, t in enumerate(texts)]


def _offline_llm(tmp: Path) -> LLMClient:
    """Cached replies of the selected open models only - no server, never writes the cache."""
    return LLMClient(CODE / "cache" / "llm_cache.json", tmp / "ledger.jsonl", backend="offline", log=lambda *a: None)


# ------------------------------------------------------------------ request intent
def non_deletion_requests(tmp: Path) -> dict:
    from model_benchmark import CHALLENGE   # team-written texts in 7 languages, never used to tune prompts
    reqs = _requests([t for t, _ in CHALLENGE])
    got = classify(reqs, _offline_llm(tmp), log=lambda *a: None)
    wrong = [(t, lab, got[r.case_id]["intent"]) for r, (t, lab) in zip(reqs, CHALLENGE)
             if got[r.case_id]["intent"] != lab]
    n_keep = sum(lab == "NOT_DELETE" for _, lab in CHALLENGE)
    return _result("Access / export / opt-out / correction requests are not deletions (7 languages)",
                   f"{n_keep} NOT_DELETE and {len(CHALLENGE) - n_keep} DELETE labelled correctly",
                   f"{len(CHALLENGE) - len(wrong)} / {len(CHALLENGE)} correct", not wrong,
                   "; ".join(f"{t[:40]}... -> {g}" for t, _, g in wrong))


class _MalformedLLM:
    """A model that answers with broken JSON (truncated generation, wrong format)."""
    available, use_cache = True, False

    def models_for(self, role):
        return ["llama-3.2-3b", "qwen2.5-vl-3b"]

    def complete(self, **kw):
        return '{"about_deletion": tru'


def malformed_model_output(tmp: Path) -> dict:
    text = "Please delete my account and all my personal data."
    got = classify(_requests([text]), _MalformedLLM(), log=lambda *a: None)["t_0"]
    flags = [f["signal"] for f in assess_unaudited(got)]
    ok = got["degraded"] and got["confidence"] == "low" and got["intent"] == "DELETE" and "INTENT_FALLBACK" in flags
    return _result("Model returns malformed JSON",
                   "no crash, no guessed label: rule classifier decides, confidence low, case flagged for review",
                   f"intent={got['intent']} confidence={got['confidence']} degraded={got['degraded']} flags={flags}",
                   ok, got["explanation"])


def model_server_unavailable(tmp: Path) -> dict:
    old = os.environ.get("LLAMA_SERVER")
    os.environ["LLAMA_SERVER"] = str(tmp / "no-such-llama-server")
    try:
        llm = LLMClient(tmp / "cache.json", tmp / "ledger.jsonl", use_cache=False, backend="llama_cpp",
                        log=lambda *a: None)
        got = classify(_requests(["Delete my account, please."]), llm, log=lambda *a: None)["t_0"]
    finally:
        os.environ.pop("LLAMA_SERVER") if old is None else os.environ.__setitem__("LLAMA_SERVER", old)
    tries = len(llm.failures)
    ok = tries == 2 and got["degraded"] and got["intent"] == "DELETE"
    return _result("Model server is down",
                   "each model retried once (bounded), then the run degrades to rules and flags the case",
                   f"{tries} models failed after {llm.max_attempts} attempts each; intent={got['intent']} "
                   f"degraded={got['degraded']}", ok, llm.failures[0]["error"][:120] if llm.failures else "")


# ------------------------------------------------------------------ completion guard
def _case(results, status):
    return next(r for r in results if r.status == status)


def forged_complete_claim(results, req_by_id, evidence) -> dict:
    """An agent (or a tampered workflow) declares an INCOMPLETE case COMPLETE, citing the workflow's own record."""
    acts = list(csv.DictReader(open(DATASET / "system_activity.csv", encoding="utf-8")))
    for r in (x for x in results if x.status == "INCOMPLETE"):
        req = req_by_id[r.case_id]
        # the deletion worker's own "done" records for this case's resources: exactly the claim under audit
        claims = {a["resource_id"]: a["activity_id"] for a in acts if a["source_system"] == "deletion_worker"
                  and a["resource_id"] in r.trace["resources"] and a["event_time"] <= req.audit_at}
        if claims:
            break
    forged = [dict(s, step_no=i, evidence_id=claims.get(s["resource_id"], next(iter(claims.values()))))
              for i, s in enumerate((s for s in r.step_dicts() if s["phase"] == "VERIFICATION"), 1)]
    v = guard.check("COMPLETE", forged, req.audit_at, "DELETE", evidence)
    return _result(f"Forged COMPLETE on {r.case_id} (truly INCOMPLETE), citing the deletion worker's own records",
                   "rejected: a workflow record is the claim being audited, not proof",
                   f"{len(v)} violations", any("a claim, not proof" in x for x in v), v[0] if v else "")


def late_evidence(results, req_by_id, evidence) -> dict:
    files = sorted((t, k) for k, t in evidence.items() if k.startswith("evidence_"))
    for r in (x for x in results if x.status == "COMPLETE"):   # a case for which a later export exists
        req = req_by_id[r.case_id]
        later = [k for t, k in files if t > req.audit_at]
        if later:
            late = later[0]
            break
    steps = [dict(s, evidence_id=late) for s in r.step_dicts()]
    v = guard.check("COMPLETE", steps, req.audit_at, "DELETE", evidence)
    return _result(f"COMPLETE on {r.case_id} backed by {late}, captured after audit_at",
                   "rejected: evidence created later cannot prove the state at audit_at",
                   f"{len(v)} violations", any("after audit_at" in x for x in v), v[0] if v else "")


def fabricated_evidence(results, req_by_id, evidence) -> dict:
    r = _case(results, "COMPLETE")
    req = req_by_id[r.case_id]
    steps = [dict(s, evidence_id="evidence_999") for s in r.step_dicts()]
    v = guard.check("COMPLETE", steps, req.audit_at, "DELETE", evidence)
    return _result(f"COMPLETE on {r.case_id} citing evidence_999, which does not exist",
                   "rejected", f"{len(v)} violations", any("does not exist" in x for x in v), v[0] if v else "")


def real_run_guard(results, req_by_id, evidence) -> dict:
    bad = {r.case_id: guard.check(r.status, r.step_dicts(), req_by_id[r.case_id].audit_at,
                                  r.trace["intent"]["intent"], evidence) for r in results}
    bad = {k: v for k, v in bad.items() if v}
    ghost = sum(1 for r in results if req_by_id[r.case_id].reported_status == "COMPLETE" and r.status == "INCOMPLETE")
    return _result(f"All {len(results)} real decisions re-checked by the guard",
                   "every COMPLETE rests on admissible evidence files; every finding is cited",
                   f"{len(results) - len(bad)} / {len(results)} pass; {ghost} cases the workflow called COMPLETE "
                   "are INCOMPLETE on the evidence", not bad, "; ".join(f"{k}: {v[0]}" for k, v in list(bad.items())[:3]))


# ------------------------------------------------------------------ identity and input
def identity_conflict(tmp: Path) -> dict:
    ids, log = tmp / "ids.csv", tmp / "audit.csv"
    with ids.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["user_id", "name", "email", "phone", "email_hash", "updated_at"])
        w.writerow(["user_a", "Ana Silva", "ana@example.org", "+1 555 0100", "", "2026-01-01T00:00:00Z"])
        w.writerow(["user_b", "Ana Silva", "ana.s@example.org", "+1 555 0199", "", "2026-01-01T00:00:00Z"])
    with log.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"])
        # user_b once used the address user_a holds today
        w.writerow(["audit_1", "user_b", "2025-05-01T00:00:00Z", "email", "ana@example.org", "ana.s@example.org"])
    res = IdentityResolver(ids, log)
    uid = res.resolve_email("ana@example.org")
    flags = [f["signal"] for f in assess_unaudited({"intent": "DELETE"}, identity_resolved=uid is not None)]
    return _result("Two people have held the same email address",
                   "not merged: the shared address resolves to nobody and the request goes to review",
                   f"resolved to {uid}; conflicts={len(res.conflicts)}; flags={flags}",
                   uid is None and "IDENTITY_UNRESOLVED" in flags, "")


def corrupted_input(tmp: Path) -> dict:
    bad = tmp / "deletion_requests.csv"
    rows = list(csv.reader(open(DATASET / "deletion_requests.csv", encoding="utf-8")))[:4]
    rows[2][rows[0].index("audit_at")] = "next tuesday"
    with bad.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    try:
        load_requests(bad)
        observed, ok = "accepted", False
    except InputError as e:
        observed, ok = f"refused: {e}", True
    return _result("A request file with a corrupted audit_at", "refused before any decision is made",
                   observed, ok)


def run_all(results=None) -> list[dict]:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        out = [non_deletion_requests(tmp), malformed_model_output(tmp), model_server_unavailable(tmp),
               identity_conflict(tmp), corrupted_input(tmp)]
        if results is None:
            from ghost_audit.pipeline import AuditPipeline
            llm = _offline_llm(tmp)
            results = AuditPipeline(DATASET, llm, log=lambda *a: None).run(
                load_requests(DATASET / "deletion_requests.csv"))
        req_by_id = {r.case_id: r for r in load_requests(DATASET / "deletion_requests.csv")}
        evidence = guard.evidence_index(DATASET)
        out += [forged_complete_claim(results, req_by_id, evidence), late_evidence(results, req_by_id, evidence),
                fabricated_evidence(results, req_by_id, evidence), real_run_guard(results, req_by_id, evidence)]
    return out


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    res = run_all()
    for r in res:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['name']}\n       expected: {r['expected']}\n"
              f"       observed: {r['observed']}" + (f"\n       detail:   {r['detail']}" if r['detail'] else ""))
    print(f"\n{sum(r['passed'] for r in res)} / {len(res)} scenarios pass")
    raise SystemExit(0 if all(r["passed"] for r in res) else 1)
