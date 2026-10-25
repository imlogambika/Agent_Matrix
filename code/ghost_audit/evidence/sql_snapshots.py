"""SQLite-compatible snapshot exports.

Three layouts exist in the dataset:

* inventory exports - typed tables plus a ``snapshot_scope`` table that says
  which systems were fully captured and when;
* topology exports - ``customer_data_<primary|replica...>`` tables holding
  ``record_json`` rows, used to investigate replication drift;
* small typed primary/replica tables (``customer_profiles_primary`` ...).

Every row becomes an Observation; every fully captured (system, table) becomes
a ScopeCapture so that a missing row can prove absence.
"""
from __future__ import annotations

import json
import re
import sqlite3

from ..models import Observation, ScopeCapture
from .records import COLLECTION_CATEGORY, from_record, system_for

TYPED_TABLES = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def _header_ts(text: str) -> str | None:
    m = re.search(r"Captured at:\s*(\S+)", text)
    return m.group(1) if m else None


def parse_sql(path: str, file_id: str, captured_at: str):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    con = sqlite3.connect(":memory:")
    con.executescript(text)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    header_ts = _header_ts(text) or captured_at
    observations: list[Observation] = []
    scopes: list[ScopeCapture] = []

    if "snapshot_scope" in tables:  # inventory layout
        scope = {r["origin_system"]: r["captured_at"] for r in con.execute("select * from snapshot_scope")}
        seen = {s: set() for s in scope}
        for table, system in TYPED_TABLES.items():
            if table not in tables:
                continue
            for row in con.execute(f'select * from "{table}"'):
                rec = dict(row)
                obs = from_record(rec, file_id=file_id, ts=scope.get(system, captured_at), system=system,
                                  source="sql_inventory", location=table,
                                  category=COLLECTION_CATEGORY.get(table))
                obs.extra["updated_at"] = rec.get("updated_at")
                observations.append(obs)
                seen.setdefault(system, set()).add(rec["resource_id"])
        for system, ts in scope.items():
            scopes.append(ScopeCapture(file_id, ts, system, seen.get(system, set()), "inventory"))
        return observations, scopes

    for table in tables:
        cols = [r[1] for r in con.execute(f'pragma table_info("{table}")')]
        rows = [dict(r) for r in con.execute(f'select * from "{table}"')]
        location = table
        if "record_json" in cols:  # topology layout
            per_system: dict[str, set] = {}
            for rec in rows:
                body = json.loads(rec["record_json"])
                body.setdefault("resource_id", rec["resource_id"])
                system = rec["origin_system"]
                obs = from_record(body, file_id=file_id, ts=header_ts, system=system,
                                  source="sql_topology", location=location,
                                  category=COLLECTION_CATEGORY.get(rec["collection_name"]))
                obs.extra["updated_at"] = rec.get("updated_at")
                obs.extra["collection"] = rec["collection_name"]
                observations.append(obs)
                per_system.setdefault(system, set()).add(rec["resource_id"])
            for system, ids in per_system.items():
                scopes.append(ScopeCapture(file_id, header_ts, system, ids, location))
        else:  # small typed tables, e.g. customer_profiles_failover_replica
            system = None
            ids = set()
            for rec in rows:
                rid = rec["resource_id"]
                system = system_for(rid)[0]
                obs = from_record(rec, file_id=file_id, ts=header_ts, system=system,
                                  source="sql_typed", location=location)
                observations.append(obs)
                ids.add(rid)
                if rec.get("source_resource_id"):
                    obs.extra["source_resource_id"] = rec["source_resource_id"]
            if system is None:
                system = {"customer_profiles": "account_service",
                          "search_documents": "search_service"}.get(table.rsplit("_", 1)[0])
            if system:
                scopes.append(ScopeCapture(file_id, header_ts, system, ids, location))
    return observations, scopes
