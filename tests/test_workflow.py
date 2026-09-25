from __future__ import annotations

import asyncio
from pathlib import Path

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import CaseScope

ROOT = Path(__file__).resolve().parents[1]


class FakeGateway:
    def __init__(self, discovered: set[str]) -> None:
        self.discovered_tools = frozenset(discovered)
        self.calls: list[str] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
        self.calls.append(tool_name)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_" + "a" * 24,
            "result_hash": "sha256:" + "0" * 64,
            "domain": "order",
            "data": {"ok": True},
        }


def scope_for(gateway: FakeGateway, tmp_path: Path) -> CaseScope:
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    return CaseScope("CASE_001", gateway, trace)


def test_undiscovered_tool_is_never_called(tmp_path: Path) -> None:
    gateway = FakeGateway({"get_order"})
    scope = scope_for(gateway, tmp_path)
    assert asyncio.run(scope.fetch("order-agent", "get_sellers", order_id="o1")) is None
    assert gateway.calls == []
    assert "NOT_DISCOVERED" in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")


def test_repeated_fetch_is_cached_within_case(tmp_path: Path) -> None:
    gateway = FakeGateway({"get_order"})
    scope = scope_for(gateway, tmp_path)
    asyncio.run(scope.fetch("order-agent", "get_order", order_id="o1"))
    asyncio.run(scope.fetch("order-agent", "get_order", order_id="o1"))
    assert gateway.calls == ["get_order"]
    assert scope.ref_list("get_order") == ["ev_" + "a" * 24]
