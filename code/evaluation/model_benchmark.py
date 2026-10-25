"""Benchmark open-source models for the two model-backed jobs and pick one per job.

Ground truth does not come from any model:

* intent      - a request is NOT_DELETE exactly when the privacy-intake workflow logged
                ``CLASSIFY_REQUEST ... non-deletion`` for that subject's account (10
                cases); every other dataset request is DELETE.  A team-written
                challenge set adds hard multilingual negatives and positives.  None
                of its texts appears in the prompt's few-shot examples.
* extraction  - hand-labelled from the 11 screenshots and the text of the 4 PDFs.
                ``key`` marks the records the audit depends on.

Selection rule, fixed before running (no peeking at which vendor wins):

* intent      - highest balanced accuracy over dataset + challenge texts; ties ->
                higher NOT_DELETE F1 -> lower compute time.
* extraction  - highest end-to-end agreement (pipeline output on every case that
                depends on an artefact, compared with the output produced from the
                hand-labelled extraction); ties -> key-field accuracy -> record F1 ->
                lower compute time.

    python3 code/evaluation/model_benchmark.py --task all --models qwen2.5-vl-3b,gemma-3-4b
    python3 code/evaluation/model_benchmark.py --report          # re-render the Markdown only
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = HERE.parent
REPO = CODE.parent
sys.path.insert(0, str(CODE))

from ghost_audit.data import IdentityResolver, load_activities, load_requests  # noqa: E402
from ghost_audit.evidence.multimodal import extract_artifact  # noqa: E402
from ghost_audit.intent import model_intent, rule_intent  # noqa: E402
from ghost_audit.llm import LLMClient  # noqa: E402
from ghost_audit.local_models import ModelRegistry  # noqa: E402
from ghost_audit.pipeline import AuditPipeline  # noqa: E402

DATASET = REPO / "dataset"
RESULTS = HERE / "runs" / "benchmark_results.json"
REPORT = HERE / "model_benchmark.md"

# ---------------------------------------------------------------- intent gold
CHALLENGE = [  # (text, label) - written by the team, not taken from the dataset or the prompt
    ("I'd like an export of everything you hold on me, but the account must stay active.", "NOT_DELETE"),
    ("Please unsubscribe me from promotional emails; I am keeping my account.", "NOT_DELETE"),
    ("My phone number changed, update it please. Nothing else.", "NOT_DELETE"),
    ("Necesito una copia de mis facturas; no cierren mi cuenta.", "NOT_DELETE"),
    ("Pourriez-vous m'envoyer les données que vous détenez sur moi ? Ne supprimez rien.", "NOT_DELETE"),
    ("Bitte korrigieren Sie meinen Nachnamen, das Konto soll bestehen bleiben.", "NOT_DELETE"),
    ("パスワードを再設定したいです。アカウントは削除しないでください。", "NOT_DELETE"),
    ("배송 주소만 변경해 주세요. 계정은 계속 사용합니다.", "NOT_DELETE"),
    ("Gostaria apenas de parar de receber newsletters, mantenham a conta.", "NOT_DELETE"),
    ("This is not a deletion request: what categories of data do you process about me?", "NOT_DELETE"),
    ("Erase my account and every copy of my personal data, including backups that can be restored.", "DELETE"),
    ("You confirmed my account was closed, yet the search page still shows my profile. Remove it.", "DELETE"),
    ("Delete my profile. Invoices may be kept if the law requires it.", "DELETE"),
    ("Por favor, borren mi cuenta y todos mis datos personales.", "DELETE"),
    ("Veuillez supprimer définitivement mon compte client.", "DELETE"),
    ("Bitte löschen Sie alle meine personenbezogenen Daten.", "DELETE"),
    ("私のアカウントと個人情報をすべて削除してください。", "DELETE"),
    ("제 계정과 개인정보를 영구적으로 삭제해 주세요.", "DELETE"),
    ("Quero que apaguem a minha conta e os meus dados.", "DELETE"),
    ("Please check whether my old export link was deleted and remove it if it still exists.", "DELETE"),
    ("Turn off whatever keeps syncing my data back and finish deleting my account.", "DELETE"),
    ("I closed the account last month; make sure nothing still identifies me.", "DELETE"),
]


def intent_gold() -> list[tuple[str, str, str]]:
    """(text, label, source) for every unique dataset text plus the challenge set."""
    acts = load_activities(DATASET / "system_activity.csv")
    resolver = IdentityResolver(DATASET / "user_identities.csv", DATASET / "user_audit_log.csv")
    llm = LLMClient(CODE / "cache" / "llm_cache.json", HERE / "runs" / "scratch.jsonl", backend="offline",
                    log=lambda *a: None)
    p = AuditPipeline(DATASET, llm, log=lambda *a: None)
    non_delete_accounts = {a.resource_id for a in acts if a.event_type == "CLASSIFY_REQUEST"}
    non_delete_users = set().union(*(p.linker.owners.get(r, set()) for r in non_delete_accounts))
    labels: dict[str, str] = {}
    for r in p.all_requests:
        uid = resolver.resolve_email(r.email)
        lab = "NOT_DELETE" if uid in non_delete_users else "DELETE"
        if labels.get(r.request_text, lab) != lab:
            raise SystemExit(f"conflicting gold for text: {r.request_text[:60]}")
        labels[r.request_text] = lab
    rows = [(t, lab, "dataset") for t, lab in sorted(labels.items())]
    return rows + [(t, lab, "challenge") for t, lab in CHALLENGE]


# ----------------------------------------------------------- extraction gold
def rec(rid=None, ref=None, rtype=None, state="PRESENT", user=None, email=None, key=False):
    return {"resource_id": rid, "business_reference": ref, "record_type": rtype, "state": state,
            "customer_reference": user, "name": None, "email": email, "phone": None, "other_parties": None,
            "retain_until": None, "key": key}


GOLD_EXTRACTION = {
    "evidence_04": [rec("account_5762", None, "USER_PROFILE", user="user_11", email="haruka.s1838@zoho.com", key=True),
                    rec("support_9538", "CS-8538", "SUPPORT_MESSAGE")],
    "evidence_09": [rec("search_428", None, "SEARCH_INDEX_ENTRY", user="user_13", email="g.silva76@gmail.com",
                        key=True),
                    rec("search_1874", None, "SEARCH_INDEX_ENTRY", email="g.silva76.jr@gmail.com")],
    "evidence_14": [rec("data_flow_1121", None, "DATA_FLOW", "ACTIVE", email="jain.aditi@gmail.com", key=True),
                    rec("data_flow_9846", None, "DATA_FLOW", "ACTIVE", email="lucia-torres@gmail.com")],
    "evidence_18": [rec(None, "NST-2026-0718-1842", None, user="user_18", email="ingrid.dahl@outlook.com", key=True)],
    "evidence_19": [rec("analytics_7438", None, "ANALYTICS_PROFILE", user="user_20", email="darwish.omar@icloud.com",
                        key=True)],
    "evidence_23": [rec("export_1260", None, "USER_EXPORT", email="ingrid.dahl@outlook.com", key=True),
                    rec("export_1261", None, "USER_EXPORT"), rec("export_1259", None, "USER_EXPORT"),
                    rec("export_1258", None, "USER_EXPORT")],
    "evidence_27": [rec(None, f"ORD-2026{n}", "LEGAL_HOLD_RECORD", user=u) for n, u in
                    (("4118", "user_08"), ("4207", "user_12"), ("4319", "user_17"), ("4402", "user_21"),
                     ("4511", "user_24"), ("4600", "user_29"))],
    "evidence_28": [rec("support_1034", "CS-5034", "SUPPORT_MESSAGE", email="kagyeman8947@fastmail.com", key=True)],
    "evidence_32": [rec("backup_9287", None, "BACKUP_ARCHIVE", "RESTORABLE", email="areed1433@gmail.com", key=True),
                    rec("backup_9291", None, "BACKUP_ARCHIVE", "RESTORABLE", email="bluepeakco@outlook.com"),
                    rec("backup_9288", None, "BACKUP_ARCHIVE", "RESTORABLE"),
                    rec("backup_9286", None, "BACKUP_ARCHIVE", "RESTORABLE")],
    "evidence_37": [rec("audit_record_003", None, "SECURITY_AUDIT_LOG", user="user_03", key=True)] +
                   [rec(f"audit_record_00{i}", None, "SECURITY_AUDIT_LOG", user="user_03") for i in (1, 2, 4, 5, 6)],
    "evidence_42": [rec(None, None, "ACCOUNT_TOMBSTONE", user="user_17", key=True)],
    "evidence_46": [rec(None, "delivery_confirmation_19.pdf", "SUPPORT_ATTACHMENT", key=True)] +
                   [rec(None, f) for f in ("delivery_confirmation_20.pdf", "return_label_4821.pdf",
                                           "return_label_4876.pdf", "invoice_NS-10493.pdf", "parcel_damage_7713.jpg",
                                           "customer_export_2026-02-10.zip", "customer_export_2026-02-12.zip")],
    "evidence_55": [rec("audit_record_003", None, "SECURITY_AUDIT_LOG", user="user_03", key=True)],
    "evidence_58": [rec("support_001", "CS-4827", "SUPPORT_ATTACHMENT", email="priya.raman@gmail.com", key=True)],
    "evidence_60": [rec("marketing_001", None, "MARKETING_PROFILE", email="sj.park@naver.com", key=True)],
}
SCORED_FIELDS = ("record_type", "state", "customer_reference", "email")


def _norm(v):
    return str(v).strip().lower() if v not in (None, "", "null") else None


def _match(gold: dict, pred: dict) -> bool:
    if gold["resource_id"]:
        return _norm(pred.get("resource_id")) == gold["resource_id"]
    if gold["business_reference"]:
        ref = _norm(pred.get("business_reference")) or ""
        return gold["business_reference"].lower() in ref or (ref and ref in gold["business_reference"].lower())
    return _norm(pred.get("customer_reference")) == gold["customer_reference"] and not _norm(pred.get("resource_id"))


def score_extraction(pred_by_file: dict) -> dict:
    m = Counter()
    for fid, golds in GOLD_EXTRACTION.items():
        preds = list(pred_by_file.get(fid, []))
        used = set()
        m["pred"] += len(preds)
        for g in golds:
            m["gold"] += 1
            m["gold_key"] += g["key"]
            hit = next((i for i, p in enumerate(preds) if i not in used and _match(g, p)), None)
            if hit is None:
                continue
            used.add(hit)
            m["tp"] += 1
            m["tp_key"] += g["key"]
            p = preds[hit]
            for f in SCORED_FIELDS:
                if g[f] is None and f != "email":
                    continue
                ok = _norm(p.get(f)) == _norm(g[f]) or (f == "email" and g[f] is None and not g["key"])
                m[f"f_{f}"] += 1
                m[f"ok_{f}"] += ok
                if g["key"]:
                    m["key_fields"] += 1
                    m["key_fields_ok"] += ok
    prec = m["tp"] / m["pred"] if m["pred"] else 0.0
    rec_ = m["tp"] / m["gold"] if m["gold"] else 0.0
    return {"record_precision": prec, "record_recall": rec_,
            "record_f1": 2 * prec * rec_ / (prec + rec_) if prec + rec_ else 0.0,
            "key_record_recall": m["tp_key"] / m["gold_key"],
            "key_field_accuracy": m["key_fields_ok"] / m["key_fields"] if m["key_fields"] else 0.0,
            "field_accuracy": {f: m[f"ok_{f}"] / m[f"f_{f}"] for f in SCORED_FIELDS if m[f"f_{f}"]}}


def pipeline_outputs(extractions: dict) -> dict:
    """Pipeline output for every case given these extractions.  Intent uses the rule classifier
    only (no reply cache), so the comparison isolates the effect of extraction."""
    llm = LLMClient(HERE / "runs" / "no_cache.json", HERE / "runs" / "scratch.jsonl", backend="offline",
                    use_cache=False, log=lambda *a: None)
    p = AuditPipeline(DATASET, llm, log=lambda *a: None, extractions=extractions)
    res = p.run(p.all_requests)
    return {r.case_id: (r.status, r.steps_json()) for r in res}


def artefact_cases(reference: dict, baseline: dict) -> list[str]:
    """Cases whose output depends on artefact extraction (gold extraction vs none at all)."""
    return sorted(c for c in reference if reference[c] != baseline.get(c))


# ------------------------------------------------------------------ runners
def run_intent(llm: LLMClient, model: str, gold: list) -> dict:
    t0 = time.time()
    n0 = len(llm.calls)
    pred = {}
    for text, _, _ in gold:
        pred[text] = model_intent(text, llm, model=model) or "INVALID"
    calls = llm.calls[n0:]
    return {"pred": pred, "wall_s": round(time.time() - t0, 1), **_usage(calls)}


def run_extraction(llm: LLMClient, model: str, manifest: dict, mode: str) -> dict:
    t0 = time.time()
    n0 = len(llm.calls)
    out = {}
    for fid in GOLD_EXTRACTION:
        meta = manifest[fid]
        if meta["file_path"].endswith(".png") and not llm.registry.models[model].vision:
            continue
        out[fid] = extract_artifact(llm, str(DATASET / meta["file_path"]), fid, meta["captured_at"], meta["origin"],
                                    model=model, mode=mode)
    return {"records": out, "wall_s": round(time.time() - t0, 1), **_usage(llm.calls[n0:])}


def _usage(calls) -> dict:
    live = [c for c in calls if not c.cached]
    return {"calls": len(calls), "live_calls": len(live), "input_tokens": sum(c.input_tokens for c in live),
            "output_tokens": sum(c.output_tokens for c in live), "compute_s": round(sum(c.compute_s for c in live), 1)}


def intent_metrics(gold: list, pred: dict) -> dict:
    out = {}
    for subset in ("dataset", "challenge", "all"):
        rows = [(t, lab) for t, lab, src in gold if subset == "all" or src == subset]
        tp = sum(pred.get(t) == "NOT_DELETE" and lab == "NOT_DELETE" for t, lab in rows)
        fp = sum(pred.get(t) == "NOT_DELETE" and lab == "DELETE" for t, lab in rows)
        fn = sum(pred.get(t) != "NOT_DELETE" and lab == "NOT_DELETE" for t, lab in rows)
        tn = sum(pred.get(t) == "DELETE" and lab == "DELETE" for t, lab in rows)
        pos = sum(lab == "DELETE" for _, lab in rows)
        neg = len(rows) - pos
        f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 1.0
        out[subset] = {"n": len(rows), "accuracy": (tp + tn) / len(rows),
                       "balanced_accuracy": ((tn / pos if pos else 1) + (tp / neg if neg else 1)) / 2,
                       "not_delete_f1": f1, "false_non_deletion": fp, "missed_non_deletion": fn,
                       "invalid": sum(pred.get(t) == "INVALID" for t, _ in rows)}
    return out


# ------------------------------------------------------------------- report
def render(results: dict, registry: ModelRegistry) -> str:
    gold_counts = results.get("_gold", {})
    lines = ["# Model Benchmark - open-source models for intent and extraction", "",
             "All candidates are open-weight models run locally with llama.cpp (4-bit GGUF, CPU only: 8-core "
             "Intel i7-1185G7, no GPU). Every request uses a JSON schema that llama.cpp compiles into a grammar, "
             "so malformed output is impossible; temperature 0. Ground truth is described in "
             "`model_benchmark.py` and never comes from a model. The selection rule was fixed before the runs.", "",
             "## Candidates", "", "| Model | Vendor | Params (B) | License | Vision | GGUF |",
             "| --- | --- | ---: | --- | --- | --- |"]
    for k, s in registry.models.items():
        lines.append(f"| `{k}` ({s.display}) | {s.vendor} | {s.params_b} | {s.license} | "
                     f"{'yes' if s.vision else 'no'} | `{s.repo}/{s.file}` |")
    it = results.get("intent", {})
    if it:
        lines += ["", "## Request intent", "",
                  "Prompt v1 asked for one label (DELETE / NOT_DELETE); every model mislabelled requests that ask to "
                  "*verify* a deletion. Prompt v2 asks two yes/no questions (is the request about deleting or "
                  "confirming a deletion? does the customer explicitly want to keep the account?) and derives the "
                  "label; only v2 results are eligible for selection. The challenge set was never used to write "
                  "either prompt.", "",
                  f"Gold: {gold_counts.get('intent_dataset', '?')} unique dataset texts (10 NOT_DELETE, derived from "
                  f"the intake log) + {gold_counts.get('intent_challenge', '?')} team-written challenge texts in 7 "
                  "languages.", "",
                  "| Model | Balanced acc. (all) | Accuracy dataset | Accuracy challenge | NOT_DELETE F1 | False "
                  "non-deletion | Missed non-deletion | Compute (s) | Tokens in / out |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for k, r in sorted(it.items(), key=lambda kv: -kv[1]["metrics"]["all"]["balanced_accuracy"]):
            a, d, c = r["metrics"]["all"], r["metrics"]["dataset"], r["metrics"]["challenge"]
            lines.append(f"| `{k}` | {a['balanced_accuracy']:.3f} | {d['accuracy']:.3f} | {c['accuracy']:.3f} | "
                         f"{a['not_delete_f1']:.3f} | {a['false_non_deletion']} | {a['missed_non_deletion']} | "
                         f"{r.get('compute_s', 0)} | {r.get('input_tokens', 0):,} / {r.get('output_tokens', 0):,} |")
    ex = results.get("extraction", {})
    if ex:
        lines += ["", "## Screenshot and PDF extraction", "",
                  f"Gold: {sum(len(v) for v in GOLD_EXTRACTION.values())} hand-labelled records in 15 artefacts, "
                  f"{sum(r['key'] for v in GOLD_EXTRACTION.values() for r in v)} of them decisive for the audit. "
                  f"End-to-end = share of the {results.get('_e2e_cases_n', '?')} cases whose output depends on an "
                  "artefact that come out identical to the run on the hand-labelled extraction.", "",
                  "| Model | End-to-end cases | Key records found | Key-field accuracy | Record F1 | Compute (s) | "
                  "Tokens in / out |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for k, r in sorted(ex.items(), key=lambda kv: (-kv[1].get("e2e", 0), -kv[1]["scores"]["key_field_accuracy"])):
            s = r["scores"]
            lines.append(f"| `{k}` | {r.get('e2e', 0):.3f} | {s['key_record_recall']:.3f} | "
                         f"{s['key_field_accuracy']:.3f} | {s['record_f1']:.3f} | {r.get('compute_s', 0)} | "
                         f"{r.get('input_tokens', 0):,} / {r.get('output_tokens', 0):,} |")
        for k, r in results.get("reference_hosted", {}).items():
            s = r["scores"]
            lines.append(f"| *{k}* | *{r['e2e']:.3f}* | *{s['key_record_recall']:.3f}* | "
                         f"*{s['key_field_accuracy']:.3f}* | *{s['record_f1']:.3f}* | - | - |")
        if results.get("reference_hosted"):
            lines += ["", "The italic row is the hosted model used by an earlier version of this agent, scored on the "
                      "same gold from cached replies; it is a reference point only and is not eligible for selection."]
    sel = results.get("_selected", {})
    if sel:
        lines += ["", "## Selection", ""] + [f"* **{role}** -> `{m}`" for role, m in sel.items()] + [
            "", "The selection is written to `code/config/models.toml` (`[selected]`) and used by the final run."]
    lines += ["", "## Notes", "",
              "* Hardware and quantisation are identical for every model, so speed numbers are comparable.",
              "* A screenshot can only be read by a model with a vision projector; text-only models are scored on "
              "intent only."]
    lines += [f"* {note}" for note in results.get("_notes", [])] + [""]
    return "\n".join(lines)


def add_ensembles(results: dict) -> None:
    """Every pair of benchmarked models + the rule classifier, majority vote (pre-registered candidate)."""
    from itertools import combinations
    from ghost_audit.intent import vote
    it = results.get("intent", {})
    singles = {k: v for k, v in it.items()
               if "pred" in v and "+" not in k and k != "rules (no model)" and "prompt v1" not in k}
    if len(singles) < 2:
        return
    gold = intent_gold()
    rules = {t: rule_intent(t) for t, _, _ in gold}
    for a, b in combinations(sorted(singles), 2):
        pred = {t: vote([singles[a]["pred"][t], singles[b]["pred"][t], rules[t]]) for t, _, _ in gold}
        it[f"{a} + {b} + rules (vote)"] = {
            "metrics": intent_metrics(gold, pred), "pred": pred,
            "compute_s": round(singles[a].get("compute_s", 0) + singles[b].get("compute_s", 0), 1),
            "input_tokens": singles[a].get("input_tokens", 0) + singles[b].get("input_tokens", 0),
            "output_tokens": singles[a].get("output_tokens", 0) + singles[b].get("output_tokens", 0)}


def add_extraction_ensembles(results: dict) -> None:
    """Union of every pair of transcribe-mode extractions (pre-registered candidate, no new calls)."""
    from itertools import combinations
    from ghost_audit.evidence.multimodal import merge_records
    ex = results.get("extraction", {})
    singles = {k: v for k, v in ex.items() if k.endswith("| transcribe") and "+" not in k}
    if len(singles) < 2:
        return
    reference = pipeline_outputs(GOLD_EXTRACTION)
    cases = results.get("_e2e_cases") or artefact_cases(reference, pipeline_outputs({f: [] for f in GOLD_EXTRACTION}))
    for a, b in combinations(sorted(singles), 2):
        recs = {fid: merge_records(singles[a]["records"].get(fid, []), singles[b]["records"].get(fid, []))
                for fid in GOLD_EXTRACTION}
        outs = pipeline_outputs(recs)
        key = f"{a.split(' |')[0]} + {b.split(' |')[0]} | transcribe"
        ex[key] = {"records": recs, "scores": score_extraction(recs),
                   "e2e": sum(outs[c] == reference[c] for c in cases) / len(cases),
                   "e2e_failures": [c for c in cases if outs[c] != reference[c]],
                   **{f: singles[a].get(f, 0) + singles[b].get(f, 0)
                      for f in ("input_tokens", "output_tokens", "compute_s")}}


def select(results: dict) -> dict:
    sel = {}
    it = {k: v for k, v in results.get("intent", {}).items() if "prompt v1" not in k}
    if it:
        sel["intent"] = max(it, key=lambda k: (it[k]["metrics"]["all"]["balanced_accuracy"],
                                               it[k]["metrics"]["all"]["not_delete_f1"], -it[k].get("compute_s", 0)))
    ex = results.get("extraction", {})
    if ex:
        sel["extraction"] = max(ex, key=lambda k: (ex[k].get("e2e", 0), ex[k]["scores"]["key_field_accuracy"],
                                                   ex[k]["scores"]["record_f1"], -ex[k].get("compute_s", 0)))
    return sel


def write_selection(sel: dict) -> None:
    path = CODE / "config" / "models.toml"
    lines = path.read_text().splitlines()
    sel = dict(sel)
    if " | " in sel.get("extraction", ""):
        sel["extraction"], mode = sel["extraction"].split(" | ")
        lines = [f'extraction_mode = "{mode}"' if l.startswith("extraction_mode = ") else l for l in lines]
    for role, choice in sel.items():
        models = [m.strip() for m in choice.replace(" + rules (vote)", "").split(" + ")]
        value = f'"{models[0]}"' if len(models) == 1 else "[" + ", ".join(f'"{m}"' for m in models) + "]"
        lines = [f"{role} = {value}" if line.startswith(f"{role} = ") else line for line in lines]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["intent", "extraction", "all"], default="all")
    ap.add_argument("--models", default="")
    ap.add_argument("--modes", default="transcribe,json", help="extraction modes to benchmark")
    ap.add_argument("--report", action="store_true", help="only re-render the report from saved results")
    ap.add_argument("--apply", action="store_true", help="write the selection into config/models.toml")
    ap.add_argument("--rescore", action="store_true", help="recompute extraction scores from saved records")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore cached replies so every call is measured (cache is still refreshed)")
    ap.add_argument("--archive-intent", default="",
                    help="rename these models' intent results to '<model> (prompt v1)' before re-running")
    args = ap.parse_args()
    registry = ModelRegistry()
    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}

    for m in filter(None, args.archive_intent.split(",")):
        if m in results.get("intent", {}):
            results["intent"][f"{m} (prompt v1)"] = results["intent"].pop(m)
    if args.rescore:
        reference = pipeline_outputs(GOLD_EXTRACTION)
        cases = artefact_cases(reference, pipeline_outputs({fid: [] for fid in GOLD_EXTRACTION}))
        results["_e2e_cases"], results["_e2e_cases_n"] = cases, len(cases)
        for key, r in results.get("extraction", {}).items():
            r["scores"] = score_extraction(r["records"])
            outs = pipeline_outputs(r["records"])
            r["e2e"] = sum(outs[c] == reference[c] for c in cases) / len(cases)
            r["e2e_failures"] = [c for c in cases if outs[c] != reference[c]]
            print(f"[rescore] {key}: e2e {r['e2e']:.3f} failures {r['e2e_failures']}")
    if not args.report and not args.rescore:
        llm = LLMClient(CODE / "cache" / "llm_cache.json",
                        HERE / "runs" / f"benchmark_{time.strftime('%Y%m%dT%H%M%S')}.jsonl", backend="llama_cpp",
                        use_cache=not args.fresh)
        models = [m for m in (args.models.split(",") if args.models else registry.models) if m]
        manifest = {r["file_id"]: r for r in csv.DictReader(open(DATASET / "evidence_files.csv"))}
        gold = intent_gold()
        results["_gold"] = {"intent_dataset": sum(s == "dataset" for *_, s in gold),
                            "intent_challenge": sum(s == "challenge" for *_, s in gold)}
        results.setdefault("intent", {})["rules (no model)"] = {
            "metrics": intent_metrics(gold, {t: rule_intent(t) for t, _, _ in gold}), "compute_s": 0}
        if args.task in ("extraction", "all"):
            reference = pipeline_outputs({fid: v for fid, v in GOLD_EXTRACTION.items()})
            baseline = pipeline_outputs({fid: [] for fid in GOLD_EXTRACTION})
            cases = artefact_cases(reference, baseline)
            results["_e2e_cases"], results["_e2e_cases_n"] = cases, len(cases)
        try:
            for m in models:
                if not registry.available(m):
                    print(f"[skip] {m}: weights not downloaded")
                    continue
                if args.task in ("intent", "all"):
                    r = run_intent(llm, m, gold)
                    r["metrics"] = intent_metrics(gold, r["pred"])
                    r["errors"] = [t for t, lab, _ in gold if r["pred"].get(t) != lab]
                    prev = results["intent"].get(m, {})
                    if r["live_calls"] == 0 and prev:   # replayed from cache: keep the measured cost
                        for f in ("live_calls", "input_tokens", "output_tokens", "compute_s", "wall_s"):
                            r[f] = prev.get(f, r[f])
                    results["intent"][m] = r
                    print(f"[intent] {m}: {json.dumps(r['metrics']['all'])} compute {r['compute_s']}s", flush=True)
                for mode in (args.modes.split(",") if args.task in ("extraction", "all") and
                             registry.models[m].vision else []):
                    r = run_extraction(llm, m, manifest, mode)
                    r["scores"] = score_extraction(r["records"])
                    outs = pipeline_outputs(r["records"])
                    r["e2e"] = sum(outs[c] == reference[c] for c in cases) / len(cases) if cases else 0.0
                    r["e2e_failures"] = [c for c in cases if outs[c] != reference[c]]
                    prev = results.get("extraction", {}).get(f"{m} | {mode}", {})
                    if r["live_calls"] == 0 and prev:   # replayed from cache: keep the measured cost
                        for f in ("live_calls", "input_tokens", "output_tokens", "compute_s", "wall_s"):
                            r[f] = prev.get(f, r[f])
                    results.setdefault("extraction", {})[f"{m} | {mode}"] = r
                    print(f"[extraction] {m} | {mode}: e2e {r['e2e']:.3f} {json.dumps(r['scores'])} "
                          f"compute {r['compute_s']}s", flush=True)
                    RESULTS.write_text(json.dumps(results, indent=1, ensure_ascii=False))
                RESULTS.parent.mkdir(parents=True, exist_ok=True)
                RESULTS.write_text(json.dumps(results, indent=1, ensure_ascii=False))
        finally:
            llm.close()
    add_ensembles(results)
    if not args.report:
        add_extraction_ensembles(results)
    results["_selected"] = select({k: {m: v for m, v in d.items() if m != "rules (no model)"}
                                   for k, d in results.items() if k in ("intent", "extraction")})
    RESULTS.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    REPORT.write_text(render(results, registry))
    print(f"selected: {results['_selected']}  report -> {REPORT}")
    if args.apply:
        write_selection(results["_selected"])
        print("written to config/models.toml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
