from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import Contracts


class TraceWriter:
    """Append observable workflow events. Never put prompts or chain-of-thought here."""

    def __init__(self, path: Path, contracts: Contracts) -> None:
        self.path = path
        self.contracts = contracts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_at: datetime | None = None

    def _next_timestamp(self) -> datetime:
        """Strictly increasing clock so event order is unambiguous even on coarse timers."""
        now = datetime.now(UTC)
        if self._last_at is not None and now <= self._last_at:
            now = self._last_at + timedelta(microseconds=1)
        self._last_at = now
        return now

    def emit(
        self,
        *,
        case_id: str,
        event_type: str,
        actor: str,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": "day09-trace-event-v1",
            "event_id": f"evt_{secrets.token_urlsafe(18)}",
            "case_id": case_id,
            "event_type": event_type,
            "occurred_at": self._next_timestamp().isoformat().replace("+00:00", "Z"),
            "actor": actor,
        }
        optional = {
            "target": target,
            "decision_code": decision_code,
            "tool_name": tool_name,
            "evidence_refs": evidence_refs,
            "attributes": attributes,
        }
        event.update({key: value for key, value in optional.items() if value is not None})
        self.contracts.validate_trace(event, "trace event")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event
