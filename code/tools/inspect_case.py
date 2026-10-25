"""Explain one case: identity, linked resources, every observation and the final steps.

    python3 code/tools/inspect_case.py case_123 [case_456 ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

from ghost_audit.data import load_requests  # noqa: E402
from ghost_audit.llm import LLMClient  # noqa: E402
from ghost_audit.pipeline import AuditPipeline  # noqa: E402


def main(case_ids):
    llm = LLMClient(CODE / "cache" / "llm_cache.json", CODE / "evaluation" / "runs" / "inspect.jsonl",
                    backend="offline")
    p = AuditPipeline(CODE.parent / "dataset", llm, log=lambda *a: None)
    reqs = {r.case_id: r for r in p.all_requests}
    results = {r.case_id: r for r in p.run([reqs[c] for c in case_ids])}
    for cid in case_ids:
        r, res = reqs[cid], results[cid]
        print(f"=== {cid} {r.email} req={r.requested_at} rep={r.reported_at} ({r.reported_status}) audit={r.audit_at}")
        print(f"    {r.request_text[:150]}")
        print(f"    user={res.trace.get('user_id')} -> {res.status}")
        for rid, info in res.trace.get("resources", {}).items():
            print(f"  * {rid} {info}")
            for o in sorted(p.store.by_resource.get(rid, []), key=lambda o: o.ts):
                late = "LATE" if p.store.captured_at(o.file_id) > r.audit_at or o.ts > r.audit_at else "    "
                print(f"      {late} {o.ts} {o.file_id:12s} {o.source:28s} present={o.present} pii={o.pii_fields} "
                      f"en={o.enabled} rest={o.restore_allowed} ids={sorted(o.emails | o.user_refs | o.lookup_keys)[:3]}")
            for a in p.activities:
                if a.resource_id == rid:
                    print(f"      act {a.activity_id} {a.event_time} {a.event_type} {a.reported_result} {a.details[:60]}")
        for i, s in enumerate(res.steps, 1):
            print(f"    {i:2d} {s.phase:12s} {s.system_id:20s} {s.resource_id:18s} {s.operation:26s} {s.evidence_id}")


if __name__ == "__main__":
    main(sys.argv[1:])
