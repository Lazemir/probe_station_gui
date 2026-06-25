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


@dataclass(frozen=True)
class RouteConfirmationAction:
    action: str
    action_key: str
    replaced_pending_point: bool

    @property
    def status_label(self) -> str:
        if self.action_key == "measure":
            return "measure"
        if self.action_key == "remeasure":
            return "remeasure"
        if self.action_key == "skip":
            return "skip"
        if self.action_key.startswith("jump:") or self.action_key.isdigit():
            point_number = self.action_key.split(":", 1)[-1]
            return f"measure from point {point_number}"
        return "next"


def route_session_action_from_payload(payload: dict[str, Any]) -> RouteSessionAction:
    action = str(payload.get("action", payload.get("command", "next"))).strip()
    action_key = action.lower()
    if action_key in {"pause", "interrupt", "stop"}:
        return RouteSessionAction(action_key, action_key)
    return RouteSessionAction(action, "confirmation")


def route_confirmation_action(
    action: str,
    *,
    waiting: bool,
    pending_point_number: int | None,
) -> RouteConfirmationAction:
    action_to_submit = str(action)
    action_key = action_to_submit.strip().lower()
    replaced_pending_point = False
    if (
        waiting
        and pending_point_number is not None
        and action_key in {"next", "resume", "continue"}
    ):
        action_to_submit = f"jump:{int(pending_point_number)}"
        action_key = action_to_submit.strip().lower()
        replaced_pending_point = True
    return RouteConfirmationAction(
        action=action_to_submit,
        action_key=action_key,
        replaced_pending_point=replaced_pending_point,
    )


__all__ = [
    "RouteConfirmationAction",
    "RouteSessionAction",
    "route_confirmation_action",
    "route_session_action_from_payload",
]
