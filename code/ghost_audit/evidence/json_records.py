"""JSON evidence: snapshot bundles, per-service readback exports, ledgers."""
from __future__ import annotations

import json

from ..models import Observation, ScopeCapture
from .records import COLLECTION_CATEGORY, add_key, from_ledger, from_record, system_for

# data_store names that are not themselves customer systems
LEDGER_STORES = {"backup_restore_service", "privacy_treatment_service", "support_storage_service"}

SERVICE_EXPORT_CATEGORY = {
    ("support_platform", "tickets"): "SUPPORT_MESSAGE",
    ("support_platform", "attachments"): "SUPPORT_ATTACHMENT",
}


def parse_json(path: str, file_id: str, captured_at: str):
    data = json.load(open(path, encoding="utf-8"))
    if "snapshots" in data:
        return _bundle(data, file_id)
    if "service_exports" in data:
        return _service_export(data, file_id, captured_at)
    if "records" in data and data["records"] and "record_type" in data["records"][0]:
        return _ledger(data["records"], file_id, "json_ledger"), []
    if "records" in data:  # single collection export
        return _collection(data, file_id, captured_at)
    return [], []


def _bundle(data: dict, file_id: str):
    observations, scopes = [], []
    for snap in data["snapshots"]:
        store, coll, ts = snap["data_store"], snap["collection"], snap["captured_at"]
        if coll == "operational_record_history" or store in LEDGER_STORES:
            observations += _ledger(snap["records"], file_id, "bundle_ledger", default_ts=ts)
            continue
        ids = set()
        for rec in snap["records"]:
            obs = from_record(rec, file_id=file_id, ts=ts, system=store, source="bundle",
                              location=coll, category=COLLECTION_CATEGORY.get(coll))
            observations.append(obs)
            ids.add(rec["resource_id"])
        scopes.append(ScopeCapture(file_id, ts, store, ids, coll))
    return observations, scopes


def _ledger(records: list, file_id: str, source: str, default_ts: str | None = None):
    out = []
    for r in records:
        out.append(from_ledger(file_id=file_id, ts=r.get("recorded_at") or default_ts,
                               system=r["system"], resource_id=r["resource_id"],
                               record_type=r["record_type"], result=r["result"],
                               subject=r.get("subject_reference") or r.get("lookup_value"),
                               source=source))
    return out


def _service_export(data: dict, file_id: str, captured_at: str):
    observations = []
    for system, colls in data["service_exports"].items():
        for coll, recs in colls.items():
            for r in recs:
                ts = r.get("observed_at") or captured_at
                if coll == "profile_lookups":
                    obs = Observation(file_id=file_id, ts=ts, system=system, resource_id=r["resource_id"],
                                      present=r.get("status_code") == 200 or bool(r.get("profile")),
                                      source="service_export", location=coll, category="USER_PROFILE")
                    if r.get("profile"):
                        sub = from_record({"resource_id": r["resource_id"], **r["profile"]}, file_id=file_id,
                                          ts=ts, system=system, source="service_export")
                        _copy_ids(obs, sub)
                        obs.pii_fields = sub.pii_fields
                    obs.status = "FOUND" if obs.present else "NOT_FOUND"
                elif coll == "index_queries":
                    docs = (r.get("hits") or {}).get("documents") or []
                    doc = next((d for d in docs if d.get("resource_id") == r["resource_id"]), None)
                    obs = from_record({"resource_id": r["resource_id"], **(doc or {})}, file_id=file_id, ts=ts,
                                      system=system, source="service_export", location=coll,
                                      category="SEARCH_INDEX_ENTRY")
                    obs.present = doc is not None
                    add_key(obs, r.get("query"), as_lookup=True)
                else:
                    obs = from_record(r, file_id=file_id, ts=ts, system=system, source="service_export",
                                      location=coll, category=SERVICE_EXPORT_CATEGORY.get((system, coll)))
                observations.append(obs)
    return observations, []


def _collection(data: dict, file_id: str, captured_at: str):
    ts = data.get("captured_at") or captured_at
    coll = data.get("collection", "")
    observations, ids, system = [], set(), None
    for rec in data["records"]:
        system = system_for(rec["resource_id"])[0]
        obs = from_record(rec, file_id=file_id, ts=ts, system=system, source="collection",
                          location=coll, category=COLLECTION_CATEGORY.get(coll))
        observations.append(obs)
        ids.add(rec["resource_id"])
    scopes = [ScopeCapture(file_id, ts, system, ids, coll)] if system else []
    return observations, scopes


def _copy_ids(dst: Observation, src: Observation) -> None:
    dst.emails |= src.emails
    dst.phones |= src.phones
    dst.names |= src.names
    dst.hashes |= src.hashes
    dst.user_refs |= src.user_refs
