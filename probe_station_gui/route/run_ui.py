"""Pure presentation rules for route-run controls."""

from __future__ import annotations

from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class RouteRunControlState:
    running: bool = False
    waiting: bool = False
    waiting_reason: str = ""
    pause_request_pending: bool = False
    interrupt_request_pending: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "running", bool(self.running))
        object.__setattr__(self, "waiting", bool(self.waiting))
        object.__setattr__(self, "waiting_reason", str(self.waiting_reason or ""))
        object.__setattr__(
            self,
            "pause_request_pending",
            bool(self.pause_request_pending),
        )
        object.__setattr__(
            self,
            "interrupt_request_pending",
            bool(self.interrupt_request_pending),
        )

    @property
    def external_measurement_waiting(self) -> bool:
        return self.waiting and self.waiting_reason == "external_measurement"

    @property
    def can_confirm_waiting(self) -> bool:
        return self.running and self.waiting and not self.external_measurement_waiting

    def with_running(self, running: bool) -> RouteRunControlState:
        running = bool(running)
        if not running:
            return replace(
                self,
                running=False,
                waiting=False,
                waiting_reason="",
                pause_request_pending=False,
                interrupt_request_pending=False,
            )
        return replace(self, running=True)

    def with_waiting(
        self,
        waiting: bool,
        reason: str = "",
    ) -> RouteRunControlState:
        waiting = bool(waiting)
        if waiting:
            return replace(
                self,
                waiting=True,
                waiting_reason=str(reason or "paused"),
                pause_request_pending=False,
                interrupt_request_pending=False,
            )
        return replace(self, waiting=False, waiting_reason="")

    def with_pause_request_pending(self, pending: bool) -> RouteRunControlState:
        pending = bool(pending)
        return replace(
            self,
            pause_request_pending=pending,
            interrupt_request_pending=(
                False if pending else self.interrupt_request_pending
            ),
        )

    def with_interrupt_request_pending(self, pending: bool) -> RouteRunControlState:
        return replace(self, interrupt_request_pending=bool(pending))

    def presentation(self) -> RouteRunControlPresentation:
        if self.can_confirm_waiting:
            return RouteRunControlPresentation(
                can_confirm_waiting=True,
                external_measurement_waiting=False,
                pause_text="Resume",
                pause_enabled=True,
                interrupt_text="Resume",
                interrupt_enabled=False,
            )
        if self.running and (
            self.pause_request_pending
            or self.interrupt_request_pending
            or self.external_measurement_waiting
        ):
            return RouteRunControlPresentation(
                can_confirm_waiting=False,
                external_measurement_waiting=self.external_measurement_waiting,
                pause_text="Interrupt",
                pause_enabled=not self.interrupt_request_pending,
                interrupt_text="Interrupt",
                interrupt_enabled=False,
            )
        return RouteRunControlPresentation(
            can_confirm_waiting=False,
            external_measurement_waiting=self.external_measurement_waiting,
            pause_text="Pause",
            pause_enabled=self.running,
            interrupt_text="Interrupt",
            interrupt_enabled=False,
        )

    def pause_action(self) -> RouteRunPauseAction:
        if self.can_confirm_waiting:
            return "resume"
        if self.running and (
            self.pause_request_pending or self.external_measurement_waiting
        ):
            return "interrupt"
        return "pause"


def route_run_control_presentation(
    *,
    running: bool,
    waiting: bool,
    waiting_reason: str,
    pause_request_pending: bool,
    interrupt_request_pending: bool,
) -> RouteRunControlPresentation:
    return RouteRunControlState(
        running=running,
        waiting=waiting,
        waiting_reason=waiting_reason,
        pause_request_pending=pause_request_pending,
        interrupt_request_pending=interrupt_request_pending,
    ).presentation()


def route_run_pause_action(
    *,
    running: bool,
    waiting: bool,
    waiting_reason: str,
    pause_request_pending: bool,
) -> RouteRunPauseAction:
    return RouteRunControlState(
        running=running,
        waiting=waiting,
        waiting_reason=waiting_reason,
        pause_request_pending=pause_request_pending,
    ).pause_action()


__all__ = [
    "RouteRunControlPresentation",
    "RouteRunControlState",
    "RouteRunPauseAction",
    "route_run_control_presentation",
    "route_run_pause_action",
]
