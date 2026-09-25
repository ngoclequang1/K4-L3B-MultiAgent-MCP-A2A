"""Reassess claim verdicts using facts already present in a validated submission.

This is deliberately limited to claim semantics. It never changes money, entity
resolution, evidence references, or the audited trace.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


def refine_claims(
    outputs: dict[str, dict[str, Any]], cases: dict[str, dict[str, Any]],
    trace_lines: list[str],
) -> tuple[dict[str, dict[str, Any]], int]:
    tool_refs: dict[tuple[str, str], set[str]] = {}
    for line in trace_lines:
        event = json.loads(line)
        if event["event_type"] == "tool_result_consumed":
            tool_refs.setdefault((event["case_id"], event.get("tool_name", "")), set()).update(
                event.get("evidence_refs", [])
            )

    refined = deepcopy(outputs)
    changed = 0
    for case_id, output in refined.items():
        case_claims = cases[case_id]["customer_request"]["claims"]
        issue = output["assessment"]["primary_issue"]
        captured = output["payment_analysis"]["captured_total_brl"]
        for claim, assessment in zip(case_claims, output["claim_assessments"], strict=True):
            if claim["claim_id"] != assessment["claim_id"]:
                raise ValueError(f"claim order mismatch for {case_id}")
            refs = set(assessment["evidence_refs"])
            if not refs & tool_refs.get((case_id, "get_policy"), set()):
                continue
            topic = claim["topic"]
            verdict = assessment["verdict"]
            replacement = None
            if topic == "unsupported_claim" and issue == "unsupported_claim":
                if verdict == "supported":
                    replacement = "unsupported"
            elif topic == "requested_full_refund" and verdict == "insufficient_evidence":
                if not isinstance(captured, (float, int)) or captured <= 0:
                    continue
                if not refs & tool_refs.get((case_id, "get_payment_timeline"), set()):
                    continue
                if issue in {"canceled_order_paid", "unavailable_order_paid"}:
                    replacement = "supported"
                elif issue in {"late_delivery_seller", "late_delivery_logistics"}:
                    replacement = "unsupported"
            if replacement:
                assessment["verdict"] = replacement
                assessment["confidence"] = min(
                    float(output["assessment"]["confidence"]), 0.65
                ) if topic == "requested_full_refund" else assessment["confidence"]
                changed += 1
    return refined, changed
