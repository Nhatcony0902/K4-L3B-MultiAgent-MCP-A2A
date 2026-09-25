from __future__ import annotations

from typing import Any

import pytest

from student_agent import rules

OPENED_AT = "2018-03-01T09:00:00-03:00"


def record(purchase: str, status: str = "delivered", **dates: str) -> dict[str, Any]:
    return {
        "order_id": "o1",
        "order_status": status,
        "order_purchase_timestamp": purchase,
        "order_delivered_carrier_date": dates.get("carrier"),
        "order_delivered_customer_date": dates.get("delivered"),
        "order_estimated_delivery_date": dates.get("estimated"),
    }


def item(limit: str, price: str = "79.00", freight: str = "10.00") -> dict[str, Any]:
    return {
        "order_item_id": "i1",
        "seller_id": "s1",
        "shipping_limit_date": limit,
        "price": price,
        "freight_value": freight,
    }


def capture(at: str, amount: str, event_type: str = "captured", status: str = "confirmed"):
    return {"event_at": at, "event_type": event_type, "amount_brl": amount, "status": status}


TARGET = record(
    "2018-02-10T09:00:00-03:00",
    carrier="2018-02-12T09:00:00-03:00",
    delivered="2018-02-19T09:00:00-03:00",
    estimated="2018-02-20T09:00:00-03:00",
)
NOISE = record("2018-05-10T09:00:00-03:00", status="canceled")
TARGET_ITEM = item("2018-02-13T09:00:00-03:00")
NOISE_ITEM = item("2018-05-13T09:00:00-03:00", freight="18.00")


def facts_for(
    target: dict[str, Any] = TARGET,
    payments: list[dict[str, Any]] | None = None,
    refunds: list[dict[str, Any]] | None = None,
    shipment_events: list[dict[str, Any]] | None = None,
) -> rules.Facts:
    period = rules.select_period([NOISE, target], OPENED_AT)
    assert period is not None
    return rules.build_facts(
        period, [TARGET_ITEM, NOISE_ITEM], payments or [], refunds or [], shipment_events or []
    )


def test_select_period_prefers_latest_record_before_case_opened() -> None:
    period = rules.select_period([NOISE, TARGET], OPENED_AT)
    assert period is not None
    assert period.record is TARGET
    assert period.contains("2018-04-01T09:00:00-03:00")
    assert not period.contains("2018-05-11T09:00:00-03:00")


def test_select_period_falls_back_to_earliest_when_all_after_open() -> None:
    period = rules.select_period([NOISE], "2018-01-01T00:00:00-03:00")
    assert period is not None and period.record is NOISE


def test_noise_period_events_are_ignored() -> None:
    facts = facts_for(payments=[capture("2018-05-10T10:00:00-03:00", "79.00")])
    assert facts.captures == []
    assert facts.items == [TARGET_ITEM]
    assert rules.classify(facts) == "unsupported_claim"


@pytest.mark.parametrize(
    ("payments", "refunds", "expected"),
    [
        (
            [
                capture("2018-02-10T10:00:00-03:00", "44.50"),
                capture("2018-02-10T11:00:00-03:00", "44.50"),
            ],
            [],
            "valid_split_payment",
        ),
        (
            [
                capture("2018-02-10T10:00:00-03:00", "64.00"),
                capture("2018-02-10T11:00:00-03:00", "64.00"),
            ],
            [],
            "duplicate_charge",
        ),
        (
            [
                capture("2018-02-10T10:00:00-03:00", "35.00"),
                capture("2018-02-10T12:00:00-03:00", "35.00", "reconciliation_mismatch", "open"),
            ],
            [],
            "payment_mismatch",
        ),
        (
            [capture("2018-02-10T10:00:00-03:00", "52.00")],
            [capture("2018-03-05T09:00:00-03:00", "52.00", "refund_requested", "failed")],
            "refund_failed",
        ),
        (
            [capture("2018-02-10T10:00:00-03:00", "89.00")],
            [capture("2018-03-05T09:00:00-03:00", "89.00", "refund_requested", "pending")],
            "refund_pending",
        ),
    ],
)
def test_payment_issue_classification(payments, refunds, expected) -> None:
    assert rules.classify(facts_for(payments=payments, refunds=refunds)) == expected


def test_canceled_and_paid() -> None:
    target = {**TARGET, "order_status": "canceled"}
    facts = facts_for(target=target, payments=[capture("2018-02-10T10:00:00-03:00", "79.00")])
    assert rules.classify(facts) == "canceled_order_paid"


@pytest.mark.parametrize(
    ("actor", "expected"),
    [
        ("seller", "late_delivery_seller"),
        ("logistics_provider", "late_delivery_logistics"),
    ],
)
def test_late_delivery_event_actor_decides_responsibility(actor, expected) -> None:
    event = {
        "event_at": "2018-03-03T09:00:00-03:00",
        "event_type": "delivered_late",
        "actor": actor,
        "status": "confirmed",
    }
    assert rules.classify(facts_for(shipment_events=[event])) == expected


def test_late_without_event_uses_shipping_limit() -> None:
    late = record(
        "2018-02-10T09:00:00-03:00",
        carrier="2018-02-15T09:00:00-03:00",
        delivered="2018-02-25T09:00:00-03:00",
        estimated="2018-02-20T09:00:00-03:00",
    )
    assert rules.classify(facts_for(target=late)) == "late_delivery_seller"


def test_policy_seller_party_uses_case_seller() -> None:
    rule = {"responsible_parties": [{"party_type": "seller", "party_id": "seller-other-case"}]}
    parties = rules.responsible_parties(rule, facts_for())
    assert parties == [{"party_type": "seller", "party_id": "s1"}]


def test_unknown_issue_falls_back_to_investigation_policy() -> None:
    rule = rules.policy_rule({"rules": {}}, "insufficient_evidence")
    assert rule["case_status"] == "needs_investigation"
    assert rule["refund_brl"] == 0.0


def test_identical_records_collapse_and_rows_dedupe() -> None:
    period = rules.select_period([TARGET, dict(TARGET)], OPENED_AT)
    assert period is not None and period.record_count == 1
    events = [capture("2018-02-10T10:00:00-03:00", "89.00")] * 2
    facts = rules.build_facts(period, [TARGET_ITEM, TARGET_ITEM], events, [], [])
    assert facts.captures == [89.0]
    assert facts.items_total == 89.0


def test_refund_links_to_capture_by_amount_not_date() -> None:
    payments = [
        capture("2018-02-10T10:00:00-03:00", "35.00"),
        capture("2018-02-10T12:00:00-03:00", "35.00", "reconciliation_mismatch", "open"),
    ]
    other_refund = capture("2018-02-12T09:00:00-03:00", "89.00", "refund_requested", "pending")
    facts = facts_for(payments=payments, refunds=[other_refund])
    assert facts.refunds == []
    assert rules.classify(facts) == "payment_mismatch"


def test_split_subset_ignores_unrelated_capture() -> None:
    payments = [
        capture("2018-02-10T10:00:00-03:00", "52.00"),
        capture("2018-02-10T10:30:00-03:00", "44.50"),
        capture("2018-02-10T11:00:00-03:00", "44.50"),
    ]
    facts = facts_for(payments=payments)
    assert "valid_split_payment" in rules.supported_issues(facts)
    assert rules.issue_captured_total("valid_split_payment", facts) == 89.0
