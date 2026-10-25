"""Plain-text operational records (shift handovers and similar)."""
from __future__ import annotations

import re

from .records import from_ledger

ROW_RE = re.compile(r"^-\s*(\S+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(.+?)\s*$")


def parse_text(path: str, file_id: str, captured_at: str):
    observations = []
    for line in open(path, encoding="utf-8"):
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        ts, system, rid, record_type, result, subject = m.groups()
        observations.append(from_ledger(file_id=file_id, ts=ts, system=system, resource_id=rid,
                                        record_type=record_type, result=result, subject=subject,
                                        source="text_handover"))
    return observations, []
