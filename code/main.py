"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repository root):

    python3 code/main.py                      # audit dataset/deletion_requests.csv -> output.csv
    python3 code/main.py --requests sample    # audit the 32 sample requests -> code/evaluation/sample_predictions.csv
    python3 code/main.py --offline            # no model calls (rules + cached model replies only)
    python3 code/main.py --no-cache           # fresh model calls (used for the usage-report run)
    python3 code/main.py --failproof          # also record every agent run as a Failproof AI session

The completion guard re-checks every decision before anything is written: if any case
is not backed by admissible evidence, output.csv is NOT written (exit code 3) and the
cases go to code/evaluation/review_queue.csv. Cases the evidence cannot settle are
always listed there as NEEDS_REVIEW (the official output keeps its three statuses).

Models are open-weight GGUF files served locally by llama.cpp; see code/config/models.toml.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
REPO = CODE_DIR.parent
sys.path.insert(0, str(CODE_DIR))

from ghost_audit.data import InputError, load_requests  # noqa: E402
from ghost_audit.llm import LLMClient  # noqa: E402
from ghost_audit.pipeline import AuditPipeline  # noqa: E402
from ghost_audit.review import render as render_escalations  # noqa: E402
from ghost_audit.telemetry import Telemetry  # noqa: E402


def write_output(results, path: Path) -> None:
    results = sorted(results, key=lambda r: int(r.case_id.split("_")[1]))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for r in results:
            w.writerow([r.case_id, r.status, r.steps_json()])


def write_review_queue(results, path: Path) -> int:
    """NEEDS_REVIEW cases: the official status stays, the reason and the resolution path are added."""
    rows = []
    for r in sorted(results, key=lambda r: int(r.case_id.split("_")[1])):
        flags = [f for f in r.trace.get("review", []) if f["severity"] == "review"]
        if flags:
            rows.append([r.case_id, r.status, "NEEDS_REVIEW", ";".join(sorted({f["signal"] for f in flags})),
                         " | ".join(f"{f['resource'] or '-'}: {f['detail']}" for f in flags)[:500],
                         flags[0]["resolution"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "review_status", "signals", "details", "resolution"])
        w.writerows(rows)
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(REPO / "dataset"))
    ap.add_argument("--requests", default="eval", help="'eval', 'sample', or a CSV path")
    ap.add_argument("--output", default=None)
    ap.add_argument("--offline", action="store_true", help="disable model calls")
    ap.add_argument("--backend", choices=["llama_cpp", "offline", "anthropic_api", "claude_cli"], default=None,
                    help="model backend (default: llama_cpp when the selected models are available)")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached model replies")
    ap.add_argument("--trace", default=None, help="write per-case reasoning trace (JSON)")
    ap.add_argument("--failproof", action="store_true",
                    help="record each case as a Failproof AI session (needs failproofai-sdk; no personal data is sent)")
    ap.add_argument("--environment", default="production", help="Failproof environment label (default: production)")
    ap.add_argument("--cases", default="", help="comma-separated case ids to audit (writes a partial output)")
    args = ap.parse_args(argv)

    dataset = Path(args.dataset)
    if args.requests == "eval":
        req_path, out = dataset / "deletion_requests.csv", REPO / "output.csv"
    elif args.requests == "sample":
        req_path, out = dataset / "sample_deletion_requests.csv", CODE_DIR / "evaluation" / "sample_predictions.csv"
    else:
        req_path, out = Path(args.requests), REPO / "output.csv"
    out = Path(args.output) if args.output else out
    only = {c.strip() for c in args.cases.split(",") if c.strip()}
    if only and not args.output:
        out = CODE_DIR / "evaluation" / "subset_output.csv"

    run_id = time.strftime("%Y%m%dT%H%M%S")
    llm = LLMClient(cache_path=CODE_DIR / "cache" / "llm_cache.json",
                    ledger_path=CODE_DIR / "evaluation" / "runs" / f"usage_{run_id}.jsonl",
                    use_cache=not args.no_cache, backend="offline" if args.offline else args.backend)
    roles = {r: llm.model_for(r) for r in ("intent", "extraction")} if llm.available else {}
    print(f"[llm] backend={llm.backend} models={roles} cache={'off' if args.no_cache else 'on'}")
    tel = Telemetry(enabled=args.failproof, environment=args.environment, run_id=run_id)
    started = time.time()
    try:
        try:
            requests = [r for r in load_requests(req_path) if not only or r.case_id in only]
        except InputError as e:
            print(f"[input] refusing to audit: {e}")
            return 2
        pipeline = AuditPipeline(dataset, llm, telemetry=tel)
        results = pipeline.run(requests)
    finally:
        llm.close()
        tel.close()
    queue_name = ("subset_" if only else "sample_" if args.requests == "sample" else "") + "review_queue.csv"
    queue = write_review_queue(results, CODE_DIR / "evaluation" / queue_name)
    rejected = {r.case_id: r.trace["guard"] for r in results if r.trace.get("guard")}
    if rejected:
        blocked = out.with_name(out.stem + ".blocked.csv")
        write_output(results, blocked)
        print(f"[guard] {len(rejected)} decisions are not backed by admissible evidence - output.csv NOT written; "
              f"see {blocked.name} and review_queue.csv")
        for cid, v in list(rejected.items())[:10]:
            print(f"   {cid}: {v[0]}")
        return 3
    write_output(results, out)
    if args.requests == "eval" and not only:
        (CODE_DIR / "evaluation" / "escalation_report.md").write_text(render_escalations(results, len(results)))
    summary = {s: sum(r.status == s for r in results) for s in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")}
    print(f"[done] {len(results)} cases -> {out}  {summary}  in {time.time() - started:.1f}s")
    print(f"[guard] {len(results)} decisions passed the completion guard; {queue} cases need review "
          f"(code/evaluation/{queue_name})")
    if llm.failures:
        print(f"[llm] {len(llm.failures)} model calls failed and fell back to rules (flagged for review)")
    if tel.active:
        print(f"[failproof] {tel.counts['sessions']} sessions, {tel.counts['events']} events emitted, "
              f"{tel.counts['dropped']} dropped; the Failproof daemon ships the spool to the org")
    live = {c.call_id for c in llm.calls if not c.cached}
    print(f"[llm] model calls={len(live)} cached replies={sum(c.cached for c in llm.calls)} "
          f"ledger={llm.ledger_path}")
    if args.trace:
        Path(args.trace).write_text(json.dumps({r.case_id: r.trace for r in results}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
