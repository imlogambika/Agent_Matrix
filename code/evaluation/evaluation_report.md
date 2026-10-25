# Evaluation Report - Ghost Data Still Exists

All numbers below are produced by scripts in this folder and can be reproduced from the
repository root:

```bash
python3 code/main.py --requests sample      # predictions for the 32 public samples
python3 code/evaluation/evaluate.py         # metrics vs dataset/sample_output.csv
python3 code/evaluation/diff_cases.py       # gold vs predicted, step by step, for non-exact cases
python3 code/evaluation/custom_cases.py     # 7 team-authored cases with gold answers
python3 code/evaluation/validate_output.py  # contract + evidence-admissibility checks on output.csv
python3 -m unittest discover -s code/tests  # 18 unit tests of the deterministic core
python3 code/evaluation/model_benchmark.py --report   # open-source model comparison (section 8)
```

## 1. Headline results (32 public samples with gold answers)

| Metric | Score |
| --- | ---: |
| Case-level status accuracy (`deletion_status`) | **100.0 %** (32 / 32) |
| Step precision (full-tuple match) | **99.47 %** |
| Step recall (full-tuple match) | **97.93 %** |
| Step F1 | **98.69 %** |
| Resource discovery recall / precision | 96.40 % / 99.07 % |
| Evidence-id accuracy on matched steps | **100.0 %** |
| Sequence accuracy (whole `steps_json` identical, in order) | 87.50 % (28 / 32) |
| Order accuracy when the step set is right | **100.0 %** |
| Custom evaluation cases (section 4) | 7 / 7 exact |
| Contract validation of the 518-row `output.csv` | 0 violations |

### Field-level accuracy

Each gold step is aligned with the predicted step for the same `(system_id, resource_id)` that
agrees on the most fields; a missing resource counts as wrong on every field.

| Field | Accuracy |
| --- | ---: |
| `phase` | 97.93 % |
| `system_id` | 97.93 % |
| `resource_id` | 97.93 % |
| `operation` | 97.93 % |
| `state` | 97.93 % |
| `evidence_id` | 97.93 % |

All residual field errors come from the four missing resources described in section 3; every
step whose resource is found is correct on all six fields.

### Metric definitions

* **Step-level**: a predicted step is a true positive only if all of `phase, system_id,
  resource_id, operation, state, evidence_id` equal a gold step (multiset match per case).
* **Field-level**: see above; isolates *which* attribute is wrong.
* **Sequence accuracy**: the complete ordered array equals the gold array.
* **Order accuracy given same steps**: among cases whose step multiset is exactly right, the
  share whose order is also right - this isolates the ordering logic from discovery errors.

## 2. Per-case results (public samples)

| Case | Gold | Predicted | Steps matched | Exact |
| --- | --- | --- | ---: | --- |
| case_01 | COMPLETE | COMPLETE | 1/1 | yes |
| case_02 | COMPLETE | COMPLETE | 1/1 | yes |
| case_03 | COMPLETE | COMPLETE | 3/3 | yes |
| case_04 | COMPLETE | COMPLETE | 1/2 | no |
| case_05 | INCOMPLETE | INCOMPLETE | 3/3 | yes |
| case_06 | INCOMPLETE | INCOMPLETE | 4/4 | yes |
| case_07 | INCOMPLETE | INCOMPLETE | 4/4 | yes |
| case_08 | INCOMPLETE | INCOMPLETE | 4/4 | yes |
| case_09 | INCOMPLETE | INCOMPLETE | 5/5 | yes |
| case_10 | INCOMPLETE | INCOMPLETE | 4/4 | yes |
| case_11 | INCOMPLETE | INCOMPLETE | 14/14 | yes |
| case_12 | INCOMPLETE | INCOMPLETE | 5/5 | yes |
| case_13 | INCOMPLETE | INCOMPLETE | 8/8 | yes |
| case_14 | INCOMPLETE | INCOMPLETE | 5/5 | yes |
| case_15 | INCOMPLETE | INCOMPLETE | 5/5 | yes |
| case_16 | INCOMPLETE | INCOMPLETE | 7/7 | yes |
| case_17 | INCOMPLETE | INCOMPLETE | 5/5 | yes |
| case_18 | INCOMPLETE | INCOMPLETE | 13/14 | no |
| case_19 | INCOMPLETE | INCOMPLETE | 8/8 | yes |
| case_20 | INCOMPLETE | INCOMPLETE | 7/8 | no |
| case_21 | COMPLETE | COMPLETE | 3/3 | yes |
| case_22 | INCOMPLETE | INCOMPLETE | 8/8 | yes |
| case_23 | INCOMPLETE | INCOMPLETE | 7/8 | no |
| case_24 | INCOMPLETE | INCOMPLETE | 7/7 | yes |
| case_25 | COMPLETE | COMPLETE | 3/3 | yes |
| case_26 | COMPLETE | COMPLETE | 6/6 | yes |
| case_27 | COMPLETE | COMPLETE | 6/6 | yes |
| case_28 | COMPLETE | COMPLETE | 6/6 | yes |
| case_29 | INCOMPLETE | INCOMPLETE | 14/14 | yes |
| case_30 | INCOMPLETE | INCOMPLETE | 14/14 | yes |
| case_31 | COMPLETE | COMPLETE | 3/3 | yes |
| case_32 | INCOMPLETE | INCOMPLETE | 5/5 | yes |

## 3. Remaining sample misses (root-caused)

| Case | Missed step | Root cause | Recoverable? |
| --- | --- | --- | --- |
| case_18 | `VERIFY_RECORD_PRESENT security_service audit_record_6645` | The id `audit_record_6645` does not occur in any participant-facing file (CSV, SQL, JSON, log, text, PDF text or image). | No - not derivable from the inputs. |
| case_20 | `VERIFY_RECORD_PRESENT security_service audit_record_5569` | Same: the id appears nowhere in the participant data. | No. |
| case_04 | `VERIFY_RECORD_ABSENT account_service account_004` | No activity row and no evidence row mentions `account_004`; it is only implied by the hand-built naming of that sample family. We deliberately do not guess ids from naming conventions. | Only by guessing ids - rejected. |
| case_23 | predicted `account_7015` instead of `account_410` | Two deletion-worker profile deletes in the same batch, both `FAILED`, neither named by any evidence, and two `NOT_STARTED` candidate cases. Offset, claim-match and co-occurrence signals are identical, so this is a genuine tie. | No deterministic signal. |

## 4. Team-authored evaluation cases

`custom_cases.py` builds a self-contained synthetic dataset (identities, audit history, activity,
rules, SQL / JSON / log evidence) in a temporary directory, runs the **real** pipeline on it with
model calls disabled, and scores it with the same `evaluate.score` function. Each case isolates
one failure mode the public samples do not test on its own.

| Id | Scenario | What it checks | Gold | Result |
| --- | --- | --- | --- | --- |
| C1 | Replica drift | Profile removed from the primary table but still present in `customer_data_replica_eu_west` of a regional snapshot. The record must be reported present (citing the snapshot) and deleted; the retained audit record is verified present. | INCOMPLETE, 4 steps | exact |
| C2 | Late evidence | A readback export before `audit_at` shows a 404 profile and an empty search result; a bundle exported **after** `audit_at` shows the search document again. The late bundle must not change the answer. | COMPLETE, 2 steps citing the pre-audit export | exact |
| C3 | Historical identifier + look-alike | Request uses the *old* email. A marketing contact keyed by the old email's hash must be found (delete); a search hit for `ines.v.jr@...` belongs to someone else and must be ignored. | INCOMPLETE, 4 steps | exact |
| C4 | Non-deletion request | Spanish access request that says it does not want the account closed. | NOT_APPLICABLE, `[]` | exact |
| C5 | Tombstone with PII | Deleted-account reference still carries name and email. Keep the stable `user_id`, redact the rest. | INCOMPLETE, 4 steps | exact |
| C6 | Resurrecting connector | Workflow claimed the search entry deleted, but an active connector re-created it. Findings for both; remediation must disable the flow **before** deleting the entry. | INCOMPLETE, 7 steps | exact |
| C7 | Decoy in the batch | Another subject's profile (look-alike name) is processed in the same deletion batch window. It must not be attributed to the requester. | COMPLETE, 1 step | exact |

Result: status accuracy 100 %, step F1 100 %, evidence accuracy 100 %, sequence accuracy 100 %.

## 5. Full evaluation set (518 requests, no gold available)

Checks that do not need gold answers:

* `validate_output.py`: header, 518 rows, sorted by case number, valid JSON, keys, consecutive
  step numbers, allowed (phase, operation, state) pairs, contract ordering, `COMPLETE` =
  evidence-backed verification only, `INCOMPLETE` findings cite evidence and future steps do
  not, `NOT_APPLICABLE` = `[]` -> **0 violations**.
* Status mix: 355 INCOMPLETE, 153 COMPLETE, 10 NOT_APPLICABLE (samples: 22 / 10 / 0).
* Claim versus reality:

  | Workflow claimed | Audit result | Cases |
  | --- | --- | ---: |
  | COMPLETE | INCOMPLETE (ghost data) | 163 |
  | PARTIALLY_COMPLETE | INCOMPLETE | 116 |
  | NOT_STARTED | INCOMPLETE | 76 |
  | COMPLETE | COMPLETE | 91 |
  | PARTIALLY_COMPLETE | COMPLETE | 36 |
  | NOT_STARTED | COMPLETE | 26 |
  | NOT_STARTED | NOT_APPLICABLE | 10 |

* Every authorised deletion case resolves to exactly one primary profile, except the 10
  artefact-only cases (each built around a single screenshot or PDF plus a data-catalog link).
* The 10 non-deletion requests (access, export, correction, marketing opt-out, login help) are
  exactly the 10 cases for which the privacy-intake workflow logged `CLASSIFY_REQUEST ...
  non-deletion`; the selected intent ensemble labels all 398 unique texts correctly (section 8).
* Decoys: 300 `profile_service` lookups exported on 2026-09-30 resolve to known users but fall
  outside every case window, and none is attached to a case.

### Validating sample-derived heuristics on the whole dataset

Two batch-matching rules were first noticed on the samples; both were re-checked on every
resource whose owner is proven by evidence (not just the 32 samples):

| Rule | Check on evidence-owned resources (all 550 cases) |
| --- | --- |
| The primary profile delete's result mirrors the workflow's reported status (SUCCESS ↔ COMPLETE, PARTIAL ↔ PARTIALLY_COMPLETE, FAILED ↔ NOT_STARTED) | 247 / 247 hold |
| Analytics rows that still carry identifiers sit in cases with violations; identifier-free rows therefore go to otherwise clean cases | 78 of 79 evidence-owned analytics rows are the exposed kind in INCOMPLETE cases |
| Deletion-worker offsets from `reported_at` | measured on all owned activities: primary profile 44-80 min before; successful treatments 3-35 min before; failures and checks 5-135 min after |

## 6. Failures found and improvements made

The pipeline was developed against the samples with a diff tool (`diff_cases.py`) and a case
inspector (`code/tools/inspect_case.py`). Each row is one iteration; scores are on the samples.

| # | Failure observed | Fix | Status acc. | Step F1 |
| --- | --- | --- | ---: | ---: |
| 0 | Baseline: identifier links + latest observation + simple batch window | - | 84.4 % | 0.706 |
| 1 | Requests such as "an old download link still works, please delete that export" were classified as non-deletion because they mention *download* / *copy* | Only explicit negations ("not asking for deletion", "do not close") override deletion verbs; model classification added on top | 93.8 % | 0.740 |
| 2 | A bundle **exported after** `audit_at` (inner snapshot dated exactly at `audit_at`) was cited and even flipped states | File-level admissibility: the manifest `captured_at` must be ≤ `audit_at` (confirmed on case_09, whose log line before the audit is newer than the cited inventory but the file was exported later) | 100 % | 0.927 |
| 3 | A later full capture that simply did not list a record overrode an explicit "present on the failover replica" reading (case_05) | Full captures only prove absence when no admissible observation names the record | 100 % | 0.927 |
| 4 | Unowned 404 profiles and anonymised analytics went to the lowest case number in an overlapping window | Offset plausibility scoring per action type; one profile / one analytics row per case; most-constrained-first matching; leftovers attributed to cases not in the request files (e.g. `analytics_9447` belongs to user_31, whose request is not in either file) | 100 % | 0.951 |
| 5 | Ties between batch-mates | Primary-delete result ↔ reported status (247/247 on owned accounts) and anonymised-analytics ↔ clean-case preference | 100 % | 0.971 |
| 6 | Two evaluation-set log files produced no facts | Parser for the pipe-delimited stream format (`| LEVEL | service | trace | event |`) | - | - |
| 7 | Screenshot / PDF facts unavailable; support attachment in a PDF treated as a message | Model-based extraction into a fixed schema; resource prefix fixes the system, the model may only refine the category within it (later moved to open-source models, rows 10-14) | 100 % | 0.987 |
| 8 | Screenshot context leaked: a ticket id printed in a profile side panel and five unrelated audit events became case resources; a register row for `user_17` was aliased onto `tombstone_17` | Screenshot records link only when the data catalog confirms them or the record shows the subject's own email/phone; customer-key aliases only resolve tombstones | 100 % | 0.987 |
| 9 | Tombstone-family cases cited an unrelated inventory as proof of profile absence | Prefer the case's own primary capture before unrelated inventories | 100 % | 0.987 |
| 10 | Switching to small open-source vision models: asked to fill the record schema directly, Qwen2.5-VL-3B put emails in id fields, duplicated rows and got backup states wrong (state accuracy 6%) | **Transcribe-then-parse**: the model only reads the screen, a fixed parser structures it; PDFs need no model. End-to-end artefact cases 8/13 -> 12/13 (13/13 after row 11) | - | - |
| 11 | The model skipped a small subtitle with `support_1034` but read the ticket number `CS-5034` | Ticket ids learned from structured records (`ticket_id`) resolve ticket numbers printed on screens | - | - |
| 12 | Every model read "please verify my deletion" as an information request (Qwen2.5-VL-3B 6 errors, Qwen2.5-1.5B 24) | Intent prompt v2 asks two yes/no questions (about deletion? explicitly keep the account?) - errors fell to 2 and 1 | - | - |
| 13 | Remaining single-model errors did not overlap (Llama: 1, Qwen-VL: 2) | Pre-registered vote: two models + the rule classifier -> 420/420 | - | - |
| 14 | Gemma-3-4B ran 12x slower: no prompt-prefix reuse with sliding-window attention | Per-model llama-server flag `--swa-full` (17 s -> 2.7 s per request) | - | - |

Robustness: every model reply is produced at temperature 0 under a JSON grammar and cached by a
hash of model, prompt, image and schema. `python3 code/main.py --offline` (no model server at
all) reproduces the final `output.csv` byte for byte, and the selected intent vote agrees with the
intake-log labels on all 398 unique request texts, so the `NOT_APPLICABLE` decisions do not hinge on
any single model's reply.

## 7. How much of this is in-sample? (evaluation honesty)

The 32 gold samples were also used while developing the rules, so the section 1 numbers are
**in-sample** and optimistic by construction. Evidence that the behaviour generalises comes from
material that was *not* used to tune it:

| Out-of-sample check | Result |
| --- | --- |
| 7 team-authored synthetic cases (section 4), built after the rules were written | 7 / 7 exact |
| Sample-derived batch rules re-checked on every evidence-owned resource in all 550 cases (section 5) | 247 / 247 and 78 / 79 |
| Contract + evidence admissibility on the 518 evaluation rows (`validate_output.py`) | 0 violations; 1,248 / 1,248 citations exist and predate `audit_at` |
| The same validator run on the organisers' `sample_output.csv` | 0 violations, which independently confirms the file-level admissibility rule |
| Unit tests of parsers, identity, policy, ordering, admissibility, matching (`tests/test_core.py`) | 18 / 18 pass |
| Intent rules on an unseen multilingual challenge set (section 8) | 72.7% accuracy, so the rules alone would not generalise; a model is needed |

## 8. Open-source model selection

See `model_benchmark.md` (generated by `model_benchmark.py`). Candidates are open-weight models from
four vendors run locally with llama.cpp; ground truth is model-free (intake-log labels and a
team-written challenge set for intent; hand-labelled screenshot and PDF records for extraction), and
the selection rule was fixed before the runs.

| Role | Selected | Score | Best alternative |
| --- | --- | --- | --- |
| Intent | Llama-3.2-3B + Qwen2.5-VL-3B + rule classifier (majority vote) | 420 / 420 (balanced accuracy 1.000) | Llama-3.2-3B alone 0.999; Qwen2.5-VL-3B 0.998; Qwen2.5-1.5B 0.975; SmolVLM2-2.2B 0.799; rules alone 0.921 |
| Screenshot extraction | Qwen2.5-VL-3B, transcribe-then-parse | 13 / 13 artefact-dependent cases identical to the run on hand-labelled extraction | Same model filling the JSON schema directly: 8 / 13 |

Gemma-3-4B could not be run to completion on the CPU-only laptop in the time available and is listed
as not eligible rather than estimated.

## 9. Known limitations

* Ids that never appear in participant data (section 3) cannot be produced.
* Within one deletion batch, two resources with no identifiers, the same action, the same result
  and no evidence co-occurrence are indistinguishable; the tie is broken by case number, which is
  right only by chance. We looked for a signal that would break such ties and found none: on 156
  pairs of evidence-owned primary-profile deletes that share a batch, the order of the deletes
  agrees with case-number order in 78, `requested_at` order in 76 and user-number order in 80 -
  chance level. These cases are therefore flagged `BATCH_TIE` in `escalation_report.md` instead
  of being presented as certain (on the samples: 4 tie cases, 3 broken correctly; the miss is
  case_23).
* Screenshot extraction depends on the model; the extraction is constrained to a schema,
  cached, and linked only when corroborated (catalog link or the subject's own identifier).
* Resource ids are ordered as strings within a (phase, operation, system) group, following a
  plain lexicographic reading of "ascending `resource_id`".
