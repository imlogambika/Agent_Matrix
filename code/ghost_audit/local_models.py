"""Open-source models served locally by llama.cpp.

``ModelRegistry`` reads ``code/config/models.toml``; ``LlamaServer`` starts one
``llama-server`` process per model (switching models stops the previous one, so
only one model is in RAM) and talks to its OpenAI-compatible endpoint.  Every
request asks for a JSON schema, which llama.cpp turns into a grammar: even a
small model can only emit valid, well-typed JSON.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "models.toml"


@dataclass
class ModelSpec:
    key: str
    display: str
    vendor: str
    license: str
    params_b: float
    repo: str
    file: str
    mmproj: str = ""
    server_args: tuple = ()      # extra llama-server flags this model needs

    @property
    def vision(self) -> bool:
        return bool(self.mmproj)


class ModelRegistry:
    def __init__(self, path: Path = CONFIG_PATH):
        cfg = tomllib.loads(path.read_text())
        self.runtime = cfg["runtime"]
        self.selected = cfg.get("selected", {})
        self.models = {k: ModelSpec(key=k, **{**v, "server_args": tuple(v.get("server_args", ()))})
                       for k, v in cfg["models"].items()}

    def binary(self) -> Path | None:
        cand = os.environ.get("LLAMA_SERVER") or self.runtime.get("binary", "")
        p = Path(os.path.expanduser(cand)) if cand else None
        return p if p and p.exists() else None

    def models_dir(self) -> Path:
        return Path(os.path.expanduser(os.environ.get("GHOST_MODELS_DIR")
                                       or self.runtime.get("models_dir", "~/.local/opt/ghost-models")))

    def local_file(self, repo: str, filename: str, download: bool) -> Path | None:
        """models_dir first, then the Hugging Face cache; download with resumable curl if asked."""
        p = self.models_dir() / filename
        if p.exists() and not p.with_suffix(p.suffix + ".part").exists():
            return p
        try:
            from huggingface_hub import hf_hub_download
            return Path(hf_hub_download(repo, filename, local_files_only=True))
        except Exception:
            pass
        if not download:
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
        subprocess.run(["curl", "-sSL", "--fail", "-C", "-", "-o", str(p), url], check=True)
        return p

    def files(self, key: str, download: bool = False):
        spec = self.models[key]
        weights = self.local_file(spec.repo, spec.file, download)
        proj = self.local_file(spec.repo, spec.mmproj, download) if spec.mmproj else None
        if weights is None or (spec.mmproj and proj is None):
            return None
        return weights, proj

    def available(self, key: str) -> bool:
        return self.binary() is not None and self.files(key) is not None


class LlamaServer:
    """One llama-server process at a time, restarted when the requested model changes."""

    def __init__(self, registry: ModelRegistry, log=print):
        self.registry = registry
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.model: str | None = None
        self.port = int(registry.runtime.get("port", 8079))
        # one model call may not hold the audit longer than this (CPU vision calls take minutes)
        self.request_timeout = int(registry.runtime.get("request_timeout_s", 900))

    def ensure(self, key: str) -> None:
        if self.model == key and self.proc and self.proc.poll() is None:
            return
        self.stop()
        if self.registry.binary() is None:   # fail fast, before any multi-GB weight download
            raise RuntimeError("llama-server binary not found (set LLAMA_SERVER or [runtime] binary)")
        files = self.registry.files(key, download=True)
        if files is None:
            raise RuntimeError(f"model files for {key} are not available")
        weights, proj = files
        rt = self.registry.runtime
        cmd = [str(self.registry.binary()), "-m", str(weights), "--port", str(self.port), "--host", "127.0.0.1",
               "-t", str(rt.get("threads", 8)), "-c", str(rt.get("ctx_size", 8192)), "-np", "1",
               "--no-webui", "--temp", "0"]
        if proj:
            cmd += ["--mmproj", str(proj), "--image-max-tokens", str(rt.get("image_max_tokens", 1536))]
        cmd += list(self.registry.models[key].server_args)
        logf = open(Path(os.environ.get("TMPDIR", "/tmp")) / f"llama-server-{key}.log", "w")
        self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        self.model = key
        deadline = time.time() + 300
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited while loading {key} (see {logf.name})")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    if r.status == 200:
                        self.log(f"[llama.cpp] serving {key}")
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(1)
        raise RuntimeError(f"llama-server did not become ready for {key}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.model = None, None

    def chat(self, key: str, system: str, prompt: str, image_path: str | None = None,
             schema: dict | None = None, max_tokens: int = 1024) -> tuple[str, dict]:
        """Returns (text, usage) where usage has prompt/completion tokens and timings."""
        self.ensure(key)
        content: list | str = prompt
        if image_path:
            mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
            content = [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                       {"type": "text", "text": prompt}]
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "temperature": 0, "max_tokens": max_tokens, "cache_prompt": True}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.request_timeout) as r:
            out = json.loads(r.read())
        text = out["choices"][0]["message"].get("content") or ""
        usage = {"prompt_tokens": out.get("usage", {}).get("prompt_tokens", 0),
                 "completion_tokens": out.get("usage", {}).get("completion_tokens", 0),
                 "timings": out.get("timings", {})}
        return text, usage
