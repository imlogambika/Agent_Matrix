"""Load every evidence file listed in the manifest into one queryable store."""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

from ..llm import LLMClient
from ..models import Activity, Observation, ScopeCapture
from .json_records import parse_json
from .logs import parse_log
from .multimodal import extract_artifact, to_observations
from .sql_snapshots import parse_sql
from .text import parse_text

ALIAS_RE = re.compile(r"linked (\w+) \(([^)]+)\)|confirmed (\w+) \(([^)]+)\)")


class EvidenceStore:
    def __init__(self, dataset_dir: Path, activities: list[Activity], llm: LLMClient, log=print,
                 extractions: dict | None = None):
        """``extractions`` (file_id -> records) replaces model extraction, for benchmarking."""
        self.dataset_dir = dataset_dir
        self.manifest = {r["file_id"]: r for r in csv.DictReader(open(dataset_dir / "evidence_files.csv"))}
        self.observations: list[Observation] = []
        self.scopes: list[ScopeCapture] = []
        self.skipped: list[str] = []
        aliases = self._aliases(activities)
        self.extractions: dict[str, list] = {}
        deferred = []
        for file_id, meta in sorted(self.manifest.items(), key=lambda kv: int(kv[0].split("_")[1])):
            path = str(dataset_dir / meta["file_path"])
            ext = Path(path).suffix.lower()
            cap = meta["captured_at"]
            if ext == ".sql":
                obs, scopes = parse_sql(path, file_id, cap)
            elif ext == ".json":
                obs, scopes = parse_json(path, file_id, cap)
            elif ext == ".log":
                obs, scopes = parse_log(path, file_id, cap)
            elif ext == ".txt":
                obs, scopes = parse_text(path, file_id, cap)
            elif ext in (".pdf", ".png", ".jpg", ".jpeg"):
                deferred.append((file_id, path, cap, meta["origin"]))
                continue
            else:
                obs, scopes = [], []
            for o in obs:
                if not o.ts:
                    o.ts = cap
            self.observations += obs
            self.scopes += scopes
        # artefacts last, so misread ids can be checked against every id seen elsewhere, and
        # ticket numbers printed on screens resolve through the ticket ids in structured records
        known = {o.resource_id for o in self.observations} | {a.resource_id for a in activities}
        for o in self.observations:
            if o.extra.get("ticket_id"):
                aliases.setdefault(o.extra["ticket_id"], o.resource_id)
        for file_id, path, cap, origin in deferred:
            if extractions is not None and file_id in extractions:
                records = extractions[file_id]
            else:
                records = extract_artifact(llm, path, file_id, cap, origin)
            self.extractions[file_id] = records
            obs = to_observations(records, file_id, cap, aliases, known)
            if not obs:
                self.skipped.append(file_id)
            self.observations += obs
        if self.skipped:
            log(f"[evidence] no linkable records extracted from: {', '.join(self.skipped)}")
        self.by_resource: dict[str, list[Observation]] = defaultdict(list)
        for o in self.observations:
            self.by_resource[o.resource_id].append(o)

    @staticmethod
    def _aliases(activities: list[Activity]) -> dict:
        """Business references (statement numbers, file names, user refs) -> resource ids."""
        out = {}
        for a in activities:
            if a.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            m = ALIAS_RE.search(a.details)
            if m:
                rid, alias = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
                if alias != rid:
                    out[alias] = rid
        return out

    def captured_at(self, file_id: str) -> str:
        return self.manifest[file_id]["captured_at"]
