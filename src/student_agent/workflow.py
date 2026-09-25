from __future__ import annotations

from typing import Any

from .business import investigate_case
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verification import build_output, verify_output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one case, verify provenance and return the public L3B output."""
    report = await investigate_case(case, gateway, trace)
    output = build_output(report)
    verify_output(case, report, output, trace)
    return output
