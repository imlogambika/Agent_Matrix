# Ghost Data Still Exists - Deletion Audit Agent

An evidence-first agent that decides, for every privacy request, whether a user's
personal data is really gone at `audit_at`, and emits the ordered findings,
remediation and verification steps required by the challenge contract.

The design principle: **use a model where perception or language is needed, and
deterministic, auditable code everywhere a privacy decision is made.**

| Concern | Approach |
| --- | --- |
| Does the request authorise deletion? (10+ languages, negations) | Open-source LLM classifies every unique text with a schema-constrained answer; multilingual rule classifier as cross-check / fallback |
| Screenshots and PDFs | Open-source vision-language model transcribes each screenshot once; a fixed parser turns the text into records. PDFs are parsed from their text layer without a model |
| Which models | Chosen by a benchmark of open-weight models from 4 vendors against model-free ground truth with a pre-registered rule (`evaluation/model_benchmark.md`): intent = Llama-3.2-3B + Qwen2.5-VL-3B + rules vote (420/420), extraction = Qwen2.5-VL-3B |
| SQL, JSON, logs (6 syntaxes), text ledgers | Deterministic parsers into one `Observation` model |
| Identity | Current + historical emails, phones, email hashes (sha256 of email) and user ids |
| Which records belong to the case | Identifier links inside the case window, data-catalog links, and the deletion-worker batch (offset plausibility, decoy rejection, slot matching) |
| State at `audit_at` | Latest admissible evidence wins; workflow results are claims only |
| Policy | `deletion_rules.csv` per data category |
| Ordering | Phase -> operation order -> `system_id` -> `resource_id` |
| Uncertainty | Escalation layer flags what the evidence cannot settle (batch ties, weak checks, data reappearing after `audit_at`) in `evaluation/escalation_report.md`; it never changes `output.csv` |

## How the submission meets each evaluation criterion

| Criterion (problem statement / README) | Where it is handled | Measured evidence |
| --- | --- | --- |
| Correct `deletion_status` | `pipeline.py`, `state.py`, `policy.py` | 100% on the 32 gold samples (`evaluation/evaluation_report.md`) |
| Current and historical identity resolution | `data.py` (every email, phone, email hash ever held; hashes derived as sha256) | All 518 request emails resolve, 100 of them historical |
| Remaining records and recreation risks discovered | `linker.py` (identifier links + deletion-batch matching), `state.py` | Resource recall 96.4% / precision 99.1% on samples |
| Cited evidence | file-level admissibility in `state.py`; `evaluation/validate_output.py` | 100% evidence-id accuracy on samples; all 1,248 citations in `output.csv` exist and predate `audit_at` |
| Policy decisions | `policy.py` driven by `deletion_rules.csv` | Unit tests in `tests/test_core.py`; 7 team-authored cases |
| Completeness and order of `steps_json` | `policy.order_steps` | 100% order accuracy whenever the step set is right; 0 contract violations |
| Multimodal interpretation | `evidence/multimodal.py` + `evidence/transcript.py` with an open-source vision model | End-to-end agreement on the 13 artefact-dependent cases (`evaluation/model_benchmark.md`) |
| Retrieval | `evidence/store.py` indexes every observation by resource id, identifier and time window | 94 files, ~2,900 observations, 358 full captures |
| Deterministic verification | `evaluation/validate_output.py`, `tests/`, `tools/inspect_case.py` | 29 unit and safety tests; byte-identical re-runs |
| Escalation / human review | `ghost_audit/review.py` | `evaluation/escalation_report.md`: every flagged case with its resolution path |
| Batching, caching, token efficiency | unique-text dedupe, llama.cpp prefix KV cache, one extraction per file, reply cache, grammar-constrained JSON | `evaluation/usage_report.md` |
| Required reports | `evaluation/evaluation_report.md`, `evaluation/usage_report.md` | Every required item present, numbers reproducible by the scripts listed there |
| No secrets | local open-weight models, no API key anywhere | `grep` for keys in the archive returns nothing |

## Quick start

```bash
# from the repository root
python3 code/main.py                       # audits dataset/deletion_requests.csv -> ./output.csv
python3 code/main.py --requests sample     # audits the 32 samples -> code/evaluation/sample_predictions.csv
python3 code/evaluation/evaluate.py        # scores sample predictions against dataset/sample_output.csv
python3 code/evaluation/custom_cases.py    # runs the 7 team-authored evaluation cases
python3 code/tools/inspect_case.py case_64 # explains one case: links, every observation, final steps
python3 code/evaluation/validate_output.py # contract + evidence-admissibility checks on output.csv
python3 -m unittest discover -s code/tests # 29 unit and safety regression tests
python3 code/tools/build_explorer.py       # static Audit Explorer page -> explorer/index.html
```

**Reproduce without any model download:** `python3 code/main.py --offline` rebuilds `output.csv`
byte-for-byte in a few seconds from the model replies cached in `code/cache/llm_cache.json`
(the deterministic core never needs a model).

Requirements: Python 3.11+ (standard library, plus `numpy` and `Pillow` for screenshot cropping and
`huggingface_hub` as an optional file locator), `pdftotext` (poppler-utils) for PDF text, and for the
model-backed steps a llama.cpp `llama-server` binary. No GPU, API key or paid service is needed.

### Open-source models (local, free)

```bash
# 1. llama.cpp CPU build (any recent release works; this one was used)
mkdir -p ~/.local/opt && cd ~/.local/opt
curl -sL -o llama.tgz https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-ubuntu-x64.tar.gz
mkdir -p llama.cpp-b11669 && tar -xzf llama.tgz -C llama.cpp-b11669
# 2. the selected GGUF weights are downloaded automatically on first use into ~/.local/opt/ghost-models
#    (or set GHOST_MODELS_DIR); to benchmark all candidates:  python3 code/evaluation/model_benchmark.py
```

`code/config/models.toml` lists every candidate model (repo, file, licence) and the `[selected]`
model per role; `LLAMA_SERVER` and `GHOST_MODELS_DIR` override paths. The client starts one
`llama-server` at a time, asks for a JSON schema on every request (llama.cpp compiles it into a
grammar, so output is always valid JSON) and records token counts and timings in a usage ledger
(`code/evaluation/runs/`).

| Backend | How it is selected | Notes |
| --- | --- | --- |
| `llama_cpp` (default) | llama-server binary + selected weights present | Open-weight models on CPU, no fees |
| `offline` | `--offline`, or no model available | Rule classifier for intent, cached replies for images, regex for PDFs |
| `anthropic_api`, `claude_cli` | only with `--backend ...` | Optional hosted alternatives; not used for the submission |

Model replies are cached in `code/cache/llm_cache.json` (keyed by a hash of model, prompt, image
and schema), so re-runs are deterministic and free. `--no-cache` forces fresh calls. Every reply the final run
uses was generated live once during the measured benchmark runs; `evaluation/final_run_cost.py`
matches each of the final run's 807 model requests to the call that generated it, and
`evaluation/usage_report.md` reports those calls (tokens, model time, estimated cost).

## Architecture

```
dataset/ ──► EvidenceStore ──────────────────────────────────────────────┐
             ├ sql_snapshots.py  inventory, primary/replica drift tables  │ Observation(file, ts, system,
             ├ json_records.py   bundles, readback exports, ledgers       │   resource, present, PII fields,
             ├ logs.py           6 log syntaxes, trace-id stitching       │   enabled, restore_allowed, ids)
             ├ text.py           shift handover ledger                    │ ScopeCapture(file, ts, system,
             ├ multimodal.py ──► VLM transcript ─┐                        │   resources listed)
             └ transcript.py  ◄── PDF text ──────┘ same schema            │
                                                                          ▼
requests ──► intent.py (LLM + rules) ──► IdentityResolver ──► Linker ──► state.reconstruct ──► Policy ──► steps
                                         current+historical    explicit     latest admissible     deletion_rules  │
                                         email/phone/hash      + batch      evidence <= audit_at   .csv            ▼
                                                                                                   review.py (escalation report)
```

### Pipeline stages

1. **Intent** (`intent.py`, `prompts/request_intent.md`). Non-deletion requests (access, export,
   correction, marketing opt-out, "do not close my account") become `NOT_APPLICABLE` with `[]`.
2. **Identity** (`data.py`). The request email may be current or historical. All emails, phones
   and email hashes a user ever had are indexed; hashes are also derived as sha256(lower(email)).
   Names are never used for linking (look-alike decoys share names).
3. **Evidence** (`evidence/`). Every file in `evidence_files.csv` is parsed into `Observation`s.
   Logs are stitched by trace id because the lookup key is logged on the *start* event and the
   resource/result on the *completion* event. SQL topology snapshots keep primary and replica
   rows separately so a copy surviving on any replica keeps the record present.
4. **Membership** (`linker.py`).
   * Explicit: an observation timestamped inside `[requested_at, audit_at]` whose identifiers
     resolve to the subject, or a data-catalog `LINK_RESOURCE_IDENTITY` activity. Screenshot
     records count only if the catalog confirms them or the record itself shows the subject's
     email/phone (a ticket id printed in a profile side panel is not proof).
   * Batch: the deletion worker acts on a case's resources at characteristic offsets from
     `reported_at` (primary profile 40-80 min before; successful treatments 0-35 min before;
     failures and status checks 5-140 min after). Resources owned by somebody else are decoys.
     Identifier-free resources (404 lookups, anonymised analytics) are matched most-constrained
     first: one primary profile / one analytics profile per case, evidence co-occurrence,
     primary-delete result matching the workflow's reported status, anonymised analytics placed
     with otherwise clean cases. Left-overs belong to cases that are not in the request files.
5. **State at audit_at** (`state.py`). A file is admissible only if its manifest `captured_at`
   is at or before `audit_at` (the public samples confirm this file-level rule), and only its
   observations at or before `audit_at` are used. The latest admissible observation decides.
   Workflow results (`SUCCESS`, `PARTIAL`, `FAILED`) never change a state. A full capture that
   omits a record proves absence only when no admissible observation names the record.
6. **Policy** (`policy.py`). Category -> treatment from `deletion_rules.csv`:
   delete (profile, search entry, export, attachment, marketing, cache, secondary id),
   redact (support message with shared context, email log, tombstone - keep `user_id` only),
   anonymise (analytics, telemetry), retain (security audit log, legal hold, backup with restore
   blocked), disable (active data flow).
7. **Steps** (`pipeline.py`, `policy.py`). `COMPLETE` cases get evidence-backed verification
   steps only. `INCOMPLETE` cases get findings (citing the proving evidence), the remediation
   plan and the post-remediation checks (empty `evidence_id`). Order: FINDING -> REMEDIATION ->
   VERIFICATION; within a phase the operation order of the contract, then `system_id`, then
   `resource_id`. Flows are disabled and restores blocked before records are changed.
8. **Escalation** (`review.py`). Each decided case is checked for what the evidence cannot settle
   on its own: an identifier-free record two cases fit equally (`BATCH_TIE`), a COMPLETE check
   backed only by a workflow record (`WEAK_VERIFICATION`), data that reappears in evidence
   captured after `audit_at` (`POST_AUDIT_REAPPEARANCE`), plus auto-resolved signals (intent
   vote split, a misread id repaired). The flags and their resolution paths go to
   `evaluation/escalation_report.md`; `output.csv` is never changed by this layer.

## Repository layout

```
code/
  main.py                      CLI entry point
  ghost_audit/                 the agent
    data.py                    CSV loading, identity resolution
    evidence/                  parsers + multimodal extraction + store
    intent.py                  request classification
    linker.py                  case membership
    state.py                   state reconstruction at audit_at
    policy.py                  deletion rules, step ordering
    pipeline.py                orchestration
    review.py                  escalation layer (what needs a reviewer, and why)
    llm.py                     model client, cache, usage ledger
    local_models.py            llama.cpp server manager + model registry
  config/models.toml           candidate open-weight models and the selected model per role
  prompts/                     model prompts (Markdown)
  cache/llm_cache.json         cached model replies (re-runs are free and deterministic)
  evaluation/
    evaluate.py                metrics vs gold
    diff_cases.py              gold vs predicted side by side
    custom_cases.py            7 team-authored cases with gold answers
    usage_report.py            renders usage_report.md from a run ledger
    final_run_cost.py          matches a cached final run's requests to the live calls that generated them
    validate_output.py         contract + evidence-admissibility checks on output.csv
    model_benchmark.py         open-source model benchmark and selection (model_benchmark.md)
    escalation_report.md       cases flagged for review by the escalation layer
    evaluation_report.md       metrics, custom cases, failures and improvements
    usage_report.md            token and cost report of the final run
  tools/inspect_case.py        per-case explanation for debugging and review
  tools/build_explorer.py      static Audit Explorer page (deployable to any static host, e.g. Vercel)
  tests/test_core.py           unit tests of parsers, identity, policy, ordering, matching
```
