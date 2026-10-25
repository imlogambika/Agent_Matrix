"""Does the request text authorise deletion?

An open-source model classifies each *unique* request text once, with a JSON
schema that only allows the two labels.  A multilingual keyword classifier runs alongside it: it is the
fallback when no model is reachable, and any disagreement is logged so it can
be reviewed in the evaluation report.
"""
from __future__ import annotations

import re
from pathlib import Path

from .llm import LLMClient, extract_json
from .models import Request

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "request_intent.md"

DELETE_WORDS = re.compile(
    r"delet|eras|remov|close (my|this|the) account|close my account|closed my account|permanent|"
    r"suppr|elimin|exclus|lösch|cancell?ar|삭제|削除|删除|注销|удал|usuń|cancel|"
    r"is gone|be gone|cannot identify me|can no longer identify|forget me", re.I)
NEGATION = re.compile(
    r"not (asking|a request) (for|to) (deletion|erase|delete)|not a request to erase|"
    r"do not (close|delete)|don.t (delete|close)|leave (the|my) (account|profile)|keep the account|"
    r"without removing|continue using|still (need|want|use)|marketing lists only|"
    r"stop the promotional", re.I)


def rule_intent(text: str) -> str:
    if NEGATION.search(text):
        return "NOT_DELETE"
    return "DELETE" if DELETE_WORDS.search(text) else "NOT_DELETE"


def _prompt_parts():
    text = PROMPT_PATH.read_text()
    system = text.split("# System", 1)[1].split("# User", 1)[0].strip()
    user = text.split("# User", 1)[1].strip()
    return system, user


# Two simple yes/no questions instead of one label: small models read "please verify my deletion"
# as an information request when asked for a single label, but answer these reliably.
INTENT_SCHEMA = {"type": "object",
                 "properties": {"about_deletion": {"type": "boolean"}, "keep_account": {"type": "boolean"}},
                 "required": ["about_deletion", "keep_account"]}


def model_intent(text: str, llm: LLMClient, model: str | None = None) -> str | None:
    """One request -> DELETE / NOT_DELETE from the model, or None when no model answers."""
    system, user = _prompt_parts()
    reply = llm.complete(task="request_intent", role="intent", system=system, model=model,
                         prompt=user.replace("{request}", text), schema=INTENT_SCHEMA, max_tokens=32)
    data = extract_json(reply)
    if isinstance(data, dict) and isinstance(data.get("about_deletion"), bool):
        return "DELETE" if data["about_deletion"] and not data.get("keep_account") else "NOT_DELETE"
    return None


def vote(labels: list[str]) -> str:
    """Majority vote; with an even split the deletion request is honoured."""
    return "NOT_DELETE" if labels.count("NOT_DELETE") > len(labels) / 2 else "DELETE"


def classify(requests: list[Request], llm: LLMClient, log=print) -> dict[str, dict]:
    """case_id -> {"intent", "source", "rule", "votes", "confidence", "explanation", "degraded"}.

    Each unique text is classified once. One configured model decides alone; two or
    more models vote together with the rule classifier (an odd number of voters, so
    no ties). If any model gives no valid answer (server down, malformed JSON) the
    rule classifier decides and the case is marked ``degraded`` for review - the
    system never presents a guess with the confidence of a full vote."""
    texts = sorted({r.request_text for r in requests})
    rule = {t: rule_intent(t) for t in texts}
    model, votes, missing = {}, {}, {}
    models = llm.models_for("intent")
    if (llm.available or llm.use_cache) and models:
        for t in texts:
            answers = {m: model_intent(t, llm, model=m) for m in models}
            labels = [lab for lab in answers.values() if lab]
            votes[t] = {m: lab or "NO_ANSWER" for m, lab in answers.items()} | {"rules": rule[t]}
            if len(labels) == len(models):
                model[t] = labels[0] if len(models) == 1 else vote(labels + [rule[t]])
            else:
                missing[t] = [m for m, lab in answers.items() if not lab]
    out = {}
    disagreements = 0
    for r in requests:
        t = r.request_text
        intent = model.get(t, rule[t])
        if t in model and model[t] != rule[t]:
            disagreements += 1
        ballot = votes.get(t, {})
        agree = sum(v == intent for v in ballot.values())
        if t in missing:
            confidence = "low"
            why = f"no valid answer from {', '.join(missing[t])}; rule classifier decided"
        elif not ballot:
            confidence, why = "low", "no model configured; rule classifier decided"
        else:
            confidence = "high" if agree == len(ballot) else "medium"
            why = f"{agree} of {len(ballot)} voters: " + ", ".join(f"{k}={v}" for k, v in ballot.items())
        out[r.case_id] = {"intent": intent, "source": "model" if t in model else "rules", "rule": rule[t],
                          "votes": ballot, "confidence": confidence, "explanation": why,
                          "degraded": t in missing}
    log(f"[intent] {len(texts)} unique texts, model-labelled {len(model)}, "
        f"rule/model disagreements on {disagreements} requests"
        + (f", {len(missing)} texts fell back to rules (model answer missing)" if missing else ""))
    return out
