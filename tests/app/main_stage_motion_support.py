from __future__ import annotations

import types

from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage.coordinate_targets import (
    CoordinatePendingEdits,
    CoordinateTargetCommonFeedratePlan,
)


class _FakeStageMotion:
    def __init__(
        self,
        controller: object | None = None,
        *,
        axis_names: tuple[str, ...] = ("X", "Y", "Z", "A", "B", "C"),
    ) -> None:
        self.controller = controller
        self.axis_names = axis_names
        self.physical_machine_pose = PhysicalMachinePose.from_mapping({})
        self.cancel_planned_calls = 0
        self.coordinate_active = False
        self.coordinate_display_basis = None
        self.coordinate_display_targets: tuple[tuple[str, float], ...] = ()
        self.coordinate_stage_position: tuple[float, ...] | None = None
        self.coordinate_programmed_feedrate: float | None = None
        self.coordinate_effective_feedrate: float | None = None
        self.coordinate_common_feedrate = CoordinateTargetCommonFeedratePlan(
            clear_common_target=True
        )
        self.active_axes: frozenset[str] = frozenset()
        self.pending: dict[str, tuple[float, float]] = {}
        self.pending_motion_lease: object | None = None
        self.start_requests: list[object] = []
        self.start_result = True
        self.feedrate_updates: list[float] = []
        self.cancel_coordinate_calls = 0

    def snapshot(self) -> object:
        return types.SimpleNamespace(
            physical_machine_pose=self.physical_machine_pose,
            presented_position=None,
            presented_stage_xy=None,
            active_axes=self.active_axes,
            coordinate_active=self.coordinate_active,
            coordinate_display_basis=self.coordinate_display_basis,
            coordinate_display_targets=self.coordinate_display_targets,
            coordinate_stage_position=self.coordinate_stage_position,
            coordinate_programmed_feedrate=self.coordinate_programmed_feedrate,
            coordinate_effective_feedrate=self.coordinate_effective_feedrate,
            coordinate_common_feedrate=self.coordinate_common_feedrate,
            pending_edit_axes=frozenset(self.pending),
            planned_stage_xy=None,
            planned_prediction_active=False,
            planned_waiting_for_fresh_status=False,
        )

    def pending_coordinate_edits(self) -> CoordinatePendingEdits:
        return CoordinatePendingEdits(
            targets=tuple(
                (axis, *self.pending[axis])
                for axis in self.axis_names
                if axis in self.pending
            ),
            motion_lease=self.pending_motion_lease,
        )

    def upsert_pending_coordinate_edit(
        self,
        axis: str,
        raw_target: float,
        display_target: float,
        *,
        motion_lease: object | None,
    ) -> None:
        self.pending[str(axis).upper()] = (float(raw_target), float(display_target))
        self.pending_motion_lease = motion_lease

    def pop_pending_coordinate_edit(self, axis: str) -> tuple[float, float] | None:
        removed = self.pending.pop(str(axis).upper(), None)
        if not self.pending:
            self.pending_motion_lease = None
        return removed

    def clear_pending_coordinate_edits(self) -> bool:
        had_pending = bool(self.pending)
        self.pending.clear()
        self.pending_motion_lease = None
        return had_pending

    def consume_pending_coordinate_edits(self) -> CoordinatePendingEdits:
        pending = self.pending_coordinate_edits()
        self.clear_pending_coordinate_edits()
        return pending

    def start_coordinate_move(self, request: object) -> bool:
        self.start_requests.append(request)
        if self.coordinate_active or not self.start_result:
            return False
        targets = tuple(
            (str(axis).upper(), float(display))
            for axis, _raw, display in request.targets
        )
        raw_targets = {
            str(axis).upper(): float(raw) for axis, raw, _display in request.targets
        }
        for axis in raw_targets:
            self.pending.pop(axis, None)
        if not self.pending:
            self.pending_motion_lease = None
        if self.controller is not None:
            accepted = self.controller.request_absolute_axis_targets_move(
                raw_targets,
                feedrate=float(request.feedrate_mm_min),
            )
            if not accepted:
                return False
        self.coordinate_active = True
        self.active_axes = frozenset(axis for axis, _display in targets)
        self.coordinate_display_targets = targets
        self.coordinate_display_basis = request.display_basis
        self.coordinate_stage_position = request.seed_position
        self.coordinate_programmed_feedrate = float(request.feedrate_mm_min)
        self.coordinate_effective_feedrate = float(request.feedrate_mm_min)
        return True

    def set_coordinate_feedrate(self, feedrate_mm_min: float) -> None:
        self.feedrate_updates.append(float(feedrate_mm_min))

    def discard_coordinate_tracking_for_manual_jog(self) -> bool:
        was_active = self.coordinate_active
        self.coordinate_active = False
        self.active_axes = frozenset()
        return was_active

    def cancel_coordinate_move(self) -> bool:
        if not self.coordinate_active:
            return False
        self.cancel_coordinate_calls += 1
        if self.controller is not None:
            self.controller.cancel_active_motion("Coordinate move cancel requested.")
        self.coordinate_active = False
        self.active_axes = frozenset()
        self.clear_pending_coordinate_edits()
        return True

    def cancel_planned_xy_move(self) -> bool:
        self.cancel_planned_calls += 1
        return True
