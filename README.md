# Agent Matrix

**Don't trust a deletion status. Verify the evidence.**

A privacy workflow can report a deletion as *Complete* while the person's data still sits in a
replica, a search index, a support attachment, an export link or a restorable backup, or while
an active sync is about to bring it back. Agent Matrix audits each deletion request against the
evidence and refuses to call a deletion complete unless admissible evidence proves it.

On the 518 requests of the *Ghost Data Still Exists* dataset:

| | Cases |
| --- | ---: |
| Requests the workflow reported **COMPLETE** that still hold personal data at `audit_at` | **163** |
| Decisions that pass the completion guard (every COMPLETE rests on evidence files captured before `audit_at`) | **518 / 518** |
| Cases sent to human review instead of being decided silently | **93** |
| Requests correctly left alone because they do not ask for deletion | 10 |

## How it works: five agents, one deterministic gate

```
request ─► intent-classifier ─► identity-resolver ─► evidence-investigator ─► historical-auditor ─► decision-verifier ─► output.csv
           2 open models +      current + historical  94 files: SQL replicas,  state of each record     policy, ordered steps,   review_queue.csv
           rules vote           emails, phones,       JSON, 6 log formats,     at audit_at; later       COMPLETION GUARD,
           (confidence,         hashes; conflicts     PDFs, screenshots        evidence is ignored      escalation, human
           explanation)         never merged          (local VLM)                                       hand-off
                    └────────────── every step recorded as a Failproof AI session (no personal data) ──────────────┘
```

| Agent | Decides | How | Output it records |
| --- | --- | --- | --- |
| `intent-classifier` | Does the request authorise deletion? (any language; access, export, correction and opt-out requests are not deletions) | Llama-3.2-3B and Qwen2.5-VL-3B (local, open weights) vote with a multilingual rule classifier | label, confidence, per-voter explanation; `degraded` when a model gave no valid answer |
| `identity-resolver` | Every identifier the person ever held | identity snapshot + audit-log history + derived email hashes; an identifier held by two people is never used | user id, how it matched (current or historical email), counts by identifier type |
| `evidence-investigator` | Which records in which sources are theirs | identifier links inside the case window, data-catalog links, deletion-worker batch matching | records, evidence files, source types, unresolved ties |
| `historical-auditor` | The state of each record at `audit_at` | only files captured at or before `audit_at`; a copy on any replica means present | present / PII exposed / flow active / restore allowed, proving evidence id and its capture time, later observations ignored |
| `decision-verifier` | COMPLETE / INCOMPLETE / NOT_APPLICABLE and the steps | `deletion_rules.csv` policy; remediation ordered so flows are disabled and restores blocked before records change; then the **completion guard** and the **escalation review** | status, steps, guard verdict, review flags |

Models are used only for language and pixels. **Every decision is deterministic code**, so it can be
reproduced, explained and cited. An LLM can never mark a deletion complete.

### The completion guard (`code/ghost_audit/guard.py`)

Before anything is written, every decision is re-checked against invariants that do not depend on how
the decision was reached:

* **COMPLETE** has only verification steps. Each cites an evidence **file** that exists and was captured at
  or before `audit_at`. A workflow activity is the claim under audit, so it is **never accepted as
  proof**.
* **INCOMPLETE** has at least one cited finding. Remediation and future checks carry no evidence. Every
  remediated record has a verification check.
* **NOT_APPLICABLE** is allowed only when the request does not authorise deletion.

If any case fails, `main.py` **does not write `output.csv`** (exit code 3) and writes `output.blocked.csv`
plus the review queue instead. The audit fails closed.

### Needs review, without breaking the output contract

The official output allows COMPLETE, INCOMPLETE and NOT_APPLICABLE. Cases the evidence cannot settle keep
their official status and are listed in `code/evaluation/review_queue.csv` as `NEEDS_REVIEW`, with the reason
and the resolution path:

| Signal | Cases (eval) | Meaning |
| --- | ---: | --- |
| `BATCH_TIE` | 75 | An identifier-free record two cases fit equally. Three candidate tie-breakers were tested on 156 evidence-owned pairs and were all at chance (78, 76 and 80 of 156), so the honest answer is a flag, not a guess |
| `POST_AUDIT_REAPPEARANCE` | 23 | Compliant at `audit_at`, but evidence captured later shows the data again |
| `INTENT_FALLBACK`, `IDENTITY_UNRESOLVED`, `GUARD_REJECTED` | 0 | Raised when a model fails, an email matches nobody, or the guard rejects a decision |

## Failproof AI in this project

Failproof plays three distinct roles, kept separate below:

**1. Governing the coding agent that built it (Claude Code + Failproof hooks on the developer machine).**
These denials are real ones from this build session:

* The always-on `block-failproofai-commands` builtin denied the agent's `failproofai --version` and
  `failproofai policies` calls. The agent cannot inspect or reconfigure its own guardrails.
* The same guard denied the agent reading or writing Failproof's own state (`~/.failproofai`, and a heredoc that
  mentioned a `.failproofai/policies` path). Policy files were therefore drafted in `policy-drafts/` for the
  operator to install.

**2. Observing the audit agents at runtime (`failproofai-sdk`, `code/ghost_audit/telemetry.py`).**
`python3 code/main.py --failproof` records:

* one Failproof **session per case**, with the orchestrator and the five agents as nested agents;
* each agent's work as a tool call with its structured result;
* the completion guard and the escalation review as **hooks** (`outcome="rejected"` when the guard blocks a
  decision);
* `human_wait` for every NEEDS_REVIEW case;
* model calls as `model_request` / `model_response` with token counts and duration.

Events contain ids, counts and decisions only. A scan of a full 518-case run (16,061 events) found 0 email
addresses, names, phone numbers or request texts. A full run writes **520 sessions / 16,061 events** to the SDK
spool. The Failproof daemon ships them to the organisation once the machine is connected (see *Connecting your
Failproof organisation*). Until then they wait in the spool, so a dashboard showing "waiting for your first
session" means the machine is not connected yet.

**3. Policies that protect the audit from the agents that work on it (`governance/policies/`).**
An auditor is only trustworthy if nobody can quietly change its evidence, its verdicts or what its models said.
No builtin protects a data directory or a verdict file, so six `PreToolUse` policies were written. Each one closes
a specific way an agent could corrupt *this* system:

| Policy | Mode | Closes |
| --- | --- | --- |
| `protect-audit-evidence` | deny | Deleting, moving, editing or planting files under `dataset/` (shell, Write and Edit) |
| `block-verdict-tampering` | deny | Hand-writing `output.csv` or the review queue, which would bypass the completion guard; verdicts come only from `python3 code/main.py` |
| `block-fabricated-model-replies` | deny | Editing `llm_cache.json`, which would fake what the models answered (e.g. turning a request into a deletion) |
| `block-evidence-exfiltration` | deny | Uploading identities or evidence (curl POST, scp, rsync to a host, netcat, gist, cloud copy) |
| `block-private-data-commit` | deny | Staging the dataset, weights, `.env` or credentials, or `git add --force` |
| `warn-decision-logic-change` | instruct | Editing the guard, policy, state or review logic without telling the user and running the safety tests |

Each policy was tested two ways:

* `bash governance/test_policies.sh` runs every policy through Failproof's own hook runner in an isolated
  sandbox: **67 / 67 cases pass**. These include must-allow look-alikes, so `cp output.csv /tmp/backup` and
  reading evidence stay allowed.
* `fp policies test` independently checks each one: **6 / 6**.

`bash governance/publish_and_deploy.sh <machine-id>` publishes them to the org and deploys them in `observe`,
then re-run it with `enforce`.

**Verified in the Failproof org `matrix` with `fp`, 2026-10-25:**

* **Sessions:** 519 `audit-orchestrator` sessions from the full audit run arrived. All five agent roles appear
  as agents (`intent-classifier`, `identity-resolver`, `evidence-investigator`, `historical-auditor`,
  `decision-verifier`), next to the Claude Code sessions that built the project.
* **Policies:** the six policies above are published as `agent-matrix-*`.
* **Deploy 1:** all six went to the development machine in `observe`. Harmless probes (a no-op `chmod`, a
  dry-run `git add`, an upload to a closed local port, same-file copies, a guard docstring edit) showed 4 of 6
  firing correctly.
* **A bypass found by observe mode:** `block-verdict-tampering` and `block-fabricated-model-replies` missed
  `cp X output.csv; …`, because the rule expected the target at the very end of the command. The sandbox tests
  had not caught it, since every test command ended with the target. The fix ships as version 2 (also applied
  to `protect-audit-evidence`), with chained-command regression cases (54 / 54).
* **Deploy 2:** the three policies were moved to v2. `fp guardrails summary` then showed all six firing exactly
  once on their probes (5 would block, 1 would instruct). None fired on any of the other 250+ tool calls of
  normal development in that hour.
* **Deploy 3: `enforce`.** The coding agent then tried to copy onto `output.csv` and to stage `dataset/`.
  Failproof blocked both before they ran, with the policies' instructions ("Writing audit verdicts by hand
  bypasses the completion guard. Re-run python3 code/main.py instead."). `fp guardrails summary` records
  them as `blocked` by `cloud/agent-matrix-block-verdict-tampering@2` and
  `cloud/agent-matrix-block-private-data-commit@1` on 1 enforcing machine. The audit pipeline, the tests and
  normal edits kept running unblocked.
* **False positives found by enforce mode:** two legitimate commands were blocked.
  * The agent appended a log entry whose prose mentioned "git add of dataset/".
  * A test-data script held an upload command inside a heredoc.

  In both cases the rules had matched text inside a heredoc. Version 3 / 2 of the five deny policies judge
  only the commands a shell line actually runs (heredoc bodies and quoted prose are data). Regression cases
  cover both false positives, and the suite is now 67 / 67.
* **Deploy 4: `enforce` with the fixed versions.** The same log entry went through, while
  `cd . && git add --dry-run dataset/` was still blocked.

Totals over these 24 hours: 6 policies, 14 published versions, 4 deployments, 467 tool calls evaluated and 5
blocks by these cloud policies (3 intended, 2 the false positives that led to v3 / v2). Roll back with
`fp fleet rollback <machine> <generation>`.

The application also enforces its own safety controls (the guard, input validation, bounded model retries,
identity-conflict handling). Those are Agent Matrix code, not Failproof features.

## Run it

```bash
bash scripts/fetch_dataset.sh                 # public dataset -> ./dataset (not redistributed here)
python3 code/main.py --offline                # 518 cases in seconds from cached model replies -> output.csv
python3 -m unittest discover -s code/tests    # 29 tests

# the judge demo
python3 code/demo.py summary                  # metrics derived from a real run
python3 code/demo.py claims                   # "COMPLETE" claims the evidence contradicts
python3 code/demo.py case case_37             # one case, agent by agent
python3 code/demo.py safety                   # 9 unsafe situations and what the system does

# with live open-weight models and Failproof runtime telemetry
python3 -m venv --system-site-packages .venv && .venv/bin/pip install --pre failproofai-sdk==0.0.1b6
.venv/bin/python code/main.py --failproof              # Failproof environment "production" by default
```

Live models: llama.cpp `llama-server` plus GGUF weights (CPU only, no API key), configured in
`code/config/models.toml`; setup is in `code/README.md`. Without them the pipeline uses its reply cache, or the
rule classifier with the cases flagged for review.

## Evaluation (measured, reproducible)

| What | Result | Command |
| --- | --- | --- |
| Status accuracy, 32 public gold samples | **100 %** | `python3 code/main.py --requests sample --offline && python3 code/evaluation/evaluate.py` |
| Step F1 (all six fields must match) | **0.987** (P 0.995 / R 0.979) | same |
| Evidence-id accuracy on matched steps | **100 %** | same |
| Sequence accuracy (whole step list identical) | 87.5 % (28 / 32) | same |
| Team-written cases (replica drift, late evidence, historical email + look-alike, Spanish non-deletion, tombstone, resurrecting sync, decoy) | **7 / 7 exact** | `python3 code/evaluation/custom_cases.py` |
| Contract and admissibility of the 518-row output | **0 violations**, 1,248 citations checked | `python3 code/evaluation/validate_output.py` |
| Safety scenarios | **9 / 9** | `python3 code/demo.py safety` |
| Unit and regression tests | **29 / 29** | `python3 -m unittest discover -s code/tests` |
| Intent: 398 dataset texts (labels from the intake workflow's own events) + 22 team-written texts in 7 languages | **420 / 420** | `code/evaluation/model_benchmark.md` |

**Honest limits.** The 32 samples were also the development set, so the sample numbers are in-sample.
The out-of-sample evidence is the custom cases, the safety scenarios and the guard on all 518 decisions.
The four sample misses are root-caused in `code/evaluation/evaluation_report.md`: two need ids that
appear nowhere in the data, one would need guessing an id from a naming pattern, and one is a genuine
batch tie. There are no gold labels for the 518 evaluation cases.

### Safety scenarios (`code/evaluation/safety_scenarios.py`, also asserted in `code/tests/test_safety.py`)

| Situation | What happens |
| --- | --- |
| Access, export, correction and opt-out requests in 7 languages | 22 / 22 labelled correctly; none treated as deletion |
| A model returns malformed JSON | No crash and no guessed label: the rule classifier decides with `confidence=low`, flagged `INTENT_FALLBACK` |
| The model server is down | Each model retried once (bounded), then the run degrades to rules and flags the case; no multi-GB download is attempted without a server |
| Two people once held the same email | Never merged: the address resolves to nobody, flagged `IDENTITY_UNRESOLVED` |
| A request file with a corrupted timestamp | Refused before any decision is made (exit 2) |
| COMPLETE claimed for a truly INCOMPLETE case, "proven" by the deletion worker's own records | **Rejected**: a workflow record is a claim, not proof |
| COMPLETE backed by evidence captured after `audit_at` | **Rejected** |
| COMPLETE citing evidence that does not exist | **Rejected** |
| All 518 real decisions | 518 / 518 pass the guard |

## Repository

```
code/main.py                    CLI: audit -> guard -> output.csv + review_queue.csv (+ --failproof)
code/demo.py                    judge demo: summary, claims, case, safety
code/ghost_audit/               the agents: intent, data (identity), evidence/, linker, state, policy,
                                guard, review, telemetry, pipeline, llm + local_models (llama.cpp)
code/evaluation/                evaluate, custom cases, safety scenarios, validator, model benchmark,
                                reports, review_queue.csv
code/tests/                     unit tests + safety regression tests
governance/policies/            6 Failproof policies + their test cases (governance/test_policies.sh)
governance/publish_and_deploy.sh  publish to the Failproof org and deploy to a machine
scripts/fetch_dataset.sh        fetch the public dataset
output.csv                      the 518-row result
```

## Connecting your Failproof organisation

These steps need a person, because they enrol the machine, need sudo or an emailed code, or change
enforcement:

```bash
npm install -g failproofai && failproofai config     # connect this machine (installs the daemon)
failproofai config --status                          # daemon running, ingest accepted
uv tool install fp-cloud-cli && fp login              # sign in to the org (emailed code)
fp --json fleet list                                  # this machine's id
bash governance/publish_and_deploy.sh <machine-id> observe   # 6 policies -> org, deployed in observe
.venv/bin/python code/main.py --failproof             # 520 audit sessions -> org
fp guardrails summary --since 24h                     # what the policies decided
bash governance/publish_and_deploy.sh <machine-id> enforce   # then enforce
```
