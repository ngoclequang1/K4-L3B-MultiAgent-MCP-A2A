"""Evidence-based entity resolution, independent of provider-specific MCP payloads.

An adapter must normalize real MCP results into CandidateEvidence. It must never
derive confirmed/rejected solely from the case's claimed ID or customer hint.
No language model is used by this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .trace import TraceWriter


@dataclass(frozen=True)
class CandidateEvidence:
    case_id: str
    order_id: str
    verdict: Literal["confirmed", "rejected", "unresolved"]
    reason_code: str
    # Each pair is (actual discovered tool name, original MCP evidence_ref).
    evidence: tuple[tuple[str, str], ...]
    customer_unique_id: str | None = None
    related_order_ids: tuple[str, ...] = ()


class EntityEvidenceSource(Protocol):
    """Case-bound adapter; verify scope against its MCP request ledger.

    confirmed means evidence identifies the complaint's order AND customer,
    not merely that the order exists. rejected requires explicit contradiction
    or an authoritative not-found response. Timeout is not rejection.
    """

    async def investigate(self, case: dict[str, Any], order_id: str) -> CandidateEvidence: ...

    def owns(self, case_id: str, tool_name: str, evidence_ref: str) -> bool: ...


@dataclass(frozen=True)
class EntityResult:
    entity_resolution: dict[str, Any]
    customer_context: dict[str, Any]
    evidence_refs: tuple[str, ...]
    candidate_decisions: tuple[CandidateEvidence, ...]


def candidate_ids(case: dict[str, Any]) -> tuple[str, ...]:
    """Input order is only an investigation order, never a ranking signal."""
    request = case.get("customer_request", {})
    candidates = case.get("candidate_order_ids", [])
    if not isinstance(request, dict) or not isinstance(candidates, list):
        raise ValueError("Invalid entity-resolution input")
    values = list(candidates)
    claimed = request.get("claimed_order_id")
    if claimed is not None:
        values.insert(0, claimed)
    if any(not isinstance(value, str) or not value or len(value) > 128 for value in values):
        raise ValueError("Candidate IDs must be nonempty strings of at most 128 characters")
    return tuple(dict.fromkeys(values))


async def resolve_entity(
    case: dict[str, Any],
    source: EntityEvidenceSource,
    trace: TraceWriter,
    *,
    timeout_seconds: float = 30,
    max_candidates: int = 20,
) -> EntityResult:
    """Resolve a single-order complaint conservatively; do not emit final output.

    Confidence is a provisional rule score, not an empirically calibrated
    probability. Multi-order complaints need a separate explicit scope policy.
    The source is responsible for MCP query budgets; candidate count is not a
    substitute for a call budget. No automatic retries of audited calls occur.
    """
    if timeout_seconds <= 0 or not 1 <= max_candidates <= 20:
        raise ValueError("Invalid resolver limits")
    case_id = case["case_id"]
    candidates = candidate_ids(case)
    if len(candidates) > max_candidates:
        raise ValueError("Too many candidates; refusing to silently truncate entity scope")
    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator",
        target="entity-agent", attributes={"candidate_count": len(candidates)},
    )
    decisions: list[CandidateEvidence] = []
    refs: set[str] = set()
    for order_id in candidates:
        try:
            decision = await asyncio.wait_for(
                source.investigate(case, order_id), timeout=timeout_seconds,
            )
        except TimeoutError:
            decision = CandidateEvidence(case_id, order_id, "unresolved", "TIMEOUT", ())
        if decision.case_id != case_id or decision.order_id != order_id:
            raise ValueError("Entity evidence belongs to another case or candidate")
        if decision.verdict not in {"confirmed", "rejected", "unresolved"}:
            raise ValueError("Invalid candidate verdict")
        if not decision.reason_code:
            raise ValueError("Candidate decision requires a reason code")
        if decision.verdict != "unresolved" and not decision.evidence:
            raise ValueError("Confirmed/rejected candidates require MCP evidence")
        if decision.verdict == "confirmed" and not decision.customer_unique_id:
            raise ValueError("Confirmed candidate requires a verified customer identity")
        for tool, ref in decision.evidence:
            if not source.owns(case_id, tool, ref):
                raise ValueError("Evidence is absent from this case's MCP request ledger")
            trace.emit(
                case_id=case_id, event_type="tool_result_consumed", actor="entity-agent",
                tool_name=tool, evidence_refs=[ref],
                attributes={"order_id": order_id},
            )
            refs.add(ref)
        decisions.append(decision)

    confirmed = [d for d in decisions if d.verdict == "confirmed"]
    rejected = [d.order_id for d in decisions if d.verdict == "rejected"]
    unresolved = any(d.verdict == "unresolved" for d in decisions)
    winner = confirmed[0] if len(confirmed) == 1 and not unresolved else None
    if winner:
        status, confidence = "resolved", 0.9
    elif decisions and len(rejected) == len(decisions):
        status, confidence = "not_found", 0.9
    else:
        # No candidates means no search evidence, not proof of nonexistence.
        status, confidence = "ambiguous", 0.0
    resolution = {
        "status": status,
        "resolved_order_ids": [winner.order_id] if winner else [],
        "rejected_candidates": rejected,
        "confidence": confidence,
    }
    context = {
        "customer_unique_id": winner.customer_unique_id if winner else None,
        "related_order_ids": list(dict.fromkeys(winner.related_order_ids)) if winner else [],
    }
    if len(context["related_order_ids"]) > 20:
        raise ValueError("Customer history exceeds output schema; explicit selection required")
    trace.emit(
        case_id=case_id, event_type="handoff", actor="entity-agent", target="coordinator",
        decision_code=f"ENTITY_{status.upper()}",
        attributes={"candidate_count": len(candidates), "confidence": confidence},
    )
    return EntityResult(resolution, context, tuple(sorted(refs)), tuple(decisions))
