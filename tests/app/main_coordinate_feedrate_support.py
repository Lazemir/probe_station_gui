import csv
import json
import subprocess
import struct
import threading
import time
import tempfile
import types
import unittest
from unittest import mock
import zlib

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main()
import main as main_module
from main import Main
from probe_station_gui.design.contact_navigation import api_route_adjusted_stage_xy
from probe_station_gui.dialogs import (
    route_measurement_dialog as route_measurement_dialog_module,
)
from probe_station_gui.dialogs.route_measurement_dialog import (
    RouteMeasurementDialog,
    RouteMeasurementRunConfiguration,
)
from probe_station_gui.route.measurement import (
    RouteExternalMeasurementSessionRunner,
    RouteContactPlacementResult,
    RouteContactHeightRecord,
    RouteContactQualityLimits,
    RouteContactQuality,
    RouteContactSeekResult,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RouteMeasurementRunner,
    RoutePhotoRecord,
)
from probe_station_gui.route.measurement_settings import (
    RouteMeasurementSettingsStore,
)
from probe_station_gui.route.dialog_adapter import (
    RouteMeasurementPointRequestCallbacks,
    request_route_measurement_for_point,
)
from probe_station_gui.instruments.meters.lcr import LCRMeterError, RouteMeterConfiguration
from probe_station_gui.settings.manager import ObjectiveCalibrationSettings, Settings
from probe_station_gui.stage.api_moves import api_move_feedrate
from probe_station_gui.stage.controller import StageControllerError
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetConfig,
    CoordinateTargetMoveState,
)
from probe_station_gui.stage.manual_jog_prediction import (
    ManualJogPredictionConfig,
    ManualJogPredictionState,
)


class _FakeTimer:
    def __init__(self) -> None:
        self.started = False

    def isActive(self) -> bool:
        return False

    def start(self) -> None:
        self.started = True


class _FakeView:
    def __init__(self) -> None:
        self.focus_count = 0
        self.finished_target_motions = 0
        self.cleared_targets = 0

    def setFocus(self, *_args, **_kwargs) -> None:  # noqa: N802 - Qt naming
        self.focus_count += 1

    def finish_target_motion_to_center(self) -> None:
        self.finished_target_motions += 1

    def clear_target_cross(self) -> None:
        self.cleared_targets += 1


class _FakeSignal:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def emit(self, message: str) -> None:
        self.messages.append(str(message))


class _FakeThread:
    instances: list["_FakeThread"] = []

    def __init__(self, *, target, args=(), daemon=None, name=None) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.name = name
        self.started = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True


class _FakeAliveThread:
    def is_alive(self) -> bool:
        return True


class _FakeJoinableThread:
    def __init__(self, *, alive: bool = True) -> None:
        self.alive = bool(alive)
        self.join_calls: list[float | None] = []

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)
        self.alive = False


class _FakeVisibleDialog:
    def __init__(self, visible: bool = True) -> None:
        self.visible = bool(visible)
        self.running: list[bool] = []
        self.waiting: list[tuple[bool, str]] = []
        self.statuses: list[str] = []
        self.progress: list[int] = []

    def isVisible(self) -> bool:  # noqa: N802 - Qt naming
        return self.visible

    def set_running(self, value: bool) -> None:
        self.running.append(bool(value))

    def set_pause_request_pending(self, _value: bool) -> None:
        pass

    def set_waiting(self, value: bool, reason: str = "") -> None:
        self.waiting.append((bool(value), str(reason)))

    def set_status(self, message: str) -> None:
        self.statuses.append(str(message))

    def reset_progress(self, total: int) -> None:
        self.progress.append(int(total))


class _FakeRouteDialogOpenState:
    def __init__(self, *, configuration: object | None = None) -> None:
        self.configuration = configuration or types.SimpleNamespace(current_point=4)
        self.route_updates: list[dict[str, object]] = []
        self.session_active_calls: list[tuple[bool, bool]] = []
        self.current_point_calls: list[tuple[int, bool]] = []
        self.running_calls: list[bool] = []
        self.waiting_calls: list[bool] = []
        self.status_calls: list[str] = []
        self.show_count = 0
        self.raise_count = 0
        self.activate_count = 0

    def set_route(self, **kwargs: object) -> None:
        self.route_updates.append(dict(kwargs))

    def set_measurement_session_active(
        self,
        active: bool,
        *,
        save: bool = True,
    ) -> None:
        self.session_active_calls.append((bool(active), bool(save)))

    def measurement_session_active(self) -> bool:
        return bool(self.session_active_calls and self.session_active_calls[-1][0])

    def set_current_point(self, point_number: int, *, save: bool = True) -> None:
        self.current_point_calls.append((int(point_number), bool(save)))

    def set_running(self, running: bool) -> None:
        self.running_calls.append(bool(running))

    def set_waiting(self, waiting: bool, reason: str = "") -> None:
        _ = reason
        self.waiting_calls.append(bool(waiting))

    def set_status(self, message: str) -> None:
        self.status_calls.append(str(message))

    def show(self) -> None:
        self.show_count += 1

    def raise_(self) -> None:
        self.raise_count += 1

    def activateWindow(self) -> None:  # noqa: N802 - Qt naming
        self.activate_count += 1

    def current_configuration(self) -> object:
        return self.configuration


class _FakeRouteMeasurementRunner:
    def __init__(self) -> None:
        self.confirmations: list[str] = []
        self.correction_requested = False
        self.stop_requested = False

    def submit_confirmation(self, action: str) -> bool:
        self.confirmations.append(str(action))
        return True

    def request_current_point_correction(self) -> None:
        self.correction_requested = True

    def stop(self) -> None:
        self.stop_requested = True


class _FakeButton:
    def __init__(self) -> None:
        self.text = ""
        self.enabled = False

    def setText(self, text: str) -> None:
        self.text = str(text)

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)


class _FakeLineEdit:
    def __init__(
        self,
        *,
        text: str = "",
        enabled: bool = True,
        modified: bool = False,
        has_focus: bool = False,
    ) -> None:
        self._text = str(text)
        self.enabled = bool(enabled)
        self.modified = bool(modified)
        self.has_focus = bool(has_focus)
        self.tool_tip = ""
        self.blocked_signals: list[bool] = []
        self.enabled_calls: list[bool] = []
        self.modified_calls: list[bool] = []
        self.clear_count = 0
        self.styles: list[tuple[str, str]] = []

    def blockSignals(self, blocked: bool) -> None:  # noqa: N802 - Qt naming
        self.blocked_signals.append(bool(blocked))

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt naming
        self.enabled = bool(enabled)
        self.enabled_calls.append(self.enabled)

    def isEnabled(self) -> bool:  # noqa: N802 - Qt naming
        return self.enabled

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._text = str(text)

    def text(self) -> str:
        return self._text

    def setModified(self, modified: bool) -> None:  # noqa: N802 - Qt naming
        self.modified = bool(modified)
        self.modified_calls.append(self.modified)

    def isModified(self) -> bool:  # noqa: N802 - Qt naming
        return self.modified

    def setToolTip(self, text: str) -> None:  # noqa: N802 - Qt naming
        self.tool_tip = str(text)

    def hasFocus(self) -> bool:  # noqa: N802 - Qt naming
        return self.has_focus

    def clear(self) -> None:
        self._text = ""
        self.clear_count += 1

    def setStyleSheet(self, _style: str) -> None:  # noqa: N802 - Qt naming
        pass


class _FakeFrame:
    def __init__(self, name: str) -> None:
        self.name = name

    def copy(self) -> "_FakeFrame":
        return _FakeFrame(self.name)


class _FakeStagePositionPanel:
    def __init__(
        self,
        axis_fields: dict[str, "_FakeLineEdit"],
        *,
        apply_button: _FakeButton | None = None,
        cancel_button: _FakeButton | None = None,
    ) -> None:
        self.axis_fields = axis_fields
        self.apply_button = apply_button
        self.cancel_button = cancel_button
        self.pending_targets: dict[str, tuple[float, float]] = {}
        self.base_styles: dict[str, tuple[str, str]] = {}
        self.return_commits: set[str] = set()
        self.is_programmatic_update = False
        self.display_plans: list[object] = []
        self.available_calls: list[bool] = []
        self.action_button_states: list[tuple[bool, bool]] = []
        self.cleared_display_values: list[dict[str, float]] = []
        self.pending_only_clear_count = 0

    def field(self, axis: str) -> _FakeLineEdit | None:
        return self.axis_fields.get(str(axis).strip().upper())

    def apply_display_plan(self, plan: object) -> None:
        self.display_plans.append(plan)
        for axis_plan in getattr(plan, "axis_updates", ()):
            field = self.axis_fields[axis_plan.axis]
            self.base_styles[axis_plan.axis] = (
                axis_plan.base_background,
                axis_plan.base_foreground,
            )
            field.setEnabled(True)
            if not field.hasFocus():
                field.setText(main_module.format_stage_axis_value(axis_plan.visible_value))
                field.setModified(False)
            field.setToolTip(axis_plan.tooltip)
            field.styles.append(
                (
                    str(axis_plan.base_background),
                    str(axis_plan.base_foreground),
                )
            )
        for axis_name in getattr(plan, "missing_axes", ()):
            field = self.axis_fields[axis_name]
            self.base_styles.pop(axis_name, None)
            self.pending_targets.pop(axis_name, None)
            field.clear()
            field.setEnabled(False)
            field.setModified(False)
            field.styles.append(("#e6e6e6", "#666666"))

    def set_fields_available(self, available: bool) -> None:
        self.available_calls.append(bool(available))

    def refresh_axis_styles(self, motion_axes: object, motion_blink_dimmed: bool) -> None:
        normalized_motion_axes = {
            str(axis).strip().upper() for axis in motion_axes if str(axis).strip()
        }
        for axis_name, field in self.axis_fields.items():
            if axis_name not in self.base_styles:
                continue
            background, foreground = self.base_styles[axis_name]
            if axis_name in self.pending_targets:
                field.styles.append(("#d7b8ff", "#1f1233"))
            elif axis_name in normalized_motion_axes and motion_blink_dimmed:
                field.styles.append(
                    (main_module.Main.STAGE_AXIS_DIMMED_BACKGROUNDS[background], foreground)
                )
            else:
                field.styles.append((background, foreground))

    def has_pending_or_modified_fields(self) -> bool:
        return bool(self.pending_targets) or any(
            field.isEnabled() and field.isModified()
            for field in self.axis_fields.values()
        )

    def set_action_buttons_enabled(
        self,
        apply_enabled: bool,
        cancel_enabled: bool,
    ) -> None:
        if self.apply_button is not None:
            self.apply_button.setEnabled(apply_enabled)
        if self.cancel_button is not None:
            self.cancel_button.setEnabled(cancel_enabled)
        self.action_button_states.append((bool(apply_enabled), bool(cancel_enabled)))

    def clear_pending_targets(self, display_values: dict[str, float]) -> bool:
        had_changes = self.has_pending_or_modified_fields()
        self.pending_targets.clear()
        self.return_commits.clear()
        self.cleared_display_values.append(dict(display_values))
        for axis_name, field in self.axis_fields.items():
            if axis_name in display_values:
                field.setText(main_module.format_stage_axis_value(display_values[axis_name]))
            else:
                field.clear()
            field.setModified(False)
        return had_changes

    def clear_pending_target_state(self) -> bool:
        had_changes = self.has_pending_or_modified_fields()
        self.pending_targets.clear()
        self.return_commits.clear()
        self.pending_only_clear_count += 1
        return had_changes


class _FakeJoystick:
    def __init__(self, current_feedrate: float) -> None:
        self._current_feedrate = float(current_feedrate)
        self.coordinate_feedrate: float | None = None
        self.coordinate_axes: list[tuple[str, ...]] = []
        self.coordinate_modes: list[str] = []
        self.common_targets: list[tuple[float, float]] = []
        self.common_cleared = 0
        self.needle_contacts: list[tuple[str, float | None]] = []
        self.mode = "jog"
        self.mode_changes: list[tuple[str, bool]] = []

    def current_linear_feedrate(self) -> float:
        return self._current_feedrate

    def set_control_mode(self, mode: str, *, emit_changed: bool = True) -> bool:
        self.mode = str(mode).strip().lower()
        self.mode_changes.append((self.mode, bool(emit_changed)))
        return True

    def select_coordinate_feedrate_for_axes(self, axes: object) -> float:
        normalized = tuple(str(axis).strip().upper() for axis in axes)
        self.coordinate_axes.append(normalized)
        self.coordinate_modes.append(self.mode)
        if self.coordinate_feedrate is not None:
            return float(self.coordinate_feedrate)
        return self._current_feedrate

    def set_common_feedrate_target(self, feedrate: float, max_feedrate: float) -> None:
        self.common_targets.append((float(feedrate), float(max_feedrate)))

    def clear_common_feedrate_target(self) -> None:
        self.common_cleared += 1

    def clear_temporary_linear_feedrate_bounds(self) -> None:
        pass

    def set_needle_contact_coordinate(
        self,
        action: str,
        value: float | None,
    ) -> None:
        self.needle_contacts.append((action, value))


class _FakeStageController:
    FEED_OVERRIDE_MIN_PERCENT = 10
    FEED_OVERRIDE_MAX_PERCENT = 200

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, float], float | None]] = []
        self.jog_stops = 0
        self.next_absolute_jog_accept = True
        self.busy = False
        self.latest_state = "Idle"
        self.latest_position = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        self.last_status_time: float | None = time.monotonic()
        self.cancelled_tasks: list[str] = []
        self.cancelled_motions: list[str] = []
        self.needle_calibrations: list[dict[str, float | None]] = []
        self.home_all_requests = 0
        self.home_axis_requests: list[str] = []
        self.absolute_jog_replace_flags: list[bool] = []
        self.status_message = _FakeSignal()

    def request_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        feedrate: float | None = None,
    ) -> bool:
        self.requests.append((dict(targets), feedrate))
        return True

    def queue_jog_stop(self) -> None:
        self.jog_stops += 1

    def queue_absolute_axis_targets_jog(
        self,
        targets: dict[str, float],
        *,
        feedrate: float,
        replace_active: bool = False,
    ) -> bool:
        self.absolute_jog_replace_flags.append(bool(replace_active))
        if not self.next_absolute_jog_accept:
            return False
        self.queue_jog_stop()
        self.requests.append((dict(targets), feedrate))
        return True

    def queue_feed_override_reset(self) -> int:
        return 100

    def is_busy(self) -> bool:
        return self.busy

    def latest_stage_state(self) -> str:
        return self.latest_state

    def latest_stage_position(self) -> tuple[float, ...]:
        return self.latest_position

    def last_status_timestamp(self) -> float | None:
        return self.last_status_time

    def request_home_all(self) -> bool:
        self.home_all_requests += 1
        return True

    def request_home_axis(self, axis: str) -> bool:
        self.home_axis_requests.append(str(axis).upper())
        return True

    def axis_max_feedrates(self) -> dict[str, float]:
        return {"X": 100.0, "Y": 150.0, "Z": 80.0, "A": 70.0, "B": 60.0}

    def cancel_active_task(self, reason: str) -> None:
        self.cancelled_tasks.append(reason)

    def cancel_active_motion(self, reason: str) -> None:
        self.cancelled_motions.append(reason)

    def apply_needle_calibration(
        self,
        *,
        raise_position_mm: float | None,
        down_position_mm: float | None,
        contact_zone_mm: float | None = None,
    ) -> None:
        self.needle_calibrations.append(
            {
                "raise_position_mm": raise_position_mm,
                "down_position_mm": down_position_mm,
                "contact_zone_mm": contact_zone_mm,
            }
        )

    def axis_a_configured_coordinate_for_lowering(self, lowering_mm: float) -> float:
        return float(lowering_mm)

    def calibrated_axis_display_value(self, axis: str, raw_value: float) -> float:
        self._last_display_axis = axis
        return float(raw_value)

    def axis_a_lowering_for_configured_coordinate(self, coordinate: float) -> float:
        return float(coordinate)


class _FakeSettingsManager:
    def __init__(self) -> None:
        self.settings = Settings()
        self.saved_count = 0
        self.replaced_settings: list[Settings] = []

    def replace(self, settings: Settings) -> None:
        self.settings = settings
        self.replaced_settings.append(settings)

    def save(self) -> None:
        self.saved_count += 1


def _coordinate_target_state() -> CoordinateTargetMoveState:
    return CoordinateTargetMoveState(
        CoordinateTargetConfig(
            axis_names=Main.STAGE_AXIS_NAMES,
            min_feedrate_mm_min=Main.MIN_FEEDRATE_MM_MIN,
            duration_padding_s=Main.PLANNED_MOVE_DURATION_PADDING_S,
            min_idle_accept_s=Main.COORDINATE_MOVE_MIN_IDLE_ACCEPT_S,
            target_tolerance_mm=Main.COORDINATE_MOVE_TARGET_TOLERANCE_MM,
        )
    )


def _make_main(current_feedrate: float = 120.0) -> tuple[
    Main,
    _FakeStageController,
    _FakeJoystick,
    _FakeTimer,
    list[str],
]:
    window = Main.__new__(Main)
    stage_controller = _FakeStageController()
    joystick = _FakeJoystick(current_feedrate)
    timer = _FakeTimer()
    statuses: list[str] = []
    estimates: list[tuple[float, ...]] = []

    window.stage_controller = stage_controller
    window.joystick_panel = joystick
    window.serial_terminal_panel = None
    window._pending_stage_axis_targets = {}
    window._stage_axis_fields = {}
    window._coordinate_targets = _coordinate_target_state()
    window._pending_homing_axes = []
    window._pending_click_to_move = None
    window._pending_planned_move_target_xy = None
    window._pending_planned_move_source_label = None
    window._planned_move_started_at = None
    window._planned_move_waiting_for_fresh_status = False
    window._pending_alignment_preparation = None
    window._pending_quick_alignment_rotation = False
    window._current_design_stage_xy = None
    window._manual_jog_prediction = ManualJogPredictionState(
        ManualJogPredictionConfig(
            axis_names=Main.STAGE_AXIS_NAMES,
            ignore_idle_after_command_s=Main.MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S,
            reconcile_smooth_threshold_mm=Main.MANUAL_JOG_RECONCILE_SMOOTH_THRESHOLD_MM,
            reconcile_smooth_alpha=Main.MANUAL_JOG_RECONCILE_SMOOTH_ALPHA,
            status_settle_hold_s=Main.MANUAL_JOG_STATUS_SETTLE_HOLD_S,
            default_stop_tail_s=Main.MANUAL_JOG_DEFAULT_STOP_TAIL_S,
            stop_tail_min_s=Main.MANUAL_JOG_STOP_TAIL_MIN_S,
            stop_tail_max_s=Main.MANUAL_JOG_STOP_TAIL_MAX_S,
            stop_tail_learn_alpha=Main.MANUAL_JOG_STOP_TAIL_LEARN_ALPHA,
        )
    )
    window._manual_jog_timer = timer
    window._seed_motion_prediction_position = lambda: (
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    window._stage_axis_target_limit_error = lambda _axis, _target: None
    window.view = _FakeView()

    def position_with_axis_values(
        raw_targets: dict[str, float],
        *,
        base_position: tuple[float, ...],
    ) -> tuple[float, ...]:
        values = list(base_position)
        for axis, value in raw_targets.items():
            values[Main.STAGE_AXIS_NAMES.index(axis)] = float(value)
        return tuple(values)

    window._position_with_axis_values = position_with_axis_values
    panel = _FakeStagePositionPanel(window._stage_axis_fields)
    window._stage_position_panel = panel
    window._pending_stage_axis_targets = panel.pending_targets
    window._stage_axis_base_styles = panel.base_styles
    window._stage_axis_return_commits = panel.return_commits
    window._set_stage_motion_axes = lambda axes: setattr(
        window, "_motion_axes", set(axes)
    )
    window._update_stage_coordinate_apply_state = lambda: None
    window._refresh_stage_axis_styles = lambda: None
    window._clear_stage_motion_axes = lambda: None
    window._clear_planned_move_prediction = lambda *, clear_wait_state: None
    window._schedule_status_refreshes = lambda _delays: None
    window._schedule_cancel_state_refresh = lambda: None
    window._show_status = (
        lambda message, _timeout_ms=None: statuses.append(str(message))
    )
    window._publish_stage_position_estimate = (
        lambda position: estimates.append(tuple(float(value) for value in position))
    )
    window._design_xy_from_raw_stage_xy = lambda _stage_xy: None
    return window, stage_controller, joystick, timer, statuses


class _RouteStartEmit:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.calls.append(tuple(args))


class _RouteStartLcr:
    def __init__(
        self,
        *,
        connected: bool = True,
        error: main_module.LCRMeterError | None = None,
    ) -> None:
        self.connected = bool(connected)
        self.error = error
        self.configurations: list[RouteMeterConfiguration] = []
        self.runtime_configurations: list[RouteMeterConfiguration] = []

    def is_connected(self) -> bool:
        return self.connected

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.configurations.append(configuration)

    def apply_route_meter_runtime_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.runtime_configurations.append(configuration)


def _route_start_point() -> RouteMeasurementPoint:
    return RouteMeasurementPoint(
        index=1,
        point_id="p001",
        label="P001",
        design_center=(100.0, 200.0),
        stage_xy=(1.0, 2.0),
        needle_1_design=(101.0, 201.0),
        needle_2_design=(99.0, 199.0),
    )


def _route_start_configuration(
    *,
    operation_mode: str = route_measurement_dialog_module.ROUTE_OPERATION_MEASURE,
    photo_autofocus_enabled: bool = False,
) -> RouteMeasurementRunConfiguration:
    return RouteMeasurementRunConfiguration(
        csv_path="route.csv",
        previous_csv_path="route.csv",
        operation_mode=operation_mode,
        photo_output_dir="photos",
        photo_settle_s=0.0,
        photo_autofocus_enabled=photo_autofocus_enabled,
        photo_autofocus_range_mm=0.03,
        initial_measurement_count=10,
        followup_measurement_count=240,
        current_point=1,
        max_relative_rms=0.01,
        contact_settle_s=0.0,
        contact_seek_range_mm=0.01,
        contact_seek_step_mm=0.001,
        previous_ok_only=False,
        meter=RouteMeterConfiguration(),
        contact_quality_limits=RouteContactQualityLimits(
            max_mad_sigma_ohm=1_500.0,
            max_p95_abs_step_ohm=2_500.0,
            max_relative_mad_sigma=0.08,
            max_relative_p95_abs_step=0.12,
        ),
    )


def _make_route_start_main(
    *,
    thread: object | None = None,
    serial_open: bool = True,
    objective_scale: object | None = 1.0,
    camera_frame: object | None = _FakeFrame("route"),
    lcr_controller: _RouteStartLcr | None = None,
) -> tuple[
    Main,
    list[tuple[str, int | None]],
    list[tuple[str, tuple[object, ...], dict[str, object]]],
    _FakeVisibleDialog,
    _RouteStartLcr,
    list[float],
]:
    point = _route_start_point()
    window = Main.__new__(Main)
    statuses: list[tuple[str, int | None]] = []
    telegrams: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    camera_calls: list[float] = []
    dialog = _FakeVisibleDialog()
    lcr = lcr_controller or _RouteStartLcr()

    window._route_measurement_thread = thread
    window.serial_connection = types.SimpleNamespace(is_open=serial_open)
    window._design_session = types.SimpleNamespace(
        route=types.SimpleNamespace(points=[object()], name="route"),
        registration=types.SimpleNamespace(valid=True),
    )
    window._route_measurement_points = lambda _route: [point]
    window._set_route_measurement_resume_point = lambda _point: None
    window._route_measurement_session_active = False
    window._set_route_measurement_pending = lambda _pending: None
    window._route_measurement_runtime_configuration = None
    window._save_route_measurement_session_metadata = lambda _configuration: None
    window._active_microscope_scale = lambda: objective_scale

    def wait_for_camera_frame(*, timeout_s: float = 0.1, **_kwargs):
        camera_calls.append(float(timeout_s))
        return camera_frame, None

    window._wait_for_camera_frame = wait_for_camera_frame
    window.lcr_controller = lcr
    window.stage_controller = types.SimpleNamespace()
    window._current_needle_feedrate = lambda: None
    window.route_measurement_status = _RouteStartEmit()
    window.route_measurement_progress = _RouteStartEmit()
    window.route_measurement_recorded = _RouteStartEmit()
    window.route_measurement_result = _RouteStartEmit()
    window.route_measurement_waiting_changed = _RouteStartEmit()
    window.design_navigator_panel = None
    window._route_measurement_dialog = dialog
    window._show_status = (
        lambda message, timeout_ms=None: statuses.append((str(message), timeout_ms))
    )
    window._send_telegram_alert = (
        lambda key, *args, **kwargs: telegrams.append((str(key), args, dict(kwargs)))
    )
    window._telegram_photo_lock = threading.Lock()
    window._update_stage_coordinate_apply_state = lambda: None
    return window, statuses, telegrams, dialog, lcr, camera_calls


def _make_cancel_main() -> tuple[Main, _FakeStageController, _FakeButton, list[str]]:
    window = Main.__new__(Main)
    stage_controller = _FakeStageController()
    cancel_button = _FakeButton()
    statuses: list[str] = []

    window.stage_controller = stage_controller
    window._stage_coordinate_apply_button = _FakeButton()
    window._stage_coordinate_cancel_button = cancel_button
    window._pending_stage_axis_targets = {}
    window._stage_axis_fields = {}
    window._stage_axis_display_values = {}
    window._stage_axis_return_commits = set()
    window._stage_axis_base_styles = {}
    window._coordinate_targets = _coordinate_target_state()
    window._route_measurement_runner = None
    window.surface_map_window = None
    window._manual_alignment_pick_slot = None
    window._pending_click_to_move = None
    window._pending_homing_axes = []
    window._homing_active_key = None
    window._pending_alignment_preparation = None
    window._pending_quick_alignment_rotation = False
    window.design_navigator_panel = None
    window.view = _FakeView()
    panel = _FakeStagePositionPanel(
        window._stage_axis_fields,
        apply_button=window._stage_coordinate_apply_button,
        cancel_button=cancel_button,
    )
    window._stage_position_panel = panel
    window._pending_stage_axis_targets = panel.pending_targets
    window._stage_axis_base_styles = panel.base_styles
    window._stage_axis_return_commits = panel.return_commits
    window._show_status = (
        lambda message, _timeout_ms=None: statuses.append(str(message))
    )
    window._clear_stage_motion_axes = lambda: None
    window._clear_planned_move_prediction = lambda *, clear_wait_state: None
    window._schedule_status_refreshes = lambda _delays: None
    window._schedule_cancel_state_refresh = lambda: None
    window._refresh_stage_axis_styles = lambda: None
    return window, stage_controller, cancel_button, statuses


def _make_stage_position_display_main() -> tuple[Main, _FakeStageController]:
    window = Main.__new__(Main)
    stage_controller = _FakeStageController()

    window.stage_controller = stage_controller
    window._stage_axis_fields = {
        "X": _FakeLineEdit(),
        "Y": _FakeLineEdit(),
        "Z": _FakeLineEdit(),
    }
    window._stage_unhomed_display_origins = {}
    window._stage_axis_raw_values = {}
    window._stage_axis_display_values = {}
    window._stage_axis_homed = set()
    window._stage_axis_base_styles = {}
    window._pending_stage_axis_targets = {}
    window._stage_limit_axes = set()
    window._stage_motion_axes = set()
    window._stage_motion_blink_dimmed = False
    window._updating_stage_position_fields = False
    panel = _FakeStagePositionPanel(window._stage_axis_fields)
    window._stage_position_panel = panel
    window._pending_stage_axis_targets = panel.pending_targets
    window._stage_axis_base_styles = panel.base_styles
    window._stage_axis_return_commits = panel.return_commits
    window._clear_stage_motion_axes = lambda: None
    window._update_stage_coordinate_apply_state = lambda: None
    window._set_stage_position_fields_available = (
        lambda available: setattr(window, "_fields_available", bool(available))
    )
    window._style_stage_axis_field = (
        lambda field, background, foreground: field.styles.append(
            (str(background), str(foreground))
        )
    )
    window._current_linear_feedrate = lambda: 123.0
    stage_controller.homed_axes = lambda: {"X", "Y"}
    return window, stage_controller


def _telegram_test_photo_bytes(width: int, height: int, fill: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    red = (fill >> 16) & 0xFF
    green = (fill >> 8) & 0xFF
    blue = fill & 0xFF
    row = b"\x00" + bytes((red, green, blue)) * int(width)
    raw = row * int(height)
    header = struct.pack(">IIBBBBB", int(width), int(height), 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
