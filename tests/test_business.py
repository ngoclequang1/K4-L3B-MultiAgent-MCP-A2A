from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

from student_agent.business import _payment, _select_order_snapshot, _shipment, investigate_case
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter


def test_snapshot_uses_purchase_before_case_opened():
    rows = [
        {"order_id": "one", "order_purchase_timestamp": "2018-04-10T09:00:00-03:00"},
        {"order_id": "one", "order_purchase_timestamp": "2018-01-21T09:00:00-03:00"},
    ]
    selected, start, end = _select_order_snapshot(rows, "one", "2018-02-02T09:00:00-03:00")
    assert selected is rows[1]
    assert start.isoformat() == "2018-01-21T09:00:00-03:00"
    assert end.isoformat() == "2018-04-10T09:00:00-03:00"


def test_shipment_does_not_hide_unresolved_timeline_conflict():
    order = {"order_id": "one", "order_delivered_customer_date": "2018-01-04T09:00:00-03:00",
             "order_estimated_delivery_date": "2017-12-30T09:00:00-03:00"}
    data = {"order_id": "one", "delivered_carrier_at": "2017-12-22T09:00:00-03:00",
            "delivered_customer_at": "2018-01-04T09:00:00-03:00",
            "estimated_delivery_at": "2017-12-30T09:00:00-03:00",
            "shipping_limits": [{"order_item_id": "item", "seller_id": "seller",
                                 "shipping_limit_at": "2017-12-23T09:00:00-03:00"}]}
    conflicts = []
    result = _shipment(order, data, [], conflicts)
    assert result["verdict"] == "logistics_delay"
    resolved_conflict = [{"field": "delivered_customer_at",
                          "selected_source": "get_customer_history"}]
    assert _shipment(order, data, [], resolved_conflict)["verdict"] == "logistics_delay"
    data["delivered_customer_at"] = "2018-01-03T09:00:00-03:00"
    assert _shipment(order, data, [], [])["verdict"] == "conflicting"


def test_payment_uses_capture_events_and_does_not_sum_unscoped_rows():
    rows = [
        {"payment_sequential": "1", "payment_value": "44.50"},
        {"payment_sequential": "2", "payment_value": "44.50"},
    ]
    timeline = {"payments": rows, "events": [
        {"event_type": "captured", "status": "confirmed", "amount_brl": "44.50"},
        {"event_type": "captured", "status": "confirmed", "amount_brl": "44.50"},
    ]}
    result = _payment(timeline, rows, {"events": []}, [])
    assert result["captured_total_brl"] == 89.0
    assert result["refunded_total_brl"] == 0.0
    assert result["_split_payment_valid"] is True
    assert _payment(timeline, rows, None, [])["refunded_total_brl"] is None


class Gateway:
    def __init__(self):
        self.calls = []
        self.data = {
            "get_customer_history": {"customer_unique_id": "customer", "orders": [
                {"order_id": "one", "customer_id": "row", "order_status": "delivered",
                 "order_purchase_timestamp": "2018-01-21T09:00:00-03:00",
                 "order_delivered_carrier_date": "2018-01-24T09:00:00-03:00",
                 "order_delivered_customer_date": "2018-01-25T09:00:00-03:00",
                 "order_estimated_delivery_date": "2018-01-26T09:00:00-03:00"},
            ]},
            "get_order": {"order_id": "one", "customer_id": "row",
                          "order_status": "delivered",
                          "order_purchase_timestamp": "2018-01-21T09:00:00-03:00"},
            "get_order_items": [{"order_id": "one", "order_item_id": "item",
                                 "seller_id": "seller", "product_id": "product",
                                 "shipping_limit_date": "2018-01-23T09:00:00-03:00"}],
            "get_product_context": [{"order_item_id": "item", "seller_id": "seller",
                                     "product_id": "product"}],
            "get_shipment_summary": {"order_id": "one", "order_status": "delivered",
                                     "delivered_carrier_at": "2018-01-24T09:00:00-03:00",
                                     "delivered_customer_at": "2018-01-25T09:00:00-03:00",
                                     "estimated_delivery_at": "2018-01-26T09:00:00-03:00",
                                     "shipping_limits": []},
            "get_order_payments": [
                {"payment_sequential": "1", "payment_value": "44.50"},
                {"payment_sequential": "2", "payment_value": "44.50"},
            ],
            "get_payment_timeline": {"payments": [
                {"payment_sequential": "1", "payment_value": "44.50"},
                {"payment_sequential": "2", "payment_value": "44.50"},
            ], "events": [
                {"event_at": "2018-01-21T10:00:00-03:00", "event_type": "captured",
                 "status": "confirmed", "amount_brl": "44.50"},
                {"event_at": "2018-01-21T11:00:00-03:00", "event_type": "captured",
                 "status": "confirmed", "amount_brl": "44.50"},
            ]},
            "get_refund_timeline": {"events": []},
            "get_policy": {"policy_version": "EC_POLICY_V2", "rules": {
                "valid_split_payment": {"case_status": "no_action", "refund_brl": 0,
                                        "recommended_action": "document_no_action",
                                        "responsible_parties": []},
            }},
        }
        self.domains = {"get_customer_history": "customer", "get_order": "order",
                        "get_order_items": "item", "get_product_context": "product",
                        "get_shipment_summary": "shipment", "get_order_payments": "payment",
                        "get_payment_timeline": "payment", "get_refund_timeline": "refund",
                        "get_policy": "policy"}

    async def describe_tools(self):
        return [{"name": name, "inputSchema": {"type": "object"}}
                for name in self.data]

    async def call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        idx = list(self.data).index(name)
        return {"domain": self.domains[name], "evidence_ref": f"ev_{idx:024d}",
                "data": deepcopy(self.data[name])}


def test_business_investigation_scope_policy_and_trace(tmp_path):
    async def run():
        case = {"case_id": "L3B_CASE_002", "opened_at": "2018-02-02T09:00:00-03:00",
                "customer_unique_id_hint": "customer", "candidate_order_ids": ["one"],
                "customer_request": {"claims": [
                    {"claim_id": "claim-a", "topic": "valid_split_payment"},
                    {"claim_id": "claim-b", "topic": "requested_full_refund"},
                ]}, "policy_version": "EC_POLICY_V2",
                "investigation_scope": {"include_product_context": True}}
        gateway = Gateway()
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        report = await investigate_case(case, gateway, trace)
        assert report.assessment["primary_issue"] == "valid_split_payment"
        assert report.assessment["case_status"] == "no_action"
        assert report.payment_analysis["captured_total_brl"] == 89.0
        assert report.claim_assessments[0]["verdict"] == "supported"
        assert report.claim_assessments[1]["verdict"] == "unsupported"
        assert report.financial_resolution["recommended_refund_brl"] == 0.0
        assert report.order_product["product_ids"] == ["product"]
        assert report.data_conflicts == []
        assert report.mcp_calls == len(gateway.calls)
        assert all(args["case_id"] == case["case_id"] for _, args in gateway.calls)
        text = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
        assert "verification_completed" in text
        assert "tool_result_consumed" in text
    asyncio.run(run())


def test_refund_pending_preserves_policy_monitor_action(tmp_path):
    async def run():
        case = {"case_id": "L3B_CASE_005", "opened_at": "2018-02-02T09:00:00-03:00",
                "customer_unique_id_hint": "customer", "candidate_order_ids": ["one"],
                "customer_request": {"claims": [
                    {"claim_id": "claim-a", "topic": "refund_pending"},
                ]}, "policy_version": "EC_POLICY_V2",
                "investigation_scope": {"include_product_context": True}}
        gateway = Gateway()
        gateway.data["get_refund_timeline"] = {"events": [
            {"event_at": "2018-01-27T09:00:00-03:00",
             "event_type": "refund_requested", "amount_brl": "89.00",
             "status": "pending"},
        ]}
        gateway.data["get_policy"]["rules"]["refund_pending"] = {
            "case_status": "needs_investigation", "refund_brl": 0,
            "recommended_action": "monitor_refund", "responsible_parties": [
                {"party_type": "payment_provider", "party_id": None},
            ],
        }
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
        report = await investigate_case(
            case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts),
        )
        assert report.assessment["primary_issue"] == "refund_pending"
        assert report.assessment["case_status"] == "needs_investigation"
        assert report.resolution_actions == ["monitor_refund"]
        assert report.payment_analysis["refundable_total_brl"] == 0.0
    asyncio.run(run())


def test_seller_delay_requires_scoped_seller_evidence(tmp_path):
    async def run():
        case = {"case_id": "L3B_CASE_010", "opened_at": "2018-02-02T09:00:00-03:00",
                "customer_unique_id_hint": "customer", "candidate_order_ids": ["one"],
                "customer_request": {"claims": [
                    {"claim_id": "claim-a", "topic": "late_delivery_seller"},
                ]}, "policy_version": "EC_POLICY_V2",
                "investigation_scope": {"include_product_context": True}}
        gateway = Gateway()
        history_row = gateway.data["get_customer_history"]["orders"][0]
        history_row["order_delivered_customer_date"] = "2018-01-27T09:00:00-03:00"
        shipment = gateway.data["get_shipment_summary"]
        shipment["delivered_customer_at"] = "2018-01-27T09:00:00-03:00"
        shipment["shipping_limits"] = [{"order_item_id": "item", "seller_id": "seller",
                                        "shipping_limit_at": "2018-01-23T09:00:00-03:00"}]
        gateway.data["get_sellers"] = [{"seller_id": "seller"}]
        gateway.domains["get_sellers"] = "seller"
        gateway.data["get_policy"]["rules"]["late_delivery_seller"] = {
            "case_status": "action_required", "refund_brl": 10,
            "recommended_action": "refund_freight", "responsible_parties": [],
        }
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
        report = await investigate_case(
            case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts),
        )
        assert report.assessment["primary_issue"] == "late_delivery_seller"
        assert report.shipment_analysis["late_seller_ids"] == ["seller"]
        assert report.root_cause_analysis["responsible_parties"] == [
            {"party_type": "seller", "party_id": "seller"},
        ]
        assert report.financial_resolution["recommended_refund_brl"] == 10.0
        assert "get_sellers" in [name for name, _ in gateway.calls]
        gateway.data["get_sellers"] = []
        disputed = await investigate_case(
            case, gateway, TraceWriter(tmp_path / "disputed.jsonl", contracts),
        )
        assert disputed.assessment["case_status"] == "needs_investigation"
        assert disputed.financial_resolution["recommended_refund_brl"] == 0.0
        assert any(conflict["selected_source"] is None
                   for conflict in disputed.data_conflicts)
    asyncio.run(run())


def test_policy_seller_is_reconciled_to_order_item_scope(tmp_path):
    async def run():
        case = {"case_id": "L3B_CASE_009", "opened_at": "2018-02-02T09:00:00-03:00",
                "customer_unique_id_hint": "customer", "candidate_order_ids": ["one"],
                "customer_request": {"claims": [
                    {"claim_id": "claim-a", "topic": "unavailable_order_paid"},
                ]}, "policy_version": "EC_POLICY_V2",
                "investigation_scope": {"include_product_context": True}}
        gateway = Gateway()
        gateway.data["get_customer_history"]["orders"][0]["order_status"] = "unavailable"
        gateway.data["get_order"]["order_status"] = "unavailable"
        gateway.data["get_policy"]["rules"]["unavailable_order_paid"] = {
            "case_status": "action_required", "refund_brl": 89,
            "recommended_action": "issue_refund", "responsible_parties": [
                {"party_type": "seller", "party_id": "unrelated-seller"},
            ],
        }
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
        report = await investigate_case(
            case, gateway, TraceWriter(tmp_path / "trace.jsonl", contracts),
        )
        assert report.root_cause_analysis["responsible_parties"] == [
            {"party_type": "seller", "party_id": "seller"},
        ]
        assert any(conflict["resolution_code"] == "SELLER_SCOPE_CONFLICT"
                   and conflict["selected_source"] == "get_order_items"
                   for conflict in report.data_conflicts)
        assert report.financial_resolution["recommended_refund_brl"] == 89.0
    asyncio.run(run())

