"""Accumulate exact Step targets before issuing one stage move."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Sequence

from probe_station_gui.stage.coordinate_targets import (
    CoordinateMoveCompletion,
    CoordinateMoveRequest,
)


DEFAULT_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")


class ExactStepClearReason(Enum):
    CONTROL_MODE_CHANGED = auto()
    HOMING_REQUESTED = auto()
    MANUAL_TERMINAL_COMMAND = auto()
    MANUAL_JOG_STARTED = auto()
    COORDINATE_MODE_CHANGED = auto()
    SETTINGS_CHANGED = auto()
    ALIGNMENT_CHANGED = auto()
    APPLICATION_CLOSED = auto()
    CONTROLLER_RESET_REQUESTED = auto()
    CANCEL_REQUESTED = auto()


@dataclass(frozen=True)
class ExactStepRequest:
    move_request: CoordinateMoveRequest
    motion_lease: object | None
    allow_pose_rebase: bool = False
    pending_targets: tuple[tuple[str, float, float], ...] = ()


@dataclass(frozen=True)
class ExactStepOutcome:
    accepted: bool
    pending_targets: tuple[tuple[str, float, float], ...]
    motion_lease: object | None
    window_started: bool


@dataclass(frozen=True)
class ExactStepQueueDecision:
    outcome: ExactStepOutcome
    removed_axes: frozenset[str]


@dataclass(frozen=True)
class ExactStepDispatchDecision:
    move_request: CoordinateMoveRequest | None
    clear_workflow: bool


@dataclass(frozen=True)
class ExactStepCompletionDecision:
    clear_workflow: bool
    removed_axes: frozenset[str]
    dispatch_followup: bool


@dataclass
class ExactStepAccumulator:
    axis_names: Sequence[str] = DEFAULT_AXIS_NAMES
    targets: dict[str, float] = field(default_factory=dict)

    @property
    def has_targets(self) -> bool:
        return bool(self.targets)

    def add(self, axis_name: str, delta: float, *, baseline: float) -> float:
        return self.add_relative(axis_name, delta, baseline=baseline)

    def add_relative(self, axis_name: str, delta: float, *, baseline: float) -> float:
        axis = self._validated_axis(axis_name)
        base = self.targets.get(axis, self._validated_value(baseline))
        target = self._validated_value(float(base) + float(delta))
        self.targets[axis] = target
        return target

    def set_absolute(self, axis_name: str, target: float) -> float:
        axis = self._validated_axis(axis_name)
        value = self._validated_value(target)
        self.targets[axis] = value
        return value

    def drain(self) -> dict[str, float]:
        snapshot = dict(self.targets)
        self.targets.clear()
        return snapshot

    def clear(self) -> None:
        self.targets.clear()

    def _validated_axis(self, axis_name: str) -> str:
        axis = str(axis_name).strip().upper()
        if axis not in self.axis_names:
            raise ValueError(f"Unsupported stage axis: {axis_name!r}")
        return axis

    @staticmethod
    def _validated_value(value: float) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Exact Step target must be finite.")
        return result


@dataclass
class ExactStepWorkflowState:
    axis_names: Sequence[str] = DEFAULT_AXIS_NAMES
    accumulator: ExactStepAccumulator = field(init=False)
    pending_axes: set[str] = field(default_factory=set)
    motion_lease: object | None = None
    pose_rebase_allowed: bool = False
    window_elapsed: bool = False
    move_request: CoordinateMoveRequest | None = None
    move_in_flight: bool = False

    def __post_init__(self) -> None:
        self.accumulator = ExactStepAccumulator(self.axis_names)

    def queue(
        self,
        request: ExactStepRequest,
        *,
        window_active: bool,
    ) -> ExactStepQueueDecision:
        if not isinstance(request, ExactStepRequest):
            raise TypeError("request must be an ExactStepRequest")
        move_request = request.move_request
        if not isinstance(move_request, CoordinateMoveRequest):
            raise TypeError("move_request must be a CoordinateMoveRequest")
        pending_targets = request.pending_targets or move_request.targets
        if not pending_targets:
            return ExactStepQueueDecision(
                outcome=ExactStepOutcome(
                    accepted=False,
                    pending_targets=(),
                    motion_lease=self.motion_lease,
                    window_started=False,
                ),
                removed_axes=frozenset(),
            )
        previous_request = self.move_request
        same_basis = bool(
            previous_request is None
            or previous_request.display_basis == move_request.display_basis
        )
        if self.motion_lease is None:
            self.motion_lease = request.motion_lease
        elif request.allow_pose_rebase and same_basis:
            self.motion_lease = request.motion_lease
            self.pose_rebase_allowed = True
        normalized = tuple(
            (str(axis).strip().upper(), float(raw), float(display))
            for axis, raw, display in pending_targets
        )
        next_axes = {axis for axis, _raw, _display in normalized}
        removed_axes = frozenset(self.pending_axes - next_axes)
        self.accumulator.clear()
        for axis, _raw_target, display_target in normalized:
            self.accumulator.set_absolute(axis, display_target)
        self.pending_axes = next_axes
        self.move_request = move_request
        window_started = not window_active and not self.window_elapsed
        return ExactStepQueueDecision(
            outcome=ExactStepOutcome(
                accepted=True,
                pending_targets=normalized,
                motion_lease=self.motion_lease,
                window_started=window_started,
            ),
            removed_axes=removed_axes,
        )

    def mark_window_elapsed(self) -> None:
        self.window_elapsed = True

    def begin_dispatch(
        self,
        *,
        coordinate_active: bool,
        controller_busy: bool,
    ) -> ExactStepDispatchDecision:
        if not self.window_elapsed or coordinate_active or controller_busy:
            return ExactStepDispatchDecision(None, False)
        if self.move_request is None or not self.accumulator.has_targets:
            return ExactStepDispatchDecision(None, True)
        move_request = self.move_request
        self.accumulator.drain()
        self.window_elapsed = False
        self.move_request = None
        return ExactStepDispatchDecision(move_request, False)

    def mark_move_started(self, move_request: CoordinateMoveRequest) -> None:
        self.move_in_flight = True
        self.pending_axes.difference_update(
            str(axis).strip().upper() for axis, _raw, _display in move_request.targets
        )

    def complete(
        self,
        completion: CoordinateMoveCompletion,
    ) -> ExactStepCompletionDecision:
        if not (
            self.move_in_flight
            or self.move_request is not None
            or self.accumulator.has_targets
        ):
            return ExactStepCompletionDecision(False, frozenset(), False)
        self.move_in_flight = False
        if not completion.success:
            return ExactStepCompletionDecision(True, frozenset(), False)
        removed_axes: set[str] = set()
        if (
            self.move_request is not None
            and completion.display_basis == self.move_request.display_basis
        ):
            for axis, reached_target in completion.display_targets:
                pending_target = self.accumulator.targets.get(axis)
                if pending_target is None:
                    continue
                if abs(float(pending_target) - float(reached_target)) > 1e-12:
                    continue
                self.accumulator.targets.pop(axis, None)
                self.pending_axes.discard(axis)
                removed_axes.add(axis)
            if not self.accumulator.has_targets:
                self.move_request = None
        clear_workflow = self.move_request is None
        return ExactStepCompletionDecision(
            clear_workflow,
            frozenset(removed_axes),
            not clear_workflow,
        )

    def clear(self) -> frozenset[str]:
        removed_axes = frozenset(self.pending_axes)
        self.accumulator.clear()
        self.pending_axes.clear()
        self.motion_lease = None
        self.pose_rebase_allowed = False
        self.window_elapsed = False
        self.move_request = None
        self.move_in_flight = False
        return removed_axes


__all__ = [
    "ExactStepAccumulator",
    "ExactStepClearReason",
    "ExactStepOutcome",
    "ExactStepRequest",
    "ExactStepWorkflowState",
]
