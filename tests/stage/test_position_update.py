from __future__ import annotations

import types

from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetConfig,
    CoordinateTargetMoveState,
)
from probe_station_gui.stage.manual_jog_prediction import (
    ManualJogPredictionConfig,
    ManualJogPredictionState,
)
from probe_station_gui.stage import position_update


AXES = ("X", "Y", "Z", "A", "B")


class _StageController:
    def __init__(self) -> None:
        self.state = "idle"
        self.position = (1.0, 2.0, 3.0, 0.0, 0.0)
        self.last_status_time = 10.0
        self.last_jog_write_time = None
        self.homed = {"X", "Y", "Z"}

    def latest_stage_state(self) -> str:
        return self.state

    def axes_are_homed(self, axes: set[str]) -> bool:
        return axes.issubset(self.homed)

    def latest_stage_position(self) -> tuple[float, ...]:
        return self.position

    def last_status_timestamp(self) -> float:
        return self.last_status_time

    def last_jog_write_timestamp(self) -> float | None:
        return self.last_jog_write_time

    def homed_axes(self) -> set[str]:
        return set(self.homed)


class _Owner:
    STAGE_AXIS_NAMES = AXES
    B_POSITION_CHANGE_TOLERANCE_DEG = 0.1
    MANUAL_JOG_RECONCILE_SMOOTH_ALPHA = 0.35

    def __init__(self) -> None:
        self.stage_controller = _StageController()
        self._stage_position_panel = None
        self._stage_axis_fields = {}
        self._stage_unhomed_display_origins = {}
        self._stage_axis_raw_values = {}
        self._stage_axis_display_values = {}
        self._stage_axis_homed = set()
        self._stage_limit_axes = set()
        self._stage_axis_base_styles = {}
        self._pending_stage_axis_targets = {}
        self._stage_motion_axes = {"X"}
        self._stage_motion_blink_dimmed = False
        self._stage_motion_blink_timer = types.SimpleNamespace(isActive=lambda: False)
        self._stage_position_panel = types.SimpleNamespace(
            refresh_axis_styles=lambda _axes, _dimmed: self.calls.append(
                ("clear_motion", None)
            )
        )
        self._coordinate_targets = CoordinateTargetMoveState(
            CoordinateTargetConfig(
                axis_names=AXES,
                min_feedrate_mm_min=1.0,
                duration_padding_s=0.0,
                min_idle_accept_s=0.0,
                target_tolerance_mm=0.001,
            )
        )
        self._manual_jog_prediction = ManualJogPredictionState(
            ManualJogPredictionConfig(
                axis_names=AXES,
                ignore_idle_after_command_s=0.15,
                reconcile_smooth_threshold_mm=0.01,
                reconcile_smooth_alpha=0.35,
                status_settle_hold_s=0.2,
                default_stop_tail_s=0.05,
                stop_tail_min_s=0.0,
                stop_tail_max_s=0.2,
                stop_tail_learn_alpha=0.2,
            )
        )
        self._planned_move_stage_xy = None
        self._planned_move_started_at = None
        self._planned_move_waiting_for_fresh_status = False
        self._planned_move_stop_status_timestamp = None
        self._pending_alignment_preparation = None
        self._last_reported_b_position = None
        self._design_session = types.SimpleNamespace(
            document=object(),
            registration=types.SimpleNamespace(valid=True),
        )
        self._current_design_stage_xy = (9.0, 9.0)
        self.contact_calibration_window = None
        self.calls: list[tuple[str, object]] = []

    def _coerce_position_tuple(self, value: object) -> tuple[float, ...] | None:
        if not isinstance(value, (tuple, list)):
            return None
        return tuple(float(item) for item in value)

    def _stage_xy_from_position(self, position: object | None) -> tuple[float, float] | None:
        return position_update.stage_xy_from_position(position)

    def _position_with_stage_xy(
        self,
        stage_xy: tuple[float, float],
        *,
        base_position: object | None = None,
    ) -> tuple[float, ...]:
        return position_update.position_with_stage_xy(
            self,
            stage_xy,
            base_position=base_position,
        )

    def _can_display_design_position(self) -> bool:
        return True

    def _maybe_restore_persisted_design(self, position: tuple[float, ...]) -> None:
        self.calls.append(("restore", position))

    def _update_stage_position_display(self, position: object | None) -> None:
        self.calls.append(("display", position))

    def _update_coordinate_display(
        self,
        *,
        center_xy: tuple[float, float] | None = None,
        cursor_xy: tuple[float, float] | None = None,
    ) -> None:
        self.calls.append(("coordinate", center_xy))

    def _update_design_position(self, stage_xy: tuple[float, float] | None) -> None:
        self.calls.append(("design", stage_xy))

    def _invalidate_design_registration(self, message: str) -> None:
        self.calls.append(("invalidate", message))

    def _log_design_position_reconcile(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
    ) -> None:
        self.calls.append(("reconcile", (predicted_stage_xy, actual_stage_xy)))

    def _format_optional_point(self, point: tuple[float, float] | None) -> str:
        return "None" if point is None else f"{point[0]:.3f},{point[1]:.3f}"

    def _finish_coordinate_move_if_idle(self, position: object | None = None) -> None:
        self.calls.append(("finish", position))

    def _publish_stage_position_estimate(
        self,
        position: tuple[float, ...] | None,
    ) -> None:
        self.calls.append(("publish", position))


def test_invalid_stage_position_uses_display_only_owner_seam() -> None:
    owner = _Owner()

    position_update.on_stage_position_changed(owner, ["bad"])

    assert owner.calls == [("display", ["bad"])]


def test_unhomed_fallback_clears_prediction_and_keeps_idle_finish_order() -> None:
    owner = _Owner()
    owner.stage_controller.homed = set()
    owner._manual_jog_prediction.stage_position = (9.0, 9.0, 3.0)
    owner._manual_jog_prediction.stage_xy = (9.0, 9.0)
    owner._planned_move_stage_xy = (8.0, 8.0)

    position_update.on_stage_position_changed(owner, (1.0, 2.0, 3.0))

    assert owner._manual_jog_prediction.stage_position is None
    assert owner._manual_jog_prediction.stage_xy is None
    assert owner._planned_move_stage_xy is None
    assert owner.calls == [
        ("restore", (1.0, 2.0, 3.0)),
        ("display", (1.0, 2.0, 3.0)),
        ("coordinate", None),
        ("design", (1.0, 2.0)),
        ("finish", (1.0, 2.0, 3.0)),
        ("clear_motion", None),
    ]


def test_preferred_design_stage_xy_clears_completed_planned_wait_state() -> None:
    owner = _Owner()
    owner._planned_move_stage_xy = (8.0, 8.0)
    owner._planned_move_waiting_for_fresh_status = True
    owner._planned_move_stop_status_timestamp = 9.0
    owner.stage_controller.last_status_time = 10.0
    owner.stage_controller.position = (1.5, 2.5, 3.0)

    stage_xy = position_update.preferred_design_stage_xy(owner)

    assert stage_xy == (1.5, 2.5)
    assert owner._planned_move_waiting_for_fresh_status is False
    assert owner._planned_move_stop_status_timestamp is None
