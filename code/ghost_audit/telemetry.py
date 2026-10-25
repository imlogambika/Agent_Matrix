"""Failproof AI runtime telemetry for the audit agents.

Every audited case is one Failproof session. Inside it the orchestrator runs five
agents (intent, identity, evidence, historical audit, decision); each agent's work
is a tool call with a structured result, the completion guard and the escalation
review are hooks, and a case the evidence cannot settle waits for a human. The
batch work shared by all cases (reading the evidence files, classifying request
texts with the local models) is one more session, with every model call recorded.

Events are written by the official ``failproofai-sdk`` to its local spool; the
Failproof daemon ships them to the organisation. Nothing here talks to a network.

Privacy: events carry case ids, user ids, resource ids, evidence ids, counts and
decisions only - never an email address, phone number, name or request text.

Telemetry is off unless ``Telemetry(enabled=True)`` (``main.py --failproof``);
without the SDK installed it is a no-op, so the audit itself never depends on it.
"""
from __future__ import annotations

import hashlib
import traceback
import uuid
from contextlib import contextmanager


class _Agent:
    """Mutable end-of-run fields for one agent span."""

    def __init__(self, name: str):
        self.name = name
        self.outcome = "success"
        self.summary = ""


class Telemetry:
    def __init__(self, enabled: bool = False, environment: str = "dev", run_id: str = "", log=print):
        self.sdk = None
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.log = log
        self._stack: list[tuple[str, str]] = []   # (session_id, agent_id), innermost last
        self.counts = {"sessions": 0, "events": 0}
        if enabled:
            try:
                import failproofai_sdk
                failproofai_sdk.configure(environment=environment)
                self.sdk = failproofai_sdk
            except ImportError:
                log("[failproof] failproofai-sdk is not installed; telemetry disabled (pip install --pre failproofai-sdk)")

    @property
    def active(self) -> bool:
        return self.sdk is not None

    def _emit(self, kind: str, **fields) -> None:
        if not self.sdk:
            return
        sid, aid = self._stack[-1] if self._stack else (None, None)
        fields.setdefault("session_id", sid)
        fields.setdefault("agent_id", aid)
        getattr(self.sdk.event, kind)(**fields)
        self.counts["events"] += 1

    # ------------------------------------------------------------------ spans
    @contextmanager
    def session(self, name: str, agent: str, goal: str, **fields):
        """A new Failproof session with its root agent."""
        sid = f"agent-matrix-{self.run_id}-{name}"
        with self._agent_span(sid, agent, goal, parent=None, **fields) as a:
            self.counts["sessions"] += self.active
            yield a

    @contextmanager
    def agent(self, name: str, goal: str):
        """A sub-agent of the current agent, in the same session."""
        sid, parent = self._stack[-1] if self._stack else (f"agent-matrix-{self.run_id}-misc", None)
        with self._agent_span(sid, name, goal, parent=parent) as a:
            yield a

    @contextmanager
    def _agent_span(self, sid, name, goal, parent, **fields):
        a = _Agent(name)
        self._stack.append((sid, name))
        self._emit("agent_start", goal=goal, **({"parent_id": parent} if parent else {}), **fields)
        try:
            yield a
        except BaseException as e:
            self._emit("error", error_type=type(e).__name__, message=str(e)[:500],
                       traceback=traceback.format_exc()[-4000:])
            self._emit("agent_end", outcome="failed", summary=f"{type(e).__name__}: {e}"[:500])
            self._stack.pop()
            raise
        self._emit("agent_end", outcome=a.outcome, summary=a.summary[:500])
        self._stack.pop()

    # ------------------------------------------------------------------ events
    def tool(self, name: str, input: dict, output=None, error: str | None = None) -> None:
        """One completed tool call (the work already happened; this records it)."""
        call_id = uuid.uuid4().hex
        self._emit("tool_use", tool_name=name, tool_call_id=call_id, input=input)
        self._emit("tool_result", tool_name=name, tool_call_id=call_id, output=output,
                   **({"error": error} if error else {}))

    def hook(self, name: str, trigger: str, input, outcome: str, output) -> None:
        hook_id = uuid.uuid4().hex
        self._emit("hook_triggered", hook_name=name, hook_id=hook_id, trigger_event=trigger, input=input)
        self._emit("hook_completed", hook_name=name, hook_id=hook_id, outcome=outcome, output=output)

    def human_review(self, case_id: str, reasons: list[str], prompt: str) -> None:
        self._emit("human_wait", input_id=f"review-{case_id}", reason=", ".join(reasons)[:500], prompt=prompt[:500],
                   options=["confirm decision", "override decision"])

    def model_call(self, *, model: str, task: str, duration_ms: int, input_tokens: int, output_tokens: int,
                   error: str | None = None) -> None:
        rid = uuid.uuid4().hex
        self._emit("model_request", model=model, request_id=rid, task=task)
        self._emit("model_response", model=model, request_id=rid, duration_ms=int(duration_ms),
                   input_tokens=int(input_tokens), output_tokens=int(output_tokens),
                   stop_reason="error" if error else "end_turn", **({"error": error[:500]} if error else {}))

    def error(self, error_type: str, message: str) -> None:
        self._emit("error", error_type=error_type, message=message[:500])

    def close(self) -> None:
        if self.sdk:
            self.sdk._writer.flush_now()   # documented flush (SDK §3); the run is about to exit


def text_ref(text: str) -> str:
    """A stable, non-reversible reference to a request text (never the text itself)."""
    return "text:" + hashlib.sha256(text.encode()).hexdigest()[:12]
