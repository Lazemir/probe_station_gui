"""Pure presentation rules for route-run controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RouteRunPauseAction = Literal["resume", "interrupt", "pause"]


@dataclass(frozen=True)
class RouteRunControlPresentation:
    can_confirm_waiting: bool
    external_measurement_waiting: bool
    pause_text: str
    pause_enabled: bool
    interrupt_text: str
    interrupt_enabled: bool


def route_run_control_presentation(
    *,
    running: bool,
    waiting: bool,
    waiting_reason: str,
    pause_request_pending: bool,
    interrupt_request_pending: bool,
) -> RouteRunControlPresentation:
    running = bool(running)
    waiting = bool(waiting)
    external_waiting = waiting and str(waiting_reason or "") == "external_measurement"
    can_confirm_waiting = running and waiting and not external_waiting
    if can_confirm_waiting:
        return RouteRunControlPresentation(
            can_confirm_waiting=True,
            external_measurement_waiting=False,
            pause_text="Resume",
            pause_enabled=True,
            interrupt_text="Resume",
            interrupt_enabled=False,
        )
    if running and (
        bool(pause_request_pending)
        or bool(interrupt_request_pending)
        or external_waiting
    ):
        return RouteRunControlPresentation(
            can_confirm_waiting=False,
            external_measurement_waiting=external_waiting,
            pause_text="Interrupt",
            pause_enabled=not bool(interrupt_request_pending),
            interrupt_text="Interrupt",
            interrupt_enabled=False,
        )
    return RouteRunControlPresentation(
        can_confirm_waiting=False,
        external_measurement_waiting=external_waiting,
        pause_text="Pause",
        pause_enabled=running,
        interrupt_text="Interrupt",
        interrupt_enabled=False,
    )


def route_run_pause_action(
    *,
    running: bool,
    waiting: bool,
    waiting_reason: str,
    pause_request_pending: bool,
) -> RouteRunPauseAction:
    external_waiting = (
        bool(waiting) and str(waiting_reason or "") == "external_measurement"
    )
    if bool(running) and bool(waiting) and not external_waiting:
        return "resume"
    if bool(running) and (bool(pause_request_pending) or external_waiting):
        return "interrupt"
    return "pause"


__all__ = [
    "RouteRunControlPresentation",
    "RouteRunPauseAction",
    "route_run_control_presentation",
    "route_run_pause_action",
]
