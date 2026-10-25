"""Side-by-side gold vs predicted steps for the cases that do not match exactly.

    python3 code/evaluation/diff_cases.py [case_id ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

from evaluate import FIELDS, REPO, key, load

HERE = Path(__file__).resolve().parent


def show(cid, gold, pred):
    gs, gsteps = gold[cid]
    ps, psteps = pred.get(cid, ("MISSING", []))
    print(f"== {cid}: gold={gs} pred={ps}")
    gk = {key(s) for s in gsteps}
    pk = {key(s) for s in psteps}
    for s in gsteps:
        print(f"   {'  ' if key(s) in pk else '- '}{' '.join(s[f] or '.' for f in FIELDS)}")
    for s in psteps:
        if key(s) not in gk:
            print(f"   + {' '.join(s[f] or '.' for f in FIELDS)}")


def main(argv):
    gold = load(REPO / "dataset" / "sample_output.csv")
    pred = load(HERE / "sample_predictions.csv")
    cases = argv[1:] or [c for c in gold if [key(s) for s in gold[c][1]] != [key(s) for s in pred.get(c, ("", []))[1]]
                         or gold[c][0] != pred.get(c, ("",))[0]]
    for c in cases:
        show(c, gold, pred)


if __name__ == "__main__":
    main(sys.argv)
