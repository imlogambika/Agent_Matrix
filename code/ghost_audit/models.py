"""Core data structures shared by every stage of the audit pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Direct identifiers that policy treats as personal data.  ``user_ref`` (the
# stable internal user_id / customer_reference) is *not* in this list because a
# tombstone or retained record may legitimately keep it.
DIRECT_ID_FIELDS = ("name", "email", "phone", "email_hash")


@dataclass
class Observation:
    """One fact about one resource, extracted from one evidence file.

    Every parser (SQL, JSON, logs, text, PDF, image) emits Observations so the
    downstream state reconstruction never has to know the source format.
    """

    file_id: str
    ts: str                      # ISO-8601 instant the fact describes
    system: str                  # system_id, e.g. account_service
    resource_id: str
    present: Optional[bool]      # True = record exists, False = proven absent
    source: str                  # parser tag, useful for debugging
    status: Optional[str] = None  # FOUND / NOT_FOUND / IDENTIFIABLE / ANONYMIZED / REDACTED / ACTIVE / DISABLED / BLOCKED
    category: Optional[str] = None  # data category when the evidence states it
    # identifiers visible in the record (lower-cased emails, normalised phones)
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    names: set = field(default_factory=set)
    hashes: set = field(default_factory=set)
    user_refs: set = field(default_factory=set)
    # keys used to *find* the record (lookup_key, correlation_key, subject_key ...)
    lookup_keys: set = field(default_factory=set)
    # which direct identifier fields are populated (None = not reported)
    pii_fields: Optional[set] = None
    enabled: Optional[bool] = None          # data flows
    restore_allowed: Optional[bool] = None  # backups
    location: str = ""                      # table / replica / collection
    extra: dict = field(default_factory=dict)

    def identifiers(self) -> set:
        return self.emails | self.phones | self.names | self.hashes | self.user_refs | self.lookup_keys


@dataclass
class ScopeCapture:
    """A full capture of one system's collection at one instant.

    If a resource of ``system`` is not listed in a scope capture, the capture is
    evidence that the resource was absent at ``ts``.
    """

    file_id: str
    ts: str
    system: str
    resource_ids: set
    location: str = ""


@dataclass
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass
class Identity:
    user_id: str
    names: set
    emails: set
    phones: set
    hashes: set
    current_email: str = ""

    def all_keys(self) -> set:
        return self.names | self.emails | self.phones | self.hashes | {self.user_id}


@dataclass
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass
class Step:
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str = ""

    def as_dict(self, step_no: int) -> dict:
        return {
            "step_no": step_no,
            "phase": self.phase,
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
            "state": self.state,
            "evidence_id": self.evidence_id,
        }
