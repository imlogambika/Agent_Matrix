"""Apply deletion_rules.csv to a reconstructed resource state.

Each data category maps to one required treatment.  For every resource the
policy returns the findings (what is wrong at audit_at), the remediation that
fixes it, and the verification that proves the required end state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Step
from .state import ResourceState

# operation -> (phase, state); the order of each list is the contract's tie-break order
FINDING_OPS = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_OPS = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_OPS = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                    "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# category -> required action (mirrors deletion_rules.csv; loaded rules override)
DEFAULT_ACTIONS = {
    "USER_PROFILE": "DELETE", "SECONDARY_IDENTIFIER": "DELETE", "SEARCH_INDEX_ENTRY": "DELETE",
    "CACHE_ENTRY": "DELETE", "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE",
    "ANALYTICS_PROFILE": "ANONYMIZE", "PRODUCT_TELEMETRY": "ANONYMIZE", "USER_EXPORT": "DELETE",
    "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT", "SECURITY_AUDIT_LOG": "RETAIN",
    "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "RETAIN", "ACCOUNT_TOMBSTONE": "REDACT",
    "DATA_FLOW": "DISABLE",  # not a stored-data category: an active flow can recreate data
}


@dataclass
class Treatment:
    findings: list = field(default_factory=list)      # (operation, evidence_id)
    remediation: list = field(default_factory=list)   # operations
    verification: list = field(default_factory=list)  # (operation, evidence_id)


class Policy:
    def __init__(self, rules: dict):
        self.actions = dict(DEFAULT_ACTIONS)
        for category, rule in rules.items():
            self.actions[category] = rule["required_action"].upper()

    def action(self, category: str) -> str:
        return self.actions.get(category, "DELETE")

    def treat(self, st: ResourceState) -> Treatment:
        t = Treatment()
        action = self.action(st.category)
        ev = st.evidence_id
        if st.category == "DATA_FLOW":
            if st.enabled:
                t.findings.append(("REPORT_ACTIVE_DATA_FLOW", ev))
                t.remediation.append("DISABLE_DATA_FLOW")
            t.verification.append(("VERIFY_DATA_FLOW_DISABLED", ev))
        elif st.category == "BACKUP_ARCHIVE":
            if st.restore_allowed:
                t.findings.append(("REPORT_RESTORE_RISK", ev))
                t.remediation.append("BLOCK_RESTORE")
            t.verification.append(("VERIFY_RESTORE_BLOCKED", ev))
        elif action == "RETAIN":
            t.verification.append(("VERIFY_RECORD_PRESENT", ev))
        elif action == "DELETE":
            if st.present:
                t.findings.append(("REPORT_REMAINING_RECORD", ev))
                t.remediation.append("DELETE_RECORD")
            t.verification.append(("VERIFY_RECORD_ABSENT", ev))
        elif action in ("REDACT", "ANONYMIZE"):
            if st.present is not False and st.pii_exposed:
                t.findings.append(("REPORT_EXPOSED_PII", ev))
                t.remediation.append(f"{action}_RECORD")
            verify = "VERIFY_RECORD_REDACTED" if action == "REDACT" else "VERIFY_RECORD_ANONYMIZED"
            t.verification.append((verify, ev))
        return t


def order_steps(steps: list[Step]) -> list[Step]:
    phase_rank = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
    op_rank = {op: i for i, op in enumerate(FINDING_OPS + REMEDIATION_OPS + VERIFICATION_OPS)}
    return sorted(steps, key=lambda s: (phase_rank[s.phase], op_rank[s.operation], s.system_id, s.resource_id))
