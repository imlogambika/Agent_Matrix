"""Turn a plain-text transcript of a screenshot or PDF into extraction records.

Used by the ``transcribe`` extraction mode: a vision model only has to read the
screen (what small open models do well); this parser does the structuring with
fixed rules, so the structure never depends on a model's reasoning.

* a record per resource id (``account_123`` ...), with the emails, phones and
  customer keys printed on the same line;
* the page's main record (the only id, or the first id in the title area) also
  takes identifiers printed on lines that carry no id (profile cards, panels);
* records for business references (statement numbers, order numbers with a
  customer key, ticket numbers, file names) when no resource id is printed beside them;
* a deleted-account reference page (a customer key, no id, no contact details)
  becomes an ACCOUNT_TOMBSTONE record;
* the state comes from the record's own line (stopped/paused connector,
  blocked restore), the type from the id prefix and the page wording.
"""
from __future__ import annotations

import re

from ..util import EMAIL_RE, USER_RE
from .records import system_for

RID_RE = re.compile(r"\b(?:account|search|support|analytics|export|data_flow|backup|audit_record|tombstone|"
                    r"marketing|object|legal_hold)_\d+\b")
PHONE_RE = re.compile(r"\+\d[\d ()-]{6,}\d")
STATEMENT_RE = re.compile(r"\b[A-Z]{2,4}-\d{4}-\d{4}-\d{4}\b")
ORDER_RE = re.compile(r"\bORD-\d{8}\b")
TICKET_RE = re.compile(r"\bCS-\d{3,6}\b")
FILE_RE = re.compile(r"\b[\w.-]+\.(?:pdf|zip|jpe?g|png|csv)\b", re.I)
TITLE_LINES = 10
DEFAULT_TYPE = {"account_service": "USER_PROFILE", "search_service": "SEARCH_INDEX_ENTRY",
                "support_platform": "SUPPORT_MESSAGE", "analytics_platform": "ANALYTICS_PROFILE",
                "export_service": "USER_EXPORT", "integration_service": "DATA_FLOW",
                "backup_service": "BACKUP_ARCHIVE", "security_service": "SECURITY_AUDIT_LOG",
                "identity_service": "ACCOUNT_TOMBSTONE", "marketing_platform": "MARKETING_PROFILE",
                "object_storage": "SUPPORT_ATTACHMENT", "legal_service": "LEGAL_HOLD_RECORD"}


def _record(rid=None, ref=None, rtype=None, state="PRESENT", user=None, email=None, phone=None):
    return {"resource_id": rid, "business_reference": ref, "record_type": rtype, "state": state,
            "customer_reference": user, "name": None, "email": email, "phone": phone, "other_parties": None,
            "retain_until": None}


def _state(rtype: str, line: str) -> str:
    low = line.lower()
    if rtype == "DATA_FLOW":
        return "DISABLED" if re.search(r"\b(paused|disabled|stopped|inactive)\b", low) else "ACTIVE"
    if rtype == "BACKUP_ARCHIVE":
        return "BLOCKED" if re.search(r"\b(blocked|denied|restore disabled)\b", low) else "RESTORABLE"
    return "PRESENT"


def _near(lines: list[str], rid: str, pattern: re.Pattern) -> list[str]:
    """Values of ``pattern`` closest to each mention of ``rid``: first the text after the id up
    to the next id; the text before it only when it is the line's sole id (a header such as
    "user_20 · analytics_7438").  Rows merged into one line keep each value with its own id."""
    after, before = [], []
    for line in lines:
        spans = [m.span() for m in RID_RE.finditer(line)]
        for k, (a, b) in enumerate(spans):
            if line[a:b] != rid:
                continue
            nxt = spans[k + 1][0] if k + 1 < len(spans) else len(line)
            prv = spans[k - 1][1] if k > 0 else 0
            after += pattern.findall(line[b:nxt])
            if len(spans) == 1:
                before += pattern.findall(line[prv:a])[::-1]
    return after + before


def parse_transcript(text: str) -> list[dict]:
    lines = [re.sub(r"\s+", " ", l).strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    page = " ".join(lines).lower()
    first_line: dict[str, int] = {}
    for i, line in enumerate(lines):
        for rid in RID_RE.findall(line):
            first_line.setdefault(rid, i)
    ordered = sorted(first_line, key=first_line.get)
    primary = ordered[0] if len(ordered) == 1 else next((r for r in ordered if first_line[r] < TITLE_LINES), None)
    loose = [l for l in lines if not RID_RE.search(l)]       # card / panel lines without an id
    loose_emails = sorted({e.lower() for l in loose for e in EMAIL_RE.findall(l)})
    loose_users = sorted({u for l in loose for u in USER_RE.findall(l)})
    loose_phones = [p for l in loose for p in PHONE_RE.findall(l)]

    records = []
    for rid in ordered:
        line = " ".join(l for l in lines if rid in l)        # every line that names this id
        system = system_for(rid)[0]
        rtype = DEFAULT_TYPE.get(system)
        if system == "support_platform" and "attachment" in page:
            rtype = "SUPPORT_ATTACHMENT"
        emails = [e.lower() for e in _near(lines, rid, EMAIL_RE)]
        users = _near(lines, rid, USER_RE)
        phones = _near(lines, rid, PHONE_RE)
        if rid == primary:
            emails = emails or (loose_emails[:1] if len(loose_emails) == 1 else [])
            users = users or (loose_users[:1] if len(loose_users) == 1 else [])
            phones = phones or loose_phones[:1]
        records.append(_record(rid, None, rtype, _state(rtype, line), users[0] if users else None,
                               emails[0] if emails else None, phones[0] if phones else None))

    seen_refs = set()
    for line in lines:
        if RID_RE.search(line):
            continue
        users = USER_RE.findall(line)
        refs = [(r, None) for r in STATEMENT_RE.findall(line)]
        refs += [(r, "LEGAL_HOLD_RECORD") for r in ORDER_RE.findall(line) if users]
        refs += [(r, "SUPPORT_ATTACHMENT") for r in FILE_RE.findall(line)]
        refs += [(r, "SUPPORT_MESSAGE") for r in TICKET_RE.findall(line)]
        for ref, rtype in refs:
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            user = users[0] if users else (loose_users[0] if len(loose_users) == 1 else None)
            email = loose_emails[0] if len(loose_emails) == 1 and rtype in (None, "SUPPORT_MESSAGE") else None
            records.append(_record(None, ref, rtype, "PRESENT", user, email))

    if not ordered:
        users = sorted(set(USER_RE.findall(" ".join(lines))))
        if len(users) == 1 and not EMAIL_RE.search(" ".join(lines)) and re.search(r"reference|customer key", page):
            records.append(_record(None, None, "ACCOUNT_TOMBSTONE", "PRESENT", users[0]))
    return records
