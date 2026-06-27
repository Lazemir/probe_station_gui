"""Pure Telegram command policy for the probe station GUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


HELP_TEXT = (
    "Probe Station Telegram commands:\n"
    "/status - current state and microscope frame\n"
    "/next_photo - send the next route structure photo\n"
    "/next_contact - send the next route contact attempt photo\n"
    "/measure, /skip, /next - answer a waiting route prompt"
)

HELP_COMMANDS = {"start", "help", "commands"}
STATUS_COMMANDS = {"status", "\u0441\u0442\u0430\u0442\u0443\u0441"}
ROUTE_PHOTO_COMMANDS = {
    "next_photo",
    "photo",
    "route_photo",
    "\u0444\u043e\u0442\u043e",
}
CONTACT_PHOTO_COMMANDS = {
    "next_contact",
    "contact",
    "\u043a\u043e\u043d\u0442\u0430\u043a\u0442",
}
ROUTE_ACTIONS = {"measure", "skip", "next"}


@dataclass(frozen=True)
class TelegramCommandRoute:
    kind: str
    action: str = ""
    text: str = ""
    callback_answer: str = ""


@dataclass(frozen=True)
class PhotoRequestPlan:
    text: str
    callback_answer: str
    request_photo: bool


@dataclass(frozen=True)
class RouteActionPlan:
    text: str
    callback_answer: str
    submit_action: str | None


@dataclass(frozen=True)
class TelegramStatusSnapshot:
    latest_status_message: str
    route_thread_active: bool
    route_waiting: bool
    route_session_active: bool
    route_current_point: object | None
    api_route_control_status_text: str
    stage_status: Mapping[str, object]
    microscope_scan_active: bool
    contact_seek_active: bool
    camera_frame_available: bool
    stage_axis_names: Sequence[str]


def parse_telegram_command(text: str) -> tuple[str, str]:
    stripped = str(text or "").strip()
    if not stripped:
        return "", ""
    if stripped.startswith("/"):
        parts = stripped.split(maxsplit=1)
        command = parts[0].lstrip("/").split("@", 1)[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        return command, args.strip()
    parts = stripped.split(maxsplit=1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return command, args.strip()


def route_message_command(text: str) -> TelegramCommandRoute | None:
    command, _args = parse_telegram_command(text)
    if command in HELP_COMMANDS:
        return TelegramCommandRoute(
            "response",
            text=HELP_TEXT,
            callback_answer="Commands.",
        )
    if command in STATUS_COMMANDS:
        return TelegramCommandRoute("status")
    if command in ROUTE_PHOTO_COMMANDS:
        return TelegramCommandRoute("route_photo")
    if command in CONTACT_PHOTO_COMMANDS:
        return TelegramCommandRoute("contact_photo")
    if command in ROUTE_ACTIONS:
        return TelegramCommandRoute("route_action", action=command)
    return None


def route_callback(data: str) -> TelegramCommandRoute:
    key = str(data or "").strip().lower()
    if key == "status":
        return TelegramCommandRoute("status")
    if key == "watch:photo":
        return TelegramCommandRoute("route_photo")
    if key == "watch:contact":
        return TelegramCommandRoute("contact_photo")
    if key.startswith("route:"):
        return TelegramCommandRoute("route_action", action=key.split(":", 1)[1])
    return TelegramCommandRoute(
        "response",
        text="Unknown Telegram action.",
        callback_answer="Unknown action.",
    )


def help_text() -> str:
    return HELP_TEXT


def default_markup_rows(route_waiting: bool) -> list[list[tuple[str, str]]]:
    rows = [
        [("Status", "status")],
        [
            ("Next photo", "watch:photo"),
            ("Next contact", "watch:contact"),
        ],
    ]
    if route_waiting:
        rows.append(
            [
                ("Measure", "route:measure"),
                ("Skip", "route:skip"),
            ]
        )
    return rows


def route_action_markup_rows() -> list[list[tuple[str, str]]]:
    return [
        [
            ("Measure", "route:measure"),
            ("Skip", "route:skip"),
        ],
        [("Status", "status")],
    ]


def next_route_photo_response(
    *,
    route_active: bool,
    structure_photos_enabled: bool,
) -> PhotoRequestPlan:
    if not route_active:
        return PhotoRequestPlan(
            "No route measurement is running.",
            "No active route.",
            False,
        )
    if not structure_photos_enabled:
        return PhotoRequestPlan(
            "The active route is not configured to capture structure photos.",
            "No route photos.",
            False,
        )
    return PhotoRequestPlan(
        "The next route structure photo will be sent here.",
        "Waiting for route photo.",
        True,
    )


def next_contact_photo_response(
    *,
    route_active: bool,
    contact_measurement_enabled: bool,
) -> PhotoRequestPlan:
    if not route_active:
        return PhotoRequestPlan(
            "No route measurement is running.",
            "No active route.",
            False,
        )
    if not contact_measurement_enabled:
        return PhotoRequestPlan(
            "The active route is not configured to measure contacts.",
            "No contact measurements.",
            False,
        )
    return PhotoRequestPlan(
        "The next route contact attempt photo will be sent here.",
        "Waiting for contact photo.",
        True,
    )


def route_action_response(
    action: str,
    *,
    route_waiting: bool,
    runner_available: bool,
    api_route_control_accepts_confirmation: bool,
) -> RouteActionPlan:
    action_key = str(action or "").strip().lower()
    if action_key not in ROUTE_ACTIONS:
        return RouteActionPlan(
            "Unknown route action.",
            "Unknown action.",
            None,
        )
    if not route_waiting:
        return RouteActionPlan(
            "Route measurement is not waiting for an action.",
            "Route is not waiting.",
            None,
        )
    if runner_available:
        return RouteActionPlan(
            f"Route measurement action submitted: {action_key}.",
            f"{action_key} submitted.",
            action_key,
        )
    if api_route_control_accepts_confirmation:
        return RouteActionPlan(
            f"API route control action submitted: {action_key}.",
            f"{action_key} submitted.",
            action_key,
        )
    return RouteActionPlan(
        "No route measurement is running.",
        "No active route.",
        None,
    )


def is_route_action(action: str) -> bool:
    return str(action or "").strip().lower() in ROUTE_ACTIONS


def thread_alive(thread: object | None) -> bool:
    return thread is not None and thread.is_alive()


def status_text(snapshot: TelegramStatusSnapshot) -> str:
    stage_status = snapshot.stage_status
    route_state = _route_state(snapshot)
    lines = [
        "Probe Station status",
        f"Current: {snapshot.latest_status_message or 'idle'}",
        f"Route: {route_state}",
        f"Serial: {'connected' if stage_status.get('connected') else 'disconnected'}",
        f"Stage: {stage_status.get('state') or 'unknown'}"
        f"{' busy' if stage_status.get('busy') else ''}",
        f"Coordinates: {stage_status.get('coordinate_display') or 'unknown'}",
    ]
    position_line = _position_line(stage_status, snapshot.stage_axis_names)
    if position_line:
        lines.append(position_line)
    homed_axes = stage_status.get("homed_axes")
    if isinstance(homed_axes, list):
        lines.append("Homed: " + (", ".join(homed_axes) if homed_axes else "none"))
    if snapshot.microscope_scan_active:
        lines.append("Microscope scan: running")
    if snapshot.contact_seek_active:
        lines.append("Contact seek: running")
    if not snapshot.camera_frame_available:
        lines.append("Camera frame: unavailable")
    return "\n".join(lines)


def _route_state(snapshot: TelegramStatusSnapshot) -> str:
    point_suffix = (
        f", point {snapshot.route_current_point}"
        if snapshot.route_current_point is not None
        else ""
    )
    if snapshot.route_thread_active:
        state = "waiting" if snapshot.route_waiting else "running"
        return f"{state}{point_suffix}"
    if snapshot.route_session_active:
        return f"session active{point_suffix}"
    return snapshot.api_route_control_status_text or "idle"


def _position_line(
    stage_status: Mapping[str, object],
    stage_axis_names: Sequence[str],
) -> str:
    position = stage_status.get("display_position")
    if not isinstance(position, dict) or not position:
        return ""
    values = []
    for axis in stage_axis_names:
        if axis not in position:
            continue
        try:
            values.append(f"{axis}={float(position[axis]):.4f}")
        except (TypeError, ValueError):
            pass
    return "Position: " + ", ".join(values) if values else ""
