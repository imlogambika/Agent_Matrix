"""Small normalisation helpers used by parsers and the identity resolver."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
NULLS = {None, "", "null", "None", "NULL", "-", "—", "no direct identifier"}


def is_null(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in NULLS)


def norm_email(v: str) -> str:
    return v.strip().lower()


def norm_phone(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def norm_name(v: str) -> str:
    v = unicodedata.normalize("NFKD", v or "")
    v = "".join(c for c in v if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", v).strip().casefold()


def parse_ts(v: str) -> datetime:
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def classify_key(value: str) -> tuple[str, str] | None:
    """Return (kind, normalised) for an identifier-looking value."""
    if is_null(value):
        return None
    v = str(value).strip()
    if USER_RE.fullmatch(v):
        return "user_ref", v
    if EMAIL_RE.fullmatch(v):
        return "email", norm_email(v)
    if HASH_RE.fullmatch(v.lower()):
        return "hash", v.lower()
    digits = norm_phone(v)
    if v.startswith("+") and len(digits) >= 7:
        return "phone", digits
    return None
