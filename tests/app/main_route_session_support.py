from __future__ import annotations

import struct
import types
import zlib

from probe_station_gui.instruments.meters.lcr_session_backend import LCRMeterError
from probe_station_gui.route.measurement import (
    RouteContactQualityLimits,
    RouteMeasurementPoint,
)
from probe_station_gui.route.measurement_config import RouteMeasurementRunConfiguration
from probe_station_gui.route.meter_config import RouteMeterConfiguration
from probe_station_gui.route.operation_modes import ROUTE_OPERATION_MEASURE
from probe_station_gui.route.session_start import snapshot_route_design_frame
from tests.app.main_coordinate_feedrate_support import (
    Main,
    _FakeButton,
    _FakeFrame,
    _FakeLineEdit,
    _FakeMicroscopeInteraction,
    _FakeStageController,
    _FakeStagePositionPanel,
    _FakeTimer,
    _FakeView,
    _FakeVisibleDialog,
    _telegram_runtime_stub,
)
from tests.app.main_stage_motion_support import _FakeStageMotion
from tests.app.route_run_execution_support import (
    activate_route_run,
    install_route_run_execution,
)


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
        error: LCRMeterError | None = None,
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
    operation_mode: str = ROUTE_OPERATION_MEASURE,
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

    if thread is None:
        install_route_run_execution(window)
    else:
        activate_route_run(window, object(), thread)
    window.serial_connection = types.SimpleNamespace(is_open=serial_open)
    window._design_session = types.SimpleNamespace(
        route=types.SimpleNamespace(points=[object()], name="route"),
        registration=types.SimpleNamespace(valid=True),
    )
    frame_usability = types.SimpleNamespace(
        usable=True,
        rejection_reason=None,
        frame_id="design-a",
        frame_version=4,
    )
    window._coordinate_system_coordinator = types.SimpleNamespace(
        current_design_lease=lambda: frame_usability,
        design_lease_is_current=lambda snapshot: snapshot is frame_usability,
    )
    window._snapshot_active_route_design_frame = lambda _usability=None: (
        snapshot_route_design_frame(
            frame_id="design-a",
            frame_version=4,
        )
    )
    window._design_contact_success_callback = lambda _frame: None
    window._route_measurement_points = lambda _route, *, frame_usability_snapshot=None: [
        point
    ]
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
    window._show_status = lambda message, timeout_ms=None: statuses.append(
        (str(message), timeout_ms)
    )
    window._telegram_runtime = _telegram_runtime_stub(
        send_alert=lambda key, *args, **kwargs: telegrams.append(
            (str(key), args, dict(kwargs))
        )
    )
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
    window._stage_axis_fields = {}
    window._stage_axis_display_values = {}
    window._stage_axis_return_commits = set()
    window._stage_axis_base_styles = {}
    window._stage_motion = _FakeStageMotion(stage_controller)
    install_route_run_execution(window)
    window.surface_map_window = None
    window._manual_alignment_pick_slot = None
    window._microscope_interaction = _FakeMicroscopeInteraction()
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
    window._stage_axis_base_styles = panel.base_styles
    window._stage_axis_return_commits = panel.return_commits
    window._stage_motion_axes = set()
    window._stage_motion_blink_dimmed = False
    window._stage_motion_blink_timer = _FakeTimer()
    window._show_status = lambda message, _timeout_ms=None: statuses.append(
        str(message)
    )
    window._schedule_status_refreshes = lambda _delays: None
    window._schedule_cancel_state_refresh = lambda: None
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
    window._stage_limit_axes = set()
    window._stage_motion_axes = set()
    window._stage_motion_blink_dimmed = False
    window._updating_stage_position_fields = False
    panel = _FakeStagePositionPanel(window._stage_axis_fields)
    window._stage_position_panel = panel
    window._stage_motion = _FakeStageMotion(stage_controller)
    window._stage_axis_base_styles = panel.base_styles
    window._stage_axis_return_commits = panel.return_commits
    window._update_stage_coordinate_apply_state = lambda: None
    window._set_stage_position_fields_available = lambda available: setattr(
        window, "_fields_available", bool(available)
    )
    window._style_stage_axis_field = lambda field, background, foreground: (
        field.styles.append((str(background), str(foreground)))
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
