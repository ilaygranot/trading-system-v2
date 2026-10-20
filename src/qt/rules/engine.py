"""Rules engine: turns rule findings into a verdict using the Playbook tiers.

- hard violation      => BLOCKED (no exception; only Ilay, by changing decisions.md)
- approvable hard     => AWAITING_APPROVAL (Ilay approves this specific case: small-account options, 3-sector exception, ...)
- default violation   => NEEDS_OVERRIDE until an agent attaches a course principle (J id) + input fact ids;
                         then it goes to Ilay marked "deviation from default" (architecture 3.1, 3.3)
- flag                => shown to Ilay, never blocks (e.g. VIX jump, horizon beyond 6 months)
- missing reason      => NEEDS_OVERRIDE until the reason is written (e.g. k between 3x and 4x, DC-19)

Exits and risk reduction are never blocked (S10-R29, part ד fix 11): every finding on them is downgraded to info.
Every money-moving action still needs Ilay's approval (principle 7), so the best status is READY_FOR_APPROVAL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from qt.playbook import Playbook, Tier, load_playbook

RISK_INCREASING = {"enter", "add"}
J_RULE = re.compile(r"^(S\d{2}|L\d{3})-J\d+$")


class Severity(str, Enum):
    BLOCK = "block"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_OVERRIDE = "needs_override"
    FLAG = "flag"
    INFO = "info"


class Status(str, Enum):
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_OVERRIDE = "needs_override"
    READY_FOR_APPROVAL = "ready_for_approval"


@dataclass
class Finding:
    rule_id: str
    message: str
    kind: str = "violation"          # violation | flag | info | needs_reason | approvable
    data: dict[str, Any] = field(default_factory=dict)
    approval_key: str | None = None  # for approvable findings
    severity: Severity = Severity.INFO
    override: "Override | None" = None


@dataclass(frozen=True)
class Override:
    """An agent's declared deviation from a default rule (architecture principle 6)."""
    rule_id: str
    principle: str               # course J rule, e.g. "S06-J6"
    fact_ids: tuple[str, ...]    # facts from the fact sheet, e.g. ("F-017",)
    reason: str

    def is_valid(self) -> bool:
        return bool(J_RULE.match(self.principle) and self.fact_ids and self.reason.strip())


@dataclass
class Verdict:
    action: str
    status: Status
    findings: list[Finding]

    def by_severity(self, sev: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is sev]

    @property
    def blocks(self) -> list[Finding]:
        return self.by_severity(Severity.BLOCK)

    @property
    def flags(self) -> list[Finding]:
        return self.by_severity(Severity.FLAG)

    def rule_ids(self, sev: Severity | None = None) -> set[str]:
        return {f.rule_id for f in self.findings if sev is None or f.severity is sev}

    @property
    def deviations(self) -> list[Finding]:
        return [f for f in self.findings if f.override is not None]


def violation(rule_id: str, message: str, **data: Any) -> Finding:
    return Finding(rule_id, message, "violation", data)


def flag(rule_id: str, message: str, **data: Any) -> Finding:
    return Finding(rule_id, message, "flag", data)


def info(rule_id: str, message: str, **data: Any) -> Finding:
    return Finding(rule_id, message, "info", data)


def needs_reason(rule_id: str, message: str, **data: Any) -> Finding:
    return Finding(rule_id, message, "needs_reason", data)


def approvable(rule_id: str, approval_key: str, message: str, **data: Any) -> Finding:
    return Finding(rule_id, message, "approvable", data, approval_key=approval_key)


def decide(action: str, findings: Iterable[Finding], overrides: Iterable[Override] = (),
           approvals: Iterable[str] = (), playbook: Playbook | None = None) -> Verdict:
    pb = playbook or load_playbook()
    ovr = {o.rule_id: o for o in overrides}
    approved = set(approvals)
    out: list[Finding] = []
    for f in findings:
        if action not in RISK_INCREASING:
            f.severity = Severity.INFO
        elif f.kind == "flag":
            f.severity = Severity.FLAG
        elif f.kind == "info":
            f.severity = Severity.INFO
        elif f.kind == "needs_reason":
            f.severity = Severity.NEEDS_OVERRIDE
        elif f.kind == "approvable":
            f.severity = Severity.FLAG if f.approval_key in approved else Severity.AWAITING_APPROVAL
        else:
            tier = pb.tier(f.rule_id)
            if tier is Tier.HARD:
                f.severity = Severity.BLOCK
            elif tier is Tier.OUT_OF_V1:
                f.severity = Severity.INFO
            else:
                o = ovr.get(f.rule_id)
                if o and o.is_valid():
                    f.override, f.severity = o, Severity.FLAG
                else:
                    f.severity = Severity.NEEDS_OVERRIDE
        out.append(f)
    sevs = {f.severity for f in out}
    if Severity.BLOCK in sevs:
        status = Status.BLOCKED
    elif Severity.AWAITING_APPROVAL in sevs:
        status = Status.AWAITING_APPROVAL
    elif Severity.NEEDS_OVERRIDE in sevs:
        status = Status.NEEDS_OVERRIDE
    else:
        status = Status.READY_FOR_APPROVAL
    return Verdict(action, status, out)
