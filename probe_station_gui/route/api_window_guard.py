"""Policy for route API commands that require an open route-control window."""

from __future__ import annotations

from typing import Any


ROUTE_API_WINDOW_ACTIONS = frozenset(
    {
        "api_route_control_resume",
        "move_to_contact",
        "contact_needles",
        "check_contact",
        "route_contact_focus",
        "route_contact_photo",
        "contact_seek",
        "start_route_session",
        "route_session_result",
        "route_session_seek",
    }
)
ROUTE_SESSION_WINDOWLESS_ACTIONS = frozenset({"pause", "interrupt", "stop", "status"})
RAW_VOLTAGE_ROUTE_CONTACT_KEYS = frozenset(
    {
        "contact_number",
        "contact",
        "structure_number",
        "point_number",
    }
)


def probe_route_api_requires_window(action: str, payload: dict[str, Any]) -> bool:
    action_key = str(action or "").strip().lower()
    if action_key in ROUTE_API_WINDOW_ACTIONS:
        return True
    if action_key == "route_session_action":
        route_action = str(
            payload.get("action", payload.get("command", "next"))
        ).strip().lower()
        return route_action not in ROUTE_SESSION_WINDOWLESS_ACTIONS
    if action_key == "raw_voltage_sweep":
        return raw_voltage_sweep_requires_route_window(payload)
    return False


def raw_voltage_sweep_requires_route_window(payload: dict[str, Any]) -> bool:
    if any(key in payload for key in RAW_VOLTAGE_ROUTE_CONTACT_KEYS):
        return True
    return (
        _api_bool(payload, "move_to_contact", "move", default=False)
        or _api_bool(payload, "lower_needles", "lower", default=False)
        or _api_bool(payload, "lift_after", default=False)
    )


def _api_bool(payload: dict[str, Any], *keys: str, default: bool) -> bool:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
    return default


__all__ = [
    "probe_route_api_requires_window",
    "raw_voltage_sweep_requires_route_window",
]
