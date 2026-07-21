"""Motion transactions behind explicit stage-control ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.jog_commands import (
    absolute_axis_targets_jog_command,
    format_gcode_value,
)
from probe_station_gui.stage.motion_command_planning import (
    absolute_axis_g1_command,
    absolute_axis_target_limit_error,
    clamped_motion_feedrate,
    ordered_absolute_axis_targets,
)
from probe_station_gui.stage.motion_timing import (
    absolute_move_distance_for_timeout,
    idle_timeout_for_distance,
    move_distance_for_timeout,
)
from probe_station_gui.stage.types import MoveVector, _Status


MotionStartedCallback = Callable[[MoveVector, float], None]
AbsoluteMotionStartedCallback = Callable[[], None]
AXIS_ORDER = ("X", "Y", "Z", "A", "B", "C")


@dataclass(frozen=True)
class RelativeMotionPlan:
    """One relative move and its execution policy."""

    move: MoveVector
    feedrate: float | None = None
    allow_relative: bool = False
    ignore_needle_safety: bool = False
    wait_for_completion: bool = True
    motion_started_callback: MotionStartedCallback | None = None
    as_jog: bool = False


@dataclass(frozen=True)
class AbsoluteMotionPlan:
    """One absolute multi-axis move and its execution policy."""

    targets: Mapping[str, float]
    feedrate: float | None = None
    ignore_needle_safety: bool = False
    wait_for_completion: bool = True
    allow_unhomed: bool = False
    as_jog: bool = False
    motion_started_callback: AbsoluteMotionStartedCallback | None = None
    machine_position_mode: bool = False
    axis_order: tuple[str, ...] = AXIS_ORDER


@dataclass(frozen=True)
class MotionTiming:
    """Feedrate defaults and timeout bounds used by a motion transaction."""

    default_feedrate: float = 600.0
    min_feedrate: float = 1.0
    idle_margin_s: float = 5.0
    idle_min_s: float = 10.0
    idle_max_s: float = 3600.0
    b_axis_soft_limit_deg: float = 45.0

    def idle_timeout(self, distance: float, feedrate: float) -> float:
        return idle_timeout_for_distance(
            distance,
            feedrate,
            min_feedrate=self.min_feedrate,
            margin_s=self.idle_margin_s,
            min_timeout_s=self.idle_min_s,
            max_timeout_s=self.idle_max_s,
        )


class MotionSafetyPort(Protocol):
    def check(self) -> None: ...

    def disabled(self) -> bool: ...


class MotionStatusPort(Protocol):
    def read(self, axes: tuple[str, ...]) -> _Status | None: ...

    def position(self, status: _Status | None) -> tuple[float, ...] | None: ...

    def axis_value(self, status: _Status | None, axis: str) -> float | None: ...


class MotionLimitPort(Protocol):
    def ensure(self, required_axes: tuple[str, ...]) -> None: ...

    def configured_limits(
        self,
        axis: str,
        status: _Status,
    ) -> tuple[float, float] | None: ...

    def software_limit_ready(self, status: _Status, axis: str) -> bool: ...

    def require_homed(
        self,
        status: _Status,
        axes: set[str],
        *,
        allow_relative: bool,
    ) -> None: ...

    def axis_limits(self) -> Mapping[str, tuple[float, float]]: ...

    def b_zero_position(self) -> float | None: ...

    def set_b_zero_position(self, value: float, *, emit_status: bool) -> None: ...


class MotionSerialPort(Protocol):
    def prepare(self) -> None: ...

    def reset_feed_override(self) -> None: ...

    def write(self, command: str) -> None: ...

    def wait_for_idle(self, *, timeout: float) -> None: ...

    def wait_for_idle_at_targets(
        self,
        targets: dict[str, float],
        *,
        timeout: float,
    ) -> None: ...


class MotionCancellationPort(Protocol):
    def check(self) -> None: ...


@dataclass(frozen=True)
class StageMotionSafetyAdapter:
    check_callback: Callable[[], None]
    disabled_callback: Callable[[], bool]

    def check(self) -> None:
        self.check_callback()

    def disabled(self) -> bool:
        return bool(self.disabled_callback())


@dataclass(frozen=True)
class StageMotionStatusAdapter:
    read_callback: Callable[[tuple[str, ...]], _Status | None]
    position_callback: Callable[[_Status | None], tuple[float, ...] | None]
    axis_value_callback: Callable[[_Status | None, str], float | None]

    def read(self, axes: tuple[str, ...]) -> _Status | None:
        return self.read_callback(axes)

    def position(self, status: _Status | None) -> tuple[float, ...] | None:
        return self.position_callback(status)

    def axis_value(self, status: _Status | None, axis: str) -> float | None:
        return self.axis_value_callback(status, axis)


@dataclass(frozen=True)
class StageMotionLimitAdapter:
    ensure_callback: Callable[[tuple[str, ...]], None]
    configured_limits_callback: Callable[
        [str, _Status], tuple[float, float] | None
    ]
    software_limit_ready_callback: Callable[[_Status, str], bool]
    require_homed_callback: Callable[[_Status, set[str], bool], None]
    axis_limits_callback: Callable[[], Mapping[str, tuple[float, float]]]
    b_zero_position_callback: Callable[[], float | None]
    set_b_zero_position_callback: Callable[[float, bool], None]

    def ensure(self, required_axes: tuple[str, ...]) -> None:
        self.ensure_callback(required_axes)

    def configured_limits(
        self,
        axis: str,
        status: _Status,
    ) -> tuple[float, float] | None:
        return self.configured_limits_callback(axis, status)

    def software_limit_ready(self, status: _Status, axis: str) -> bool:
        return bool(self.software_limit_ready_callback(status, axis))

    def require_homed(
        self,
        status: _Status,
        axes: set[str],
        *,
        allow_relative: bool,
    ) -> None:
        self.require_homed_callback(status, axes, allow_relative)

    def axis_limits(self) -> Mapping[str, tuple[float, float]]:
        return self.axis_limits_callback()

    def b_zero_position(self) -> float | None:
        return self.b_zero_position_callback()

    def set_b_zero_position(self, value: float, *, emit_status: bool) -> None:
        self.set_b_zero_position_callback(float(value), bool(emit_status))


@dataclass(frozen=True)
class FluidNCMotionSerialAdapter:
    """Real controller-session adapter used by ``StageController``."""

    prepare_callback: Callable[[], None]
    write_callback: Callable[[str], None]
    reset_feed_override_callback: Callable[[], None]
    wait_for_idle_callback: Callable[[float], None]
    wait_for_idle_at_targets_callback: Callable[[dict[str, float], float], None]

    def prepare(self) -> None:
        self.prepare_callback()

    def reset_feed_override(self) -> None:
        self.reset_feed_override_callback()

    def write(self, command: str) -> None:
        self.write_callback(command)

    def wait_for_idle(self, *, timeout: float) -> None:
        self.wait_for_idle_callback(float(timeout))

    def wait_for_idle_at_targets(
        self,
        targets: dict[str, float],
        *,
        timeout: float,
    ) -> None:
        self.wait_for_idle_at_targets_callback(dict(targets), float(timeout))


@dataclass(frozen=True)
class StageMotionCancellationAdapter:
    check_callback: Callable[[], None]

    def check(self) -> None:
        self.check_callback()


@dataclass
class RecordingMotionSerialAdapter:
    """In-memory serial adapter for transaction tests and diagnostics."""

    writes: list[str] = field(default_factory=list)
    idle_timeouts: list[float] = field(default_factory=list)
    target_waits: list[tuple[dict[str, float], float]] = field(default_factory=list)
    feed_override_resets: int = 0

    def prepare(self) -> None:
        pass

    def reset_feed_override(self) -> None:
        self.feed_override_resets += 1

    def write(self, command: str) -> None:
        self.writes.append(command)

    def wait_for_idle(self, *, timeout: float) -> None:
        self.idle_timeouts.append(float(timeout))

    def wait_for_idle_at_targets(
        self,
        targets: dict[str, float],
        *,
        timeout: float,
    ) -> None:
        self.target_waits.append((dict(targets), float(timeout)))


class StageMotionExecution:
    """Execute checked relative and absolute motion plans."""

    def __init__(
        self,
        *,
        safety: MotionSafetyPort,
        limits: MotionLimitPort,
        status: MotionStatusPort,
        serial: MotionSerialPort,
        cancellation: MotionCancellationPort,
        timing: MotionTiming | None = None,
    ) -> None:
        self._safety = safety
        self._limits = limits
        self._status = status
        self._serial = serial
        self._cancellation = cancellation
        self._timing = timing or MotionTiming()

    def run_relative(self, plan: RelativeMotionPlan) -> None:
        move = plan.move
        if move.is_zero():
            return
        moved_axes = tuple(
            axis for axis, delta in move.items() if abs(delta) >= 1e-6
        )
        self._cancellation.check()
        if not plan.ignore_needle_safety:
            self._safety.check()
        if not self._safety.disabled():
            self._limits.ensure(moved_axes)
            limited_axes = self._relative_status_axes(move)
            if self._relative_status_required(move):
                status = self._status.read(limited_axes)
                self.check_relative_limits(
                    move,
                    status=status,
                    allow_relative=plan.allow_relative,
                )
        move_parts = self._move_parts(move)
        if not move_parts:
            return
        feedrate = clamped_motion_feedrate(
            plan.feedrate,
            default_feedrate=self._timing.default_feedrate,
            min_feedrate=self._timing.min_feedrate,
        )
        distance = move_distance_for_timeout(move)
        g1_command = (
            "G1 " + " ".join(move_parts) + f" F{format_gcode_value(feedrate)}"
        )
        self._serial.reset_feed_override()
        if plan.as_jog:
            self._serial.write(
                "$J=G91 G21 "
                + " ".join(move_parts)
                + f" F{format_gcode_value(feedrate)}"
            )
            self._relative_motion_started(plan, feedrate)
            if plan.wait_for_completion:
                self._serial.wait_for_idle(
                    timeout=self._timing.idle_timeout(distance, feedrate)
                )
            return
        self._serial.write("G21")
        self._serial.write("G91")
        self._serial.write(g1_command)
        self._relative_motion_started(plan, feedrate)
        self._serial.write("G90")
        if plan.wait_for_completion:
            self._serial.wait_for_idle(
                timeout=self._timing.idle_timeout(distance, feedrate)
            )

    def run_absolute(self, plan: AbsoluteMotionPlan) -> None:
        targets = ordered_absolute_axis_targets(
            plan.targets,
            axis_order=plan.axis_order,
        )
        if not targets:
            return
        self._serial.prepare()
        self._cancellation.check()
        if not plan.ignore_needle_safety:
            self._safety.check()
        current_values: dict[str, float] = {}
        if not self._safety.disabled():
            axes = tuple(targets)
            self._limits.ensure(axes)
            status = self._status.read(axes)
            current_values = self._validate_absolute_targets(
                targets,
                status=status,
                allow_unhomed=plan.allow_unhomed,
            )
        feedrate = clamped_motion_feedrate(
            plan.feedrate,
            default_feedrate=self._timing.default_feedrate,
            min_feedrate=self._timing.min_feedrate,
        )
        distance = absolute_move_distance_for_timeout(targets, current_values)
        timeout = self._timing.idle_timeout(distance, feedrate)
        if plan.as_jog:
            self._serial.write(
                absolute_axis_targets_jog_command(
                    targets,
                    feedrate,
                    axis_order=plan.axis_order,
                    machine_position_mode=plan.machine_position_mode,
                )
            )
            self._absolute_motion_started(plan)
            if plan.wait_for_completion:
                self._serial.wait_for_idle_at_targets(targets, timeout=timeout)
            return
        self._serial.write("G21")
        self._serial.write("G90")
        self._serial.reset_feed_override()
        self._serial.write(absolute_axis_g1_command(targets, feedrate))
        self._absolute_motion_started(plan)
        if plan.wait_for_completion:
            self._serial.wait_for_idle(timeout=timeout)

    def validate_absolute(
        self,
        plan: AbsoluteMotionPlan,
        *,
        status: _Status | None = None,
    ) -> None:
        targets = ordered_absolute_axis_targets(
            plan.targets,
            axis_order=plan.axis_order,
        )
        if not targets or self._safety.disabled():
            return
        axes = tuple(targets)
        self._limits.ensure(axes)
        if status is None:
            status = self._status.read(axes)
        self._validate_absolute_targets(
            targets,
            status=status,
            allow_unhomed=plan.allow_unhomed,
        )

    def write_relative_unchecked(
        self,
        move: MoveVector,
        *,
        feedrate: float | None = None,
    ) -> None:
        move_parts = self._move_parts(move)
        if not move_parts:
            return
        effective_feedrate = clamped_motion_feedrate(
            feedrate,
            default_feedrate=self._timing.default_feedrate,
            min_feedrate=self._timing.min_feedrate,
        )
        self._serial.write(
            "G1 "
            + " ".join(move_parts)
            + f" F{format_gcode_value(effective_feedrate)}"
        )

    def check_relative_limits(
        self,
        move: MoveVector,
        *,
        status: _Status | None = None,
        allow_relative: bool = False,
    ) -> None:
        axis_limits = self._limits.axis_limits()
        if not axis_limits and abs(move.b) < 1e-6:
            return
        if status is None:
            moved_axes = self._relative_status_axes(move)
            status = self._status.read(moved_axes)
        positions = self._status.position(status)
        if status is None or not positions:
            return
        self.ensure_b_axis_zero_reference(status)
        axis_indexes = {axis: index for index, axis in enumerate(AXIS_ORDER)}
        for axis, delta in move.items():
            if abs(delta) < 1e-6:
                continue
            index = axis_indexes.get(axis)
            if index is None or index >= len(positions):
                continue
            if axis == "B":
                self._check_relative_b_limit(status, delta)
                continue
            self._check_relative_linear_limit(
                status=status,
                positions=positions,
                axis=axis,
                index=index,
                delta=delta,
                allow_relative=allow_relative,
            )

    def _check_relative_b_limit(self, status: _Status, delta: float) -> None:
        current_b = self.relative_b_position(status)
        limit = self._timing.b_axis_soft_limit_deg
        target_b = current_b + delta
        if target_b < -limit or target_b > limit:
            raise StageControllerError(
                f"B move {delta:+.3f} exceeds software limit "
                f"({-limit:.3f}, {limit:.3f}) relative to B zero."
            )

    def _check_relative_linear_limit(
        self,
        *,
        status: _Status,
        positions: tuple[float, ...],
        axis: str,
        index: int,
        delta: float,
        allow_relative: bool,
    ) -> None:
        limits = self._limits.configured_limits(axis, status)
        if not limits or not self._limits.software_limit_ready(status, axis):
            return
        self._limits.require_homed(
            status,
            {axis},
            allow_relative=allow_relative,
        )
        min_value, max_value = limits
        target = positions[index] + delta
        if target < min_value or target > max_value:
            raise StageControllerError(
                f"{axis} move {delta:+.3f} exceeds limits "
                f"({min_value:.3f}, {max_value:.3f})."
            )

    def require_position_for_absolute_motion(
        self,
        status: _Status,
        *,
        required_axes: int,
    ) -> tuple[float, ...]:
        position = self._status.position(status)
        if position is None or len(position) < required_axes:
            raise StageControllerError(
                "Controller did not report a complete position for absolute motion."
            )
        return tuple(float(value) for value in position[:required_axes])

    def ensure_b_axis_zero_reference(self, status: _Status) -> None:
        if self._limits.b_zero_position() is not None:
            return
        if self._status.axis_value(status, "B") is None:
            return
        self.set_b_axis_zero_reference(status, emit_status=False)

    def set_b_axis_zero_reference(
        self,
        status: _Status,
        *,
        emit_status: bool = True,
    ) -> None:
        b_position = self._status.axis_value(status, "B")
        if b_position is None:
            raise StageControllerError("B axis position unavailable.")
        self._limits.set_b_zero_position(b_position, emit_status=emit_status)

    def relative_b_position(self, status: _Status) -> float:
        b_position = self._status.axis_value(status, "B")
        if b_position is None:
            raise StageControllerError("B axis position unavailable.")
        self.ensure_b_axis_zero_reference(status)
        zero = self._limits.b_zero_position()
        if zero is None:
            raise StageControllerError("B zero reference is not initialized.")
        return b_position - zero

    @staticmethod
    def move_vector_for_axis(axis: str, delta: float) -> MoveVector:
        try:
            field_name = axis.lower().strip()
            if field_name not in {"x", "y", "z", "a", "b", "c"}:
                raise KeyError(axis)
            return MoveVector(**{field_name: delta})
        except KeyError as exc:
            raise StageControllerError(f"Unsupported axis: {axis}") from exc

    def _validate_absolute_targets(
        self,
        targets: dict[str, float],
        *,
        status: _Status | None,
        allow_unhomed: bool,
    ) -> dict[str, float]:
        if status is None:
            raise StageControllerError("Unable to read position for absolute move.")
        self._limits.require_homed(
            status,
            set(targets),
            allow_relative=allow_unhomed,
        )
        current_values: dict[str, float] = {}
        for axis, value in targets.items():
            current_value = self._status.axis_value(status, axis)
            if current_value is not None:
                current_values[axis] = float(current_value)
            limits = self._limits.configured_limits(axis, status)
            if limits and self._limits.software_limit_ready(status, axis):
                error = absolute_axis_target_limit_error(axis, value, limits)
                if error is not None:
                    raise StageControllerError(error)
        return current_values

    @staticmethod
    def _move_parts(move: MoveVector) -> list[str]:
        return [
            f"{axis}{format_gcode_value(value, decimals=6)}"
            for axis, value in move.items()
            if abs(value) >= 1e-6
        ]

    def _relative_status_axes(self, move: MoveVector) -> tuple[str, ...]:
        axis_limits = self._limits.axis_limits()
        return tuple(
            axis
            for axis, delta in move.items()
            if abs(delta) >= 1e-6 and (axis == "B" or axis in axis_limits)
        )

    def _relative_status_required(self, move: MoveVector) -> bool:
        return bool(self._limits.axis_limits()) or abs(move.b) >= 1e-6

    @staticmethod
    def _relative_motion_started(plan: RelativeMotionPlan, feedrate: float) -> None:
        if plan.motion_started_callback is not None:
            plan.motion_started_callback(plan.move, feedrate)

    @staticmethod
    def _absolute_motion_started(plan: AbsoluteMotionPlan) -> None:
        if plan.motion_started_callback is not None:
            plan.motion_started_callback()


__all__ = [
    "AbsoluteMotionPlan",
    "FluidNCMotionSerialAdapter",
    "MotionTiming",
    "RecordingMotionSerialAdapter",
    "RelativeMotionPlan",
    "StageMotionCancellationAdapter",
    "StageMotionExecution",
    "StageMotionLimitAdapter",
    "StageMotionSafetyAdapter",
    "StageMotionStatusAdapter",
]
