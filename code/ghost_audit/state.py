"""Reconstruct each resource's state at a case's ``audit_at``.

Rules
-----
* Only evidence files whose manifest ``captured_at`` is at or before ``audit_at``
  are admissible, and within them only observations timestamped at or before it.
* The latest admissible observation decides the state; rows captured at the same
  instant in one file (primary + replica tables) are combined, so a copy that
  survives in any replica keeps the record present.
* Workflow results in system_activity.csv are claims and never change a state.
* A full capture that omits a record is used only when no admissible
  observation names the record at all - it then serves as the absence proof.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .evidence.records import system_for
from .models import Observation

# categories whose required end state is "record absent"
DELETE_CATEGORIES = {"USER_PROFILE", "SEARCH_INDEX_ENTRY", "USER_EXPORT", "SUPPORT_ATTACHMENT",
                     "MARKETING_PROFILE", "SECONDARY_IDENTIFIER", "CACHE_ENTRY"}
# the deletion worker's attempted action tells a support attachment from a message
SUPPORT_ACTION_CATEGORY = {"DELETE_RECORD": "SUPPORT_ATTACHMENT", "REDACT_RECORD": "SUPPORT_MESSAGE"}
SPECIFIC_CATEGORIES = ("SUPPORT_ATTACHMENT", "MARKETING_PROFILE", "SECONDARY_IDENTIFIER", "LEGAL_HOLD_RECORD")


@dataclass
class ResourceState:
    resource_id: str
    system: str
    category: str
    present: Optional[bool] = None        # None = no admissible evidence either way
    pii_exposed: bool = False
    enabled: Optional[bool] = None
    restore_allowed: Optional[bool] = None
    evidence_id: str = ""                 # evidence proving the state at audit_at
    observed_at: str = ""
    attempted: set = field(default_factory=set)
    notes: list = field(default_factory=list)


def infer_category(resource_id: str, all_obs: list[Observation], attempted: set) -> str:
    system, category = system_for(resource_id)
    cats = [o.category for o in all_obs if o.category]
    specific = [c for c in SPECIFIC_CATEGORIES if c in cats]
    if specific:
        category = specific[0]
    elif cats:
        category = max(sorted(set(cats)), key=cats.count)
    if system == "support_platform" and category == "SUPPORT_MESSAGE":
        for ev in sorted(attempted):
            category = SUPPORT_ACTION_CATEGORY.get(ev, category)
    if system == "analytics_platform" and "DELETE_RECORD" in attempted:
        category = "MARKETING_PROFILE"
    return category


def _apply_group(st: ResourceState, group: list[Observation], history: list[Observation]) -> None:
    known = [o.present for o in group if o.present is not None]
    st.present = any(known) if known else None
    flags = [o.enabled for o in group if o.enabled is not None]
    st.enabled = any(flags) if flags else None
    flags = [o.restore_allowed for o in group if o.restore_allowed is not None]
    st.restore_allowed = any(flags) if flags else None
    readings = [o for o in group if o.pii_fields is not None]
    if readings:
        st.pii_exposed = any(o.pii_fields for o in readings)
    else:
        # a bare "FOUND" row says nothing about PII: carry the last explicit reading forward
        earlier = [o for o in history if o.pii_fields is not None]
        st.pii_exposed = bool(max(earlier, key=lambda o: o.ts).pii_fields) if earlier else False
    st.evidence_id = group[0].file_id
    st.observed_at = group[0].ts


def reconstruct(resource_id: str, store, audit_at: str, attempted: set, footprint: set,
                requested_at: str, primary_file: str = "") -> ResourceState:
    all_obs = store.by_resource.get(resource_id, [])
    system, _ = system_for(resource_id)
    st = ResourceState(resource_id, system, infer_category(resource_id, all_obs, attempted),
                       attempted=set(attempted))
    admissible = [o for o in all_obs if o.ts <= audit_at and store.captured_at(o.file_id) <= audit_at]
    if admissible:
        latest = max(admissible, key=lambda o: (o.ts, o.file_id))
        group = [o for o in admissible if o.ts == latest.ts and o.file_id == latest.file_id]
        _apply_group(st, group, admissible)
        if st.present is False:
            st.pii_exposed = False
        return st

    # no admissible observation: look for a full capture that proves absence
    if st.category in DELETE_CATEGORIES:
        captures = [s for s in store.scopes if s.system == system and s.ts <= audit_at
                    and store.captured_at(s.file_id) <= audit_at and resource_id not in s.resource_ids]
        in_case = [s for s in captures if s.file_id in footprint]
        typed = [s for s in captures if s.location in ("inventory", "customer_profiles_primary",
                                                       "customer_profiles_failover_replica",
                                                       "search_documents_primary")
                 and s.ts >= requested_at]
        proof = None
        if in_case:
            proof = max(in_case, key=lambda s: (s.ts, s.file_id))
        elif primary_file:
            # the capture that documents the rest of this case no longer lists the record
            st.present = False
            st.evidence_id = primary_file
            st.notes.append("absent from the case's primary capture")
        elif typed:
            proof = min(typed, key=lambda s: (s.ts, s.file_id))
        if proof:
            st.present = False
            st.evidence_id = proof.file_id
            st.observed_at = proof.ts
            st.notes.append("absent from full capture")
    return st
