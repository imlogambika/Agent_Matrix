# Token Usage and Cost Report

Run analysed: `usage_20261025T151638_generating_calls.jsonl` - final full-dataset run, 2026-10-25

The final full-dataset run that produced `output.csv` (518 requests) is `usage_20261025T151638.jsonl` (`python3 code/main.py`, selected models from `config/models.toml`). It made 807 model requests, all answered from the reply cache. Every one of those replies was generated live, exactly once, by the same open-weight model with the same prompt during the measured runs in `evaluation/runs/` (the cache key is a hash of model, prompt, image and schema). `final_run_cost.py` matched each of the 807 requests to the live call that generated its reply (807 calls, ledger `usage_20261025T151638_generating_calls.jsonl`), so the figures below are the complete cost of producing `output.csv` from scratch. Re-running the final command costs nothing further.

## Models and where they are used

| Role | What the model does | Model |
| --- | --- | --- |
| intent | Classifies each unique request text (398 texts across the sample and evaluation files) as deletion / non-deletion, any language | `llama-3.2-3b` - Llama-3.2-3B-Instruct (Meta, llama3.2), `qwen2.5-vl-3b` - Qwen2.5-VL-3B-Instruct (Alibaba Qwen, qwen-research) |
| extraction | Transcribes each of the 11 screenshots once; a fixed parser turns the text into records (the 4 PDFs are parsed from their text layer without a model) | `qwen2.5-vl-3b` - Qwen2.5-VL-3B-Instruct (Alibaba Qwen, qwen-research) |

All models are open-weight GGUF files (4-bit) served locally by llama.cpp on a CPU (8-core Intel i7-1185G7, no GPU). Everything else in the pipeline - parsing 79 SQL/JSON/log/text files, identity resolution, state reconstruction, policy and ordering - is deterministic code with no model cost.

## Per-model totals

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Model time (s) | API fees (USD) | Est. energy cost (USD) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| llama.cpp (local, open weights) | `llama-3.2-3b` - Llama-3.2-3B-Instruct (Meta, llama3.2) | 398 | 165,613 | 7,144 | 172,757 | 1365 | 0.00 | 0.0016 |
| llama.cpp (local, open weights) | `qwen2.5-vl-3b` - Qwen2.5-VL-3B-Instruct (Alibaba Qwen, qwen-research) | 409 | 175,089 | 12,507 | 187,596 | 2025 | 0.00 | 0.0024 |
| **Overall** | | **807** | **340,702** | **19,651** | **360,353** | **3390** | **0.00** | **0.0040** |

## Per-task breakdown

| Task | Calls | Input tokens | Output tokens | Model time (s) |
| --- | ---: | ---: | ---: | ---: |
| `request_intent` | 796 | 322,463 | 14,308 | 2044 |
| `screenshot_transcription` | 11 | 18,239 | 5,343 | 1345 |

## Per-request averages

| Metric | Value |
| --- | ---: |
| Requests processed | 518 |
| Model calls per request | 1.558 |
| Input tokens per request | 657.7 |
| Output tokens per request | 37.9 |
| Total tokens per request | 695.7 |
| Model time per request (s) | 6.54 |
| Estimated cost per request (USD) | 0.000008 |
| Estimated total cost (USD) | 0.0040 |

## How the numbers are measured

* Every call is appended to the usage ledger by `ghost_audit/llm.py`; token counts are the `usage.prompt_tokens` / `usage.completion_tokens` that llama-server returns (image tokens included in the prompt count).
* API fees are zero: the weights run locally under their open licenses.
* Energy cost = model wall time x 28 W (the CPU's rated package power, an upper bound for this load) x $0.15 per kWh. Change with `--watts` / `--usd-per-kwh`.
* Cached replies (`0` in this ledger) cost nothing and are excluded.

## Token-efficiency choices

* Each unique request text is classified once; the shared instruction prefix stays in llama.cpp's KV cache, so each call only processes the request's own tokens.
* Each screenshot / PDF is extracted once per file and reused by every case that cites it.
* Vision input is capped (`image_max_tokens` in `config/models.toml`) to bound CPU time per screenshot.
* Output is grammar-constrained JSON with tight `max_tokens`, so no tokens are spent on prose or retries.
