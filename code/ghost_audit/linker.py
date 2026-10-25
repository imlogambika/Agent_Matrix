"""Decide which resources belong to which deletion case.

Two independent sources of membership:

1. Explicit identity links - an observation captured inside the case window
   ``[requested_at, audit_at]`` whose identifiers (current or historical email,
   phone, email hash, user_id) resolve to the subject, or a data-catalog
   ``LINK_RESOURCE_IDENTITY`` activity naming the subject.
2. The deletion batch - the deletion worker acts on every resource of a case
   shortly before / after the case's ``reported_at``.  Records that carry no
   identifier any more (a 404 profile lookup, an anonymised analytics row) can
   only be tied to their case this way.

Batch assignment works in three stages:

* candidates - every case whose ``reported_at`` sits at a plausible offset from
  the activity (see :func:`plausibility`);
* ownership  - if any evidence names the resource's owner, only that owner's
  cases stay candidates; a resource owned by someone outside the batch is a
  decoy and is dropped;
* matching   - identifier-free resources are placed most-constrained-first.  A
  case holds one primary profile and one analytics profile, so a slot already
  filled by evidence is never filled twice (the leftover belongs to a case that
  is not in the request files).  Ties are broken by evidence co-occurrence, by
  the primary delete's result matching the workflow's reported status, and by
  placing anonymised analytics rows with cases that show no other violation.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .data import IdentityResolver
from .evidence.records import system_for
from .models import Activity, Observation, Request
from .util import parse_ts

WORKER_EVENTS = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
                 "RETAIN_RECORD", "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS"}
EXCLUSIVE_SLOTS = {"account_service", "analytics_platform"}
LINK_USER_RE = re.compile(r"\bto (\S+@\S+?) and (user_\d+)")


@dataclass
class CaseLinks:
    request: Request
    user_id: str
    explicit: set = field(default_factory=set)          # resource ids linked by identifiers
    batch: dict = field(default_factory=dict)           # resource id -> worker activities
    footprint: set = field(default_factory=set)         # evidence files touching the case
    ties: dict = field(default_factory=dict)            # resource id -> rival cases it could equally belong to

    def resources(self) -> set:
        return self.explicit | set(self.batch)


def slot(resource_id: str) -> str:
    return system_for(resource_id)[0]


def case_number(case_id: str) -> int:
    return int(case_id.split("_")[1])


class Linker:
    def __init__(self, resolver: IdentityResolver, store, activities: list[Activity]):
        self.resolver = resolver
        self.store = store
        self.activities = activities
        self.owners: dict[str, set] = defaultdict(set)
        self.obs_owner: dict[int, set] = {}
        for o in store.observations:
            if o.source == "multimodal":
                # a screen shows context around a record; only the record's own contact details
                # (or a catalog link, below) say whose it is
                own = resolver.owners(Observation(o.file_id, o.ts, o.system, o.resource_id, o.present, o.source,
                                                  emails=o.emails, phones=o.phones))
            else:
                own = resolver.owners(o)
            self.obs_owner[id(o)] = own
            self.owners[o.resource_id] |= own
        self.catalog_links = []  # (time, resource_id, user_id, activity_id)
        self.leftovers: dict = {}  # batch resources no visible case can take
        for a in activities:
            if a.event_type == "LINK_RESOURCE_IDENTITY":
                m = LINK_USER_RE.search(a.details)
                if m:
                    uid = resolver.resolve_email(m.group(1)) or m.group(2)
                    self.owners[a.resource_id].add(uid)
                    self.catalog_links.append((a.event_time, a.resource_id, uid, a.activity_id))

    # ------------------------------------------------------------------
    def link(self, cases: list[tuple[Request, str]], has_findings=None) -> dict[str, CaseLinks]:
        """``has_findings(case_links)`` reports whether the evidence-linked resources
        alone already show a violation; it steers identifier-free analytics rows."""
        links = {req.case_id: CaseLinks(req, uid) for req, uid in cases}
        for cl in links.values():
            self._explicit(cl)
        self._batches(links, has_findings or (lambda cl: False))
        return links

    def _explicit(self, cl: CaseLinks) -> None:
        req, uid = cl.request, cl.user_id
        catalogued = {rid for _, rid, owner, _ in self.catalog_links if owner == uid}
        ident = self.resolver.users[uid]
        for o in self.store.observations:
            if o.source == "multimodal" and o.resource_id not in catalogued and not (
                    o.emails & ident.emails or o.phones & ident.phones):
                continue  # only on screen next to the subject: not proof the record is theirs
            # membership may come from a file exported after audit_at; the state
            # itself is later rebuilt from admissible files only
            if req.requested_at <= o.ts <= req.audit_at and uid in self.obs_owner[id(o)]:
                cl.explicit.add(o.resource_id)
                cl.footprint.add(o.file_id)
        start = (parse_ts(req.requested_at) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for t, rid, owner, _ in self.catalog_links:
            if owner == uid and start <= t <= req.audit_at:
                cl.explicit.add(rid)

    def _batches(self, links: dict[str, CaseLinks], has_findings) -> None:
        pending = self._candidates(links)
        rest = []
        for rid, acts, scores in pending:
            hit = [c for c in scores if rid in links[c].explicit]
            for c in hit:  # evidence already ties the resource to the case
                links[c].batch[rid] = acts
            if not hit:
                rest.append((rid, acts, scores))
        self._match_unowned(links, rest, has_findings)

    def _candidates(self, links: dict[str, CaseLinks]) -> list:
        """(resource, worker activities, {case_id: plausibility}) for every worker resource."""
        per_resource: dict[str, list] = defaultdict(list)
        for a in self.activities:
            if a.source_system == "deletion_worker" and a.event_type in WORKER_EVENTS:
                per_resource[a.resource_id].append(a)
        by_reported: dict[str, list] = defaultdict(list)
        for cl in links.values():
            by_reported[cl.request.reported_at].append(cl)
        reported = sorted((parse_ts(r), cls) for r, cls in by_reported.items())

        pending = []
        for rid, acts in per_resource.items():
            scores: dict[str, int] = {}
            for a in acts:
                t = parse_ts(a.event_time)
                for rep, cls in reported:
                    if abs((rep - t).total_seconds()) > 3 * 3600:
                        continue
                    sc = plausibility(a, (rep - t).total_seconds() / 60)
                    for cl in cls:
                        if sc and a.event_time <= cl.request.audit_at:
                            scores[cl.request.case_id] = max(scores.get(cl.request.case_id, 0), sc)
            owners = self.owners.get(rid, set())
            if owners:
                scores = {c: v for c, v in scores.items() if links[c].user_id in owners}
            if scores:  # an owned resource with no owner case in range is a decoy
                pending.append((rid, acts, scores))
        return pending

    def _match_unowned(self, links: dict[str, CaseLinks], rest: list, has_findings) -> None:
        clean = {c: not has_findings(cl) for c, cl in links.items()}
        acts_of = {rid: acts for rid, acts, _ in rest}

        def options(rid, scores):
            files = {o.file_id for o in self.store.by_resource.get(rid, [])}
            s = slot(rid)
            opts = []
            for c, plaus in scores.items():
                if s in EXCLUSIVE_SLOTS and any(slot(r) == s for r in links[c].resources()):
                    continue
                score = plaus * 10 + (5 if files & links[c].footprint else 0)
                if s == "account_service" and claim_matches(acts_of[rid], links[c].request.reported_status):
                    score += 8   # the primary delete's result is what the workflow reported
                if s == "analytics_platform" and clean[c]:
                    score += 3   # identifier-free analytics rows belong to otherwise clean cases
                opts.append((score, c))
            return opts

        while rest:
            ranked = []
            for rid, acts, scores in rest:
                opts = options(rid, scores)
                if opts:
                    best = max(v for v, _ in opts)
                    top = sorted((c for v, c in opts if v == best), key=case_number)
                    ranked.append((len(top), -best, min(a.event_time for a in acts), rid, acts, top))
            if not ranked:
                break  # what is left belongs to cases outside the request files
            ranked.sort(key=lambda x: x[:4])
            _, _, _, rid, acts, top = ranked[0]
            links[top[0]].batch[rid] = acts
            if len(top) > 1:
                links[top[0]].ties[rid] = top[1:]
            rest = [x for x in rest if x[0] != rid]
        self.leftovers = {rid: sorted(scores) for rid, _, scores in rest}


CLAIM_FOR_RESULT = {"SUCCESS": "COMPLETE", "PARTIAL": "PARTIALLY_COMPLETE", "FAILED": "NOT_STARTED"}


def claim_matches(acts: list[Activity], reported_status: str) -> bool:
    """The workflow's reported status mirrors the result of its primary-profile delete."""
    return any(a.event_type == "DELETE_RECORD" and a.target_system == "account_service"
               and CLAIM_FOR_RESULT.get(a.reported_result) == reported_status for a in acts)


def plausibility(a: Activity, offset_min: float) -> int:
    """How well an activity's offset from a case's reported_at fits that case.

    ``offset_min`` = reported_at - event_time.  The deletion worker submits the
    primary profile 40-80 min before the status is reported, applies successful
    treatments in the last ~35 min before it, and logs failures, partial results
    and status checks 5-140 min after it.  Hand-run workflows act within a few
    minutes of the report.  2 = typical, 1 = possible, 0 = implausible.
    """
    if a.event_type == "DELETE_RECORD" and a.target_system == "account_service":
        if 40 <= offset_min <= 80:
            return 2
    elif a.reported_result == "SUCCESS" and not a.event_type.startswith("CHECK_"):
        if 0 <= offset_min <= 35:
            return 2
        if -140 <= offset_min <= -35:
            return 1
    elif -140 <= offset_min <= -5:
        return 2
    return 1 if -15 <= offset_min <= 15 else 0
