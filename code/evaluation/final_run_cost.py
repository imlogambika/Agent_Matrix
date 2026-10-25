"""Attribute the cost of a cached final run to the live calls that generated its replies.

The final run that wrote output.csv replays model replies from code/cache/llm_cache.json.
Each of those replies was generated live, exactly once, by the same model and prompt during
a benchmark run whose ledger is kept in runs/. This script picks, for every call the final
run made, the live call that produced its reply, checks that the counts match, and writes
them as one ledger that usage_report.py renders:

    python3 code/evaluation/final_run_cost.py code/evaluation/runs/usage_<final>.jsonl \\
        intent=runs/benchmark_A.jsonl,runs/benchmark_B.jsonl screens=runs/benchmark_C.jsonl
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from model_benchmark import intent_gold  # noqa: E402

DATASET = HERE.parents[1] / "dataset"


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv) -> int:
    final = Path(argv[1])
    sources = dict(a.split("=", 1) for a in argv[2:])
    run = rows(final)
    need = Counter((c["task"], c["model"]) for c in run)
    texts = {r["request_text"] for f in ("sample_deletion_requests.csv", "deletion_requests.csv")
             for r in csv.DictReader(open(DATASET / f, encoding="utf-8"))}
    gold = [t for t, _, _ in intent_gold()]
    out = []
    for ledger in filter(None, sources.get("intent", "").split(",")):
        live = [c for c in rows(HERE / ledger) if c["task"] == "request_intent" and not c["cached"]]
        for model in sorted({c["model"] for c in live}):
            if ("request_intent", model) not in need:
                continue
            mine = [c for c in live if c["model"] == model]
            assert len(mine) == len(gold), (ledger, model, len(mine))   # one call per gold text, in gold order
            out += [c for c, t in zip(mine, gold) if t in texts]
    for ledger in filter(None, sources.get("screens", "").split(",")):
        out += [c for c in rows(HERE / ledger) if c["task"] == "screenshot_transcription" and not c["cached"]
                and ("screenshot_transcription", c["model"]) in need]
    got = Counter((c["task"], c["model"]) for c in out)
    if got != need:
        print(f"mismatch: final run needs {dict(need)}, generating calls found {dict(got)}")
        return 1
    for i, c in enumerate(out, 1):   # call ids restart in every ledger; number the combined calls afresh
        c["call_id"] = i
    dest = final.with_name(final.stem + "_generating_calls.jsonl")
    dest.write_text("".join(json.dumps(c) + "\n" for c in out))
    print(f"{sum(need.values())} replayed calls matched to their generating calls -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
