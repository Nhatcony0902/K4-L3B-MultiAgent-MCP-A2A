"""Deterministic investigation rules over MCP evidence (no I/O, unit-testable)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any

MONEY_TOLERANCE = 0.01
REFUND_DONE_STATUSES = frozenset({"completed", "succeeded", "refunded", "confirmed"})
OPEN_STATUSES = frozenset({"open", "pending", "confirmed"})

ISSUE_TO_PAYMENT_VERDICT = {
    "refund_failed": "refund_failed",
    "refund_pending": "refund_pending",
    "payment_mismatch": "capture_mismatch",
    "duplicate_charge": "duplicate_capture",
    "insufficient_evidence": "insufficient_evidence",
}
LATE_ISSUE_TO_SHIPMENT_VERDICT = {
    "late_delivery_seller": "seller_delay",
    "late_delivery_logistics": "logistics_delay",
}
FALLBACK_POLICY_RULE = {
    "case_status": "needs_investigation",
    "recommended_action": "escalate_investigation",
    "refund_brl": 0.0,
    "responsible_parties": [{"party_type": "unknown", "party_id": None}],
}


def parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def same_amount(left: float, right: float) -> bool:
    return abs(left - right) <= MONEY_TOLERANCE


@dataclass(frozen=True)
class Period:
    """Time window owning one order record; events inside it belong to that record."""

    record: dict[str, Any]
    start: datetime
    end: datetime | None
    record_count: int

    def contains(self, value: Any) -> bool:
        moment = parse_ts(value)
        if moment is None:
            return False
        return moment >= self.start and (self.end is None or moment < self.end)


def candidate_periods(records: list[dict[str, Any]]) -> list[Period]:
    """One period per distinct purchase timestamp; identical duplicate records collapse."""
    by_start: dict[datetime, dict[str, Any]] = {}
    for record in records:
        moment = parse_ts(record.get("order_purchase_timestamp"))
        if moment is not None and moment not in by_start:
            by_start[moment] = record
    starts = sorted(by_start)
    return [
        Period(by_start[s], s, starts[i + 1] if i + 1 < len(starts) else None, len(starts))
        for i, s in enumerate(starts)
    ]


def default_period(periods: list[Period], opened_at: str) -> Period | None:
    """Latest record purchased at/before the case opened; else the earliest record."""
    if not periods:
        return None
    opened = parse_ts(opened_at)
    eligible = [p for p in periods if opened is None or p.start <= opened]
    return eligible[-1] if eligible else periods[0]


def select_period(records: list[dict[str, Any]], opened_at: str) -> Period | None:
    return default_period(candidate_periods(records), opened_at)


@dataclass
class Facts:
    order_id: str
    status: str | None
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None
    items: list[dict[str, Any]] = field(default_factory=list)
    captures: list[float] = field(default_factory=list)
    open_mismatches: list[float] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    shipment_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def items_total(self) -> float:
        return round(
            sum(money(i.get("price")) + money(i.get("freight_value")) for i in self.items), 2
        )

    @property
    def captured_total(self) -> float:
        return round(sum(self.captures), 2)

    @property
    def refunded_total(self) -> float:
        return round(
            sum(
                money(r.get("amount_brl"))
                for r in self.refunds
                if str(r.get("status", "")).lower() in REFUND_DONE_STATUSES
                and r.get("event_type") != "refund_requested"
            ),
            2,
        )

    @property
    def seller_ids(self) -> list[str]:
        return _unique(i.get("seller_id") for i in self.items)

    @property
    def item_ids(self) -> list[str]:
        return _unique(i.get("order_item_id") for i in self.items)


def _unique(values: Any) -> list[str]:
    seen: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in seen:
            seen.append(value)
    return seen


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique_rows: list[dict[str, Any]] = []
    for row in rows:
        key = json.dumps(row, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique_rows.append(row)
    return unique_rows


def _in_period(period: Period, rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    rows = _dedupe(rows)
    # A single known record owns everything, even rows whose timestamp is missing.
    if period.record_count <= 1:
        return rows
    return [row for row in rows if period.contains(row.get(key))]


def build_facts(
    period: Period,
    items: list[dict[str, Any]],
    payment_events: list[dict[str, Any]],
    refund_events: list[dict[str, Any]],
    shipment_events: list[dict[str, Any]],
) -> Facts:
    record = period.record
    scoped_payments = _in_period(period, payment_events, "event_at")
    captures = [
        money(e.get("amount_brl"))
        for e in scoped_payments
        if e.get("event_type") == "captured"
        and str(e.get("status", "confirmed")).lower() != "failed"
    ]
    refunds = [
        e for e in _dedupe(refund_events) if str(e.get("event_type", "")).startswith("refund")
    ]
    if captures:
        # A refund belongs to the record whose capture it reverses (amount link), not by date.
        refunds = [
            e for e in refunds if any(same_amount(money(e.get("amount_brl")), c) for c in captures)
        ]
    else:
        refunds = _in_period(period, refunds, "event_at")
    return Facts(
        order_id=record.get("order_id", ""),
        status=record.get("order_status"),
        carrier_at=parse_ts(record.get("order_delivered_carrier_date")),
        delivered_at=parse_ts(record.get("order_delivered_customer_date")),
        estimated_at=parse_ts(record.get("order_estimated_delivery_date")),
        items=_in_period(period, items, "shipping_limit_date"),
        captures=captures,
        open_mismatches=[
            money(e.get("amount_brl"))
            for e in scoped_payments
            if e.get("event_type") == "reconciliation_mismatch"
            and str(e.get("status", "")).lower() in OPEN_STATUSES
        ],
        refunds=refunds,
        shipment_events=_in_period(period, shipment_events, "event_at"),
    )


def late_actor(facts: Facts) -> str | None:
    """Return 'seller' / 'logistics_provider' when delivery was late, else None."""
    late_events = [e for e in facts.shipment_events if e.get("event_type") == "delivered_late"]
    if late_events:
        actor = str(late_events[0].get("actor", "")).lower()
        return "seller" if actor == "seller" else "logistics_provider"
    if facts.delivered_at and facts.estimated_at and facts.delivered_at > facts.estimated_at:
        limits = [parse_ts(i.get("shipping_limit_date")) for i in facts.items]
        limits = [value for value in limits if value]
        if facts.carrier_at and limits and facts.carrier_at > min(limits):
            return "seller"
        return "logistics_provider"
    return None


def split_subset(facts: Facts) -> list[float] | None:
    """Smallest group (>=2) of captures that exactly pays the order total."""
    total = facts.items_total
    if not facts.items or len(facts.captures) < 2:
        return None
    for size in range(2, len(facts.captures) + 1):
        for group in combinations(facts.captures, size):
            if same_amount(sum(group), total):
                return list(group)
    return None


def supported_issues(facts: Facts) -> list[str]:
    """Every issue the period evidence supports, in precedence order."""
    status = (facts.status or "").lower()
    refund_states = {str(r.get("status", "")).lower() for r in facts.refunds}
    found: list[str] = []
    if status == "canceled" and facts.captures:
        found.append("canceled_order_paid")
    if status == "unavailable" and facts.captures:
        found.append("unavailable_order_paid")
    if "failed" in refund_states:
        found.append("refund_failed")
    if "pending" in refund_states:
        found.append("refund_pending")
    if facts.open_mismatches:
        found.append("payment_mismatch")
    if split_subset(facts):
        found.append("valid_split_payment")
    repeated = [a for a, n in Counter(facts.captures).items() if n >= 2]
    if any(not same_amount(2 * a, facts.items_total) for a in repeated):
        found.append("duplicate_charge")
    actor = late_actor(facts)
    if actor == "seller":
        found.append("late_delivery_seller")
    elif actor == "logistics_provider":
        found.append("late_delivery_logistics")
    return found or ["unsupported_claim"]


def classify(facts: Facts) -> str:
    """Primary issue from period-scoped evidence; highest precedence wins."""
    return supported_issues(facts)[0]


def issue_captured_total(issue: str, facts: Facts) -> float:
    if issue == "valid_split_payment":
        group = split_subset(facts)
        if group:
            return round(sum(group), 2)
    return facts.captured_total


def shipment_verdict(issue: str, facts: Facts) -> str:
    if issue in LATE_ISSUE_TO_SHIPMENT_VERDICT:
        return LATE_ISSUE_TO_SHIPMENT_VERDICT[issue]
    status = (facts.status or "").lower()
    if status == "delivered" and facts.delivered_at:
        return "on_time"
    return "insufficient_evidence"


def payment_verdict(issue: str, facts: Facts) -> str:
    if issue in ISSUE_TO_PAYMENT_VERDICT:
        return ISSUE_TO_PAYMENT_VERDICT[issue]
    if facts.refunded_total > 0:
        return "refunded"
    return "reconciled" if facts.captures else "insufficient_evidence"


def policy_rule(policy: dict[str, Any] | None, issue: str) -> dict[str, Any]:
    rules = (policy or {}).get("rules") or {}
    rule = rules.get(issue)
    return rule if isinstance(rule, dict) else FALLBACK_POLICY_RULE


def responsible_parties(rule: dict[str, Any], facts: Facts | None) -> list[dict[str, Any]]:
    """Policy decides party types; seller identity comes from this case's own evidence."""
    sellers = facts.seller_ids if facts else []
    parties: list[dict[str, Any]] = []
    for party in rule.get("responsible_parties") or []:
        party_type = party.get("party_type", "unknown")
        party_id = party.get("party_id")
        if party_type == "seller":
            party_id = sellers[0] if sellers else None
        parties.append({"party_type": party_type, "party_id": party_id})
    return parties[:5] or [{"party_type": "unknown", "party_id": None}]


def claim_verdict(topic: str, issue: str, refund: float, captured: float) -> str:
    if topic == "requested_full_refund":
        if refund <= 0:
            return "unsupported"
        return (
            "supported"
            if captured and refund >= captured - MONEY_TOLERANCE
            else "partially_supported"
        )
    if issue == "insufficient_evidence":
        return "insufficient_evidence"
    return "supported" if topic == issue else "unsupported"
