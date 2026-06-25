"""Route API session action parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RouteSessionAction:
    action: str
    kind: str

    @property
    def is_confirmation(self) -> bool:
        return self.kind == "confirmation"


def route_session_action_from_payload(payload: dict[str, Any]) -> RouteSessionAction:
    action = str(payload.get("action", payload.get("command", "next"))).strip()
    action_key = action.lower()
    if action_key in {"pause", "interrupt", "stop"}:
        return RouteSessionAction(action_key, action_key)
    return RouteSessionAction(action, "confirmation")


__all__ = [
    "RouteSessionAction",
    "route_session_action_from_payload",
]
