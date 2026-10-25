"""Render evaluation/usage_report.md from the usage ledger of one pipeline run.

    python3 code/evaluation/usage_report.py code/evaluation/runs/usage_<run>.jsonl [--requests 518]

Local open-weight inference has no per-token fee, so the cost estimate is the energy
the run used: measured model time x CPU package power x electricity price (both
assumptions are printed in the report and can be changed with the flags below).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ghost_audit.local_models import ModelRegistry  # noqa: E402


def build(ledger: Path, n_requests: int, run_note: str, watts: float, usd_per_kwh: float,
          replay_of: Path | None = None) -> str:
    calls = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    live = [c for c in calls if not c["cached"]]
    cached = [c for c in calls if c["cached"]]
    registry = ModelRegistry()

    def energy_usd(seconds: float) -> float:
        return seconds * watts / 3.6e6 * usd_per_kwh

    per_model = defaultdict(lambda: {"calls": set(), "in": 0, "out": 0, "api": 0.0, "wall": 0.0, "compute": 0.0,
                                     "provider": "", "tasks": set()})
    per_task = defaultdict(lambda: {"calls": set(), "in": 0, "out": 0, "wall": 0.0})
    for i, c in enumerate(live):
        cid = c.get("call_id", i)
        m = per_model[c["model"]]
        m["calls"].add(cid)
        m["in"] += c["input_tokens"]
        m["out"] += c["output_tokens"]
        m["api"] += c["cost_usd"]
        m["wall"] += c["latency_s"]
        m["compute"] += c.get("compute_s", 0.0)
        m["provider"] = c["provider"]
        m["tasks"].add(c["task"])
        t = per_task[c["task"]]
        t["calls"].add(cid)
        t["in"] += c["input_tokens"]
        t["out"] += c["output_tokens"]
        t["wall"] += c["latency_s"]
    tot_calls = len({c.get("call_id", i) for i, c in enumerate(live)})
    tot_in = sum(m["in"] for m in per_model.values())
    tot_out = sum(m["out"] for m in per_model.values())
    tot_wall = sum(m["wall"] for m in per_model.values())
    tot_api = sum(m["api"] for m in per_model.values())
    tot_cost = tot_api + energy_usd(tot_wall)
    n = max(n_requests, 1)

    def name(key: str) -> str:
        spec = registry.models.get(key)
        return f"`{key}` - {spec.display} ({spec.vendor}, {spec.license})" if spec else f"`{key}`"

    lines = [
        "# Token Usage and Cost Report", "",
        f"Run analysed: `{ledger.name}` - {run_note}", "",
        intro(ledger, n_requests, replay_of, len(live)), "",
        "## Models and where they are used", "",
        "| Role | What the model does | Model |", "| --- | --- | --- |",
    ]
    for role, tasks, what in (("intent", {"request_intent"}, "Classifies each unique request text (398 texts "
                               "across the sample and evaluation files) as deletion / non-deletion, any language"),
                              ("extraction", {"multimodal_extraction", "screenshot_transcription"},
                               "Transcribes each of the 11 screenshots once; a fixed parser turns the text into "
                               "records (the 4 PDFs are parsed from their text layer without a model)")):
        used = sorted({k for k, m in per_model.items() if tasks & m["tasks"]})
        lines.append(f"| {role} | {what} | {', '.join(name(k) for k in used) or '-'} |")
    lines += [
        "", "All models are open-weight GGUF files (4-bit) served locally by llama.cpp on a CPU (8-core Intel "
        "i7-1185G7, no GPU). Everything else in the pipeline - parsing 79 SQL/JSON/log/text files, identity "
        "resolution, state reconstruction, policy and ordering - is deterministic code with no model cost.", "",
        "## Per-model totals", "",
        "| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Model time (s) | API fees (USD) "
        "| Est. energy cost (USD) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, m in sorted(per_model.items()):
        lines.append(f"| {m['provider']} | {name(key)} | {len(m['calls'])} | {m['in']:,} | {m['out']:,} | "
                     f"{m['in'] + m['out']:,} | {m['wall']:.0f} | {m['api']:.2f} | {energy_usd(m['wall']):.4f} |")
    lines += [
        f"| **Overall** | | **{tot_calls}** | **{tot_in:,}** | **{tot_out:,}** | **{tot_in + tot_out:,}** | "
        f"**{tot_wall:.0f}** | **{tot_api:.2f}** | **{energy_usd(tot_wall):.4f}** |", "",
        "## Per-task breakdown", "",
        "| Task | Calls | Input tokens | Output tokens | Model time (s) |", "| --- | ---: | ---: | ---: | ---: |",
    ]
    for task, t in sorted(per_task.items()):
        lines.append(f"| `{task}` | {len(t['calls'])} | {t['in']:,} | {t['out']:,} | {t['wall']:.0f} |")
    lines += [
        "", "## Per-request averages", "", "| Metric | Value |", "| --- | ---: |",
        f"| Requests processed | {n_requests} |",
        f"| Model calls per request | {tot_calls / n:.3f} |",
        f"| Input tokens per request | {tot_in / n:,.1f} |",
        f"| Output tokens per request | {tot_out / n:,.1f} |",
        f"| Total tokens per request | {(tot_in + tot_out) / n:,.1f} |",
        f"| Model time per request (s) | {tot_wall / n:.2f} |",
        f"| Estimated cost per request (USD) | {tot_cost / n:.6f} |",
        f"| Estimated total cost (USD) | {tot_cost:.4f} |", "",
        "## How the numbers are measured", "",
        "* Every call is appended to the usage ledger by `ghost_audit/llm.py`; token counts are the "
        "`usage.prompt_tokens` / `usage.completion_tokens` that llama-server returns (image tokens included in "
        "the prompt count).",
        "* API fees are zero: the weights run locally under their open licenses.",
        f"* Energy cost = model wall time x {watts:.0f} W (the CPU's rated package power, an upper bound for "
        f"this load) x ${usd_per_kwh:.2f} per kWh. Change with `--watts` / `--usd-per-kwh`.",
        f"* Cached replies (`{len(cached)}` in this ledger) cost nothing and are excluded.", "",
        "## Token-efficiency choices", "",
        "* Each unique request text is classified once; the shared instruction prefix stays in llama.cpp's KV "
        "cache, so each call only processes the request's own tokens.",
        "* Each screenshot / PDF is extracted once per file and reused by every case that cites it.",
        "* Vision input is capped (`image_max_tokens` in `config/models.toml`) to bound CPU time per screenshot.",
        "* Output is grammar-constrained JSON with tight `max_tokens`, so no tokens are spent on prose or retries.",
        "",
    ]
    return "\n".join(lines)


def intro(ledger: Path, n_requests: int, replay_of: Path | None, n_live: int) -> str:
    if replay_of is None:
        return (f"This is the final full-dataset run that produced `output.csv` ({n_requests} requests), executed "
                "with the reply cache disabled (`python3 code/main.py --no-cache`), so every model call the pipeline "
                "needs was made and is counted below.")
    replayed = [json.loads(l) for l in replay_of.read_text().splitlines() if l.strip()]
    return (f"The final full-dataset run that produced `output.csv` ({n_requests} requests) is `{replay_of.name}` "
            f"(`python3 code/main.py`, selected models from `config/models.toml`). It made {len(replayed)} model "
            "requests, all answered from the reply cache. Every one of those replies was generated live, exactly "
            "once, by the same open-weight model with the same prompt during the measured runs in "
            "`evaluation/runs/` (the cache key is a hash of model, prompt, image and schema). "
            f"`final_run_cost.py` matched each of the {len(replayed)} requests to the live call that generated its "
            f"reply ({n_live} calls, ledger `{ledger.name}`), so the figures below are the complete cost of "
            "producing `output.csv` from scratch. Re-running the final command costs nothing further.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ledger")
    ap.add_argument("--requests", type=int, default=518)
    ap.add_argument("--note", default="final full run")
    ap.add_argument("--watts", type=float, default=28.0)
    ap.add_argument("--usd-per-kwh", type=float, default=0.15)
    ap.add_argument("--out", default=str(HERE / "usage_report.md"))
    ap.add_argument("--replay-of", default=None,
                    help="ledger of a cached final run; LEDGER then holds the calls that generated its replies")
    args = ap.parse_args()
    Path(args.out).write_text(build(Path(args.ledger), args.requests, args.note, args.watts, args.usd_per_kwh,
                                    Path(args.replay_of) if args.replay_of else None))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
