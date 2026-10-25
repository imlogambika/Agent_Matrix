"""Model client: open-source models via llama.cpp by default, with caching and token accounting.

Backends:

* ``llama_cpp``     - default. Open-weight models from ``config/models.toml`` served
                      locally by ``llama-server`` (CPU is enough). No API key, no fees.
* ``offline``       - no model: callers fall back to deterministic rules and to
                      previously cached model replies.
* ``anthropic_api`` / ``claude_cli`` - optional hosted alternatives, used only when
                      requested explicitly (``--backend``); not used for the submission.

Callers ask for a *role* (``intent``, ``extraction``); the client maps it to the model
selected for that role.  Every call that reaches a model is appended to the usage
ledger that ``evaluation/usage_report.py`` turns into ``usage_report.md``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .local_models import LlamaServer, ModelRegistry

HOSTED_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output) for the optional hosted backends.
PRICING = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}


@dataclass
class Usage:
    task: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float          # API fees; 0 for local open-source inference
    cached: bool
    latency_s: float         # wall time of the call
    call_id: int = 0         # rows sharing a call_id came from one pipeline call
    compute_s: float = 0.0   # model compute time reported by llama.cpp (prompt + generation)


class LLMClient:
    def __init__(self, cache_path: Path, ledger_path: Path, use_cache: bool = True, backend: str | None = None,
                 log=print):
        self.cache_path = cache_path
        self.ledger_path = ledger_path
        self.use_cache = use_cache
        self.log = log
        self.cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.registry = ModelRegistry()
        self.server = LlamaServer(self.registry, log=log)
        self.backend = backend or os.environ.get("GHOST_BACKEND") or self._detect_backend()
        self.calls: list[Usage] = []
        self._next_call = 0
        self.max_attempts = 2
        self.failures: list[dict] = []   # model calls that failed every attempt (the run degraded, not crashed)
        self.telemetry = None            # set by the pipeline when Failproof telemetry is on

    def _detect_backend(self) -> str:
        roles = [m for r in ("intent", "extraction") for m in self.models_for_config(r)]
        if self.registry.binary() and roles and all(self.registry.available(k) for k in roles):
            return "llama_cpp"
        return "offline"

    @property
    def available(self) -> bool:
        return self.backend != "offline"

    def models_for_config(self, role: str) -> list[str]:
        sel = self.registry.selected.get(role, [])
        return [sel] if isinstance(sel, str) else list(sel)

    def models_for(self, role: str) -> list[str]:
        """Models assigned to a role; a list in the config means an ensemble."""
        if self.backend in ("anthropic_api", "claude_cli"):
            return [HOSTED_MODEL]
        sel = self.registry.selected.get(role, [])
        return [sel] if isinstance(sel, str) else list(sel)

    def model_for(self, role: str) -> str:
        models = self.models_for(role)
        return models[0] if models else ""

    def close(self) -> None:
        self.server.stop()

    # ------------------------------------------------------------------ public
    def complete(self, *, task: str, role: str, system: str, prompt: str, image_path: str | None = None,
                 schema: dict | None = None, max_tokens: int = 1024, model: str | None = None) -> str | None:
        """Return the model's reply text, or None when no backend can answer."""
        model = model or self.model_for(role)
        image_hash = hashlib.sha256(Path(image_path).read_bytes()).hexdigest() if image_path else None
        key = hashlib.sha256(json.dumps([model, system, prompt, image_hash, schema], sort_keys=True)
                             .encode()).hexdigest()
        self._next_call += 1
        if self.use_cache and key in self.cache:
            hit = self.cache[key]
            self._record(Usage(task, hit.get("provider", "cache"), model, 0, 0, 0.0, True, 0.0, self._next_call))
            return hit["text"]
        if not self.available:
            return None
        start = time.time()
        for attempt in range(1, self.max_attempts + 1):   # bounded: a dead model server must not stall the audit
            try:
                text, usages, provider = self._call_backend(model, system, prompt, image_path, schema, max_tokens)
                break
            except Exception as e:   # server down, timeout, refusal: retried, then reported and degraded
                if attempt < self.max_attempts:
                    self.log(f"[llm] {task} with {model} failed ({e}); retry {attempt}/{self.max_attempts - 1}")
                    continue
                self.failures.append({"task": task, "model": model, "error": str(e)[:300]})
                self.log(f"[llm] {task} with {model} failed after {attempt} attempts ({e}); falling back")
                if self.telemetry:
                    self.telemetry.model_call(model=model, task=task, duration_ms=round((time.time() - start) * 1000),
                                              input_tokens=0, output_tokens=0, error=str(e))
                return None
        latency = round(time.time() - start, 2)
        for used_model, tin, tout, cost, compute in usages:
            self._record(Usage(task, provider, used_model, tin, tout, cost, False, latency, self._next_call,
                               round(compute, 2)))
            if self.telemetry:
                self.telemetry.model_call(model=used_model, task=task, duration_ms=round(latency * 1000),
                                          input_tokens=tin, output_tokens=tout)
        self.cache[key] = {"text": text, "provider": provider, "model": model, "task": task}
        self._save_cache()
        return text

    def _call_backend(self, model, system, prompt, image_path, schema, max_tokens):
        if self.backend == "llama_cpp":
            text, u = self.server.chat(model, system, prompt, image_path, schema, max_tokens)
            t = u.get("timings", {})
            compute = (t.get("prompt_ms", 0) + t.get("predicted_ms", 0)) / 1000
            return text, [(model, u["prompt_tokens"], u["completion_tokens"], 0.0, compute)], \
                "llama.cpp (local, open weights)"
        if self.backend == "anthropic_api":
            text, usages = self._call_api(model, system, prompt, image_path, max_tokens)
            return text, usages, "anthropic_api"
        text, usages = self._call_cli(model, system, prompt, image_path)
        return text, usages, "claude_cli"

    # ----------------------------------------------------- optional hosted backends
    def _call_api(self, model, system, prompt, image_path, max_tokens):
        import anthropic  # optional dependency, only for this backend

        content = []
        if image_path:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(Path(image_path).read_bytes())
                                                        .decode()}})
        content.append({"type": "text", "text": prompt})
        resp = anthropic.Anthropic().beta.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}],
            output_config={"effort": "low"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"model declined the request: {resp.stop_details}")
        text = "".join(b.text for b in resp.content if b.type == "text")
        tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
        pin, pout = PRICING.get(model, PRICING[HOSTED_MODEL])
        return text, [(model, tin, tout, (tin * pin + tout * pout) / 1e6, 0.0)]

    def _call_cli(self, model, system, prompt, image_path):
        binary = os.environ.get("CLAUDE_BIN") or os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
        base = [binary, "-p", "--output-format", "json", "--model", model, "--system-prompt", system, "--tools", "",
                "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]
        if image_path:
            msg = {"type": "user", "message": {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(Path(image_path).read_bytes()).decode()}},
                {"type": "text", "text": prompt}]}}
            cmd = [x if x != "json" else "stream-json" for x in base] + ["--input-format", "stream-json", "--verbose"]
            out = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True, timeout=600)
            lines = [json.loads(l) for l in out.stdout.splitlines() if l.strip().startswith("{")]
            body = next((l for l in reversed(lines) if l.get("type") == "result"), None)
        else:
            out = subprocess.run(base, input=prompt, capture_output=True, text=True, timeout=600)
            body = json.loads(out.stdout[out.stdout.find("{"):]) if "{" in out.stdout else None
        if not body or body.get("is_error"):
            raise RuntimeError(f"claude CLI call failed: {out.stderr[-500:]}")
        usages = []
        for used_model, v in (body.get("modelUsage") or {}).items():
            tin = v.get("inputTokens", 0) + v.get("cacheCreationInputTokens", 0) + v.get("cacheReadInputTokens", 0)
            usages.append((used_model, tin, v.get("outputTokens", 0), float(v.get("costUSD", 0.0)), 0.0))
        return body["result"], usages

    # ----------------------------------------------------------------- ledger
    def _record(self, usage: Usage) -> None:
        self.calls.append(usage)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a") as fh:
            fh.write(json.dumps(asdict(usage)) + "\n")

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False))
        tmp.replace(self.cache_path)


def extract_json(text: str):
    """Parse the JSON value in a model reply (tolerates code fences and surrounding prose)."""
    if text is None:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not starts:
        return None
    i = min(starts)
    closer = "}" if s[i] == "{" else "]"
    try:
        return json.loads(s[i:s.rfind(closer) + 1])
    except json.JSONDecodeError:
        return None
