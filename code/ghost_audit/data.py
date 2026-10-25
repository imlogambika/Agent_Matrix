"""CSV loaders and identity resolution (current + historical identifiers)."""
from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from pathlib import Path

from .models import Activity, Identity, Request
from .util import norm_email, norm_name, norm_phone, parse_ts


REQUEST_COLUMNS = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]


class InputError(ValueError):
    """A request file the audit cannot trust; raised before any decision is made."""


def load_requests(path: Path) -> list[Request]:
    """Load and validate a request file: every column present, ids unique, timestamps parseable."""
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUEST_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise InputError(f"{path.name}: missing columns {', '.join(missing)}")
        rows = list(reader)
    seen, out = set(), []
    for n, r in enumerate(rows, 2):
        if not r["case_id"] or not r["email"]:
            raise InputError(f"{path.name} line {n}: empty case_id or email")
        if r["case_id"] in seen:
            raise InputError(f"{path.name} line {n}: duplicate case_id {r['case_id']}")
        seen.add(r["case_id"])
        for col in ("requested_at", "reported_at", "audit_at"):
            try:
                parse_ts(r[col])
            except (TypeError, ValueError):
                raise InputError(f"{path.name} line {n}: {col} is not a timestamp: {r[col]!r}") from None
        if r["reported_status"] not in ("COMPLETE", "PARTIALLY_COMPLETE", "NOT_STARTED"):
            raise InputError(f"{path.name} line {n}: unknown reported_status {r['reported_status']!r}")
        out.append(Request(**{c: r[c] for c in REQUEST_COLUMNS}))
    return out


def load_activities(path: Path) -> list[Activity]:
    return [Activity(**r) for r in csv.DictReader(open(path, encoding="utf-8"))]


def load_rules(path: Path) -> dict:
    return {r["data_category"]: r for r in csv.DictReader(open(path, encoding="utf-8"))}


def email_hash(email: str) -> str:
    return hashlib.sha256(norm_email(email).encode()).hexdigest()


class IdentityResolver:
    """Maps any identifier a subject has ever had to its stable user_id."""

    def __init__(self, identities_csv: Path, audit_csv: Path):
        self.users: dict[str, Identity] = {}
        for r in csv.DictReader(open(identities_csv, encoding="utf-8")):
            self.users[r["user_id"]] = Identity(
                user_id=r["user_id"], names={norm_name(r["name"])}, emails={norm_email(r["email"])},
                phones={norm_phone(r["phone"])}, hashes={r["email_hash"].lower()},
                current_email=norm_email(r["email"]))
        self.history: dict[str, list[dict]] = defaultdict(list)
        for r in csv.DictReader(open(audit_csv, encoding="utf-8")):
            ident = self.users.get(r["user_id"])
            if not ident:
                continue
            self.history[r["user_id"]].append(r)
            for value in (r["old_value"], r["new_value"]):
                if r["field_name"] == "email":
                    ident.emails.add(norm_email(value))
                elif r["field_name"] == "phone":
                    ident.phones.add(norm_phone(value))
                elif r["field_name"] == "email_hash":
                    ident.hashes.add(value.lower())
                elif r["field_name"] == "name":
                    ident.names.add(norm_name(value))
        for ident in self.users.values():  # hashes are sha256(lower(email))
            ident.hashes |= {email_hash(e) for e in ident.emails}
        self.index: dict[tuple, str] = {}
        self.conflicts: set[tuple] = set()   # identifiers held by more than one user: never resolved
        for uid, ident in self.users.items():
            keys = [("user_ref", uid)] + [("email", e) for e in ident.emails] + \
                   [("phone", p) for p in ident.phones] + [("hash", h) for h in ident.hashes]
            for key in keys:
                if self.index.setdefault(key, uid) != uid:
                    self.conflicts.add(key)
        for key in self.conflicts:   # two people share it: linking on it could merge them
            del self.index[key]

    def resolve_email(self, email: str) -> str | None:
        return self.index.get(("email", norm_email(email)))

    def owners(self, obs) -> set:
        """User ids that an observation's identifiers point to (names are ignored)."""
        found = set()
        for kind, values in (("email", obs.emails), ("phone", obs.phones), ("hash", obs.hashes),
                             ("user_ref", obs.user_refs)):
            for v in values:
                if (kind, v) in self.index:
                    found.add(self.index[(kind, v)])
        for v in obs.lookup_keys:
            for kind in ("email", "user_ref", "hash", "phone"):
                if (kind, v) in self.index:
                    found.add(self.index[(kind, v)])
        return found
