from __future__ import annotations

import json

from student_agent.refinement import refine_claims


def _event(case_id: str, tool: str, ref: str) -> str:
    return json.dumps({"case_id": case_id, "event_type": "tool_result_consumed",
                       "tool_name": tool, "evidence_refs": [ref]})


def test_refinement_reuses_audited_policy_and_payment_evidence():
    cases = {"case": {"customer_request": {"claims": [
        {"claim_id": "a", "topic": "unavailable_order_paid"},
        {"claim_id": "b", "topic": "requested_full_refund"},
    ]}}}
    output = {"assessment": {"primary_issue": "unavailable_order_paid",
                             "confidence": 0.55},
              "payment_analysis": {"captured_total_brl": 89.0},
              "claim_assessments": [
                  {"claim_id": "a", "verdict": "supported", "confidence": 0.55,
                   "evidence_refs": ["order"]},
                  {"claim_id": "b", "verdict": "insufficient_evidence",
                   "confidence": 0.0, "evidence_refs": ["policy", "payment"]},
              ]}
    trace = [_event("case", "get_policy", "policy"),
             _event("case", "get_payment_timeline", "payment")]
    result, changed = refine_claims({"case": output}, cases, trace)
    assert changed == 1
    assert result["case"]["claim_assessments"][1]["verdict"] == "supported"
    assert result["case"]["claim_assessments"][1]["confidence"] == 0.55
    assert output["claim_assessments"][1]["verdict"] == "insufficient_evidence"
    assert result["case"]["payment_analysis"] == output["payment_analysis"]


def test_refinement_does_not_decide_claim_without_policy_ref():
    cases = {"case": {"customer_request": {"claims": [
        {"claim_id": "b", "topic": "requested_full_refund"},
    ]}}}
    output = {"assessment": {"primary_issue": "late_delivery_seller",
                             "confidence": 0.55},
              "payment_analysis": {"captured_total_brl": 18.0},
              "claim_assessments": [
                  {"claim_id": "b", "verdict": "insufficient_evidence",
                   "confidence": 0.0, "evidence_refs": ["payment"]},
              ]}
    trace = [_event("case", "get_policy", "policy"),
             _event("case", "get_payment_timeline", "payment")]
    result, changed = refine_claims({"case": output}, cases, trace)
    assert changed == 0
    assert result["case"] == output
