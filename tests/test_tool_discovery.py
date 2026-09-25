import asyncio
from types import SimpleNamespace

import pytest

from student_agent.mcp_gateway import EvidenceGateway


class Tool:
    def __init__(self, name):
        self.name = name

    def model_dump(self, **kwargs):
        return {"name": self.name, "inputSchema": {"type": "object"}}


class Session:
    def __init__(self, repeat=False):
        self.cursors = []
        self.repeat = repeat

    async def list_tools(self, *, params=None):
        cursor = params.cursor if params else None
        self.cursors.append(cursor)
        return SimpleNamespace(
            tools=[Tool("first" if cursor is None else "second")],
            next_cursor="page2" if cursor is None or self.repeat else None,
        )


def test_discovery_reads_all_pages():
    session = Session()
    gateway = EvidenceGateway(session, None)
    result = asyncio.run(gateway.describe_tools())
    assert [tool["name"] for tool in result] == ["first", "second"]
    assert session.cursors == [None, "page2"]


def test_discovery_rejects_cursor_loop():
    with pytest.raises(RuntimeError, match="pagination"):
        asyncio.run(EvidenceGateway(Session(repeat=True), None).describe_tools())
