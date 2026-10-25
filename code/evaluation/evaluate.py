"""Score predictions against gold answers (dataset/sample_output.csv by default).

Metrics
-------
* case status accuracy          - deletion_status exact match
* step precision / recall / F1  - steps compared as full tuples
                                  (phase, system_id, resource_id, operation, state, evidence_id)
* field accuracy                - for each gold step, the best-matching predicted step on
                                  (system_id, resource_id); share of fields that agree
* resource recall / precision   - discovered (system_id, resource_id) pairs
* evidence accuracy             - evidence_id agreement on matched steps
* sequence accuracy             - whole steps_json array identical, in order
* order accuracy                - among cases whose step *sets* match, share whose order also matches

    python3 code/evaluation/evaluate.py [predictions.csv] [gold.csv]
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def load(path: Path) -> dict:
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"]))
            for r in csv.DictReader(open(path, encoding="utf-8"))}


def key(step) -> tuple:
    return tuple(step[f] for f in FIELDS)


def score(pred: dict, gold: dict) -> dict:
    m = Counter()
    per_case = {}
    for cid, (gstatus, gsteps) in gold.items():
        pstatus, psteps = pred.get(cid, ("MISSING", []))
        m["cases"] += 1
        m["status_ok"] += pstatus == gstatus
        gk, pk = Counter(map(key, gsteps)), Counter(map(key, psteps))
        tp = sum((gk & pk).values())
        m["tp"] += tp
        m["gold_steps"] += sum(gk.values())
        m["pred_steps"] += sum(pk.values())
        gres = {(s["system_id"], s["resource_id"]) for s in gsteps}
        pres = {(s["system_id"], s["resource_id"]) for s in psteps}
        m["res_tp"] += len(gres & pres)
        m["res_gold"] += len(gres)
        m["res_pred"] += len(pres)
        # field accuracy: align each gold step with the predicted step of the same
        # resource and phase that agrees on most fields
        for g in gsteps:
            cands = [p for p in psteps if (p["system_id"], p["resource_id"]) == (g["system_id"], g["resource_id"])]
            best = max(cands, key=lambda p: sum(p[f] == g[f] for f in FIELDS), default=None)
            for f in FIELDS:
                m[f"field_total_{f}"] += 1
                m[f"field_ok_{f}"] += best is not None and best[f] == g[f]
            if best is not None:
                m["ev_total"] += 1
                m["ev_ok"] += best["evidence_id"] == g["evidence_id"]
        same_seq = [key(s) for s in gsteps] == [key(s) for s in psteps]
        m["seq_ok"] += same_seq
        if gk == pk:
            m["set_ok"] += 1
            m["order_ok"] += same_seq
        per_case[cid] = {"gold": gstatus, "pred": pstatus, "tp": tp, "gold_n": len(gsteps), "pred_n": len(psteps),
                         "exact": same_seq and pstatus == gstatus}
    p = m["tp"] / m["pred_steps"] if m["pred_steps"] else 0.0
    r = m["tp"] / m["gold_steps"] if m["gold_steps"] else 0.0
    out = {
        "cases": m["cases"],
        "status_accuracy": m["status_ok"] / m["cases"],
        "step_precision": p, "step_recall": r, "step_f1": 2 * p * r / (p + r) if p + r else 0.0,
        "resource_recall": m["res_tp"] / m["res_gold"] if m["res_gold"] else 0.0,
        "resource_precision": m["res_tp"] / m["res_pred"] if m["res_pred"] else 0.0,
        "evidence_accuracy": m["ev_ok"] / m["ev_total"] if m["ev_total"] else 0.0,
        "sequence_accuracy": m["seq_ok"] / m["cases"],
        "order_accuracy_given_same_steps": m["order_ok"] / m["set_ok"] if m["set_ok"] else 0.0,
        "field_accuracy": {f: m[f"field_ok_{f}"] / m[f"field_total_{f}"] for f in FIELDS if m[f"field_total_{f}"]},
        "per_case": per_case,
    }
    return out


def main(argv) -> int:
    pred = Path(argv[1]) if len(argv) > 1 else HERE / "sample_predictions.csv"
    gold = Path(argv[2]) if len(argv) > 2 else REPO / "dataset" / "sample_output.csv"
    res = score(load(pred), load(gold))
    for k, v in res.items():
        if k == "per_case":
            continue
        print(f"{k:34s} {json.dumps(v) if isinstance(v, dict) else (f'{v:.4f}' if isinstance(v, float) else v)}")
    bad = {c: v for c, v in res["per_case"].items() if not v["exact"]}
    print(f"\nnon-exact cases ({len(bad)}):")
    for c, v in bad.items():
        print(f"  {c}: gold={v['gold']} pred={v['pred']} matched_steps={v['tp']}/{v['gold_n']} pred_n={v['pred_n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
