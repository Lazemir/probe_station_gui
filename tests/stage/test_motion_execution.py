from __future__ import annotations

import pytest

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.motion_execution import (
    AbsoluteMotionPlan,
    RelativeMotionPlan,
    StageMotionExecution,
)
from probe_station_gui.stage.types import MoveVector, _Status


class _CancellationAdapter:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def check(self) -> None:
        self.events.append("cancel:check")


class _SafetyAdapter:
    def __init__(self, events: list[str], *, allowed: bool) -> None:
        self.events = events
        self.allowed = allowed

    def check(self) -> None:
        self.events.append("safety:check")
        if not self.allowed:
            raise StageControllerError("Needles are not raised.")

    def disabled(self) -> bool:
        return False


class _StatusAdapter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.read_axes: list[tuple[str, ...]] = []

    def read(self, axes: tuple[str, ...]) -> _Status:
        self.read_axes.append(axes)
        self.events.append("status:read")
        return _Status(
            state="Idle",
            position=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            work_position=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            homed_axes=set(axes),
        )

    def position(self, status: _Status | None) -> tuple[float, ...] | None:
        return None if status is None else status.display_position

    def axis_value(self, status: _Status | None, axis: str) -> float | None:
        position = self.position(status)
        index = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}[axis]
        if position is None or index >= len(position):
            return None
        return position[index]


class _LimitAdapter:
    def __init__(
        self,
        events: list[str],
        *,
        allowed: bool,
        configured: bool = True,
        ready: bool = False,
    ) -> None:
        self.events = events
        self.allowed = allowed
        self.configured = configured
        self.ready = ready
        self.b_zero: float | None = None

    def ensure(self, required_axes: tuple[str, ...]) -> None:
        del required_axes
        self.events.append("limits:ensure")

    def configured_limits(
        self,
        axis: str,
        status: _Status,
    ) -> tuple[float, float] | None:
        del axis, status
        self.events.append("limits:check")
        if not self.allowed:
            raise StageControllerError("X move exceeds limits.")
        return (0.0, 10.0) if self.configured else None

    def software_limit_ready(self, status: _Status, axis: str) -> bool:
        del status, axis
        return self.ready

    def require_homed(
        self,
        status: _Status,
        axes: set[str],
        *,
        allow_relative: bool,
    ) -> None:
        del status, axes, allow_relative

    def axis_limits(self) -> dict[str, tuple[float, float]]:
        return {"X": (0.0, 10.0)} if self.configured else {}

    def b_zero_position(self) -> float | None:
        return self.b_zero

    def set_b_zero_position(self, value: float, *, emit_status: bool) -> None:
        del emit_status
        self.b_zero = value


class _SerialAdapter:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def prepare(self) -> None:
        pass

    def reset_feed_override(self) -> None:
        pass

    def write(self, command: str) -> None:
        self.events.append(f"serial:{command}")

    def wait_for_idle(self, *, timeout: float) -> None:
        del timeout

    def wait_for_idle_at_targets(
        self,
        targets: dict[str, float],
        *,
        timeout: float,
    ) -> None:
        del targets, timeout


def _x_move_plan(*, delta_mm: float, feedrate: float) -> RelativeMotionPlan:
    return RelativeMotionPlan(
        move=MoveVector(x=delta_mm),
        feedrate=feedrate,
    )


def test_relative_execution_checks_safety_and_limits_before_serial_write() -> None:
    events: list[str] = []
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=True),
        limits=_LimitAdapter(events, allowed=True),
        status=_StatusAdapter(events),
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )

    execution.run_relative(_x_move_plan(delta_mm=0.25, feedrate=120.0))

    assert events[:5] == [
        "cancel:check",
        "safety:check",
        "limits:ensure",
        "status:read",
        "limits:check",
    ]
    assert events[-4:] == [
        "serial:G21",
        "serial:G91",
        "serial:G1 X0.25 F120",
        "serial:G90",
    ]


@pytest.mark.parametrize(
    ("safety_allowed", "limits_allowed"),
    [(False, True), (True, False)],
)
def test_relative_execution_rejection_writes_no_serial(
    safety_allowed: bool,
    limits_allowed: bool,
) -> None:
    events: list[str] = []
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=safety_allowed),
        limits=_LimitAdapter(events, allowed=limits_allowed),
        status=_StatusAdapter(events),
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )

    with pytest.raises(StageControllerError):
        execution.run_relative(_x_move_plan(delta_mm=0.25, feedrate=120.0))

    assert not any(event.startswith("serial:") for event in events)


def test_relative_callback_runs_after_g1_acceptance_before_g90_restore() -> None:
    events: list[str] = []
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=True),
        limits=_LimitAdapter(events, allowed=True),
        status=_StatusAdapter(events),
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )
    plan = RelativeMotionPlan(
        move=MoveVector(x=0.1),
        wait_for_completion=False,
        motion_started_callback=lambda _move, _feedrate: events.append("callback"),
    )

    execution.run_relative(plan)

    assert events.index("serial:G1 X0.1 F600") < events.index("callback")
    assert events.index("callback") < events.index("serial:G90")


def test_relative_execution_without_configured_limits_skips_status_read() -> None:
    events: list[str] = []
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=True),
        limits=_LimitAdapter(events, allowed=True, configured=False),
        status=_StatusAdapter(events),
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )

    execution.run_relative(_x_move_plan(delta_mm=0.25, feedrate=120.0))

    assert "limits:ensure" in events
    assert "status:read" not in events
    assert events[-4:] == [
        "serial:G21",
        "serial:G91",
        "serial:G1 X0.25 F120",
        "serial:G90",
    ]


def test_unlimited_axis_move_reads_status_when_other_axis_limits_exist() -> None:
    events: list[str] = []
    status = _StatusAdapter(events)
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=True),
        limits=_LimitAdapter(events, allowed=True),
        status=status,
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )

    execution.run_relative(
        RelativeMotionPlan(
            move=MoveVector(c=0.25),
            feedrate=120.0,
            wait_for_completion=False,
        )
    )

    assert status.read_axes == [()]
    assert events == [
        "cancel:check",
        "safety:check",
        "limits:ensure",
        "status:read",
        "limits:check",
        "serial:G21",
        "serial:G91",
        "serial:G1 C0.25 F120",
        "serial:G90",
    ]


def test_absolute_execution_emits_existing_g90_transaction() -> None:
    events: list[str] = []
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=True),
        limits=_LimitAdapter(events, allowed=True),
        status=_StatusAdapter(events),
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )

    execution.run_absolute(
        AbsoluteMotionPlan(
            targets={"Y": -5.0, "X": 10.0},
            feedrate=123.4,
            wait_for_completion=False,
        )
    )

    assert events[:4] == [
        "cancel:check",
        "safety:check",
        "limits:ensure",
        "status:read",
    ]
    assert events[-3:] == [
        "serial:G21",
        "serial:G90",
        "serial:G1 X10 Y-5 F123.4",
    ]


def test_absolute_limit_rejection_writes_no_serial() -> None:
    events: list[str] = []
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=True),
        limits=_LimitAdapter(events, allowed=True, ready=True),
        status=_StatusAdapter(events),
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )

    with pytest.raises(StageControllerError, match="X target"):
        execution.run_absolute(AbsoluteMotionPlan(targets={"X": -0.25}))

    assert not any(event.startswith("serial:") for event in events)


def test_b_reference_is_initialized_before_relative_soft_limit_rejection() -> None:
    events: list[str] = []
    limits = _LimitAdapter(events, allowed=True)
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=True),
        limits=limits,
        status=_StatusAdapter(events),
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )

    with pytest.raises(StageControllerError, match="relative to B zero"):
        execution.run_relative(RelativeMotionPlan(move=MoveVector(b=46.0)))

    assert limits.b_zero == 0.0
    assert not any(event.startswith("serial:") for event in events)


def test_absolute_motion_requires_complete_reported_position() -> None:
    events: list[str] = []
    execution = StageMotionExecution(
        safety=_SafetyAdapter(events, allowed=True),
        limits=_LimitAdapter(events, allowed=True),
        status=_StatusAdapter(events),
        serial=_SerialAdapter(events),
        cancellation=_CancellationAdapter(events),
    )
    status = _Status(state="Idle", display_position=(1.0, 2.0))

    with pytest.raises(StageControllerError, match="complete position"):
        execution.require_position_for_absolute_motion(status, required_axes=3)
