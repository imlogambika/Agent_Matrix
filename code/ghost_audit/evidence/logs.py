"""Operational log parsing.

The observability exports use six different line syntaxes (logfmt, JSON,
RFC-5424 syslog, bracketed traces, pipe-delimited streams and cloud-runtime
envelopes).  Each line is tokenised into ``(ts, service, trace, event, kv)``;
lookups are then stitched to their results through the trace id, because the
identifier used for a lookup (``lookup_key`` ...) is usually logged on the
*start* event while the resource and its state appear on the *completion*.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from ..models import Observation
from .records import add_key, from_ledger, from_record, system_for

KV_RE = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
LOOKUP_FIELDS = ("lookup_key", "match_key", "subject_key", "scan_key", "subject_ref", "owner_key",
                 "correlation_key")
NOISE_SERVICES = {"log_router", "dns_cache", "secret_agent", "webhook_delivery", "queue_consumer",
                  "feature_flags", "circuit_breaker", "lambda_extension", "readback_collector",
                  "cert_agent", "checkpoint_writer", "http_client", "kernel_monitor", "runtime_config",
                  "scheduler", "systemd", "thread_pool", "attachment_preview"}


def tokenize(line: str):
    m = TS_RE.match(line.rstrip("\n"))
    if not m:
        return None
    ts, rest = m.groups()
    kv: dict = {}
    service = trace = event = None
    brace = rest.find('{"')
    if brace >= 0:  # structured JSON envelope
        try:
            kv = json.loads(rest[brace:])
        except json.JSONDecodeError:
            kv = {}
    if not kv:
        body = rest
        inner = ""
        m_msg = re.search(r'message="((?:[^"\\]|\\.)*)"', rest)
        if m_msg and '\\"' in m_msg.group(1):     # cloud-runtime envelope: key=values escaped inside message
            inner = m_msg.group(1).replace('\\"', '"')
            body = rest[:m_msg.start()] + rest[m_msg.end():]
        for part in (body.replace('\\"', '"'), inner):
            for km in KV_RE.finditer(part):
                kv[km.group(1)] = km.group(2) if km.group(2) is not None else km.group(3)
        if (m2 := re.match(r"<\d+>1 \S+ (\w+) (\S+) - - ", rest)):            # syslog
            service, trace = m2.groups()
        elif (m2 := re.match(r"\[\w+\] \[(\w+)\] \[([^\]]+)\] (\w+):", rest)):  # bracketed
            service, trace, event = m2.groups()
        elif (m2 := re.match(r"\|?\s*\w+\s*\|\s*(\w+)\s*\|\s*(\S+)\s*\|\s*(\w+)\s*\|", rest)):  # pipe stream
            service, trace, event = m2.groups()
    service = service or kv.get("service") or kv.get("function") or kv.get("logger")
    trace = trace or kv.get("trace_id") or kv.get("requestId")
    event = event or kv.get("event")
    return ts, service, trace, event, kv


def _present_from(kv: dict, event: str):
    if "rows" in kv:
        return str(kv["rows"]) != "0"
    if "http_status" in kv:
        return str(kv["http_status"]) == "200"
    if event == "index_query_completed":
        return str(kv.get("result", "hit")) == "hit"
    if "available" in kv:
        return str(kv["available"]).lower() == "true"
    if "archive_present" in kv:
        return str(kv["archive_present"]).lower() == "true"
    if "marker_found" in kv:
        return True  # record still readable; marker check says whether it was deleted
    return True


def _truthy(v):
    return None if v is None else str(v).lower() in ("true", "1", "allowed", "allow")


def parse_log(path: str, file_id: str, captured_at: str):
    lines = open(path, encoding="utf-8").read().splitlines()
    tokens = [t for t in (tokenize(l) for l in lines) if t]
    trace_keys: dict[str, set] = defaultdict(set)
    for ts, service, trace, event, kv in tokens:
        for f in LOOKUP_FIELDS:
            if f in kv and trace:
                trace_keys[trace].add(kv[f])

    observations: list[Observation] = []
    for ts, service, trace, event, kv in tokens:
        if service in NOISE_SERVICES:
            continue
        if event == "record_lookup" and "system" in kv:  # ledger-style diagnostic exports
            observations.append(from_ledger(file_id=file_id, ts=ts, system=kv["system"],
                                            resource_id=kv["resource_id"], record_type=kv["record_type"],
                                            result=kv["result"], subject=kv.get("subject_reference"),
                                            source="log_ledger"))
            continue
        rid = kv.get("resource_id")
        if not rid or not event or event.endswith("_started") or event in (
                "cache_lookup", "connection_acquired", "dependency_retry", "dependency_recovered"):
            continue
        obs = _observation(ts, service, event, kv, rid, file_id)
        if obs is None:
            continue
        for key in trace_keys.get(trace, ()):
            add_key(obs, key, as_lookup=True)
        for f in LOOKUP_FIELDS:
            if f in kv:
                add_key(obs, kv[f], as_lookup=True)
        observations.append(obs)
    return observations, []


def _observation(ts, service, event, kv, rid, file_id):
    system = system_for(rid)[0]
    if service == "profile_service":
        system = "profile_service"
    rec = {k: v for k, v in kv.items() if k not in ("event", "service", "level", "severity")}
    rec["resource_id"] = rid
    obs = from_record(rec, file_id=file_id, ts=ts, system=system, source=f"log:{event}")
    obs.present = _present_from(kv, event)
    obs.extra["event"] = event
    pii = set(obs.pii_fields or ())

    if event in ("message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"):
        add_key(obs, kv.get("matched_value"))
        val = kv.get("matched_value")
        if val:
            pii.add("email" if "@" in val else "identifier")
        if event == "attachment_pii_scan":
            obs.category = "SUPPORT_ATTACHMENT"
            obs.extra["file_name"] = kv.get("file")
        elif event == "message_pii_scan":
            obs.category = "SUPPORT_MESSAGE"
    if event in ("analytics_profile_read", "analytics_identity_scan") and "direct_identifiers" in kv:
        if str(kv["direct_identifiers"]) == "0":
            pii = set()
        elif not pii:
            pii.add("identifier")
    if event == "contact_lookup_completed":
        # marketing contact keyed by an identifier; carries opt-in state
        obs.category = "MARKETING_PROFILE"
        obs.extra["email_opt_in"] = kv.get("email_opt_in")
    if event == "message_read":
        text = kv.get("message_text", "")
        obs.category = "SUPPORT_MESSAGE"
        if text and "[REDACTED]" not in text and "@" not in text:
            pass
    if event in ("connector_heartbeat", "connector_config_loaded", "connector_config_read_completed"):
        obs.enabled = _truthy(kv.get("enabled"))
        obs.extra["scheduler_registered"] = kv.get("scheduler_registered")
    if event == "restore_candidate_evaluated":
        eligible = _truthy(kv.get("eligible"))
        blocked = _truthy(kv.get("deletion_block"))
        obs.restore_allowed = bool(eligible) and not blocked
    if event == "restore_authorization_completed":
        obs.restore_allowed = str(kv.get("decision", "")).lower() in ("allow", "allowed", "approve", "approved")
    if event == "restore_policy_read_completed":
        obs.restore_allowed = _truthy(kv.get("restore_allowed"))
    if event == "retained_order_loaded":
        obs.category = "LEGAL_HOLD_RECORD"
    if event == "deletion_marker_check":
        obs.extra["marker_found"] = kv.get("marker_found")
    obs.pii_fields = pii
    return obs
