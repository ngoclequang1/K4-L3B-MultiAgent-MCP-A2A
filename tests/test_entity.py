from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.entity import CandidateEvidence, candidate_ids, resolve_entity
from student_agent.trace import TraceWriter

CASE = {
    "case_id": "L3B_CASE_001",
    "customer_request": {"claimed_order_id": "wrong"},
    "candidate_order_ids": ["wrong", "right"],
}
REF = "ev_" + "a" * 24


class Source:
    def __init__(self, verdicts, *, owns=True, timeout=False):
        self.verdicts = verdicts
        self.trusted = owns
        self.timeout = timeout
        self.calls = []

    async def investigate(self, case, order_id):
        self.calls.append(order_id)
        if self.timeout and order_id == "wrong":
            raise TimeoutError
        return CandidateEvidence(
            case["case_id"], order_id, self.verdicts[order_id], "TEST_EVIDENCE",
            (("fixture_lookup", REF),), "verified-customer", ("related-only",),
        )

    def owns(self, case_id, tool_name, evidence_ref):
        return self.trusted


@pytest.fixture
def trace(tmp_path):
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    return TraceWriter(tmp_path / "trace.jsonl", contracts)


def test_resolves_evidence_not_claimed_id(trace):
    source = Source({"wrong": "rejected", "right": "confirmed"})
    result = asyncio.run(resolve_entity(CASE, source, trace))
    assert result.entity_resolution["resolved_order_ids"] == ["right"]
    assert result.entity_resolution["rejected_candidates"] == ["wrong"]
    assert result.customer_context["related_order_ids"] == ["related-only"]
    assert source.calls == ["wrong", "right"]


@pytest.mark.parametrize("verdicts", [
    {"wrong": "confirmed", "right": "confirmed"},
    {"wrong": "unresolved", "right": "confirmed"},
])
def test_ambiguity_never_selects_first(trace, verdicts):
    result = asyncio.run(resolve_entity(CASE, Source(verdicts), trace))
    assert result.entity_resolution["status"] == "ambiguous"
    assert result.entity_resolution["resolved_order_ids"] == []


def test_timeout_is_not_rejection(trace):
    source = Source({"right": "confirmed"}, timeout=True)
    result = asyncio.run(resolve_entity(CASE, source, trace))
    assert result.entity_resolution["status"] == "ambiguous"
    assert result.entity_resolution["rejected_candidates"] == []


def test_all_authoritatively_rejected(trace):
    result = asyncio.run(resolve_entity(
        CASE, Source({"wrong": "rejected", "right": "rejected"}), trace,
    ))
    assert result.entity_resolution["status"] == "not_found"


def test_foreign_evidence_is_rejected(trace):
    with pytest.raises(ValueError, match="ledger"):
        asyncio.run(resolve_entity(
            CASE, Source({"wrong": "rejected", "right": "confirmed"}, owns=False), trace,
        ))


def test_empty_candidates_do_not_prove_not_found(trace):
    result = asyncio.run(resolve_entity({"case_id": CASE["case_id"]}, Source({}), trace))
    assert result.entity_resolution["status"] == "ambiguous"


def test_candidate_types_and_limit(trace):
    with pytest.raises(ValueError):
        candidate_ids({"candidate_order_ids": [None]})
    with pytest.raises(ValueError, match="Too many"):
        asyncio.run(resolve_entity(CASE, Source({}), trace, max_candidates=1))
