"""Screenshots and PDFs: model-based extraction into Observations.

PDF text is pulled with ``pdftotext`` (when installed) and sent as text; PNG
screenshots are sent as images to an open-source vision-language model.  The reply
is constrained by a JSON schema, and a misread resource id is snapped to the single
known id one character away.  Replies are cached by the LLM client, so a
re-run reuses earlier extractions.  If no model is reachable, PDFs fall back to
a regex extractor; screenshots are skipped (and reported) because their facts
are not machine-readable without a vision model.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..llm import LLMClient, extract_json
from ..models import Observation
from ..util import EMAIL_RE, USER_RE, is_null, norm_email, norm_name, norm_phone
from .records import system_for
from .transcript import parse_transcript

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "multimodal_extraction.md"
TRANSCRIBE_PATH = Path(__file__).resolve().parents[2] / "prompts" / "screenshot_transcription.md"
RID_RE = re.compile(r"\b(?:account|search|support|analytics|export|data_flow|backup|audit_record|tombstone|"
                    r"marketing|object|legal_hold)_\d+\b")
CATEGORY_SYSTEM = {
    "USER_PROFILE": "account_service", "SEARCH_INDEX_ENTRY": "search_service",
    "SUPPORT_MESSAGE": "support_platform", "SUPPORT_ATTACHMENT": "support_platform",
    "ANALYTICS_PROFILE": "analytics_platform", "USER_EXPORT": "export_service", "DATA_FLOW": "integration_service",
    "BACKUP_ARCHIVE": "backup_service", "SECURITY_AUDIT_LOG": "security_service",
    "ACCOUNT_TOMBSTONE": "identity_service", "MARKETING_PROFILE": "marketing_platform",
    "LEGAL_HOLD_RECORD": "legal_service",
}
STATE_MAP = {
    "PRESENT": dict(present=True), "ABSENT": dict(present=False),
    "ACTIVE": dict(present=True, enabled=True), "DISABLED": dict(present=True, enabled=False),
    "RESTORABLE": dict(present=True, restore_allowed=True), "BLOCKED": dict(present=True, restore_allowed=False),
}


NULLABLE = {"type": ["string", "null"]}
RECORDS_SCHEMA = {
    "type": "object",
    "properties": {"records": {"type": "array", "maxItems": 12, "items": {
        "type": "object",
        "properties": {
            "resource_id": NULLABLE, "business_reference": NULLABLE,
            "record_type": {"type": "string", "enum": sorted(CATEGORY_SYSTEM)},
            "state": {"type": "string", "enum": sorted(STATE_MAP)},
            "customer_reference": NULLABLE, "name": NULLABLE, "email": NULLABLE, "phone": NULLABLE,
            "other_parties": NULLABLE, "retain_until": NULLABLE},
        "required": ["resource_id", "business_reference", "record_type", "state", "customer_reference", "name",
                     "email", "phone", "other_parties", "retain_until"]}}},
    "required": ["records"]}


def _prompt_parts(path: Path = PROMPT_PATH):
    text = path.read_text()
    system = text.split("# System", 1)[1].split("# User", 1)[0].strip()
    user = text.split("# User", 1)[1].strip()
    return system, user


def fill(template: str, **values) -> str:
    """Substitute {name} placeholders without touching other braces (the prompt contains JSON)."""
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def pdf_text(path: str) -> str:
    if not shutil.which("pdftotext"):
        return ""
    out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True)
    return out.stdout


def prepare_image(path: str) -> str:
    """Trim the application's navigation sidebar and the blank area below the last line of
    text, so a vision model's fixed token budget is spent on readable content.  Applied
    identically to every vision model; returns the original path if PIL is unavailable."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return path
    img = Image.open(path).convert("RGB")
    lum = np.asarray(img.convert("L")).astype(int)
    h, w = lum.shape
    dark_cols = np.median(lum, axis=0) < 80
    left = 0
    while left < w and dark_cols[left]:
        left += 1
    if left > 0.3 * w:           # dark-themed screen, not a sidebar
        left = 0
    edges = (np.abs(np.diff(lum[:, left:], axis=1)) > 40).sum(axis=1)
    text_rows = np.where(edges > 20)[0]
    bottom = min(h, int(text_rows.max()) + 24) if len(text_rows) else h
    out_dir = Path(tempfile.gettempdir()) / "ghost-audit-images"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]}.png"
    if not out.exists():
        img.crop((left, 0, w, bottom)).save(out)
    return str(out)


def merge_records(*lists: list[dict]) -> list[dict]:
    """Union of several extractions of one artefact.  A record is keyed by its resource id, else by
    its business reference, else by (type, customer key); a field missing in one reading is filled
    from another.  Spurious records are harmless: a screen record only links to a subject through a
    catalog link or the subject's own contact details."""
    merged: dict[tuple, dict] = {}
    for recs in lists:
        for r in recs:
            key = (("rid", r.get("resource_id")) if r.get("resource_id") else
                   ("ref", r.get("business_reference")) if r.get("business_reference") else
                   ("key", r.get("record_type"), r.get("customer_reference")))
            if key not in merged:
                merged[key] = dict(r)
            else:
                for f, v in r.items():
                    if merged[key].get(f) in (None, "") and v not in (None, ""):
                        merged[key][f] = v
    return list(merged.values())


def extract_artifact(llm: LLMClient, path: str, file_id: str, captured_at: str, origin: str,
                     model: str | None = None, mode: str | None = None) -> list[dict]:
    if model is None and len(llm.models_for("extraction")) > 1:
        return merge_records(*(extract_artifact(llm, path, file_id, captured_at, origin, m, mode)
                               for m in llm.models_for("extraction")))
    """``transcribe``: the model only reads the screen, ``parse_transcript`` structures it
    (PDF text needs no model).  ``json``: the model fills the record schema directly."""
    mode = mode or llm.registry.selected.get("extraction_mode", "transcribe")
    is_pdf = path.lower().endswith(".pdf")
    doc = pdf_text(path) if is_pdf else ""
    if mode == "transcribe":
        if is_pdf:
            return parse_transcript(doc)
        system, user = _prompt_parts(TRANSCRIBE_PATH)
        reply = llm.complete(task="screenshot_transcription", role="extraction", system=system, prompt=user,
                             image_path=prepare_image(path), max_tokens=1200, model=model)
        return parse_transcript(reply or "")
    system, user = _prompt_parts()
    prompt = fill(user, file_name=Path(path).name, captured_at=captured_at, origin=origin,
                  document_text=f"Document text:\n```\n{doc}\n```" if doc else "The artifact is attached.")
    reply = llm.complete(task="multimodal_extraction", role="extraction", system=system, prompt=prompt,
                         image_path=None if is_pdf else prepare_image(path), schema=RECORDS_SCHEMA,
                         max_tokens=1500, model=model)
    data = extract_json(reply)
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"]
    if is_pdf:
        return _regex_pdf(doc)
    return []


BUSINESS_REF_RE = re.compile(r"\b[A-Z]{2,4}-\d{4}-\d{4}-\d{4}\b")


def _regex_pdf(text: str) -> list[dict]:
    """Offline fallback: one record per resource id or statement number in the PDF text."""
    emails = sorted(set(EMAIL_RE.findall(text)))
    refs = sorted(set(USER_RE.findall(text)))
    shared = {"state": "PRESENT", "email": emails[0] if len(emails) == 1 else None,
              "customer_reference": refs[0] if len(refs) == 1 else None}
    out = []
    for rid in sorted(set(RID_RE.findall(text))):
        rec = {"resource_id": rid, "system_id": system_for(rid)[0], **shared}
        if rid.startswith("support_") and "attachment" in text.lower():
            rec["record_type"] = "SUPPORT_ATTACHMENT"
        out.append(rec)
    for ref in sorted(set(BUSINESS_REF_RE.findall(text))):
        out.append({"resource_id": None, "business_reference": ref, **shared})
    return out


def resolve_alias(record: dict, aliases: dict) -> str | None:
    """Map a business reference (statement number, file name) to a resource id.

    A bare customer key (``user_17``) only identifies a deleted-account tombstone.
    """
    ref = str(record.get("business_reference") or "").strip()
    key = str(record.get("customer_reference") or "").strip()
    for alias, rid in aliases.items():
        if ref and (alias == ref or (len(alias) > 8 and alias in ref)):
            return rid
        if key and alias == key and record.get("record_type") == "ACCOUNT_TOMBSTONE" and rid.startswith("tombstone_"):
            return rid
    return None


def _edit1(a: str, b: str) -> bool:
    """True when a and b differ by exactly one substitution, insertion or deletion."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def snap_id(rid: str, known_ids: set) -> str:
    """A vision model may misread one digit; accept the single known id one edit away."""
    if not known_ids or rid in known_ids:
        return rid
    prefix = rid.rsplit("_", 1)[0]
    near = [k for k in known_ids if k.startswith(prefix + "_") and _edit1(rid, k)]
    return near[0] if len(near) == 1 else rid


def to_observations(records: list[dict], file_id: str, captured_at: str, aliases: dict,
                    known_ids: set | None = None) -> list[Observation]:
    out = []
    for r in records:
        rid = str(r.get("resource_id") or "").strip()
        if RID_RE.fullmatch(rid):
            snapped = snap_id(rid, known_ids or set())
            if snapped != rid:
                r = {**r, "_read_as": rid}
            rid = snapped
        else:
            rid = resolve_alias(r, aliases)
        if not rid:
            continue
        system, category = system_for(rid)
        # the resource prefix fixes the system; the model's type may only refine it within that system
        model_type = r.get("record_type")
        if model_type and CATEGORY_SYSTEM.get(model_type) == system:
            category = model_type
        obs = Observation(file_id=file_id, ts=captured_at, system=system, resource_id=rid,
                          present=True, source="multimodal", category=category)
        for k, v in STATE_MAP.get(str(r.get("state", "PRESENT")).upper(), {}).items():
            setattr(obs, k, v)
        pii = set()
        if not is_null(r.get("name")):
            obs.names.add(norm_name(r["name"]))
            pii.add("name")
        if not is_null(r.get("email")) and EMAIL_RE.fullmatch(str(r["email"]).strip()):
            obs.emails.add(norm_email(r["email"]))
            pii.add("email")
        if not is_null(r.get("phone")):
            obs.phones.add(norm_phone(r["phone"]))
            pii.add("phone")
        if not is_null(r.get("customer_reference")) and USER_RE.fullmatch(str(r["customer_reference"])):
            obs.user_refs.add(r["customer_reference"])
        obs.pii_fields = pii
        if r.get("other_parties"):
            obs.extra["other_parties"] = r["other_parties"]
        if r.get("_read_as"):
            obs.extra["read_as"] = r["_read_as"]
        if r.get("retain_until"):
            obs.extra["retention_until"] = r["retain_until"]
        out.append(obs)
    return out
