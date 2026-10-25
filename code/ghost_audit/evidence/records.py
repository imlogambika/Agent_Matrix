"""Turn a raw record dict (from SQL, JSON or a log) into an Observation.

All structured sources share field vocabularies (``customer_reference``,
``requester_email``, ``subject_selector`` ...), so one mapper handles them.
"""
from __future__ import annotations

import re

from ..models import Observation
from ..util import EMAIL_RE, classify_key, is_null, norm_email, norm_name, norm_phone

NAME_FIELDS = ("full_name", "name", "display_name", "requester_name")
EMAIL_FIELDS = ("email", "owner_email", "customer_email", "requester_email", "uploaded_by")
PHONE_FIELDS = ("phone", "owner_phone", "requester_phone")
HASH_FIELDS = ("email_hash",)
REF_FIELDS = ("user_id", "customer_reference", "subject_reference", "subject_ref")
# fields whose value may be either an email or a user reference
MIXED_FIELDS = ("subject_selector", "lookup_value")
TEXT_FIELDS = ("message_text", "message_excerpt")

# collection / table name -> data category
COLLECTION_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "connector_configurations": "DATA_FLOW",
    "connector_subscriptions": "DATA_FLOW",
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "retained_order_records": "LEGAL_HOLD_RECORD",
}

# resource-id prefix -> (system_id, default category)
PREFIX_SYSTEM = {
    "account": ("account_service", "USER_PROFILE"),
    "search": ("search_service", "SEARCH_INDEX_ENTRY"),
    "analytics": ("analytics_platform", "ANALYTICS_PROFILE"),
    "support": ("support_platform", "SUPPORT_MESSAGE"),
    "export": ("export_service", "USER_EXPORT"),
    "data_flow": ("integration_service", "DATA_FLOW"),
    "backup": ("backup_service", "BACKUP_ARCHIVE"),
    "audit_record": ("security_service", "SECURITY_AUDIT_LOG"),
    "tombstone": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "legal_hold": ("legal_service", "LEGAL_HOLD_RECORD"),
    "marketing": ("marketing_platform", "MARKETING_PROFILE"),
    "cache": ("cache_service", "CACHE_ENTRY"),
    "email_delivery": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
    "object": ("object_storage", "SUPPORT_ATTACHMENT"),
    "telemetry": ("telemetry_service", "PRODUCT_TELEMETRY"),
}


def system_for(resource_id: str) -> tuple[str, str]:
    m = re.match(r"([a-z_]+?)_\d+$", resource_id)
    prefix = m.group(1) if m else resource_id
    return PREFIX_SYSTEM.get(prefix, ("unknown_service", "UNKNOWN"))


def _bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "enabled", "allowed"):
        return True
    if s in ("0", "false", "no", "disabled", "blocked", "denied"):
        return False
    return None


def add_key(obs: Observation, value, as_lookup: bool = False) -> None:
    k = classify_key(value) if value is not None else None
    if not k:
        return
    kind, v = k
    target = obs.lookup_keys if as_lookup else {
        "email": obs.emails, "user_ref": obs.user_refs, "hash": obs.hashes, "phone": obs.phones,
    }[kind]
    target.add(v)


def from_record(rec: dict, *, file_id: str, ts: str, system: str | None, source: str,
                location: str = "", category: str | None = None) -> Observation:
    rid = rec["resource_id"]
    sys_default, cat_default = system_for(rid)
    obs = Observation(file_id=file_id, ts=ts, system=system or sys_default, resource_id=rid,
                      present=True, source=source, location=location,
                      category=category or cat_default)
    pii = set()
    for f in NAME_FIELDS:
        if not is_null(rec.get(f)):
            obs.names.add(norm_name(rec[f]))
            pii.add("name")
    for f in EMAIL_FIELDS:
        if not is_null(rec.get(f)) and EMAIL_RE.fullmatch(str(rec[f]).strip()):
            obs.emails.add(norm_email(rec[f]))
            pii.add("email")
    for f in PHONE_FIELDS:
        if not is_null(rec.get(f)):
            obs.phones.add(norm_phone(rec[f]))
            pii.add("phone")
    for f in HASH_FIELDS:
        if not is_null(rec.get(f)):
            obs.hashes.add(str(rec[f]).lower())
            pii.add("email_hash")
    for f in REF_FIELDS + MIXED_FIELDS:
        if not is_null(rec.get(f)):
            k = classify_key(rec[f])
            if k and k[0] == "email":
                obs.emails.add(k[1])
                pii.add("email")
            elif k:
                add_key(obs, rec[f])
    for f in TEXT_FIELDS:
        text = rec.get(f)
        if not is_null(text):
            found = EMAIL_RE.findall(str(text))
            for e in found:
                obs.emails.add(norm_email(e))
            if found:
                pii.add("message")
            obs.extra["message"] = text
    # nested search hit documents
    doc = rec.get("document")
    if isinstance(doc, dict):
        sub = from_record({"resource_id": rid, **doc}, file_id=file_id, ts=ts, system=system,
                          source=source)
        _merge_ids(obs, sub)
        pii |= sub.pii_fields or set()
    obs.pii_fields = pii
    if "enabled" in rec:
        obs.enabled = _bool(rec["enabled"])
    if "restore_allowed" in rec:
        obs.restore_allowed = _bool(rec["restore_allowed"])
    for f in ("ticket_id", "account_status", "retention_until", "retained_until", "event_code", "event_name",
              "file_name", "connector_name", "download_available", "storage_class", "order_reference",
              "retention_reason", "mode", "destination", "last_delivery_status"):
        if f in rec and rec[f] is not None:
            obs.extra[f] = rec[f]
    return obs


def _merge_ids(dst: Observation, src: Observation) -> None:
    dst.emails |= src.emails
    dst.phones |= src.phones
    dst.names |= src.names
    dst.hashes |= src.hashes
    dst.user_refs |= src.user_refs
    dst.lookup_keys |= src.lookup_keys


LEDGER_RESULT = {
    # result -> (present, status)
    "FOUND": (True, "FOUND"),
    "NOT_FOUND": (False, "NOT_FOUND"),
    "IDENTIFIABLE": (True, "IDENTIFIABLE"),
    "ANONYMIZED": (True, "ANONYMIZED"),
    "REDACTED": (True, "REDACTED"),
    "ACTIVE": (True, "ACTIVE"),
    "DISABLED": (True, "DISABLED"),
    "BLOCKED": (True, "BLOCKED"),
}


def from_ledger(*, file_id: str, ts: str, system: str, resource_id: str, record_type: str,
                result: str, subject: str, source: str) -> Observation:
    """Rows of the form  system | resource | RECORD_TYPE | RESULT | subject."""
    present, status = LEDGER_RESULT.get(result.upper(), (None, result.upper()))
    category = record_type.upper()
    obs = Observation(file_id=file_id, ts=ts, system=system, resource_id=resource_id,
                      present=present, status=status, category=category, source=source)
    add_key(obs, subject, as_lookup=True)
    if category == "DATA_FLOW":
        obs.enabled = status == "ACTIVE"
    if category == "BACKUP_ARCHIVE":
        obs.restore_allowed = status == "ACTIVE"
    if status in ("ANONYMIZED", "REDACTED"):
        obs.pii_fields = set()
    elif status == "IDENTIFIABLE":
        obs.pii_fields = {"email"}
    return obs
