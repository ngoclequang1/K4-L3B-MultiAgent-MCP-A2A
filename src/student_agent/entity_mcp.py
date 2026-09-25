"""Adapter for the discovered L3B order/customer MCP tools."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from .entity import CandidateEvidence
from .mcp_gateway import EvidenceGateway


class MCPEntitySource:
    def __init__(self, gateway: EvidenceGateway, case_id: str, tools: list[dict[str, Any]]):
        self.gateway = gateway
        self.case_id = case_id
        self.tools = {tool["name"]: tool for tool in tools}
        self.cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.ledger: set[tuple[str, str, str]] = set()
        self.records: dict[str, dict[str, Any]] = {}
        self.calls = 0

    @classmethod
    async def create(cls, gateway: EvidenceGateway, case_id: str) -> MCPEntitySource:
        tools = await gateway.describe_tools()
        source = cls(gateway, case_id, tools)
        for name in ("get_order", "get_customer_history"):
            if name not in source.tools:
                raise ValueError(f"Required entity capability unavailable: {name}")
        return source

    def owns(self, case_id: str, tool_name: str, evidence_ref: str) -> bool:
        return (case_id, tool_name, evidence_ref) in self.ledger

    async def _fetch(self, tool: str, argument: str, value: str) -> dict[str, Any]:
        return await self.fetch(tool, **{argument: value})

    async def fetch(self, tool: str, **arguments: str) -> dict[str, Any]:
        """One audited, schema-checked call, cached inside this case only."""
        key = (tool, *sorted(arguments.items()))
        if key not in self.cache:
            if tool not in self.tools:
                raise ValueError(f"MCP tool unavailable: {tool}")
            payload = {"case_id": self.case_id, **arguments}
            Draft202012Validator(self.tools[tool]["inputSchema"]).validate(payload)
            if self.calls >= 21:
                raise ValueError("Case tool budget exhausted")
            self.calls += 1
            evidence = await self.gateway.call(tool, **payload)
            expected = {
                "get_order": "order", "get_customer_history": "customer",
                "get_order_items": "item", "get_product_context": "product",
                "get_sellers": "seller", "get_shipment_summary": "shipment",
                "get_order_payments": "payment", "get_payment_timeline": "payment",
                "get_refund_timeline": "refund", "get_policy": "policy",
            }.get(tool)
            if evidence["domain"] != expected:
                raise ValueError("Unexpected evidence domain")
            if not isinstance(evidence["data"], (dict, list)):
                raise ValueError("Unexpected evidence payload")
            ref = evidence["evidence_ref"]
            record = {
                "case_id": self.case_id,
                "tool_name": tool,
                "arguments": dict(arguments),
                "evidence_ref": ref,
                "result_hash": evidence.get("result_hash"),
                "domain": evidence["domain"],
            }
            if ref in self.records and self.records[ref] != record:
                raise ValueError("MCP evidence_ref reused for a different result")
            self.cache[key] = evidence
            self.ledger.add((self.case_id, tool, ref))
            self.records[ref] = record
        return self.cache[key]

    async def investigate(self, case: dict[str, Any], order_id: str) -> CandidateEvidence:
        if case["case_id"] != self.case_id:
            raise ValueError("Entity source cannot be shared between cases")
        evidence: list[tuple[str, str]] = []

        def decision(verdict, reason, customer=None, related=()):
            return CandidateEvidence(
                self.case_id, order_id, verdict, reason, tuple(evidence), customer, related,
            )

        hint = case.get("customer_unique_id_hint")
        if not isinstance(hint, str) or not hint:
            return decision("unresolved", "MISSING_CUSTOMER_ANCHOR")
        history = await self._fetch("get_customer_history", "customer_unique_id", hint)
        evidence.append(("get_customer_history", history["evidence_ref"]))
        data = history["data"]
        rows = data.get("orders")
        if history.get("warnings") or data.get("customer_unique_id") != hint:
            return decision("unresolved", "CUSTOMER_IDENTITY_UNVERIFIED")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("order_id"), str)
            or not isinstance(row.get("customer_id"), str) for row in rows
        ):
            return decision("unresolved", "INVALID_CUSTOMER_HISTORY")
        matches = [row for row in rows if row["order_id"] == order_id]
        # Discovered tool describes this as authoritative scoped customer history.
        # A pagination/incompleteness marker prevents negative inference.
        if any(data.get(key) for key in ("next_cursor", "nextCursor", "has_more")):
            return decision("unresolved", "INCOMPLETE_CUSTOMER_HISTORY")
        if data.get("complete") is False or data.get("truncated"):
            return decision("unresolved", "INCOMPLETE_CUSTOMER_HISTORY")
        if not matches:
            return decision("rejected", "NOT_IN_CUSTOMER_HISTORY")
        order = await self._fetch("get_order", "order_id", order_id)
        evidence.append(("get_order", order["evidence_ref"]))
        row = order["data"]
        if order.get("warnings") or row.get("order_id") != order_id:
            return decision("unresolved", "ORDER_IDENTITY_UNVERIFIED")
        owners = {match["customer_id"] for match in matches}
        if len(owners) != 1 or row.get("customer_id") not in owners:
            return decision("unresolved", "CUSTOMER_ORDER_CONFLICT")
        related = tuple(dict.fromkeys(row["order_id"] for row in rows))
        return decision("confirmed", "ORDER_AND_CUSTOMER_MATCH", hint, related)
