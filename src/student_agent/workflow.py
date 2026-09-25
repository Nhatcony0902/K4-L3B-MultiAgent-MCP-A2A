from __future__ import annotations

import json
from typing import Any

from . import OUTPUT_SCHEMA_VERSION, rules
from .llm import LlmConfig, LlmVerifier
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PRIMARY_ISSUES = [
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
]
TRANSPORT_RETRIES = 1
CONFIDENCE_CLAIM_MATCH = 0.98
CONFIDENCE_CLAIM_MISMATCH = 0.7
CONFIDENCE_NO_EVIDENCE = 0.3
LLM_DISAGREEMENT_PENALTY = 0.15
ENTITY_CONFIDENCE_VERIFIED = 0.95
ENTITY_CONFIDENCE_UNVERIFIED = 0.6

REFUND_ISSUES = frozenset({"refund_pending", "refund_failed"})
BASE_EVIDENCE_TOOLS = ("get_customer_history", "get_order", "get_policy")
# Evidence precision: each issue cites only the domains that prove it.
ISSUE_EVIDENCE_TOOLS = {
    "late_delivery_seller": ("get_order_items", "get_shipment_summary"),
    "late_delivery_logistics": ("get_order_items", "get_shipment_summary"),
    "canceled_order_paid": ("get_order_items", "get_payment_timeline"),
    "unavailable_order_paid": ("get_order_items", "get_payment_timeline"),
    "refund_pending": ("get_payment_timeline", "get_refund_timeline"),
    "refund_failed": ("get_payment_timeline", "get_refund_timeline"),
    "payment_mismatch": ("get_payment_timeline",),
    "duplicate_charge": ("get_payment_timeline",),
    "valid_split_payment": ("get_order_items", "get_payment_timeline"),
    "unsupported_claim": ("get_order_items", "get_shipment_summary", "get_payment_timeline"),
}

_llm_state: dict[str, LlmVerifier | None] = {}


class GatewayUnavailable(Exception):
    """MCP refused calls that must succeed; the runner restarts the whole run."""


def _llm() -> LlmVerifier | None:
    if "verifier" not in _llm_state:
        config = LlmConfig.from_env()
        _llm_state["verifier"] = LlmVerifier(config) if config else None
    return _llm_state["verifier"]


class CaseScope:
    """Per-case MCP access: case-scoped cache, bounded retries, evidence + trace linkage."""

    def __init__(self, case_id: str, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case_id = case_id
        self.gateway = gateway
        self.trace = trace
        self.refs: dict[str, str] = {}
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any] | None] = {}

    def emit(self, event_type: str, actor: str, **kwargs: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **kwargs)

    def assign(self, actor: str, target: str, task: str) -> None:
        self.emit("task_assigned", actor, target=target, decision_code=task)

    def handoff(self, actor: str, target: str, code: str) -> None:
        self.emit("handoff", actor, target=target, decision_code=code)

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Any:
        key = (tool, tuple(sorted(arguments.items())))
        if key in self._cache:
            evidence = self._cache[key]
            return evidence["data"] if evidence else None
        evidence = None
        for attempt in range(TRANSPORT_RETRIES + 1):
            try:
                evidence = await self.gateway.call(tool, case_id=self.case_id, **arguments)
                break
            except RuntimeError:
                break  # tool-level error (e.g. no rows): deterministic, never retry
            except Exception:  # transport failure: bounded retry, then surface to runner
                if attempt == TRANSPORT_RETRIES:
                    raise
        self._cache[key] = evidence
        if evidence is None:
            self.emit("tool_result_consumed", actor, tool_name=tool, decision_code="NO_EVIDENCE")
            return None
        self.refs[tool] = evidence["evidence_ref"]
        self.emit(
            "tool_result_consumed",
            actor,
            tool_name=tool,
            evidence_refs=[evidence["evidence_ref"]],
            attributes={
                "domain": evidence["domain"],
                "warnings": len(evidence.get("warnings", [])),
            },
        )
        return evidence["data"]

    def ref_list(self, *tools: str) -> list[str]:
        return [self.refs[t] for t in tools if t in self.refs]


async def _entity_agent(case: dict[str, Any], scope: CaseScope) -> dict[str, Any]:
    request = case.get("customer_request", {})
    claimed = request.get("claimed_order_id")
    candidates = [c for c in case.get("candidate_order_ids", []) if isinstance(c, str)]
    if claimed and claimed not in candidates:
        candidates.insert(0, claimed)
    hint = case.get("customer_unique_id_hint")

    history = None
    if hint:
        history = await scope.fetch("entity-agent", "get_customer_history", customer_unique_id=hint)
    if hint and history is None:
        # Customer history must exist for a hinted customer; a refusal means the gateway is
        # unhealthy, so abort the whole run instead of emitting a degraded answer.
        raise GatewayUnavailable(f"get_customer_history refused for {scope.case_id}")
    history_orders = (history or {}).get("orders") or []
    owned = {o.get("order_id") for o in history_orders}

    resolved = [c for c in candidates if c in owned]
    order_id = claimed if claimed in resolved else (resolved[0] if resolved else None)
    verified = order_id is not None
    order_row = None
    if order_id is None:
        # History did not confirm ownership: probe candidates directly, bounded by the list.
        for candidate in candidates[:2]:
            order_row = await scope.fetch("entity-agent", "get_order", order_id=candidate)
            if order_row:
                order_id = candidate
                break

    status = "resolved" if order_id else "not_found"
    return {
        "status": status,
        "order_id": order_id,
        "rejected": [c for c in candidates if c != order_id],
        "customer_unique_id": (history or {}).get("customer_unique_id") if history else None,
        "history_orders": history_orders,
        "order_row": order_row,
        "confidence": ENTITY_CONFIDENCE_VERIFIED
        if verified
        else (ENTITY_CONFIDENCE_UNVERIFIED if order_id else 0.0),
    }


async def _specialists(
    order_id: str, policy_version: str, scope: CaseScope, need_refunds: bool
) -> dict[str, Any]:
    scope.assign("coordinator", "order-agent", "FETCH_ORDER_ITEMS")
    scope.assign("coordinator", "shipment-agent", "ANALYZE_SHIPMENT")
    scope.assign("coordinator", "payment-agent", "ANALYZE_PAYMENT_REFUND")
    scope.assign("coordinator", "policy-agent", "LOAD_POLICY")
    # Sequential on purpose: bursts of parallel calls triggered gateway refusals.
    order = await scope.fetch("order-agent", "get_order", order_id=order_id)
    items = await scope.fetch("order-agent", "get_order_items", order_id=order_id)
    shipment = await scope.fetch("shipment-agent", "get_shipment_summary", order_id=order_id)
    payments = await scope.fetch("payment-agent", "get_payment_timeline", order_id=order_id)
    # Query budget: refund lifecycle is only fetched when the claim is about a refund.
    refunds = (
        await scope.fetch("payment-agent", "get_refund_timeline", order_id=order_id)
        if need_refunds
        else None
    )
    policy = await scope.fetch("policy-agent", "get_policy", policy_version=policy_version)
    for actor in ("order-agent", "shipment-agent", "payment-agent", "policy-agent"):
        scope.handoff(actor, "conflict-resolver", "SPECIALIST_RESULT")
    return {
        "order": order,
        "items": items if isinstance(items, list) else [],
        "shipment": shipment or {},
        "payments": payments or {},
        "refunds": refunds or {},
        "policy": policy,
    }


def _conflicts(entity: dict[str, Any], found: dict[str, Any], period: rules.Period) -> list[dict]:
    conflicts: list[dict[str, Any]] = []
    order = found["order"] or {}
    order_id = period.record.get("order_id")
    raw_records = [o for o in entity["history_orders"] if o.get("order_id") == order_id]
    if period.record_count == 1 and len(raw_records) > 1:
        conflicts.append(
            {
                "field": "order_record",
                "sources": ["get_customer_history", "get_payment_timeline"],
                "selected_source": "get_customer_history",
                "resolution_code": "DEDUPLICATE_IDENTICAL_RECORDS",
            }
        )
    elif period.record_count > 1 or (
        order
        and order.get("order_purchase_timestamp") != period.record.get("order_purchase_timestamp")
    ):
        conflicts.append(
            {
                "field": "order_record",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "SELECT_RECORD_EFFECTIVE_AT_CASE_OPEN",
            }
        )
    shipment = found["shipment"]
    if shipment and shipment.get("delivered_customer_at") != period.record.get(
        "order_delivered_customer_date"
    ):
        conflicts.append(
            {
                "field": "shipment_timeline",
                "sources": ["get_shipment_summary", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "SELECT_RECORD_EFFECTIVE_AT_CASE_OPEN",
            }
        )
    return conflicts[:5]


def _facts_text(facts: rules.Facts) -> str:
    return json.dumps(
        {
            "order_status": facts.status,
            "delivered_to_carrier": facts.carrier_at.isoformat() if facts.carrier_at else None,
            "delivered_to_customer": facts.delivered_at.isoformat() if facts.delivered_at else None,
            "estimated_delivery": facts.estimated_at.isoformat() if facts.estimated_at else None,
            "items_total_brl": facts.items_total,
            "captured_amounts_brl": facts.captures,
            "open_reconciliation_mismatch_brl": facts.open_mismatches,
            "refund_events": [
                {
                    "type": r.get("event_type"),
                    "status": r.get("status"),
                    "amount": r.get("amount_brl"),
                }
                for r in facts.refunds
            ],
            "shipment_events": [
                {"type": e.get("event_type"), "actor": e.get("actor")}
                for e in facts.shipment_events
            ],
        },
        ensure_ascii=False,
    )


def _issue_refs(issue: str, scope: CaseScope) -> list[str]:
    return scope.ref_list(*ISSUE_EVIDENCE_TOOLS.get(issue, ()))


def _output_refs(issue: str, scope: CaseScope) -> list[str]:
    tools = (*BASE_EVIDENCE_TOOLS, *ISSUE_EVIDENCE_TOOLS.get(issue, ()))
    return list(dict.fromkeys(scope.ref_list(*tools)))[:30]


def _insufficient_output(case_id: str, entity: dict[str, Any], scope: CaseScope) -> dict:
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": CONFIDENCE_NO_EVIDENCE,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": [],
            "rejected_candidates": entity["rejected"][:20],
            "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity["customer_unique_id"],
            "related_order_ids": [],
        },
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": list(dict.fromkeys(scope.refs.values()))[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["escalate_investigation"],
    }


def _verify(output: dict[str, Any]) -> list[str]:
    """Cross-field invariants; returns violated invariant codes."""
    failures: list[str] = []
    status = output["assessment"]["case_status"]
    refund = output["financial_resolution"]["recommended_refund_brl"]
    lines_total = round(
        sum(line["amount_brl"] for line in output["financial_resolution"]["refund_lines"]), 2
    )
    if not rules.same_amount(lines_total, refund):
        failures.append("REFUND_LINES_TOTAL")
    if status == "no_action" and refund > 0:
        failures.append("NO_ACTION_WITH_REFUND")
    if refund > 0 and status != "action_required":
        failures.append("REFUND_WITHOUT_ACTION")
    sellers = set(output["affected_entities"]["seller_ids"])
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in sellers:
            failures.append("SELLER_NOT_AFFECTED")
    if not set(output["shipment_analysis"]["late_seller_ids"]) <= sellers:
        failures.append("LATE_SELLER_NOT_AFFECTED")
    if len(output["resolution_actions"]) != len(set(output["resolution_actions"])):
        failures.append("DUPLICATE_ACTIONS")
    if not output["evidence_refs"]:
        failures.append("NO_EVIDENCE")
    return failures


def _select_scoped_facts(
    periods: list[rules.Period],
    opened_at: str,
    found: dict[str, Any],
    claim_topics: list[str],
) -> tuple[rules.Period | None, rules.Facts | None, str]:
    """Pick the record whose evidence supports the claim; else the record effective at open."""

    def facts_of(period: rules.Period) -> rules.Facts:
        return rules.build_facts(
            period,
            found["items"],
            found["payments"].get("events") or [],
            found["refunds"].get("events") or [],
            found["shipment"].get("events") or [],
        )

    default = rules.default_period(periods, opened_at)
    if default is None:
        return None, None, "NO_RECORD"
    ranked = [default, *[p for p in reversed(periods) if p is not default]]
    for period in ranked:
        facts = facts_of(period)
        if any(topic in rules.supported_issues(facts) for topic in claim_topics):
            return period, facts, "SELECT_RECORD_SUPPORTING_CLAIM"
    return default, facts_of(default), "SELECT_RECORD_EFFECTIVE_AT_CASE_OPEN"


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    scope = CaseScope(case_id, gateway, trace)

    scope.assign("coordinator", "entity-agent", "RESOLVE_ENTITY")
    entity = await _entity_agent(case, scope)
    scope.handoff("entity-agent", "coordinator", f"ENTITY_{entity['status'].upper()}")
    if not entity["order_id"]:
        output = _insufficient_output(case_id, entity, scope)
        scope.emit("verification_completed", "verifier", decision_code="INSUFFICIENT_EVIDENCE")
        return output

    order_id = entity["order_id"]
    claims = case.get("customer_request", {}).get("claims") or []
    issue_topics = [c.get("topic") for c in claims if c.get("topic") != "requested_full_refund"]
    need_refunds = not issue_topics or any(t in REFUND_ISSUES for t in issue_topics)
    found = await _specialists(order_id, case.get("policy_version", ""), scope, need_refunds)

    records = [o for o in entity["history_orders"] if o.get("order_id") == order_id]
    if not records and (found["order"] or entity["order_row"]):
        records = [found["order"] or entity["order_row"]]
    period, facts, selection = _select_scoped_facts(
        rules.candidate_periods(records), case.get("opened_at", ""), found, issue_topics
    )
    if period is None or facts is None:
        output = _insufficient_output(case_id, entity, scope)
        scope.emit("verification_completed", "verifier", decision_code="INSUFFICIENT_EVIDENCE")
        return output

    conflicts = _conflicts(entity, found, period)
    scope.emit(
        "policy_decided",
        "conflict-resolver",
        decision_code=selection,
        attributes={"records": period.record_count, "conflicts": len(conflicts)},
    )
    scope.handoff("conflict-resolver", "policy-agent", "SCOPED_FACTS")

    supported = rules.supported_issues(facts)
    claimed = [topic for topic in issue_topics if topic in supported]
    issue = claimed[0] if claimed else supported[0]
    rule = rules.policy_rule(found["policy"], issue)
    refund = round(rules.money(rule.get("refund_brl")), 2)
    action = str(rule.get("recommended_action") or "document_no_action")
    captured = rules.issue_captured_total(issue, facts)
    scope.emit(
        "policy_decided",
        "policy-agent",
        decision_code=issue.upper(),
        evidence_refs=scope.ref_list("get_policy") or None,
    )
    scope.handoff("policy-agent", "verifier", "DRAFT_OUTPUT")

    confidence = CONFIDENCE_CLAIM_MATCH if claimed else CONFIDENCE_CLAIM_MISMATCH

    late_sellers = facts.seller_ids if issue == "late_delivery_seller" else []
    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": rule.get("case_status", "needs_investigation"),
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": facts.item_ids[:20],
            "seller_ids": facts.seller_ids[:20],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [
            {
                "claim_id": str(c.get("claim_id"))[:64],
                "verdict": rules.claim_verdict(str(c.get("topic")), issue, refund, captured),
                "confidence": confidence,
                "evidence_refs": (
                    scope.ref_list("get_payment_timeline", "get_refund_timeline", "get_policy")
                    if issue != "unsupported_claim"
                    else scope.ref_list("get_payment_timeline", "get_policy")
                    if c.get("topic") == "requested_full_refund"
                    else _issue_refs(issue, scope)
                ),
            }
            for c in claims[:5]
            if c.get("claim_id")
        ],
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": [order_id],
            "rejected_candidates": entity["rejected"][:20],
            "confidence": entity["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity["customer_unique_id"],
            "related_order_ids": list(
                dict.fromkeys(
                    o.get("order_id") for o in entity["history_orders"] if o.get("order_id")
                )
            )[:20],
        },
        "shipment_analysis": {
            "verdict": rules.shipment_verdict(issue, facts),
            "late_seller_ids": late_sellers,
            "timeline_complete": bool(
                facts.carrier_at and facts.delivered_at and facts.estimated_at
            ),
        },
        "payment_analysis": {
            "verdict": rules.payment_verdict(issue, facts),
            "captured_total_brl": captured,
            "refunded_total_brl": facts.refunded_total,
            "refundable_total_brl": refund,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": rules.responsible_parties(rule, facts),
        },
        "evidence_refs": _output_refs(issue, scope),
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": (
                [{"reason_code": action, "amount_brl": refund, "entity_id": order_id}]
                if refund > 0
                else []
            ),
        },
        "resolution_actions": [action],
    }

    llm = _llm()
    llm_label = await llm.label(_facts_text(facts), PRIMARY_ISSUES) if llm else None
    agrees = llm_label is None or llm_label == issue
    if not agrees:
        output["assessment"]["confidence"] = round(confidence - LLM_DISAGREEMENT_PENALTY, 2)
    scope.emit(
        "verification_completed",
        "llm-verifier",
        decision_code="LLM_AGREES"
        if llm_label == issue
        else ("LLM_UNAVAILABLE" if llm_label is None else "LLM_DISAGREES"),
        attributes={"model": llm.config.model if llm else None, "label": llm_label},
    )

    failures = _verify(output)
    if failures:
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.5)
    scope.emit(
        "verification_completed",
        "verifier",
        decision_code="PASS" if not failures else "FAIL_" + failures[0],
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={"failed_invariants": len(failures)},
    )
    scope.handoff("verifier", "coordinator", "VERIFIED_OUTPUT")
    return output
