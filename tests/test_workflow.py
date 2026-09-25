from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.verification import VerificationError, verify_output
from student_agent.workflow import solve_case
from test_business import Gateway

CASE = {
    "case_id": "L3B_CASE_002", "opened_at": "2018-02-02T09:00:00-03:00",
    "customer_unique_id_hint": "customer", "candidate_order_ids": ["one"],
    "customer_request": {"claims": [
        {"claim_id": "claim-a", "topic": "valid_split_payment"},
        {"claim_id": "claim-b", "topic": "requested_full_refund"},
    ]},
    "policy_version": "EC_POLICY_V2",
    "investigation_scope": {"include_product_context": True},
}


def test_workflow_builds_valid_output_with_trace(tmp_path):
    async def run():
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        trace.emit(case_id=CASE["case_id"], event_type="case_received", actor="coordinator")
        output = await solve_case(CASE, Gateway(), trace)
        trace.emit(case_id=CASE["case_id"], event_type="case_finalized", actor="coordinator")
        contracts.validate_output(output, "mock output")
        assert output["assessment"]["primary_issue"] == "valid_split_payment"
        assert output["affected_entities"]["order_ids"] == ["one"]
        assert output["payment_analysis"]["captured_total_brl"] == 89.0
        assert len(output["evidence_refs"]) == 9
        events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
        assert events[0]["event_type"] == "case_received"
        assert events[-1]["event_type"] == "case_finalized"
        assert any(event["event_type"] == "verification_completed"
                   and event["actor"] == "output-verifier"
                   and event["attributes"]["passed"] is True for event in events)
    asyncio.run(run())


def test_verifier_rejects_foreign_claim_ref_and_bad_money(tmp_path):
    async def run():
        from student_agent.business import investigate_case
        from student_agent.verification import build_output

        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        report = await investigate_case(CASE, Gateway(), trace)
        output = build_output(report)
        bad = deepcopy(output)
        bad["claim_assessments"][0]["evidence_refs"] = ["ev_" + "z" * 24]
        with pytest.raises(VerificationError, match="claim refers to evidence"):
            verify_output(CASE, report, bad, trace)
        bad = deepcopy(output)
        bad["financial_resolution"]["recommended_refund_brl"] = 2.0
        with pytest.raises(VerificationError, match="refund lines"):
            verify_output(CASE, report, bad, trace)
        bad = deepcopy(output)
        bad["evidence_refs"].append("ev_" + "z" * 24)
        with pytest.raises(VerificationError, match="evidence absent from case ledger"):
            verify_output(CASE, report, bad, trace)
    asyncio.run(run())


def test_verifier_rejects_missing_consumed_event(tmp_path):
    async def run():
        from student_agent.business import investigate_case
        from student_agent.verification import build_output

        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        report = await investigate_case(CASE, Gateway(), trace)
        trace.events = [event for event in trace.events
                        if event["event_type"] != "tool_result_consumed"]
        with pytest.raises(VerificationError, match="missing consumed trace"):
            verify_output(CASE, report, build_output(report), trace)
    asyncio.run(run())


def test_ambiguous_entity_stays_unresolved_in_output(tmp_path):
    async def run():
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        case = deepcopy(CASE)
        case.pop("customer_unique_id_hint")
        gateway = Gateway()
        output = await solve_case(case, gateway, trace)
        contracts.validate_output(output, "ambiguous output")
        assert output["entity_resolution"]["status"] == "ambiguous"
        assert output["affected_entities"]["order_ids"] == []
        assert output["assessment"]["case_status"] == "needs_investigation"
        assert output["assessment"]["confidence"] == 0.0
        assert [name for name, _ in gateway.calls] == ["get_policy"]
    asyncio.run(run())
