"""Case-scoped business investigation from audited MCP evidence.

This produces a diagnostic report. It does not fabricate a submission when
evidence is absent and does not call an LLM.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .entity import EntityResult, resolve_entity
from .entity_mcp import MCPEntitySource
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@dataclass(frozen=True)
class BusinessReport:
    case_id: str
    entity_resolution: dict[str, Any]
    customer_context: dict[str, Any]
    order_product: dict[str, Any]
    shipment_analysis: dict[str, Any]
    payment_analysis: dict[str, Any]
    policy_analysis: dict[str, Any]
    assessment: dict[str, Any]
    root_cause_analysis: dict[str, Any]
    claim_assessments: list[dict[str, Any]]
    data_conflicts: list[dict[str, Any]]
    financial_resolution: dict[str, Any]
    resolution_actions: list[str]
    evidence_refs: list[str]
    evidence_records: list[dict[str, Any]]
    investigation_notes: list[str]
    mcp_calls: int


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        return []
    return value


def _ids(rows: list[dict[str, Any]], key: str) -> list[str]:
    return list(dict.fromkeys(
        row[key] for row in rows if isinstance(row.get(key), str) and row[key]
    ))[:20]


def _conflict(field: str, left: str, right: str, selected: str | None, code: str):
    return {
        "field": field, "sources": [left, right], "selected_source": selected,
        "resolution_code": code,
    }


def _window_rows(
    rows: list[dict[str, Any]], key: str, start: datetime, end: datetime | None,
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        at = _time(row.get(key))
        if at and at >= start and (end is None or at < end):
            result.append(row)
    return result


def _select_order_snapshot(
    history_rows: list[dict[str, Any]], order_id: str, opened_at: Any,
) -> tuple[dict[str, Any] | None, datetime | None, datetime | None]:
    opened = _time(opened_at)
    snapshots = [(at, row) for row in history_rows
                 if row.get("order_id") == order_id
                 if (at := _time(row.get("order_purchase_timestamp"))) is not None]
    if not opened or not snapshots:
        return None, None, None
    eligible = [(at, row) for at, row in snapshots if at <= opened]
    if not eligible:
        return None, None, None
    start, selected = max(eligible, key=lambda pair: pair[0])
    future = [at for at, _ in snapshots if at > start]
    end = min(future) if future else None
    return selected, start, end


def _scope_payment_rows(
    payments: list[dict[str, Any]], events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts = Counter(
        str(_money(event.get("amount_brl"))) for event in events
        if event.get("event_type") == "captured" and event.get("status") == "confirmed"
        and _money(event.get("amount_brl")) is not None
    )
    selected = []
    for row in payments:
        amount = _money(row.get("payment_value"))
        key = str(amount) if amount is not None else ""
        if counts[key]:
            selected.append(row)
            counts[key] -= 1
    return selected


def _shipment(
    order: dict[str, Any], data: dict[str, Any] | None, items: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    empty = {"verdict": "insufficient_evidence", "late_seller_ids": [],
             "timeline_complete": False, "shipment_ids": []}
    if data is None:
        return empty
    if data.get("order_id") != order.get("order_id"):
        conflicts.append(_conflict("order_id", "get_order", "get_shipment_summary", None,
                                   "ENTITY_MISMATCH"))
        return empty
    for order_key, shipment_key in (
        ("order_delivered_carrier_date", "delivered_carrier_at"),
        ("order_delivered_customer_date", "delivered_customer_at"),
        ("order_estimated_delivery_date", "estimated_delivery_at"),
    ):
        left, right = order.get(order_key), data.get(shipment_key)
        if left and right and left != right:
            conflicts.append(_conflict(shipment_key, "get_order", "get_shipment_summary",
                                       None, "TIMELINE_CONFLICT"))
    carrier = _time(data.get("delivered_carrier_at"))
    delivered = _time(data.get("delivered_customer_at"))
    estimated = _time(data.get("estimated_delivery_at"))
    complete = all((carrier, delivered, estimated))
    limits = _rows(data.get("shipping_limits"))
    late_sellers: set[str] = set()
    for limit in limits:
        deadline = _time(limit.get("shipping_limit_at"))
        seller = limit.get("seller_id")
        if carrier and deadline and carrier > deadline and isinstance(seller, str):
            late_sellers.add(seller)
    # Conflicting limits for the same item/seller cannot establish responsibility.
    deadline_map: dict[tuple[str, str], set[str]] = {}
    for limit in limits:
        pair = (str(limit.get("order_item_id")), str(limit.get("seller_id")))
        deadline_map.setdefault(pair, set()).add(str(limit.get("shipping_limit_at")))
    if any(len(values) > 1 for values in deadline_map.values()):
        conflicts.append(_conflict("shipping_limit_at", "get_order_items",
                                   "get_shipment_summary", None, "INCONSISTENT_LIMITS"))
        late_sellers.clear()
    for item in items:
        item_limits = [row for row in limits
                       if row.get("order_item_id") == item.get("order_item_id")]
        if item_limits and all(
            row.get("shipping_limit_at") != item.get("shipping_limit_date")
            for row in item_limits
        ):
            conflicts.append(_conflict("shipping_limit_at", "get_order_items",
                                       "get_shipment_summary", None, "SOURCE_DISAGREEMENT"))
            break
    status = str(data.get("order_status", ""))
    if status in {"returned", "returning"}:
        verdict = "returned"
    elif status in {"lost", "missing"}:
        verdict = "lost"
    elif not complete:
        verdict = "insufficient_evidence"
    elif delivered <= estimated:
        verdict = "on_time"
    elif late_sellers:
        verdict = "seller_delay"
    elif carrier and delivered and estimated:
        verdict = "logistics_delay"
    else:
        verdict = "insufficient_evidence"
    if any(c["field"] in {"delivered_customer_at", "estimated_delivery_at"}
           and c["selected_source"] is None for c in conflicts):
        verdict = "conflicting"
    return {"verdict": verdict, "late_seller_ids": sorted(late_sellers)[:20],
            "timeline_complete": bool(complete),
            "shipment_ids": _ids(_rows(data.get("events")), "shipment_id")}


def _payment(
    timeline: dict[str, Any] | None, base_rows: list[dict[str, Any]] | None,
    refunds: dict[str, Any] | None, conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    empty = {"verdict": "insufficient_evidence", "captured_total_brl": None,
             "refunded_total_brl": None, "refundable_total_brl": None,
             "payment_references": [], "_events": [], "_refund_events": [],
             "_split_payment_valid": False}
    if timeline is None:
        return empty
    payments = _rows(timeline.get("payments"))
    events = _rows(timeline.get("events"))
    refund_events = _rows(refunds.get("events")) if refunds else []
    captures = [event for event in events if event.get("event_type") == "captured"
                and event.get("status") == "confirmed"]
    amounts = [_money(event.get("amount_brl")) for event in captures]
    captured = sum(amounts, Decimal(0)) if captures and all(
        amount is not None for amount in amounts
    ) else None
    completed = [event for event in refund_events
                 if event.get("status") in {"completed", "succeeded", "confirmed"}
                 and event.get("event_type") in {"refund_completed", "refunded"}]
    refund_amounts = [_money(event.get("amount_brl")) for event in completed]
    refunded = sum(refund_amounts, Decimal(0)) if refunds is not None and all(
        amount is not None for amount in refund_amounts
    ) else None
    pending = any(event.get("status") == "pending" for event in refund_events)
    failed = any(event.get("status") == "failed" for event in refund_events)
    base = _rows(base_rows) if base_rows is not None else []
    if base_rows is not None and sorted(map(str, base)) != sorted(map(str, payments)):
        conflicts.append(_conflict("payments", "get_order_payments",
                                   "get_payment_timeline", None, "PAYMENT_RECORD_CONFLICT"))
    values = [_money(row.get("payment_value")) for row in payments]
    payment_total = sum(values, Decimal(0)) if payments and all(
        value is not None for value in values
    ) else None
    if captured is not None and payment_total is not None and captured != payment_total:
        conflicts.append(_conflict("captured_total_brl", "get_order_payments",
                                   "get_payment_timeline", "get_payment_timeline",
                                   "CAPTURE_VS_BASE_MISMATCH"))
    if failed:
        verdict = "refund_failed"
    elif pending:
        verdict = "refund_pending"
    elif completed and refunded is not None:
        verdict = "refunded"
    elif captured is None:
        verdict = "insufficient_evidence"
    elif captured != payment_total and payment_total is not None:
        verdict = "capture_mismatch"
    elif any(event.get("event_type") in {"duplicate_capture", "duplicate_charge"}
             for event in events):
        verdict = "duplicate_capture"
    elif any(event.get("event_type") == "reconciliation_mismatch" for event in events):
        verdict = "capture_mismatch"
    else:
        verdict = "reconciled"
    refs = _ids(payments, "payment_reference")
    sequential = {row.get("payment_sequential") for row in payments}
    split_valid = (len(sequential) >= 2 and captured is not None
                   and payment_total == captured and verdict == "reconciled")
    return {"verdict": verdict, "captured_total_brl": _float(captured),
            "refunded_total_brl": _float(refunded), "refundable_total_brl": None,
            "payment_references": refs, "_events": events,
            "_refund_events": refund_events, "_split_payment_valid": split_valid}


def _claim_verdict(topic: str, issue: str, shipment: str, payment: str,
                   captured: float | None, refund: float | None) -> str:
    if issue == "insufficient_evidence":
        return "insufficient_evidence"
    if topic == "requested_full_refund":
        if captured is None or refund is None:
            return "insufficient_evidence"
        return "supported" if refund >= captured and captured > 0 else "unsupported"
    if topic == issue:
        return "supported"
    if topic in {"late_delivery_seller", "late_delivery_logistics"}:
        return "unsupported" if shipment == "on_time" else "insufficient_evidence"
    if topic in {"refund_pending", "refund_failed", "payment_mismatch", "duplicate_charge"}:
        return "unsupported" if payment == "reconciled" else "insufficient_evidence"
    if topic == "valid_split_payment":
        return "insufficient_evidence"
    return "insufficient_evidence"


async def investigate_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter,
) -> BusinessReport:
    """Collect scoped facts and derive cautious policy-backed business findings."""
    case_id = case["case_id"]
    source = await MCPEntitySource.create(gateway, case_id)
    entity: EntityResult = await resolve_entity(case, source, trace)
    refs = set(entity.evidence_refs)
    notes: list[str] = []
    conflicts: list[dict[str, Any]] = []
    order_product: dict[str, Any] = {"order_id": None, "order_status": None,
                                     "item_ids": [], "seller_ids": [], "product_ids": []}
    shipment = {"verdict": "insufficient_evidence", "late_seller_ids": [],
                "timeline_complete": False, "shipment_ids": []}
    payment = {"verdict": "insufficient_evidence", "captured_total_brl": None,
               "refunded_total_brl": None, "refundable_total_brl": None,
               "payment_references": [], "_events": [], "_refund_events": [],
               "_split_payment_valid": False}
    policy_data: dict[str, Any] | None = None
    domain_refs: dict[str, str] = {}

    async def use(tool: str, actor: str, **arguments: str) -> Any:
        evidence = await source.fetch(tool, **arguments)
        ref = evidence["evidence_ref"]
        refs.add(ref)
        domain_refs[tool] = ref
        trace.emit(case_id=case_id, event_type="tool_result_consumed", actor=actor,
                   tool_name=tool, evidence_refs=[ref])
        return evidence["data"]

    resolved = entity.entity_resolution["resolved_order_ids"]
    if resolved:
        order_id = resolved[0]
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator",
                   target="order-agent")
        order = await use("get_order", "order-agent", order_id=order_id)
        history = await source.fetch(
            "get_customer_history",
            customer_unique_id=entity.customer_context["customer_unique_id"],
        )
        domain_refs["get_customer_history"] = history["evidence_ref"]
        snapshot, start, end = _select_order_snapshot(
            _rows(history["data"].get("orders")), order_id, case.get("opened_at"),
        )
        if snapshot is None or start is None:
            raise ValueError("Cannot establish a purchase snapshot before case opened_at")
        if order.get("order_purchase_timestamp") != snapshot.get("order_purchase_timestamp"):
            conflicts.append(_conflict("order_purchase_timestamp", "get_customer_history",
                                       "get_order", "get_customer_history",
                                       "SNAPSHOT_SCOPE_SELECTED"))
        all_items = _rows(await use("get_order_items", "order-agent", order_id=order_id))
        items = _window_rows(all_items, "shipping_limit_date", start, end)
        if not items:
            notes.append("No order items could be assigned to purchase window")
        product_rows: list[dict[str, Any]] = []
        if case.get("investigation_scope", {}).get("include_product_context"):
            product_rows = _rows(await use("get_product_context", "order-agent",
                                           order_id=order_id))
        if order.get("order_id") != order_id:
            raise ValueError("Order evidence does not match resolved entity")
        if any(row.get("order_id") != order_id for row in all_items):
            raise ValueError("Item evidence contains another order")
        order_product = {"order_id": order_id, "order_status": snapshot.get("order_status"),
                         "purchase_timestamp": snapshot.get("order_purchase_timestamp"),
                         "item_ids": _ids(items, "order_item_id"),
                         "seller_ids": _ids(items, "seller_id"),
                         "product_ids": _ids([
                             row for row in product_rows if row.get("order_item_id")
                             in _ids(items, "order_item_id")
                         ], "product_id")}
        if product_rows:
            item_map = {(row.get("order_item_id"), row.get("seller_id")) for row in items}
            if any((row.get("order_item_id"), row.get("seller_id")) not in item_map
                   for row in product_rows if row.get("product_id")
                   in order_product["product_ids"]):
                conflicts.append(_conflict("product_item_seller", "get_order_items",
                                           "get_product_context", None, "PRODUCT_SCOPE_CONFLICT"))
        trace.emit(case_id=case_id, event_type="handoff", actor="order-agent",
                   target="coordinator", decision_code="ORDER_FACTS_READY")

        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator",
                   target="shipment-agent")
        shipment_data = await use("get_shipment_summary", "shipment-agent",
                                  order_id=order_id)
        scoped_shipment = dict(shipment_data)
        for order_key, shipment_key in (
            ("order_delivered_carrier_date", "delivered_carrier_at"),
            ("order_delivered_customer_date", "delivered_customer_at"),
            ("order_estimated_delivery_date", "estimated_delivery_at"),
        ):
            if snapshot.get(order_key) != shipment_data.get(shipment_key):
                conflicts.append(_conflict(shipment_key, "get_customer_history",
                                           "get_shipment_summary", "get_customer_history",
                                           "SNAPSHOT_SCOPE_SELECTED"))
            scoped_shipment[shipment_key] = snapshot.get(order_key)
        scoped_shipment["shipping_limits"] = _window_rows(
            _rows(shipment_data.get("shipping_limits")), "shipping_limit_at", start, end,
        )
        scoped_shipment["events"] = _window_rows(
            _rows(shipment_data.get("events")), "event_at", start, end,
        )
        shipment = _shipment(snapshot, scoped_shipment, items, conflicts)
        if shipment["verdict"] == "seller_delay":
            seller_rows = _rows(await use("get_sellers", "shipment-agent", order_id=order_id))
            known_sellers = set(_ids(seller_rows, "seller_id"))
            if not set(shipment["late_seller_ids"]).issubset(known_sellers):
                conflicts.append(_conflict("late_seller_ids", "get_shipment_summary",
                                           "get_sellers", None, "SELLER_IDENTITY_UNVERIFIED"))
                shipment["verdict"] = "conflicting"
        trace.emit(case_id=case_id, event_type="handoff", actor="shipment-agent",
                   target="coordinator", decision_code=shipment["verdict"].upper())

        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator",
                   target="payment-agent")
        base = _rows(await use("get_order_payments", "payment-agent", order_id=order_id))
        timeline = await use("get_payment_timeline", "payment-agent", order_id=order_id)
        scoped_timeline = dict(timeline)
        scoped_events = _window_rows(_rows(timeline.get("events")), "event_at", start, end)
        scoped_timeline["events"] = scoped_events
        scoped_timeline["payments"] = _scope_payment_rows(
            _rows(timeline.get("payments")), scoped_events,
        )
        scoped_base = _scope_payment_rows(base, scoped_events)
        refund_data = None
        try:
            refund_data = await use("get_refund_timeline", "payment-agent", order_id=order_id)
            refund_data = {**refund_data, "events": _window_rows(
                _rows(refund_data.get("events")), "event_at", start, end,
            )}
        except RuntimeError:
            notes.append("refund_timeline_unavailable; refunded_total_brl remains null")
        payment = _payment(scoped_timeline, scoped_base, refund_data, conflicts)
        trace.emit(case_id=case_id, event_type="handoff", actor="payment-agent",
                   target="coordinator", decision_code=payment["verdict"].upper())

    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator",
               target="policy-agent")
    version = case.get("policy_version")
    if isinstance(version, str):
        policy_data = await use("get_policy", "policy-agent", policy_version=version)
        if policy_data.get("policy_version") != version:
            raise ValueError("Policy response version does not match input")
    else:
        notes.append("policy_version_missing")
    issue = "insufficient_evidence"
    status = "needs_investigation"
    shipment_verdict = shipment["verdict"]
    payment_verdict = payment["verdict"]
    order_status = order_product["order_status"]
    captured = payment["captured_total_brl"]
    if resolved:
        if order_status in {"canceled", "cancelled"} and captured is not None and captured > 0:
            issue = "canceled_order_paid"
        elif order_status in {"unavailable", "unavailable_order"} and captured and captured > 0:
            issue = "unavailable_order_paid"
        elif payment_verdict == "refund_failed":
            issue = "refund_failed"
        elif payment_verdict == "refund_pending":
            issue = "refund_pending"
        elif payment_verdict == "duplicate_capture":
            issue = "duplicate_charge"
        elif payment_verdict == "capture_mismatch":
            issue = "payment_mismatch"
        elif shipment_verdict == "seller_delay":
            issue = "late_delivery_seller"
        elif shipment_verdict == "logistics_delay":
            issue = "late_delivery_logistics"
        elif payment["_split_payment_valid"]:
            issue = "valid_split_payment"
        elif payment_verdict == "reconciled" and shipment_verdict == "on_time":
            issue = "unsupported_claim"
    material_conflicts = {
        "MULTIPLE_ORDER_SNAPSHOTS", "TIMELINE_CONFLICT", "ENTITY_MISMATCH",
        "PAYMENT_RECORD_CONFLICT", "PRODUCT_SCOPE_CONFLICT",
    }
    if any(conflict["resolution_code"] in material_conflicts for conflict in conflicts):
        issue = "insufficient_evidence"
        notes.append("Material source conflict prevents a reliable primary issue")
    rule = None
    if policy_data:
        rules = policy_data.get("rules", {})
        if isinstance(rules, dict):
            rule = rules.get(issue)
    policy_parties = rule.get("responsible_parties", []) if isinstance(rule, dict) else []
    if not isinstance(policy_parties, list):
        policy_parties = []
    parties = [dict(party) for party in policy_parties if isinstance(party, dict)]
    scoped_sellers = order_product["seller_ids"]
    for party in parties:
        if party.get("party_type") != "seller" or party.get("party_id") in scoped_sellers:
            continue
        selected = "get_order_items" if len(scoped_sellers) == 1 else None
        conflicts.append(_conflict("responsible_parties.seller_id", "get_policy",
                                   "get_order_items", selected, "SELLER_SCOPE_CONFLICT"))
        if selected:
            party["party_id"] = scoped_sellers[0]
        else:
            party["party_type"] = "unknown"
            party["party_id"] = None
    refund_amount = _money(rule.get("refund_brl")) if isinstance(rule, dict) else None
    action = rule.get("recommended_action") if isinstance(rule, dict) else None
    if isinstance(rule, dict) and rule.get("case_status") in {
        "action_required", "no_action", "needs_investigation"
    }:
        status = rule["case_status"]
    if issue == "insufficient_evidence":
        status = "needs_investigation"
    if (
        refund_amount is not None and captured is not None
        and refund_amount > Decimal(str(captured))
    ):
        conflicts.append(_conflict("recommended_refund_brl", "get_policy",
                                   "get_payment_timeline", None, "REFUND_EXCEEDS_CAPTURE"))
        refund_amount = None
        status = "needs_investigation"
    if refund_amount is not None and refund_amount > 0 and captured is None:
        notes.append("Cannot recommend payment without a verified captured amount")
        refund_amount = None
        status = "needs_investigation"
    if refund_amount is not None and payment["refunded_total_brl"] is not None:
        already = Decimal(str(payment["refunded_total_brl"]))
        refund_amount = max(Decimal(0), refund_amount - already)
    refund_unverified = (
        refund_amount is not None and refund_amount > 0
        and payment["refunded_total_brl"] is None
    )
    if refund_unverified:
        notes.append("Cannot recommend payment without a reliable refunded total")
        refund_amount = None
        status = "needs_investigation"
    if refund_amount is not None and not any(
        conflict["selected_source"] is None for conflict in conflicts
    ):
        payment["refundable_total_brl"] = _float(refund_amount)
    open_conflicts = [conflict for conflict in conflicts
                      if conflict["selected_source"] is None]
    if open_conflicts:
        notes.append("Unresolved source conflicts require independent verification")
        status = "needs_investigation"
        refund_amount = None
        payment["refundable_total_brl"] = None
    confidence = 0.0 if issue == "insufficient_evidence" else (
        0.55 if open_conflicts or refund_unverified else 0.85
    )
    # These are provisional decision scores, not calibrated probabilities.
    if entity.entity_resolution["status"] != "resolved":
        confidence = 0.0
    if issue == "late_delivery_seller" and shipment["late_seller_ids"]:
        parties = [{"party_type": "seller", "party_id": seller}
                   for seller in shipment["late_seller_ids"]]
    parties = [party for party in parties if isinstance(party, dict)
               and party.get("party_type") in {
                   "seller", "platform", "logistics_provider", "payment_provider",
                   "customer", "unknown",
               }][:5]
    financial = {"currency": "BRL", "recommended_refund_brl": _float(refund_amount)
                 if refund_amount is not None else 0.0, "refund_lines": []}
    if refund_amount is not None and refund_amount > 0:
        financial["refund_lines"] = [{"reason_code": issue.upper(),
                                      "amount_brl": _float(refund_amount),
                                      "entity_id": resolved[0] if resolved else None}]
    actions = [action] if (
        isinstance(action, str) and action and rule
        and (status != "needs_investigation"
             or rule.get("case_status") == "needs_investigation")
    ) else []
    if open_conflicts or issue == "insufficient_evidence":
        actions = []
    if not actions and status == "needs_investigation":
        actions = ["investigate_missing_or_conflicting_evidence"]
    trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent",
               decision_code=issue.upper())
    trace.emit(case_id=case_id, event_type="handoff", actor="policy-agent",
               target="coordinator", decision_code="POLICY_REVIEWED")
    claims = []
    for claim in case.get("customer_request", {}).get("claims", [])[:5]:
        topic = claim.get("topic")
        if not isinstance(topic, str) or not isinstance(claim.get("claim_id"), str):
            continue
        relevant = []
        if topic.startswith("late_delivery") and "get_shipment_summary" in domain_refs:
            relevant.append(domain_refs["get_shipment_summary"])
            if "get_customer_history" in domain_refs:
                relevant.append(domain_refs["get_customer_history"])
        elif topic.startswith(("payment", "duplicate", "valid_split")):
            relevant.extend(domain_refs[name] for name in (
                "get_order_payments", "get_payment_timeline") if name in domain_refs)
        elif topic.startswith("refund") or topic == "requested_full_refund":
            relevant.extend(domain_refs[name] for name in (
                "get_refund_timeline", "get_policy", "get_payment_timeline",
                "get_customer_history") if name in domain_refs)
        else:
            relevant.extend(domain_refs[name] for name in (
                "get_order", "get_policy") if name in domain_refs)
        verdict = _claim_verdict(topic, issue, shipment_verdict, payment_verdict,
                                 captured, _float(refund_amount))
        if topic == "valid_split_payment" and payment["_split_payment_valid"]:
            verdict = "supported"
        claims.append({"claim_id": claim["claim_id"], "verdict": verdict,
                       "confidence": confidence if verdict != "insufficient_evidence" else 0.0,
                       "evidence_refs": list(dict.fromkeys(relevant))})
    # Diagnostics contain only schema-compatible public fields plus investigation notes.
    payment_public = {key: value for key, value in payment.items() if not key.startswith("_")}
    policy_analysis = {"policy_version": version, "matched_rule": issue if rule else None,
                       "recommended_action": action, "refund_brl": _float(refund_amount)}
    root_cause = {"ranked_causes": ([{"cause_code": issue.upper(), "rank": 1}]
                                     if issue != "insufficient_evidence" else []),
                  "responsible_parties": parties}
    assessment = {"primary_issue": issue, "case_status": status,
                  "confidence": confidence}
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator",
               target="verifier")
    passed = not open_conflicts and not refund_unverified and issue != "insufficient_evidence"
    passed = passed and entity.entity_resolution["status"] == "resolved"
    passed = passed and set(refs).issubset({entry[2] for entry in source.ledger})
    passed = passed and (captured is None or captured >= 0)
    passed = passed and (refund_amount is None or refund_amount >= 0)
    passed = passed and sum(
        Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]
    ) == Decimal(str(financial["recommended_refund_brl"]))
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier",
               target="coordinator", attributes={"passed": passed,
                                                  "conflict_count": len(conflicts)})
    if len(refs) > 30:
        raise ValueError("Evidence set exceeds output schema; explicit selection required")
    return BusinessReport(
        case_id, entity.entity_resolution, entity.customer_context, order_product,
        shipment, payment_public, policy_analysis, assessment, root_cause, claims,
        sorted(conflicts, key=lambda conflict: conflict["selected_source"] is not None)[:5],
        financial, actions, sorted(refs),
        [source.records[ref] for ref in sorted(refs)], notes, source.calls,
    )
