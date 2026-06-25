"""Pure state transitions for API route control."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


API_ROUTE_CONTROL_DEFAULT_LABEL = "API route control"
API_ROUTE_CONTROL_START_ACTIONS = frozenset({"start", "begin", "activate"})
API_ROUTE_CONTROL_PAUSE_ACK_ACTIONS = frozenset({"paused", "pause_ack", "ack_pause"})
API_ROUTE_CONTROL_RESUME_ACTIONS = frozenset(
    {"resume", "continue", "measure", "remeasure", "skip", "next"}
)
API_ROUTE_CONTROL_FINISH_ACTIONS = frozenset(
    {"finish", "complete", "clear", "done"}
)
API_ROUTE_CONTROL_CLEAR_ACTIONS = frozenset({"ack", "clear_action"})
API_ROUTE_CONTROL_REQUIRES_ACTIVE_KINDS = frozenset(
    {"pause", "pause_ack", "interrupt", "resume", "stop"}
)


def normalize_route_control_action(action: str) -> str | None:
    normalized = str(action or "").strip().lower()
    if normalized in {"resume", "next", "continue"}:
        return "next"
    if normalized == "measure":
        return "measure"
    if normalized == "remeasure":
        return "remeasure"
    if normalized in {"skip", "stop", "interrupt", "seek"}:
        return normalized
    if normalized.isdigit():
        return f"jump:{int(normalized)}"
    if normalized.startswith("jump:"):
        try:
            return f"jump:{int(normalized.split(':', 1)[1].strip())}"
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class ApiRouteControlCommand:
    kind: str
    action: str
    label: str = ""
    explicit_action: str = ""

    @property
    def starts_control(self) -> bool:
        return self.kind == "start"

    @property
    def requires_active_control(self) -> bool:
        return self.kind in API_ROUTE_CONTROL_REQUIRES_ACTIVE_KINDS


@dataclass(frozen=True)
class ApiRouteControlUiState:
    active: bool
    waiting: bool
    waiting_reason: str
    control_waiting_reason: str
    pause_pending: bool


def api_route_control_command_from_payload(
    payload: dict[str, Any],
) -> ApiRouteControlCommand:
    action = str(payload.get("action", payload.get("command", "status"))).strip().lower()
    label = str(payload.get("label", payload.get("name", "")) or "").strip()
    explicit_action = str(
        payload.get("pending_action", payload.get("next_action", "")) or ""
    ).strip().lower()
    if action in API_ROUTE_CONTROL_START_ACTIONS:
        return ApiRouteControlCommand("start", action, label=label)
    if action == "pause":
        return ApiRouteControlCommand("pause", action, label=label)
    if action in API_ROUTE_CONTROL_PAUSE_ACK_ACTIONS:
        return ApiRouteControlCommand("pause_ack", action, label=label)
    if action == "interrupt":
        return ApiRouteControlCommand("interrupt", action, label=label)
    if action in API_ROUTE_CONTROL_RESUME_ACTIONS or action.startswith("jump:"):
        return ApiRouteControlCommand(
            "resume",
            action,
            label=label,
            explicit_action=explicit_action,
        )
    if action == "stop":
        return ApiRouteControlCommand("stop", action, label=label)
    if action in API_ROUTE_CONTROL_FINISH_ACTIONS:
        return ApiRouteControlCommand("finish", action, label=label)
    if action in API_ROUTE_CONTROL_CLEAR_ACTIONS:
        return ApiRouteControlCommand("clear_action", action, label=label)
    if action == "status":
        return ApiRouteControlCommand("status", action, label=label)
    return ApiRouteControlCommand("unknown", action, label=label)


@dataclass(frozen=True)
class ApiRouteControlState:
    """State machine for the external API route-control workflow."""

    active: bool = False
    pause_requested: bool = False
    paused: bool = False
    stop_requested: bool = False
    pending_action: str = ""
    label: str = ""
    updated_utc: str = ""

    def status_payload(self, *, route_control_window_open: bool) -> dict[str, Any]:
        return {
            "accepted": True,
            "active": bool(self.active),
            "pause_requested": bool(self.pause_requested),
            "paused": bool(self.paused),
            "stop_requested": bool(self.stop_requested),
            "pending_action": str(self.pending_action or ""),
            "label": str(self.label or ""),
            "updated_utc": str(self.updated_utc or ""),
            "route_control_window_open": bool(route_control_window_open),
        }

    def start(
        self,
        *,
        label: str,
        updated_utc: str,
    ) -> tuple["ApiRouteControlState", str]:
        next_label = str(label or "").strip()
        if not next_label:
            next_label = self.label or API_ROUTE_CONTROL_DEFAULT_LABEL
        state = replace(
            self,
            active=True,
            pause_requested=False,
            paused=False,
            stop_requested=False,
            pending_action="",
            label=next_label,
            updated_utc=str(updated_utc or ""),
        )
        return state, f"{state.display_label}: running."

    def request_pause(
        self,
        *,
        updated_utc: str,
    ) -> tuple["ApiRouteControlState", str]:
        state = replace(
            self,
            pause_requested=True,
            paused=False,
            stop_requested=False,
            pending_action="",
            updated_utc=str(updated_utc or ""),
        )
        return state, f"{state.display_label}: pause requested."

    def ack_pause(
        self,
        *,
        updated_utc: str,
    ) -> tuple["ApiRouteControlState", str]:
        state = replace(
            self,
            pause_requested=False,
            paused=True,
            stop_requested=False,
            pending_action="",
            updated_utc=str(updated_utc or ""),
        )
        return state, f"{state.display_label}: paused."

    def interrupt(
        self,
        *,
        updated_utc: str,
    ) -> tuple["ApiRouteControlState", str]:
        state = replace(
            self,
            pause_requested=False,
            paused=True,
            stop_requested=False,
            pending_action="",
            updated_utc=str(updated_utc or ""),
        )
        return state, f"{state.display_label}: interrupted; paused."

    def resume(
        self,
        *,
        action: str,
        explicit_action: str = "",
        updated_utc: str,
    ) -> tuple["ApiRouteControlState", str]:
        pending_action = str(action or "").strip().lower()
        if pending_action == "continue":
            pending_action = "resume"
        override = str(explicit_action or "").strip().lower()
        if override:
            pending_action = override
        state = replace(
            self,
            pause_requested=False,
            paused=False,
            pending_action=pending_action,
            updated_utc=str(updated_utc or ""),
        )
        action_label = "resumed" if pending_action == "resume" else pending_action
        return state, f"{state.display_label}: {action_label}."

    def stop(
        self,
        *,
        updated_utc: str,
    ) -> tuple["ApiRouteControlState", str]:
        state = replace(
            self,
            pause_requested=False,
            paused=False,
            stop_requested=True,
            pending_action="",
            updated_utc=str(updated_utc or ""),
        )
        return state, f"{state.display_label}: stop requested."

    def finish(
        self,
        *,
        label: str,
        updated_utc: str,
    ) -> tuple["ApiRouteControlState", str]:
        next_label = str(label or "").strip() or self.label
        state = replace(
            self,
            active=False,
            pause_requested=False,
            paused=False,
            stop_requested=False,
            pending_action="",
            label=next_label,
            updated_utc=str(updated_utc or ""),
        )
        return state, f"{state.display_label}: finished."

    def clear_action(
        self,
        *,
        updated_utc: str,
    ) -> tuple["ApiRouteControlState", None]:
        return replace(
            self,
            pending_action="",
            updated_utc=str(updated_utc or ""),
        ), None

    def start_failed_window_not_open(self) -> tuple["ApiRouteControlState", str]:
        state = replace(
            self,
            active=False,
            pause_requested=False,
            paused=False,
            stop_requested=False,
            pending_action="",
        )
        return state, f"{state.display_label}: route control window did not open."

    @property
    def display_label(self) -> str:
        return self.label or API_ROUTE_CONTROL_DEFAULT_LABEL

    def ui_state(self) -> ApiRouteControlUiState:
        waiting = bool(self.active and self.paused)
        pause_pending = bool(self.active and self.pause_requested and not self.paused)
        return ApiRouteControlUiState(
            active=bool(self.active),
            waiting=waiting,
            waiting_reason="paused" if waiting else "",
            control_waiting_reason="paused",
            pause_pending=pause_pending,
        )


__all__ = [
    "API_ROUTE_CONTROL_DEFAULT_LABEL",
    "ApiRouteControlCommand",
    "ApiRouteControlState",
    "ApiRouteControlUiState",
    "api_route_control_command_from_payload",
    "normalize_route_control_action",
]
