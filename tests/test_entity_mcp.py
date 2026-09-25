import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.entity import resolve_entity
from student_agent.entity_mcp import MCPEntitySource
from student_agent.trace import TraceWriter


class Gateway:
    def __init__(self):
        self.calls = []
        self.history = {
            "customer_unique_id": "customer", "orders": [
                {"order_id": "right", "customer_id": "row-customer"},
                {"order_id": "right", "customer_id": "row-customer"},
            ],
        }
        self.order = {"order_id": "right", "customer_id": "row-customer"}

    async def describe_tools(self):
        return [{"name": name, "inputSchema": {"type": "object"}} for name in (
            "get_order", "get_customer_history",
        )]

    async def call(self, tool, **kwargs):
        self.calls.append((tool, kwargs))
        history = tool == "get_customer_history"
        return {
            "domain": "customer" if history else "order",
            "evidence_ref": "ev_" + ("h" if history else "o") * 24,
            "data": deepcopy(self.history if history else self.order),
        }


CASE = {
    "case_id": "L3B_CASE_001", "customer_unique_id_hint": "customer",
    "candidate_order_ids": ["wrong", "right"],
}


def test_real_payload_shape_resolves_and_caches_history(tmp_path):
    async def run():
        gateway = Gateway()
        source = await MCPEntitySource.create(gateway, CASE["case_id"])
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts/schemas")
        result = await resolve_entity(CASE, source, TraceWriter(tmp_path / "trace", contracts))
        assert result.entity_resolution["resolved_order_ids"] == ["right"]
        assert result.entity_resolution["rejected_candidates"] == ["wrong"]
        assert result.customer_context["related_order_ids"] == ["right"]
        assert [name for name, _ in gateway.calls] == ["get_customer_history", "get_order"]
        assert all(args["case_id"] == CASE["case_id"] for _, args in gateway.calls)
        with pytest.raises(ValueError, match="between cases"):
            await source.investigate({"case_id": "OTHER_CASE"}, "right")
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["incomplete", "owner_conflict", "wrong_customer"])
def test_partial_or_conflicting_evidence_stays_unresolved(mode):
    async def run():
        gateway = Gateway()
        if mode == "incomplete":
            gateway.history["has_more"] = True
        elif mode == "owner_conflict":
            gateway.order["customer_id"] = "someone-else"
        else:
            gateway.history["customer_unique_id"] = "someone-else"
        source = await MCPEntitySource.create(gateway, CASE["case_id"])
        result = await source.investigate(CASE, "right")
        assert result.verdict == "unresolved"
    asyncio.run(run())


def test_missing_hint_does_not_guess_customer():
    async def run():
        gateway = Gateway()
        source = await MCPEntitySource.create(gateway, CASE["case_id"])
        result = await source.investigate({"case_id": CASE["case_id"]}, "right")
        assert result.verdict == "unresolved"
        assert gateway.calls == []
    asyncio.run(run())
