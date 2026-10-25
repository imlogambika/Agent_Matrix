# Model Benchmark - open-source models for intent and extraction

All candidates are open-weight models run locally with llama.cpp (4-bit GGUF, CPU only: 8-core Intel i7-1185G7, no GPU). Every request uses a JSON schema that llama.cpp compiles into a grammar, so malformed output is impossible; temperature 0. Ground truth is described in `model_benchmark.py` and never comes from a model. The selection rule was fixed before the runs.

## Candidates

| Model | Vendor | Params (B) | License | Vision | GGUF |
| --- | --- | ---: | --- | --- | --- |
| `qwen2.5-vl-3b` (Qwen2.5-VL-3B-Instruct) | Alibaba Qwen | 3.8 | qwen-research | yes | `ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf` |
| `gemma-3-4b` (Gemma-3-4B-it) | Google | 4.3 | gemma | yes | `ggml-org/gemma-3-4b-it-GGUF/gemma-3-4b-it-Q4_K_M.gguf` |
| `smolvlm2-2.2b` (SmolVLM2-2.2B-Instruct) | Hugging Face | 2.2 | apache-2.0 | yes | `ggml-org/SmolVLM2-2.2B-Instruct-GGUF/SmolVLM2-2.2B-Instruct-Q4_K_M.gguf` |
| `qwen2.5-1.5b` (Qwen2.5-1.5B-Instruct) | Alibaba Qwen | 1.5 | apache-2.0 | no | `Qwen/Qwen2.5-1.5B-Instruct-GGUF/qwen2.5-1.5b-instruct-q4_k_m.gguf` |
| `llama-3.2-3b` (Llama-3.2-3B-Instruct) | Meta | 3.2 | llama3.2 | no | `bartowski/Llama-3.2-3B-Instruct-GGUF/Llama-3.2-3B-Instruct-Q4_K_M.gguf` |

## Request intent

Prompt v1 asked for one label (DELETE / NOT_DELETE); every model mislabelled requests that ask to *verify* a deletion. Prompt v2 asks two yes/no questions (is the request about deleting or confirming a deletion? does the customer explicitly want to keep the account?) and derives the label; only v2 results are eligible for selection. The challenge set was never used to write either prompt.

Gold: 398 unique dataset texts (10 NOT_DELETE, derived from the intake log) + 22 team-written challenge texts in 7 languages.

| Model | Balanced acc. (all) | Accuracy dataset | Accuracy challenge | NOT_DELETE F1 | False non-deletion | Missed non-deletion | Compute (s) | Tokens in / out |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `llama-3.2-3b + qwen2.5-vl-3b + rules (vote)` | 1.000 | 1.000 | 1.000 | 1.000 | 0 | 0 | 2433.7 | 339,456 / 15,092 |
| `llama-3.2-3b` | 0.999 | 0.997 | 1.000 | 0.976 | 1 | 0 | 1722.8 | 174,355 / 7,532 |
| `qwen2.5-vl-3b` | 0.998 | 0.995 | 1.000 | 0.952 | 2 | 0 | 710.9 | 165,101 / 7,560 |
| `qwen2.5-vl-3b (prompt v1)` | 0.992 | 0.987 | 0.955 | 0.870 | 6 | 0 | 556.7 | 175,181 / 4,246 |
| `qwen2.5-1.5b + qwen2.5-vl-3b + rules (vote)` | 0.975 | 1.000 | 0.955 | 0.974 | 0 | 1 | 1163.0 | 330,202 / 15,120 |
| `qwen2.5-1.5b` | 0.975 | 1.000 | 0.955 | 0.974 | 0 | 1 | 452.1 | 165,101 / 7,560 |
| `llama-3.2-3b + qwen2.5-1.5b + rules (vote)` | 0.975 | 1.000 | 0.955 | 0.974 | 0 | 1 | 2174.9 | 339,456 / 15,092 |
| `qwen2.5-1.5b (prompt v1)` | 0.970 | 0.945 | 0.909 | 0.625 | 24 | 0 | 285.1 | 175,181 / 4,416 |
| `llama-3.2-3b + smolvlm2-2.2b + rules (vote)` | 0.950 | 1.000 | 0.909 | 0.947 | 0 | 2 | 6888.0 | 353,063 / 15,512 |
| `qwen2.5-1.5b + smolvlm2-2.2b + rules (vote)` | 0.950 | 1.000 | 0.909 | 0.947 | 0 | 2 | 5617.3 | 343,809 / 15,540 |
| `qwen2.5-vl-3b + smolvlm2-2.2b + rules (vote)` | 0.950 | 1.000 | 0.909 | 0.947 | 0 | 2 | 5876.1 | 343,809 / 15,540 |
| `rules (no model)` | 0.921 | 1.000 | 0.727 | 0.850 | 3 | 3 | 0 | 0 / 0 |
| `smolvlm2-2.2b` | 0.799 | 0.992 | 0.727 | 0.727 | 1 | 8 | 5165.2 | 178,708 / 7,980 |

## Screenshot and PDF extraction

Gold: 41 hand-labelled records in 15 artefacts, 14 of them decisive for the audit. End-to-end = share of the 13 cases whose output depends on an artefact that come out identical to the run on the hand-labelled extraction.

| Model | End-to-end cases | Key records found | Key-field accuracy | Record F1 | Compute (s) | Tokens in / out |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `qwen2.5-vl-3b | transcribe` | 1.000 | 0.929 | 0.889 | 0.874 | 1343.8 | 18,239 / 5,343 |
| `qwen2.5-vl-3b | json` | 0.615 | 0.571 | 0.593 | 0.674 | 1386.3 | 25,680 / 4,955 |
| *claude-opus-5-5 (hosted, closed; reference only)* | *1.000* | *1.000* | *1.000* | *1.000* | - | - |

The italic row is the hosted model used by an earlier version of this agent, scored on the same gold from cached replies; it is a reference point only and is not eligible for selection.

## Selection

* **intent** -> `llama-3.2-3b + qwen2.5-vl-3b + rules (vote)`
* **extraction** -> `qwen2.5-vl-3b | transcribe`

The selection is written to `code/config/models.toml` (`[selected]`) and used by the final run.

## Notes

* Hardware and quantisation are identical for every model, so speed numbers are comparable.
* A screenshot can only be read by a model with a vision projector; text-only models are scored on intent only.
* `smolvlm2-2.2b` intent: the 420 replies were generated live in `runs/benchmark_20261025T001734.jsonl` (token counts and compute time are taken from that ledger); the run was interrupted by a machine restart before its scores were saved, so they were recomputed from those exact replies. Its screenshot transcription did not finish (1 of 11 screens), so it is not scored on extraction.
* `gemma-3-4b` was not run to completion within the time available on this CPU-only laptop (127 of 420 intent calls, prompt v1, before the `--swa-full` fix) and is not eligible. Intent was therefore chosen among 4 completed candidates; for extraction `qwen2.5-vl-3b` is the only vision candidate that completed, so it is selected by default and its quality is judged against the hand-labelled gold (13 of 13 artefact-dependent cases end to end).
* The JSON-schema extraction mode was measured on `qwen2.5-vl-3b` only; it lost to transcribe-then-parse by 0.31 end-to-end, so later candidates were scheduled in the transcribe mode.
