"""Independent output and provenance checks before a case is finalized."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .business import BusinessReport
from .trace import TraceWriter


class VerificationError(ValueError):
    pass


def build_output(report: BusinessReport) -> dict[str, Any]:
    """Map internal investigation fields to exactly the public L3B schema."""
    resolved = report.entity_resolution["resolved_order_ids"]
    order = report.order_product
    shipment = report.shipment_analysis
    payment = report.payment_analysis
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": report.case_id,
        "assessment": {**report.assessment, "secondary_issues": []},
        "affected_entities": {
            "order_ids": list(resolved),
            "item_ids": list(order["item_ids"]),
            "seller_ids": list(order["seller_ids"]),
            "payment_references": list(payment["payment_references"]),
            "shipment_ids": list(shipment["shipment_ids"]),
        },
        "claim_assessments": report.claim_assessments,
        "entity_resolution": report.entity_resolution,
        "customer_context": report.customer_context,
        "shipment_analysis": {
            key: shipment[key] for key in ("verdict", "late_seller_ids", "timeline_complete")
        },
        "payment_analysis": {
            key: payment[key] for key in (
                "verdict", "captured_total_brl", "refunded_total_brl", "refundable_total_brl",
            )
        },
        "root_cause_analysis": report.root_cause_analysis,
        "evidence_refs": report.evidence_refs,
        "data_conflicts": report.data_conflicts,
        "financial_resolution": report.financial_resolution,
        "resolution_actions": report.resolution_actions,
    }


def verify_output(
    case: dict[str, Any], report: BusinessReport, output: dict[str, Any], trace: TraceWriter,
) -> None:
    """Reject incorrect scope, unsupported refs and contradictory final fields."""
    case_id = case["case_id"]
    errors: list[str] = []
    try:
        trace.contracts.validate_output(output, f"outputs/{case_id}.json")
    except ValueError as exc:
        errors.append(f"schema: {exc}")
    if output.get("case_id") != case_id or report.case_id != case_id:
        errors.append("case_id mismatch")
    resolution = output.get("entity_resolution", {})
    resolved = set(resolution.get("resolved_order_ids", []))
    rejected = set(resolution.get("rejected_candidates", []))
    affected = output.get("affected_entities", {})
    if resolved & rejected:
        errors.append("resolved order is also rejected")
    if set(affected.get("order_ids", [])) != resolved:
        errors.append("affected orders differ from resolved orders")
    if report.order_product["order_id"] and report.order_product["order_id"] not in resolved:
        errors.append("investigated order differs from resolved order")
    if len(resolved) > 1:
        errors.append("this workflow supports one resolved order per case")
    if resolution.get("status") != "resolved" and resolved:
        errors.append("unresolved entity has affected order")
    if resolution.get("status") == "resolved" and len(resolved) != 1:
        errors.append("resolved entity requires one order")
    if affected.get("item_ids") != report.order_product["item_ids"]:
        errors.append("item scope changed after investigation")
    if affected.get("seller_ids") != report.order_product["seller_ids"]:
        errors.append("seller scope changed after investigation")

    records = {record["evidence_ref"]: record for record in report.evidence_records}
    refs = set(output.get("evidence_refs", []))
    if refs != set(report.evidence_refs) or not refs.issubset(records):
        errors.append("submitted evidence differs from case ledger")
    consumed: set[tuple[str, str]] = set()
    for event in trace.events:
        if event["case_id"] == case_id and event["event_type"] == "tool_result_consumed":
            consumed.update((event.get("tool_name", ""), ref)
                            for ref in event.get("evidence_refs", []))
    for ref in refs:
        record = records.get(ref)
        if record is None:
            errors.append(f"evidence absent from case ledger: {ref}")
            continue
        if record["case_id"] != case_id or (record["tool_name"], ref) not in consumed:
            errors.append(f"evidence outside this case or missing consumed trace: {ref}")
    claim_ids = [claim.get("claim_id") for claim in
                 case.get("customer_request", {}).get("claims", [])[:5]]
    assessments = output.get("claim_assessments", [])
    if [entry.get("claim_id") for entry in assessments] != claim_ids:
        errors.append("claim assessments do not match input claims")
    for entry in assessments:
        claim_refs = set(entry.get("evidence_refs", []))
        if not claim_refs.issubset(refs):
            errors.append("claim refers to evidence outside output")
        if (entry.get("verdict") in {"supported", "unsupported", "partially_supported"}
                and (not claim_refs or entry.get("confidence", 0) <= 0)):
            errors.append("decided claim lacks evidence or confidence")

    shipment = output.get("shipment_analysis", {})
    cause = output.get("root_cause_analysis", {})
    parties = cause.get("responsible_parties", [])
    issue = output.get("assessment", {}).get("primary_issue")
    if shipment.get("verdict") == "seller_delay" and not shipment.get("late_seller_ids"):
        errors.append("seller delay without late seller")
    if issue == "late_delivery_seller":
        seller_ids = set(shipment.get("late_seller_ids", []))
        if not seller_ids or not any(
            party.get("party_type") == "seller" and party.get("party_id") in seller_ids
            for party in parties
        ):
            errors.append("seller responsibility unsupported")
    if issue == "late_delivery_logistics" and not any(
        party.get("party_type") == "logistics_provider" for party in parties
    ):
        errors.append("logistics responsibility unsupported")
    scoped_sellers = set(affected.get("seller_ids", []))
    if any(party.get("party_type") == "seller" and party.get("party_id") not in scoped_sellers
           for party in parties):
        errors.append("seller responsibility outside affected seller scope")
    for conflict in output.get("data_conflicts", []):
        selected = conflict.get("selected_source")
        if selected is not None and selected not in conflict.get("sources", []):
            errors.append("conflict selected source is not listed")
        if selected is None and output.get("assessment", {}).get("case_status") != (
            "needs_investigation"
        ):
            errors.append("unresolved conflict requires investigation status")
    if any(conflict.get("selected_source") is None
           for conflict in output.get("data_conflicts", [])) and (
               output.get("assessment", {}).get("confidence", 1) > 0.6
           ):
        errors.append("confidence too high for unresolved conflict")

    payment = output.get("payment_analysis", {})
    captured = payment.get("captured_total_brl")
    refunded = payment.get("refunded_total_brl")
    if captured is not None and refunded is not None and refunded > captured + 0.01:
        errors.append("refunded total exceeds captured total")
    financial = output.get("financial_resolution", {})
    lines = financial.get("refund_lines", [])
    try:
        total = sum((Decimal(str(line.get("amount_brl", 0))) for line in lines), Decimal(0))
        recommended = Decimal(str(financial.get("recommended_refund_brl", 0)))
    except (InvalidOperation, TypeError, ValueError):
        errors.append("refund amounts are not valid decimals")
        total = Decimal(0)
        recommended = Decimal(0)
    if total != recommended:
        errors.append("refund lines do not sum to recommended refund")
    if captured is not None and recommended > Decimal(str(captured)):
        errors.append("recommended refund exceeds captured total")
    if recommended > 0 and refunded is None:
        errors.append("positive refund without verified refunded total")
    if output.get("assessment", {}).get("case_status") == "no_action" and recommended > 0:
        errors.append("no_action with positive refund")
    if issue == "insufficient_evidence" and output.get("assessment", {}).get(
        "case_status"
    ) != "needs_investigation":
        errors.append("insufficient evidence requires investigation status")
    if len(set(output.get("resolution_actions", []))) != len(
        output.get("resolution_actions", [])
    ):
        errors.append("duplicate resolution actions")

    passed = not errors
    trace.emit(
        case_id=case_id, event_type="verification_completed", actor="output-verifier",
        target="coordinator", decision_code="OUTPUT_ACCEPTED" if passed else "OUTPUT_REJECTED",
        attributes={"passed": passed, "error_count": len(errors),
                    "first_error": errors[0][:160] if errors else None},
    )
    if errors:
        raise VerificationError("; ".join(errors))
