"""Build the Audit Explorer: a static page that shows every case the agent audited.

Runs the pipeline offline (deterministic core + cached model replies, so it reproduces
output.csv), then writes:

    explorer/index.html          full HTML document - deploy as a static site (e.g. `vercel deploy explorer`)
    explorer/artifact.html       the same page without the document wrapper (for hosted page viewers)

Customer emails are masked; only stable user ids, resource ids and evidence ids are shown.

    python3 code/tools/build_explorer.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
REPO = CODE.parent
sys.path.insert(0, str(CODE))

from ghost_audit.data import load_requests  # noqa: E402
from ghost_audit.llm import LLMClient  # noqa: E402
from ghost_audit.pipeline import AuditPipeline  # noqa: E402

TEMPLATE = Path(__file__).with_name("explorer_template.html")


def mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}{'•' * 3}@{domain}"


def build() -> dict:
    llm = LLMClient(CODE / "cache" / "llm_cache.json", CODE / "evaluation" / "runs" / "explorer.jsonl",
                    backend="offline", log=lambda *a: None)
    p = AuditPipeline(REPO / "dataset", llm, log=lambda *a: None)
    reqs = load_requests(REPO / "dataset" / "deletion_requests.csv")
    results = {r.case_id: r for r in p.run(reqs)}
    manifest = {r["file_id"]: r for r in csv.DictReader(open(REPO / "dataset" / "evidence_files.csv"))}
    cases = []
    for r in reqs:
        res = results[r.case_id]
        cases.append({
            "id": r.case_id, "status": res.status, "claim": r.reported_status, "email": mask(r.email),
            "requested": r.requested_at, "reported": r.reported_at, "audit": r.audit_at, "text": r.request_text,
            "user": res.trace.get("user_id", ""),
            "steps": [s.as_dict(i + 1) for i, s in enumerate(res.steps)],
            "resources": res.trace.get("resources", {}),
            "review": res.trace.get("review", []),
        })
    evidence = {fid: {"name": Path(m["file_path"]).name, "origin": m["origin"], "captured": m["captured_at"]}
                for fid, m in manifest.items()}
    return {"cases": cases, "evidence": evidence}


def main() -> int:
    data = build()
    page = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    out = REPO / "explorer"
    out.mkdir(exist_ok=True)
    (out / "artifact.html").write_text(page)
    title = re.search(r"<title>.*?</title>", page).group(0)
    body = page.replace(title, "")
    (out / "index.html").write_text(
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
        f"{title}</head><body>{body}</body></html>\n")
    print(f"wrote {out / 'index.html'} and {out / 'artifact.html'} ({len(page) // 1024} KB, "
          f"{len(data['cases'])} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
