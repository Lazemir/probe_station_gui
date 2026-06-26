import sys
import csv
import json
import subprocess
import struct
import threading
import time
import tempfile
import types
import unittest
import zlib
from pathlib import Path


def _restore_real_imports_for_main() -> None:
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]
    serial_module = sys.modules.get("serial")
    if serial_module is not None and not hasattr(serial_module, "__path__"):
        for name in list(sys.modules):
            if name == "serial" or name.startswith("serial."):
                del sys.modules[name]
    package = sys.modules.get("probe_station_gui")
    if package is not None and not hasattr(package, "__path__"):
        for name in list(sys.modules):
            if name == "probe_station_gui" or name.startswith("probe_station_gui."):
                del sys.modules[name]


_restore_real_imports_for_main()
import main as main_module
from main import Main
from probe_station_gui.dialogs import (
    route_measurement_dialog as route_measurement_dialog_module,
)
from probe_station_gui.dialogs.route_measurement_dialog import (
    RouteMeasurementDialog,
    RouteMeasurementRunConfiguration,
)
from probe_station_gui.route.measurement import (
    RouteExternalMeasurementSessionRunner,
    RouteContactHeightRecord,
    RouteContactQualityLimits,
    RouteContactQuality,
    RouteContactSeekResult,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RouteMeasurementRunner,
    RoutePhotoRecord,
)
from probe_station_gui.instruments.meters.lcr import RouteMeterConfiguration
from probe_station_gui.settings.manager import ObjectiveCalibrationSettings, Settings


class _FakeTimer:
    def __init__(self) -> None:
        self.started = False

    def isActive(self) -> bool:
        return False

    def start(self) -> None:
        self.started = True


class _FakeButton:
    def __init__(self) -> None:
        self.enabled = False

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt naming
        self.enabled = bool(enabled)


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


class _FakeFrame:
    def __init__(self, name: str) -> None:
        self.name = name

    def copy(self) -> "_FakeFrame":
        return _FakeFrame(self.name)


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
    window._coordinate_move_axis = None
    window._coordinate_move_axes = set()
    window._coordinate_move_origin_position = None
    window._coordinate_move_stage_position = None
    window._coordinate_move_target_position = None
    window._coordinate_move_started_at = None
    window._coordinate_move_ends_at = None
    window._coordinate_move_programmed_feedrate = None
    window._coordinate_move_effective_feedrate = None
    window._coordinate_move_seen_active_state = False
    window._coordinate_move_reissue_cancel_pending = False
    window._pending_homing_axes = []
    window._pending_click_to_move = None
    window._pending_planned_move_target_xy = None
    window._pending_planned_move_source_label = None
    window._planned_move_started_at = None
    window._planned_move_waiting_for_fresh_status = False
    window._pending_alignment_preparation = None
    window._pending_quick_alignment_rotation = False
    window._manual_jog_stage_position = None
    window._manual_jog_stage_xy = None
    window._manual_jog_axis_velocities = {}
    window._manual_jog_stop_axis_velocities = {}
    window._manual_jog_velocity_xy = None
    window._manual_jog_stop_prediction_until = None
    window._manual_jog_stop_tail_position = None
    window._manual_jog_waiting_for_fresh_status = False
    window._manual_jog_settle_until = 0.0
    window._manual_jog_stop_status_timestamp = None
    window._manual_jog_command_started_at = None
    window._manual_jog_last_timestamp = None
    window._manual_jog_last_prediction_log_at = 0.0
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
    window._stage_axis_return_commits = set()
    window._stage_axis_base_styles = {}
    window._coordinate_move_axis = None
    window._coordinate_move_axes = set()
    window._coordinate_move_seen_active_state = False
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
    window._show_status = (
        lambda message, _timeout_ms=None: statuses.append(str(message))
    )
    window._clear_stage_motion_axes = lambda: None
    window._clear_planned_move_prediction = lambda *, clear_wait_state: None
    window._schedule_status_refreshes = lambda _delays: None
    window._schedule_cancel_state_refresh = lambda: None
    window._refresh_stage_axis_styles = lambda: None
    return window, stage_controller, cancel_button, statuses


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


class MainCoordinateFeedrateTest(unittest.TestCase):
    def test_api_keithley_meter_configuration_accepts_code_auto_ranges(self) -> None:
        window = Main.__new__(Main)
        window.lcr_controller = types.SimpleNamespace(
            meter_type=lambda: main_module.ROUTE_METER_KEITHLEY
        )

        config = Main._api_route_meter_configuration(
            window,
            {
                "meter_type": "keithley",
                "measurement_voltage_v": 0.03,
                "ranges": {
                    "mode": "code_auto",
                    "expected_resistance_ohm": 100_000.0,
                    "max_current_a": 10e-6,
                },
                "nplc": 5,
            },
            voltages_v=None,
        )

        self.assertEqual(config.meter_type, main_module.ROUTE_METER_KEITHLEY)
        self.assertEqual(config.keithley.range_mode, "code_auto")
        self.assertEqual(config.keithley.measurement_voltage_v, 0.03)
        self.assertEqual(config.keithley.expected_resistance_ohm, 100_000.0)
        self.assertEqual(config.keithley.maximum_current_a, 10e-6)
        self.assertEqual(config.keithley.nplc, 5)

    def test_api_keithley_voltage_range_defaults_to_raw_sweep_span(self) -> None:
        window = Main.__new__(Main)
        window.lcr_controller = types.SimpleNamespace(
            meter_type=lambda: main_module.ROUTE_METER_KEITHLEY
        )

        config = Main._api_route_meter_configuration(
            window,
            {
                "meter_type": "keithley",
                "nplc": 5,
            },
            voltages_v=[-0.3, 0.1, 0.25],
        )

        self.assertEqual(config.keithley.measurement_voltage_v, 0.3)
        self.assertEqual(config.keithley.voltage_range_v, 0.3)
        self.assertEqual(config.keithley.source_voltage_range_v, 0.3)
        self.assertEqual(config.keithley.voltmeter_range_v, 0.3)

    def test_api_configure_meter_reports_unexpected_instrument_error(self) -> None:
        class _FailingLcr:
            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                _configuration: RouteMeterConfiguration,
            ) -> None:
                raise RuntimeError(
                    "VI_ERROR_TMO (-1073807339): Timeout expired before operation completed."
                )

        window = Main.__new__(Main)
        window.lcr_controller = _FailingLcr()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )

        response = Main._api_configure_meter(window, {})

        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["error_type"], "RuntimeError")
        self.assertIn("Measurement instrument setup failed", response["message"])
        self.assertIn("VI_ERROR_TMO", response["message"])

    def test_api_prepare_route_meter_connects_shared_lcr_controller(self) -> None:
        class _Lcr:
            def __init__(self) -> None:
                self.connected = False
                self.runtime_configurations: list[RouteMeterConfiguration] = []
                self.applied_configurations: list[RouteMeterConfiguration] = []
                self.connect_count = 0

            def is_connected(self) -> bool:
                return self.connected

            def apply_route_meter_runtime_configuration(
                self,
                configuration: RouteMeterConfiguration,
            ) -> None:
                self.runtime_configurations.append(configuration)

            def connect_now(self) -> None:
                self.connect_count += 1
                self.connected = True

            def apply_route_meter_configuration(
                self,
                configuration: RouteMeterConfiguration,
            ) -> None:
                self.applied_configurations.append(configuration)

        configuration = RouteMeterConfiguration()
        lcr = _Lcr()
        window = Main.__new__(Main)
        window.lcr_controller = lcr

        response = Main._api_prepare_route_meter_controller(window, configuration)

        self.assertIsNone(response)
        self.assertEqual(lcr.runtime_configurations, [configuration])
        self.assertEqual(lcr.connect_count, 1)
        self.assertEqual(lcr.applied_configurations, [configuration])

    def test_api_prepare_route_meter_reports_busy_instrument_task(self) -> None:
        class _Lcr:
            def __init__(self) -> None:
                self.wait_calls: list[float] = []
                self.applied_configurations: list[RouteMeterConfiguration] = []

            def wait_until_idle(self, timeout_s: float) -> bool:
                self.wait_calls.append(float(timeout_s))
                return False

            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                configuration: RouteMeterConfiguration,
            ) -> None:
                self.applied_configurations.append(configuration)

        configuration = RouteMeterConfiguration()
        lcr = _Lcr()
        window = Main.__new__(Main)
        window.lcr_controller = lcr

        response = Main._api_prepare_route_meter_controller(window, configuration)

        self.assertIsNotNone(response)
        assert response is not None
        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["message"], "Measurement instrument task is still running.")
        self.assertEqual(lcr.wait_calls, [45.0])
        self.assertEqual(lcr.applied_configurations, [])

    def test_resistance_standby_toggle_reflects_controller_state(self) -> None:
        class _Lcr:
            def __init__(self) -> None:
                self.requests: list[bool] = []

            def set_live_polling_enabled(self, enabled: bool) -> None:
                self.requests.append(bool(enabled))

            def live_polling_enabled(self) -> bool:
                return False

        class _Panel:
            def __init__(self) -> None:
                self.states: list[bool] = []

            def set_standby_enabled(self, enabled: bool) -> None:
                self.states.append(bool(enabled))

        lcr = _Lcr()
        panel = _Panel()
        window = Main.__new__(Main)
        window.lcr_controller = lcr
        window.resistance_panel = panel

        Main._on_resistance_standby_enabled_changed(window, True)

        self.assertEqual(lcr.requests, [True])
        self.assertEqual(panel.states, [False])

    def test_meter_auto_connect_uses_shared_lcr_controller(self) -> None:
        class _SettingsManager:
            def serial_auto_connect_enabled(self) -> bool:
                return False

            def meter_auto_connect_enabled(self) -> bool:
                return True

        class _Lcr:
            def __init__(self) -> None:
                self.request_count = 0

            def is_connected(self) -> bool:
                return False

            def request_connect(self) -> None:
                self.request_count += 1

        lcr = _Lcr()
        window = Main.__new__(Main)
        window.serial_connection_panel = None
        window.serial_connection = None
        window.settings_manager = _SettingsManager()
        window.lcr_controller = lcr

        Main._auto_connect_if_possible(window)

        self.assertEqual(lcr.request_count, 1)

    def test_api_raw_voltage_sweep_reports_unexpected_instrument_error(self) -> None:
        class _FailingLcr:
            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                _configuration: RouteMeterConfiguration,
            ) -> None:
                pass

            def read_voltage_sweep_now(self, _voltages_v: list[float]) -> dict[str, object]:
                raise RuntimeError(
                    "VI_ERROR_TMO (-1073807339): Timeout expired before operation completed."
                )

        window = Main.__new__(Main)
        window.lcr_controller = _FailingLcr()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window._api_contact_number = lambda _payload, required=False: None
        window._api_bool = lambda _payload, *names, default=False: default
        window._api_float = (
            lambda _payload, *names, default=0.0, minimum=None: default
        )
        window._api_needle_feedrate = lambda _payload: None
        window._api_timestamp_utc = lambda: "2026-06-06T12:00:00+00:00"

        response = Main._api_raw_voltage_sweep(window, {"voltages_v": [0.0]})

        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["error_type"], "RuntimeError")
        self.assertIn("Raw voltage sweep failed", response["message"])
        self.assertIn("VI_ERROR_TMO", response["message"])
        self.assertIsNone(response["contact"])

    def test_api_route_session_reports_unexpected_instrument_setup_error(self) -> None:
        class _FailingLcr:
            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                _configuration: RouteMeterConfiguration,
            ) -> None:
                raise RuntimeError(
                    "Invalid session handle. The resource might be closed."
                )

        point = RouteMeasurementPoint(
            index=12,
            point_id="p012",
            label="P012",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )
        window = Main.__new__(Main)
        window._route_measurement_thread = None
        window._route_measurement_runner = None
        window._route_measurement_waiting = False
        window._last_route_measurement_result = None
        window._route_measurement_current_point = 12
        window.serial_connection = types.SimpleNamespace(is_open=True)
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(points=[object()], name="route"),
            registration=types.SimpleNamespace(valid=True),
        )
        window._route_measurement_points = lambda _route: [point]
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window.lcr_controller = _FailingLcr()

        response = Main._api_start_route_session(window, {})

        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["error_type"], "RuntimeError")
        self.assertIn("Route measurement instrument setup failed", response["message"])
        self.assertIn("Invalid session handle", response["message"])

    def test_combine_telegram_contact_photos_side_by_side(self) -> None:
        script = r"""
import struct
import zlib
from PySide6.QtGui import QImage
from main import Main

def png_bytes(width, height, fill):
    def chunk(kind, data):
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
    header = struct.pack(">IIBBBBB", int(width), int(height), 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * int(height)))
        + chunk(b"IEND", b"")
    )

combined = Main._combine_telegram_contact_photos(
    png_bytes(8, 4, 0x00FF0000),
    png_bytes(2, 4, 0x0000FF00),
)
assert combined is not None
image = QImage()
assert image.loadFromData(combined[0])
assert combined[1] == "route-contact-comparison.jpg"
assert image.width() == 10
assert image.height() == 4
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            timeout=20,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_route_contact_result_sends_one_combined_telegram_photo(self) -> None:
        window = Main.__new__(Main)
        before = (
            _telegram_test_photo_bytes(8, 4, 0x00FF0000),
            "before.jpg",
            "Route contact before needle press:\nPoint 1/2, structure 3, P003.",
        )
        after = (
            _telegram_test_photo_bytes(2, 4, 0x0000FF00),
            "after.jpg",
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )
        sent: list[tuple[str, tuple[bytes, str] | None, object | None]] = []

        window._route_measurement_dialog = None
        window._take_pending_telegram_contact_photos = lambda: (before, after)
        window._telegram_contact_photo_payload = (
            lambda _before, _after: (
                (b"combined", "route-contact-comparison.jpg"),
                "Route contact check:\n"
                "Left: before needle press. Right: contact attempt.\n"
                "Point 1/2, structure 3, P003, status=bad_contact.",
            )
        )
        window._send_telegram_bot_message = (
            lambda message, *, photo=None, reply_markup=None: sent.append(
                (message, photo, reply_markup)
            )
        )
        window._telegram_default_markup = lambda: "markup"

        Main._on_route_measurement_result(
            window,
            types.SimpleNamespace(),
            1,
            2,
            True,
        )

        self.assertEqual(len(sent), 1)
        message, photo, reply_markup = sent[0]
        self.assertIn("Left: before needle press. Right: contact attempt.", message)
        self.assertIn("status=bad_contact", message)
        self.assertIsNotNone(photo)
        assert photo is not None
        self.assertEqual(photo[1], "route-contact-comparison.jpg")
        self.assertEqual(reply_markup, "markup")

    def test_route_waiting_bad_contact_sends_one_combined_contact_photo(self) -> None:
        window = Main.__new__(Main)
        before = (
            _telegram_test_photo_bytes(8, 4, 0x00FF0000),
            "before.jpg",
            "Route contact before needle press:\nPoint 1/2, structure 3, P003.",
        )
        after = (
            _telegram_test_photo_bytes(2, 4, 0x0000FF00),
            "after.jpg",
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )
        alerts: list[tuple[str, str, dict[str, object]]] = []
        record = RouteMeasurementRecord(
            timestamp="2026-06-03T17:21:46",
            structure_number=3,
            nplc="1",
            measurement_type="test",
            n_measurements=250,
            resistance_ohm=298000.0,
            resistance_rms_ohm=19900.0,
            relative_rms=0.0666,
            status="bad_contact",
            contact_quality=RouteContactQuality(
                assessed=True,
                good=False,
                status="bad_contact",
                median_ohm=297000.0,
                mad_sigma_ohm=19100.0,
                p95_abs_step_ohm=48300.0,
                span_ohm=102000.0,
                compliance_hits=0,
                polarity_sign_mismatch_count=2,
                reasons=("mad_sigma_too_high", "step_noise_too_high"),
            ),
        )

        window._last_telegram_attention_message = ""
        window._last_route_measurement_result = (record, 1, 2, True)
        window._route_measurement_waiting = False
        window._pending_route_measure_point = None
        window._latest_route_contact_failure_telegram_photos = lambda: (before, after)
        window._telegram_contact_photo_payload = (
            lambda _before, _after: (
                (b"combined", "route-contact-comparison.jpg"),
                "Route contact check:\n"
                "Left: before needle press. Right: contact attempt.\n"
                "Point 1/2, structure 3, P003, status=bad_contact.",
            )
        )
        window._send_telegram_alert = (
            lambda key, text, **kwargs: alerts.append((key, text, kwargs))
        )
        window._telegram_route_actions_markup = lambda: "actions"
        window.design_navigator_panel = None
        window._route_measurement_dialog = None

        Main._on_route_measurement_waiting_changed(window, True)

        self.assertEqual(len(alerts), 1)
        key, text, kwargs = alerts[0]
        self.assertEqual(key, "route_attention")
        self.assertIn("Measured route point 1/2", text)
        self.assertIn("status=bad_contact", text)
        self.assertIn("reasons=mad_sigma_too_high, step_noise_too_high", text)
        self.assertIn("p95_step=", text)
        self.assertIn("span=", text)
        self.assertIn("compliance_hits=0", text)
        self.assertIn("polarity_mismatches=2", text)
        self.assertIn("Left: before needle press. Right: contact attempt.", text)
        self.assertFalse(kwargs["attach_photo"])
        self.assertEqual(kwargs["reply_markup"], "actions")
        photo = kwargs["photo"]
        self.assertIsNotNone(photo)
        assert photo is not None
        self.assertEqual(photo[1], "route-contact-comparison.jpg")

    def test_route_photo_autofocus_reports_status_through_signal(self) -> None:
        window = Main.__new__(Main)
        emitted: list[str] = []
        requested_ranges: list[float] = []

        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda message: emitted.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            run_external_local_autofocus=lambda *, range_mm: requested_ranges.append(
                float(range_mm)
            )
            or "focus-result"
        )
        configuration = types.SimpleNamespace(photo_autofocus_range_mm=0.03)

        result = Main._route_photo_autofocus(
            window,
            types.SimpleNamespace(),
            2,
            5,
            configuration=configuration,
        )

        self.assertEqual(result, "focus-result")
        self.assertEqual(requested_ranges, [0.03])
        self.assertEqual(
            emitted,
            ["Route photo autofocus: point 2/5, +/-0.030 mm."],
        )

    def test_api_stage_local_focus_runs_local_autofocus(self) -> None:
        window = Main.__new__(Main)
        calls: list[tuple[float, float | None]] = []

        class _FocusResult:
            objective_name = "X50"
            mode = "local"
            start_z_mm = 4.0
            best_z_mm = 4.002
            best_score = 0.2
            sample_count = 9
            edge_peak = False
            range_mm = 0.03
            fine_step_mm = 0.002
            lower_z_mm = 3.97
            upper_z_mm = 4.03

            @property
            def delta_um(self) -> float:
                return 2.0

            def summary(self) -> str:
                return "focused"

            def to_dict(self) -> dict[str, object]:
                return {
                    "objective_name": self.objective_name,
                    "mode": self.mode,
                    "focus_start_z_mm": self.start_z_mm,
                    "focus_best_z_mm": self.best_z_mm,
                    "focus_delta_um": self.delta_um,
                    "focus_score": self.best_score,
                    "focus_sample_count": self.sample_count,
                    "focus_edge_peak": self.edge_peak,
                    "autofocus_range_mm": self.range_mm,
                    "autofocus_fine_step_mm": self.fine_step_mm,
                    "autofocus_lower_z_mm": self.lower_z_mm,
                    "autofocus_upper_z_mm": self.upper_z_mm,
                }

        window.stage_controller = types.SimpleNamespace(
            _needles_known=True,
            _needles_up=True,
            _needles_zone="raise",
            begin_external_task=lambda _label: None,
            finish_external_task=lambda: None,
            run_external_local_autofocus=lambda *, range_mm, step_mm=None: (
                calls.append((float(range_mm), step_mm))
                or _FocusResult()
            ),
        )

        result = Main._api_stage_local_focus(
            window,
            {"range_mm": 0.03, "step_mm": 0.002},
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(calls, [(0.03, 0.002)])
        self.assertEqual(result["focus"]["objective_name"], "X50")
        self.assertEqual(result["focus"]["focus_best_z_mm"], 4.002)

    def test_wait_for_camera_frame_requires_fresh_counter_after_marker(self) -> None:
        window = Main.__new__(Main)
        window._latest_camera_frame_condition = threading.Condition()
        window._latest_camera_frame = _FakeFrame("old")
        window._latest_camera_frame_counter = 7

        frame, counter = Main._wait_for_camera_frame(
            window,
            after_counter=7,
            timeout_s=0.0,
        )

        self.assertIsNone(frame)
        self.assertEqual(counter, 7)

        frame, counter = Main._wait_for_camera_frame(window, timeout_s=0.0)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.name, "old")
        self.assertEqual(counter, 7)

    def test_record_route_photo_writes_focus_map_csv(self) -> None:
        window = Main.__new__(Main)
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(name="route-a")
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            photo_path = Path(tmpdir) / "photos" / "point-001.png"
            record = RoutePhotoRecord(
                timestamp="2026-05-29T12:00:00+03:00",
                path=str(photo_path),
                structure_number=1,
                point_index=1,
                point_id="p001",
                label="P001",
                design_center=(10.0, 20.0),
                stage_xy=(1.0, 2.0),
                focus={
                    "objective_name": "X20",
                    "focus_start_z_mm": 9.98,
                    "focus_best_z_mm": 10.0,
                    "focus_delta_um": 20.0,
                    "focus_score": 12.5,
                    "focus_sample_count": 7,
                    "focus_edge_peak": False,
                    "autofocus_range_mm": 0.03,
                    "autofocus_fine_step_mm": 0.005,
                    "autofocus_lower_z_mm": 9.95,
                    "autofocus_upper_z_mm": 10.01,
                },
            )

            Main._record_route_photo(window, record, 1, 3)

            focus_map_path = photo_path.parent / "route-photo-focus-map.csv"
            with focus_map_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route_name"], "route-a")
        self.assertEqual(rows[0]["photo_path"], str(photo_path))
        self.assertEqual(rows[0]["design_x"], "10.0")
        self.assertEqual(rows[0]["stage_y"], "2.0")
        self.assertEqual(rows[0]["focus_best_z_mm"], "10.0")
        self.assertEqual(rows[0]["focus_delta_um"], "20.0")

    def test_route_points_keep_contact_xy_separate_from_photo_objective_xy(self) -> None:
        window = Main.__new__(Main)
        settings = Settings()
        settings.objectives.active_name = "X50"
        settings.objectives.objectives = {
            "X5": ObjectiveCalibrationSettings(
                name="X5",
                magnification=5.0,
                xy_offset_configured=True,
            ),
            "X50": ObjectiveCalibrationSettings(
                name="X50",
                magnification=50.0,
                xy_offset_x_mm=0.25,
                xy_offset_y_mm=-0.25,
                xy_offset_configured=True,
            ),
        }
        window.settings_manager = types.SimpleNamespace(
            objectives_configuration=lambda: settings.objectives
        )
        window._design_session = types.SimpleNamespace(
            stage_from_design=lambda _design_xy: (1.0, 2.0)
        )
        route_point = types.SimpleNamespace(
            enabled=True,
            camera_center=(10.0, 20.0),
            id="p001",
            label="P001",
        )
        route = types.SimpleNamespace(
            points=[route_point],
            needle_hits_for_point=lambda _point: [],
        )

        point = Main._route_measurement_points(window, route)[0]

        self.assertEqual(point.stage_xy, (1.0, 2.0))
        self.assertEqual(point.photo_stage_xy, (1.25, 1.75))

    def test_sample_load_focus_uses_active_objective_cache(self) -> None:
        window = Main.__new__(Main)
        settings = Settings()
        settings.objectives.active_name = "X20"
        window.settings_manager = types.SimpleNamespace(
            objectives_configuration=lambda: settings.objectives
        )
        window.stage_controller = types.SimpleNamespace(
            latest_stage_position=lambda: (0.0, 0.0, 9.0)
        )
        window._last_sample_focus_z_by_objective = {
            "X5": 1.25,
            "X20": 2.5,
        }

        self.assertEqual(Main._sample_load_focus_z(window), 2.5)

        settings.objectives.active_name = "X5"

        self.assertEqual(Main._sample_load_focus_z(window), 1.25)

    def test_sample_focus_memory_is_stored_per_objective(self) -> None:
        window = Main.__new__(Main)
        settings = Settings()
        latest_z = [3.0]
        window.settings_manager = types.SimpleNamespace(
            objectives_configuration=lambda: settings.objectives
        )
        window.stage_controller = types.SimpleNamespace(
            latest_stage_position=lambda: (0.0, 0.0, latest_z[0])
        )
        window._last_sample_focus_z_by_objective = {}

        settings.objectives.active_name = "X5"
        Main._remember_sample_focus_from_latest(window)
        latest_z[0] = 7.0
        settings.objectives.active_name = "X20"
        Main._remember_sample_focus_from_latest(window)

        self.assertEqual(
            window._last_sample_focus_z_by_objective,
            {"X5": 3.0, "X20": 7.0},
        )

    def test_sample_unload_cancel_keeps_registration_and_does_not_start(self) -> None:
        window = Main.__new__(Main)
        invalidations: list[str] = []
        remembered: list[bool] = []
        window._design_session = types.SimpleNamespace(
            registration=types.SimpleNamespace(valid=True)
        )
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._has_cancelable_operation = lambda: False
        window._invalidate_design_registration = lambda reason: invalidations.append(
            reason
        )
        window._remember_sample_focus_from_latest = lambda: remembered.append(True)
        window._current_needle_feedrate = lambda: 7.0
        window.stage_controller = types.SimpleNamespace(
            max_feedrate_for_axes=lambda _axes: 900.0
        )

        original_box = main_module.QMessageBox
        original_thread = main_module.threading.Thread
        _FakeThread.instances = []
        main_module.QMessageBox = types.SimpleNamespace(
            Yes=1,
            No=2,
            question=lambda *_args, **_kwargs: 2,
        )
        main_module.threading.Thread = _FakeThread
        try:
            Main._request_sample_unload(window)
        finally:
            main_module.QMessageBox = original_box
            main_module.threading.Thread = original_thread

        self.assertEqual(invalidations, [])
        self.assertEqual(remembered, [])
        self.assertEqual(_FakeThread.instances, [])

    def test_sample_unload_confirm_clears_registration_before_start(self) -> None:
        window = Main.__new__(Main)
        events: list[object] = []
        window._design_session = types.SimpleNamespace(
            registration=types.SimpleNamespace(valid=True)
        )
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._has_cancelable_operation = lambda: False
        window._invalidate_design_registration = lambda reason: events.append(
            ("invalidate", reason)
        )
        window._remember_sample_focus_from_latest = lambda: events.append("remember")
        window._current_needle_feedrate = lambda: 7.0
        window._run_sample_unload = lambda *_args: None
        window.stage_controller = types.SimpleNamespace(
            max_feedrate_for_axes=lambda axes: 900.0
            if tuple(axes) == ("X", "Y")
            else 1.0
        )

        original_box = main_module.QMessageBox
        original_thread = main_module.threading.Thread
        _FakeThread.instances = []
        main_module.QMessageBox = types.SimpleNamespace(
            Yes=1,
            No=2,
            question=lambda *_args, **_kwargs: 1,
        )
        main_module.threading.Thread = _FakeThread
        try:
            Main._request_sample_unload(window)
        finally:
            main_module.QMessageBox = original_box
            main_module.threading.Thread = original_thread

        self.assertEqual(
            events,
            [
                (
                    "invalidate",
                    "Design registration cleared before sample unload.",
                ),
                "remember",
            ],
        )
        self.assertEqual(len(_FakeThread.instances), 1)
        self.assertTrue(_FakeThread.instances[0].started)
        self.assertEqual(_FakeThread.instances[0].args, (900.0, 7.0))

    def test_sample_unload_without_registration_skips_prompt_and_reset(self) -> None:
        window = Main.__new__(Main)
        invalidations: list[str] = []
        remembered: list[bool] = []
        window._design_session = types.SimpleNamespace(registration=None)
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._has_cancelable_operation = lambda: False
        window._invalidate_design_registration = lambda reason: invalidations.append(
            reason
        )
        window._remember_sample_focus_from_latest = lambda: remembered.append(True)
        window._current_needle_feedrate = lambda: 7.0
        window._run_sample_unload = lambda *_args: None
        window.stage_controller = types.SimpleNamespace(
            max_feedrate_for_axes=lambda _axes: 900.0
        )

        original_box = main_module.QMessageBox
        original_thread = main_module.threading.Thread
        _FakeThread.instances = []

        def _unexpected_question(*_args, **_kwargs):
            raise AssertionError("confirmation should not be shown")

        main_module.QMessageBox = types.SimpleNamespace(
            Yes=1,
            No=2,
            question=_unexpected_question,
        )
        main_module.threading.Thread = _FakeThread
        try:
            Main._request_sample_unload(window)
        finally:
            main_module.QMessageBox = original_box
            main_module.threading.Thread = original_thread

        self.assertEqual(invalidations, [])
        self.assertEqual(remembered, [True])
        self.assertEqual(len(_FakeThread.instances), 1)
        self.assertTrue(_FakeThread.instances[0].started)

    def test_route_session_active_reads_new_and_legacy_settings(self) -> None:
        self.assertTrue(
            Main._route_measurement_session_active_from_settings(
                {"measurement_session_active": True}
            )
        )
        self.assertTrue(
            Main._route_measurement_session_active_from_settings(
                {"measurement_pending": True}
            )
        )
        self.assertFalse(
            Main._route_measurement_session_active_from_settings(
                {
                    "measurement_session_active": False,
                    "measurement_pending": True,
                }
            )
        )

    def test_route_completion_telegram_reports_csv_session_total(self) -> None:
        window = Main.__new__(Main)
        alerts: list[tuple[str, str, dict[str, object]]] = []
        statuses: list[str] = []
        pending: list[bool] = []
        resumed: list[int] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write("timestamp,point_index,status\n")
                handle.write("2026-05-31T20:00:00+03:00,1,ok\n")
                handle.write("2026-05-31T20:01:00+03:00,2,ok\n")

            window._route_measurement_thread = None
            window._route_measurement_runner = object()
            window._route_measurement_waiting = True
            window._route_measurement_photo_enabled = False
            window._route_measurement_measure_enabled = True
            window._route_measurement_point_numbers = [1, 2]
            window._route_measurement_current_point = 2
            window._telegram_photo_lock = threading.Lock()
            window._telegram_route_photo_requested = True
            window._telegram_contact_photo_requested = True
            window._telegram_pending_contact_before_photo = (b"before", "before.jpg", "")
            window._telegram_pending_contact_photo = (b"after", "after.jpg", "")
            window._last_route_pre_contact_photo = (1, 2, b"pre", "pre.jpg", "")
            window.design_navigator_panel = None
            window._route_measurement_dialog = None
            window._update_stage_coordinate_apply_state = lambda: None
            window._set_route_measurement_resume_point = resumed.append
            window._set_route_measurement_pending = pending.append
            window._show_status = (
                lambda message, _timeout_ms=None: statuses.append(str(message))
            )
            window._send_telegram_alert = (
                lambda key, text, **kwargs: alerts.append((key, text, kwargs))
            )

            Main._on_route_measurement_finished(
                window,
                True,
                "Route measurement complete: 1 measurements saved to route.csv.",
                str(csv_path),
            )

        self.assertEqual(resumed, [1])
        self.assertEqual(pending, [False])
        self.assertEqual(len(alerts), 1)
        key, text, kwargs = alerts[0]
        self.assertEqual(key, "route_completed")
        self.assertIn("Session total: 2 measurements in CSV.", text)
        self.assertEqual(kwargs["document_path"], csv_path)

    def test_route_completion_stores_final_api_status_payload(self) -> None:
        class _Runner:
            def status_payload(self) -> dict[str, object]:
                return {"state": "completed", "accepted": True}

        runner = _Runner()
        window = Main.__new__(Main)
        window._route_measurement_thread = None
        window._route_measurement_runner = runner
        window._api_route_session_id = "session-1"
        window._api_route_last_status = None
        window._api_route_lcr_controller = object()
        window._route_measurement_runtime_configuration = object()
        window._route_measurement_waiting = True
        window._route_measurement_waiting_reason = "external"
        window._last_route_measurement_result = object()
        window._pending_route_measure_point = object()
        window._route_measurement_photo_enabled = True
        window._route_measurement_measure_enabled = False
        window._route_measurement_context_close_requested = False
        window._route_measurement_point_numbers = [1]
        window._route_measurement_current_point = 1
        window._telegram_photo_lock = threading.Lock()
        window._telegram_route_photo_requested = False
        window._telegram_contact_photo_requested = False
        window._telegram_pending_contact_before_photo = None
        window._telegram_pending_contact_photo = None
        window._last_route_pre_contact_photo = None
        window.design_navigator_panel = None
        window._route_measurement_dialog = None
        window._resume_resistance_standby_polling = lambda: None
        window._update_stage_coordinate_apply_state = lambda: None
        window._set_route_measurement_pending = lambda _pending: None
        window._show_status = lambda *_args, **_kwargs: None
        window._send_telegram_alert = lambda *_args, **_kwargs: None

        Main._on_route_measurement_finished(
            window,
            runner,
            True,
            "Route photo capture complete.",
            "",
        )

        self.assertEqual(
            window._api_route_last_status,
            {"state": "completed", "accepted": True},
        )

    def test_context_close_failure_suppresses_route_failure_telegram(self) -> None:
        window = Main.__new__(Main)
        alerts: list[tuple[object, ...]] = []
        pending: list[bool] = []
        resumed: list[int] = []
        statuses: list[str] = []

        window._route_measurement_thread = None
        window._route_measurement_runner = object()
        window._api_route_session_id = None
        window._api_route_lcr_controller = None
        window._route_measurement_runtime_configuration = None
        window._route_measurement_waiting = True
        window._route_measurement_waiting_reason = "external"
        window._last_route_measurement_result = object()
        window._pending_route_measure_point = object()
        window._route_measurement_photo_enabled = True
        window._route_measurement_measure_enabled = True
        window._route_measurement_context_close_requested = True
        window._route_measurement_point_numbers = [1, 2]
        window._route_measurement_current_point = 2
        window._telegram_photo_lock = threading.Lock()
        window._telegram_route_photo_requested = False
        window._telegram_contact_photo_requested = False
        window._telegram_pending_contact_before_photo = None
        window._telegram_pending_contact_photo = None
        window._last_route_pre_contact_photo = None
        window.design_navigator_panel = None
        window._route_measurement_dialog = None
        window._resume_resistance_standby_polling = lambda: None
        window._update_stage_coordinate_apply_state = lambda: None
        window._set_route_measurement_resume_point = resumed.append
        window._set_route_measurement_pending = pending.append
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window._send_telegram_alert = (
            lambda *args, **_kwargs: alerts.append(tuple(args))
        )

        Main._on_route_measurement_finished(
            window,
            False,
            "Route window closed.",
            "partial.csv",
        )

        self.assertEqual(resumed, [2])
        self.assertEqual(pending, [True])
        self.assertEqual(statuses, ["Route window closed."])
        self.assertEqual(alerts, [])
        self.assertFalse(window._route_measurement_context_close_requested)

    def test_route_progress_updates_dialog_progress_bar(self) -> None:
        window = Main.__new__(Main)
        resumed: list[int] = []
        progress: list[tuple[int, int, int]] = []

        window._set_route_measurement_resume_point = resumed.append
        window._route_measurement_dialog = types.SimpleNamespace(
            set_progress=lambda position, total, point_number: progress.append(
                (position, total, point_number)
            )
        )

        Main._on_route_measurement_progress(window, 3, 7, 12)

        self.assertEqual(resumed, [12])
        self.assertEqual(progress, [(3, 7, 12)])

    def test_api_route_session_opens_measurement_controls_without_starting_gui_run(self) -> None:
        window = Main.__new__(Main)
        calls: list[bool] = []
        window._open_route_measurement_dialog = (
            lambda *, start_context=True: calls.append(bool(start_context))
        )

        Main._show_route_measurement_dialog_for_api_session(window)

        self.assertEqual(calls, [False])

    def test_api_route_session_started_slot_updates_controls_on_gui_thread(self) -> None:
        window = Main.__new__(Main)
        calls: list[object] = []
        panel = types.SimpleNamespace(
            set_route_measurement_running=lambda value: calls.append(
                ("panel_running", value)
            ),
            set_route_measurement_waiting=lambda value, reason="": calls.append(
                ("panel_waiting", value, reason)
            ),
            set_route_measurement_status=lambda value: calls.append(
                ("panel_status", value)
            ),
        )
        dialog = types.SimpleNamespace(
            set_running=lambda value: calls.append(("dialog_running", value)),
            set_waiting=lambda value, reason="": calls.append(
                ("dialog_waiting", value, reason)
            ),
            reset_progress=lambda value: calls.append(("dialog_progress", value)),
            set_status=lambda value: calls.append(("dialog_status", value)),
        )
        window._route_measurement_waiting = False
        window.design_navigator_panel = panel
        window._route_measurement_dialog = dialog
        window._set_route_measurement_resume_point = lambda value: calls.append(
            ("resume_point", value)
        )
        window._set_route_measurement_pending = lambda value: calls.append(
            ("pending", value)
        )
        window._show_route_measurement_dialog_for_api_session = lambda: calls.append(
            ("open_controls",)
        )
        window._show_status = lambda message: calls.append(("status", message))
        window._update_stage_coordinate_apply_state = lambda: calls.append(
            ("update_stage_controls",)
        )

        Main._on_route_measurement_started(window, "started", 420, 7, True)

        self.assertIn(("resume_point", 7), calls)
        self.assertIn(("pending", True), calls)
        self.assertIn(("open_controls",), calls)
        self.assertIn(("dialog_progress", 420), calls)
        self.assertIn(("status", "started"), calls)

    def test_route_started_preserves_initial_waiting_state_for_api_controls(
        self,
    ) -> None:
        window = Main.__new__(Main)
        calls: list[object] = []
        panel = types.SimpleNamespace(
            set_route_measurement_running=lambda value: calls.append(
                ("panel_running", value)
            ),
            set_route_measurement_waiting=lambda value, reason="": calls.append(
                ("panel_waiting", value, reason)
            ),
            set_route_measurement_status=lambda value: calls.append(
                ("panel_status", value)
            ),
        )
        dialog = types.SimpleNamespace(
            set_running=lambda value: calls.append(("dialog_running", value)),
            set_waiting=lambda value, reason="": calls.append(
                ("dialog_waiting", value, reason)
            ),
            reset_progress=lambda value: calls.append(("dialog_progress", value)),
            set_status=lambda value: calls.append(("dialog_status", value)),
        )
        window._route_measurement_waiting = True
        window.design_navigator_panel = panel
        window._route_measurement_dialog = dialog
        window._set_route_measurement_resume_point = lambda value: calls.append(
            ("resume_point", value)
        )
        window._set_route_measurement_pending = lambda value: calls.append(
            ("pending", value)
        )
        window._show_route_measurement_dialog_for_api_session = lambda: calls.append(
            ("open_controls",)
        )
        window._show_status = lambda message: calls.append(("status", message))
        window._update_stage_coordinate_apply_state = lambda: calls.append(
            ("update_stage_controls",)
        )

        Main._on_route_measurement_started(window, "started", 420, 7, True)

        self.assertIn(("panel_waiting", True, "paused"), calls)
        self.assertIn(("dialog_running", True), calls)
        self.assertIn(("dialog_waiting", True, "paused"), calls)

    def test_route_progress_eta_uses_current_run_baseline(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        dialog._progress_started_at = 100.0
        dialog._progress_baseline_completed = 187
        original_monotonic = route_measurement_dialog_module.time.monotonic
        route_measurement_dialog_module.time.monotonic = lambda: 160.0
        try:
            text = RouteMeasurementDialog._progress_eta_text(dialog, 188, 292)
        finally:
            route_measurement_dialog_module.time.monotonic = original_monotonic

        self.assertIn("Remaining: 1:44:00 | Finish:", text)
        self.assertNotIn("Remaining: 00:33", text)

    def test_route_contact_move_raises_needles_and_moves_only(self) -> None:
        window = Main.__new__(Main)
        calls: list[object] = []
        statuses: list[str] = []
        finished: list[tuple[bool, str]] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(
                ("move", x_mm, y_mm)
            ),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda message: statuses.append(str(message))
        )
        window.route_contact_move_finished = types.SimpleNamespace(
            emit=lambda success, message: finished.append((bool(success), str(message)))
        )

        Main._run_route_contact_move(window, point, 75.0)

        self.assertEqual(
            calls,
            [
                ("begin", "route contact move"),
                ("needles", "raise", 75.0),
                ("move", 1.25, 2.5),
                ("finish",),
            ],
        )
        self.assertTrue(statuses)
        self.assertEqual(
            finished,
            [(True, "Route contact move complete: point 7 P007.")],
        )

    def test_route_contact_move_applies_saved_api_route_shift(self) -> None:
        window = Main.__new__(Main)
        calls: list[object] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        window._api_route_offset_xy = (0.1, -0.2)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(
                ("move", x_mm, y_mm)
            ),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.route_measurement_status = types.SimpleNamespace(emit=lambda _message: None)
        window.route_contact_move_finished = types.SimpleNamespace(
            emit=lambda _success, _message: None
        )

        Main._run_route_contact_move(window, point, 75.0)

        self.assertIn(("move", 1.35, 2.3), calls)

    def test_api_route_control_save_shift_updates_direct_route_offset(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        window._route_measurement_runner = None
        window._route_measurement_thread = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_offset_xy = (0.0, 0.0)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
        }
        window._stage_xy_from_position = lambda _position: (1.75, 2.25)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (0.0, 0.0, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(window._api_route_offset_xy, (0.5, -0.25))
        self.assertEqual(
            Main._api_route_adjusted_stage_xy(window, point),
            (1.75, 2.25),
        )
        self.assertEqual(
            statuses,
            ["Route shift saved: dX=+0.5000 mm, dY=-0.2500 mm."],
        )

    def test_cancel_route_measurement_session_clears_persisted_state(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            route_point = types.SimpleNamespace(camera_center=(10.0, 20.0))
            route = types.SimpleNamespace(points=[route_point])
            selected: list[int] = []

            def select_route_point(index: int) -> object:
                selected.append(index)
                return route_point

            window._route_measurement_thread = None
            window._route_measurement_runner = None
            window._route_measurement_dialog = None
            window._route_measurement_session_active = True
            window._route_measurement_current_point = 5
            window.settings_manager = types.SimpleNamespace(
                config_dir=lambda: config_dir
            )
            window._design_session = types.SimpleNamespace(
                route=route,
                selected_route_point_index=-1,
                select_route_point=select_route_point,
            )
            window._last_selected_design_point = None
            window._refresh_design_panel = lambda: None
            window._persist_controller_state_if_available = lambda: None
            window._show_status = (
                lambda message, _timeout_ms=None: statuses.append(str(message))
            )

            Main._cancel_route_measurement_session(window)

            settings_path = config_dir / "route-measurement-settings.json"
            with settings_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)

        self.assertFalse(window._route_measurement_session_active)
        self.assertEqual(window._route_measurement_current_point, 1)
        self.assertEqual(selected, [0])
        self.assertFalse(data["measurement_session_active"])
        self.assertFalse(data["measurement_pending"])
        self.assertEqual(data["current_point"], 1)
        self.assertIn("Route measurement session cancelled.", statuses)

    def test_measure_selected_route_point_while_waiting_submits_jump(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        statuses: list[str] = []

        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_runner = runner
        window._route_measurement_waiting = True
        window._route_measurement_current_point = 38
        window._pending_route_measure_point = 123
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._request_route_measurement_for_point(window, 91)

        self.assertEqual(runner.confirmations, ["jump:91"])
        self.assertIsNone(window._pending_route_measure_point)
        self.assertEqual(statuses, ["Route measurement: measure from point 91."])

    def test_resume_after_waiting_point_change_jumps_to_selected_point(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        resumed: list[int] = []
        statuses: list[str] = []

        window._route_measurement_runner = runner
        window._route_measurement_waiting = True
        window._pending_route_measure_point = None
        window._route_contact_move_thread = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = False
        window._set_route_measurement_resume_point = resumed.append
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._on_route_measurement_current_point_changed(window, 42)
        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(resumed, [42])
        self.assertIsNone(window._pending_route_measure_point)
        self.assertEqual(runner.confirmations, ["jump:42"])
        self.assertEqual(statuses, ["Route measurement: measure from point 42."])

    def test_measure_selected_route_point_while_running_queues_interrupt(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        statuses: list[str] = []

        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_runner = runner
        window._route_measurement_waiting = False
        window._pending_route_measure_point = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        cancellations: list[str] = []
        window.stage_controller = types.SimpleNamespace(
            cancel_active_task=lambda reason: cancellations.append(str(reason))
        )
        window._clear_stage_motion_axes = lambda: None
        window._schedule_status_refreshes = lambda _delays: None
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._request_route_measurement_for_point(window, 91)

        self.assertTrue(runner.correction_requested)
        self.assertEqual(cancellations, ["Route measurement interrupt requested."])
        self.assertEqual(window._pending_route_measure_point, 91)
        self.assertEqual(
            statuses,
            [
                "Stopping contact measurement, then measuring point 91."
            ],
        )
        self.assertEqual(runner.confirmations, [])

    def test_api_route_control_interrupt_cancels_gui_work_and_pauses(self) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main()
        aborts: list[str] = []

        window._route_measurement_runner = None
        window._route_measurement_waiting = False
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._pending_route_measure_point = 91
        window._contact_seek_stop_requested = threading.Event()
        window._api_route_control_active = True
        window._api_route_control_pause_requested = True
        window._api_route_control_paused = False
        window._api_route_control_stop_requested = True
        window._api_route_control_label = "chip 163"
        window._api_route_control_updated_utc = ""
        window.lcr_controller = types.SimpleNamespace(
            abort_current_measurement=lambda: aborts.append("lcr")
        )
        window._api_route_lcr_controller = None
        window._controller_reports_active_motion = lambda: True

        Main._request_route_measurement_point_correction(window)

        self.assertIsNone(window._pending_route_measure_point)
        self.assertTrue(window._contact_seek_stop_requested.is_set())
        self.assertEqual(aborts, [])
        self.assertEqual(
            stage_controller.cancelled_tasks,
            ["API route control interrupt requested."],
        )
        self.assertEqual(
            stage_controller.cancelled_motions,
            ["API route control interrupt requested."],
        )
        self.assertTrue(window._api_route_control_active)
        self.assertFalse(window._api_route_control_pause_requested)
        self.assertTrue(window._api_route_control_paused)
        self.assertFalse(window._api_route_control_stop_requested)
        self.assertEqual(statuses[-1], "chip 163: interrupted; paused.")

    def test_contact_seek_cancel_does_not_abort_measurement_instrument(self) -> None:
        window = Main.__new__(Main)
        aborts: list[str] = []
        cancelled_motions: list[str] = []
        statuses: list[str] = []

        window._contact_seek_stop_requested = threading.Event()
        window.lcr_controller = types.SimpleNamespace(
            abort_current_measurement=lambda: aborts.append("lcr")
        )
        window.stage_controller = types.SimpleNamespace(
            cancel_active_motion=lambda reason: cancelled_motions.append(str(reason))
        )
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._cancel_contact_seek(window)

        self.assertTrue(window._contact_seek_stop_requested.is_set())
        self.assertEqual(aborts, [])
        self.assertEqual(cancelled_motions, ["Contact seek cancel requested."])
        self.assertEqual(statuses, ["Contact seek cancel requested."])

    def test_api_route_control_confirmation_preserves_requested_action(self) -> None:
        window = Main.__new__(Main)
        actions: list[dict[str, object]] = []

        window._route_measurement_runner = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = lambda *_args: None

        Main._submit_route_measurement_confirmation(window, "skip")

        self.assertEqual(actions, [{"action": "skip"}])

    def test_api_route_control_action_is_consumed_explicitly(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        window._api_route_control_active = True
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = True
        window._api_route_control_stop_requested = False
        window._api_route_control_pending_action = ""
        window._api_route_control_label = "chip 163"
        window._api_route_control_updated_utc = ""
        window.design_navigator_panel = None
        window._route_measurement_dialog = _FakeVisibleDialog(True)
        window._api_timestamp_utc = lambda: "now"
        window._show_status = lambda message, _timeout_ms=None: statuses.append(str(message))

        response = Main._api_route_control_action(window, {"action": "skip"})

        self.assertFalse(window._api_route_control_pause_requested)
        self.assertFalse(window._api_route_control_paused)
        self.assertEqual(window._api_route_control_pending_action, "skip")
        self.assertEqual(response["pending_action"], "skip")

        ack = Main._api_route_control_action(window, {"action": "ack"})

        self.assertEqual(window._api_route_control_pending_action, "")
        self.assertEqual(ack["pending_action"], "")
        self.assertEqual(statuses[-1], "chip 163: skip.")

    def test_api_route_control_start_clears_waiting_route_runner(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        opened: list[bool] = []
        runner = _FakeRouteMeasurementRunner()
        thread = _FakeJoinableThread()

        window._route_measurement_runner = runner
        window._route_measurement_thread = thread
        window._route_measurement_waiting = True
        window._route_measurement_waiting_reason = "paused"
        window._route_measurement_session_active = True
        window._pending_route_measure_point = 33
        window._api_route_control_active = False
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = False
        window._api_route_control_stop_requested = False
        window._api_route_control_pending_action = ""
        window._api_route_control_label = ""
        window._api_route_control_updated_utc = ""
        window.design_navigator_panel = None
        window._route_measurement_dialog = None
        window._api_timestamp_utc = lambda: "now"
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )

        def open_controls() -> bool:
            opened.append(True)
            window._route_measurement_dialog = _FakeVisibleDialog(True)
            return True

        window._show_route_measurement_dialog_for_api_session = open_controls

        response = Main._api_route_control_action(
            window,
            {"action": "start", "label": "chip 163"},
        )

        self.assertTrue(response["accepted"])
        self.assertTrue(runner.stop_requested)
        self.assertEqual(thread.join_calls, [2.0])
        self.assertIsNone(window._route_measurement_runner)
        self.assertIsNone(window._route_measurement_thread)
        self.assertFalse(window._route_measurement_waiting)
        self.assertFalse(window._route_measurement_session_active)
        self.assertIsNone(window._pending_route_measure_point)
        self.assertTrue(window._api_route_control_active)
        self.assertEqual(statuses[-1], "chip 163: running.")
        self.assertEqual(opened, [True])

    def test_api_route_control_start_rejects_when_controls_do_not_open(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        opened: list[bool] = []

        window._route_measurement_runner = None
        window._route_measurement_thread = None
        window._api_route_control_active = False
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = False
        window._api_route_control_stop_requested = False
        window._api_route_control_pending_action = ""
        window._api_route_control_label = ""
        window._api_route_control_updated_utc = ""
        window.design_navigator_panel = None
        window._route_measurement_dialog = None
        window._api_timestamp_utc = lambda: "now"
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )
        window._show_route_measurement_dialog_for_api_session = (
            lambda: opened.append(True) or False
        )

        response = Main._api_route_control_action(
            window,
            {"action": "start", "label": "chip 163"},
        )

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertFalse(window._api_route_control_active)
        self.assertEqual(opened, [True])
        self.assertIn("window did not open", response["message"])

    def test_api_route_control_start_rejects_busy_route_runner(self) -> None:
        window = Main.__new__(Main)

        window._route_measurement_runner = _FakeRouteMeasurementRunner()
        window._route_measurement_thread = _FakeJoinableThread()
        window._route_measurement_waiting = False
        window._api_route_control_active = False

        response = Main._api_route_control_action(
            window,
            {"action": "start", "label": "chip 163"},
        )

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertIn("already active", response["message"])
        self.assertFalse(window._api_route_control_active)

    def test_api_route_control_start_rejects_external_route_session(self) -> None:
        window = Main.__new__(Main)
        runner = types.SimpleNamespace(
            stop=lambda: None,
            submit_external_result=lambda _result: True,
        )

        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeJoinableThread()
        window._route_measurement_waiting = True
        window._api_route_control_active = False

        response = Main._api_route_control_action(
            window,
            {"action": "start", "label": "chip 163"},
        )

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertFalse(window._api_route_control_active)

    def test_probe_route_api_rejects_route_command_without_control_window(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        window._route_measurement_dialog = None
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        response = Main._submit_api_command_request(
            window,
            {
                "action": "move_to_contact",
                "payload": {"contact_number": 33},
            },
        )

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertFalse(response["route_control_window_open"])
        self.assertIn("Probe route control window is closed", response["message"])
        self.assertEqual(statuses, [response["message"]])

    def test_api_route_control_command_uses_gui_thread_bridge(self) -> None:
        window = Main.__new__(Main)
        requests: list[tuple[dict[str, object], float]] = []

        def submit(request, *, timeout_s=5.0):
            requests.append((dict(request), float(timeout_s)))
            return {"accepted": True, "bridged": True}

        window._api_bridge = types.SimpleNamespace(submit=submit)

        response = Main._submit_api_command_request_from_api_thread(
            window,
            {
                "action": "api_route_control_action",
                "payload": {"action": "start"},
            },
        )

        self.assertTrue(response["accepted"])
        self.assertTrue(response["bridged"])
        self.assertEqual(len(requests), 1)
        bridged_request, timeout_s = requests[0]
        self.assertEqual(bridged_request["action"], "command")
        self.assertEqual(
            bridged_request["command"],
            {
                "action": "api_route_control_action",
                "payload": {"action": "start"},
            },
        )
        self.assertEqual(timeout_s, 10.0)

    def test_probe_route_api_guard_uses_gui_thread_before_direct_command(self) -> None:
        window = Main.__new__(Main)
        bridge_requests: list[tuple[dict[str, object], float]] = []
        dispatch_calls: list[tuple[dict[str, object], bool]] = []

        def submit(request, *, timeout_s=5.0):
            bridge_requests.append((dict(request), float(timeout_s)))
            return {"accepted": True, "route_control_window_open": True}

        def dispatch(command_request, *, apply_route_control_guard):
            dispatch_calls.append(
                (dict(command_request), bool(apply_route_control_guard))
            )
            return {"accepted": True, "dispatched": True}

        window._api_bridge = types.SimpleNamespace(submit=submit)
        window._dispatch_api_command_request = dispatch

        command = {
            "action": "move_to_contact",
            "payload": {"contact_number": 33},
        }
        response = Main._submit_api_command_request_from_api_thread(window, command)

        self.assertTrue(response["accepted"])
        self.assertTrue(response["dispatched"])
        self.assertEqual(len(bridge_requests), 1)
        guard_request, timeout_s = bridge_requests[0]
        self.assertEqual(guard_request["action"], "probe_route_window_guard")
        self.assertEqual(guard_request["guard_action"], "move_to_contact")
        self.assertEqual(guard_request["payload"], {"contact_number": 33})
        self.assertEqual(timeout_s, 10.0)
        self.assertEqual(dispatch_calls, [(command, False)])

    def test_probe_route_api_guard_does_not_cover_bare_stage_move(self) -> None:
        window = Main.__new__(Main)

        self.assertFalse(
            Main._probe_route_api_requires_window(
                window,
                "move_to_coordinates",
                {"x": 1.0},
            )
        )

    def test_api_route_control_resume_rejects_closed_control_window(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        window._route_measurement_dialog = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        response = Main._api_route_control_action(window, {"action": "resume"})

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertFalse(response["route_control_window_open"])
        self.assertEqual(statuses, [response["message"]])

    def test_api_route_control_pause_request_waits_for_ack_before_resume(self) -> None:
        window = Main.__new__(Main)
        calls: list[tuple[str, object]] = []
        statuses: list[str] = []

        panel = types.SimpleNamespace(
            set_route_measurement_running=lambda value: calls.append(("running", value)),
            set_route_measurement_pause_request_pending=lambda value: calls.append(
                ("pending", value)
            ),
            set_route_measurement_waiting=lambda value, reason="": calls.append(
                ("waiting", value, reason)
            ),
            set_route_measurement_status=lambda value: calls.append(("status", value)),
        )
        window.design_navigator_panel = panel
        window._route_measurement_dialog = None
        window._api_route_control_active = True
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = False
        window._api_route_control_stop_requested = False
        window._api_route_control_pending_action = ""
        window._api_route_control_label = "chip 163"
        window._api_route_control_updated_utc = ""
        window._api_timestamp_utc = lambda: "now"
        window._show_status = lambda message, _timeout_ms=None: statuses.append(str(message))

        pause = Main._api_route_control_action(window, {"action": "pause"})

        self.assertTrue(pause["pause_requested"])
        self.assertFalse(pause["paused"])
        self.assertTrue(window._api_route_control_pause_requested)
        self.assertFalse(window._api_route_control_paused)
        self.assertFalse(window._route_measurement_waiting)
        self.assertIn(("pending", True), calls)
        self.assertIn(("waiting", False, "paused"), calls)

        paused = Main._api_route_control_action(window, {"action": "pause_ack"})

        self.assertFalse(paused["pause_requested"])
        self.assertTrue(paused["paused"])
        self.assertFalse(window._api_route_control_pause_requested)
        self.assertTrue(window._api_route_control_paused)
        self.assertTrue(window._route_measurement_waiting)
        self.assertIn(("pending", False), calls)

    def test_api_route_control_pending_pause_click_interrupts(self) -> None:
        window = Main.__new__(Main)
        interrupts: list[str] = []

        window._route_measurement_runner = None
        window._api_route_control_active = True
        window._api_route_control_pause_requested = True
        window._api_route_control_paused = False
        window._interrupt_api_route_controlled_operation = (
            lambda reason: interrupts.append(str(reason))
        )
        window._show_status = lambda *_args: None

        Main._request_pause_route_measurement(window)

        self.assertEqual(interrupts, ["API route control interrupt requested."])

    def test_api_route_control_pause_uses_control_state_with_existing_route_runner(self) -> None:
        window = Main.__new__(Main)
        actions: list[dict[str, object]] = []
        runner = types.SimpleNamespace(
            request_pause_after_current_point=lambda: actions.append(
                {"action": "runner_pause"}
            )
        )

        window._route_measurement_runner = runner
        window._api_route_control_active = True
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = False
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = lambda *_args: None

        Main._request_pause_route_measurement(window)

        self.assertEqual(actions, [{"action": "pause"}])

    def test_api_route_control_interrupt_uses_control_state_with_existing_route_runner(self) -> None:
        window = Main.__new__(Main)
        interrupts: list[str] = []
        runner = _FakeRouteMeasurementRunner()

        window._route_measurement_runner = runner
        window._api_route_control_active = True
        window._api_route_control_pause_requested = True
        window._api_route_control_paused = False
        window._interrupt_api_route_controlled_operation = (
            lambda reason: interrupts.append(str(reason))
        )
        window._show_status = lambda *_args: None

        Main._request_route_measurement_point_correction(window)

        self.assertFalse(runner.correction_requested)
        self.assertEqual(interrupts, ["API route control interrupt requested."])

    def test_api_route_control_resume_uses_control_state_with_existing_route_runner(self) -> None:
        window = Main.__new__(Main)
        actions: list[dict[str, object]] = []
        runner = _FakeRouteMeasurementRunner()

        window._route_measurement_runner = runner
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = lambda *_args: None

        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(runner.confirmations, [])
        self.assertEqual(actions, [{"action": "resume"}])

    def test_api_route_control_save_shift_uses_control_state_with_existing_route_runner(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        runner_calls: list[object] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda point_number: runner_calls.append(
                ("select", point_number)
            )
            or (True, "selected"),
            save_current_position_adjustment=lambda stage_xy: runner_calls.append(
                ("save", stage_xy)
            )
            or (True, "saved by runner"),
        )

        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_offset_xy = (0.0, 0.0)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
        }
        window._stage_xy_from_position = lambda _position: (1.75, 2.25)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (0.0, 0.0, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(runner_calls, [])
        self.assertEqual(window._api_route_offset_xy, (0.5, -0.25))
        self.assertEqual(
            statuses,
            ["Route shift saved: dX=+0.5000 mm, dY=-0.2500 mm."],
        )

    def test_blocked_route_shift_save_does_not_read_dialog_configuration(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        dialog_calls: list[str] = []

        class _Dialog:
            def current_configuration(self) -> object:
                dialog_calls.append("current_configuration")
                return types.SimpleNamespace(current_point=7)

        window._route_measurement_runner = None
        window._route_measurement_thread = None
        window._route_measurement_dialog = _Dialog()
        window._route_measurement_current_point = None
        window._api_route_control_active = True
        window._api_route_control_paused = False
        window._show_route_measurement_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._save_route_measurement_shift(window)

        self.assertEqual(dialog_calls, [])
        self.assertEqual(statuses, ["Pause API route control before saving shift."])

    def test_api_route_control_save_shift_then_resume_does_not_skip_contact(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        actions: list[dict[str, object]] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )

        window._route_measurement_runner = None
        window._route_measurement_thread = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_offset_xy = (0.0, 0.0)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
        }
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._stage_xy_from_position = lambda _position: (1.75, 2.25)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (0.0, 0.0, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)
        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(window._api_route_offset_xy, (0.5, -0.25))
        self.assertEqual(actions, [{"action": "resume"}])

    def test_route_contact_move_requires_paused_api_route_control(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        window._route_contact_move_thread = None
        window._route_measurement_thread = None
        window._api_route_control_active = True
        window._api_route_control_paused = False
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._request_route_contact_move(window, 33)

        self.assertEqual(
            statuses,
            ["Pause API route control before moving to a contact."],
        )

    def test_telegram_measure_submits_waiting_route_action(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        statuses: list[str] = []

        window._route_measurement_waiting = True
        window._route_measurement_runner = runner
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._telegram_default_markup = lambda: "markup"
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        response = Main._telegram_route_action_response(window, "measure")

        self.assertEqual(runner.confirmations, ["measure"])
        self.assertEqual(response.callback_answer, "measure submitted.")
        self.assertEqual(response.reply_markup, "markup")
        self.assertEqual(statuses, ["Route measurement: measure."])

    def test_telegram_remeasure_action_is_disabled(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()

        window._route_measurement_waiting = True
        window._route_measurement_runner = runner
        window._telegram_default_markup = lambda: "markup"

        response = Main._telegram_route_action_response(window, "remeasure")

        self.assertEqual(runner.confirmations, [])
        self.assertEqual(response.callback_answer, "Unknown action.")
        self.assertEqual(response.reply_markup, "markup")

    def test_telegram_route_actions_markup_omits_remeasure(self) -> None:
        original = main_module.telegram_inline_keyboard
        main_module.telegram_inline_keyboard = lambda rows: rows
        try:
            markup = Main._telegram_route_actions_markup()
        finally:
            main_module.telegram_inline_keyboard = original

        labels = [
            label
            for row in markup
            for label, _callback in row
        ]
        self.assertIn("Measure", labels)
        self.assertIn("Skip", labels)
        self.assertNotIn("Remeasure", labels)

    def test_telegram_skip_submits_paused_api_route_control_action(self) -> None:
        window = Main.__new__(Main)
        actions: list[dict[str, object]] = []

        window._route_measurement_waiting = True
        window._route_measurement_runner = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._telegram_default_markup = lambda: "markup"
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = lambda *_args: None

        response = Main._telegram_route_action_response(window, "skip")

        self.assertEqual(actions, [{"action": "skip"}])
        self.assertEqual(response.callback_answer, "skip submitted.")
        self.assertEqual(response.reply_markup, "markup")

    def test_waiting_dialog_measure_confirms_current_contact(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        confirmed: list[bool] = []
        remeasured: list[bool] = []
        measured: list[bool] = []

        dialog._running = True
        dialog._waiting = True
        dialog.measure_current_requested = types.SimpleNamespace(
            emit=lambda: confirmed.append(True)
        )
        dialog.remeasure_requested = types.SimpleNamespace(
            emit=lambda: remeasured.append(True)
        )
        dialog._emit_measure_requested = lambda: measured.append(True)

        RouteMeasurementDialog._emit_next_or_measure_requested(dialog)

        self.assertEqual(confirmed, [True])
        self.assertEqual(remeasured, [])
        self.assertEqual(measured, [])

    def test_waiting_dialog_pause_and_interrupt_buttons_resume(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        resumed: list[bool] = []
        interrupted: list[bool] = []
        paused: list[bool] = []
        dialog._running = True
        dialog._waiting = True
        dialog._waiting_reason = "paused"
        dialog._pause_request_pending = False
        dialog._interrupt_request_pending = False
        dialog._pause_button = _FakeButton()
        dialog._interrupt_button = _FakeButton()
        dialog.next_requested = types.SimpleNamespace(
            emit=lambda: resumed.append(True)
        )
        dialog.pause_requested = types.SimpleNamespace(
            emit=lambda: paused.append(True)
        )
        dialog.interrupt_requested = types.SimpleNamespace(
            emit=lambda: interrupted.append(True)
        )

        RouteMeasurementDialog._update_pause_interrupt_buttons(dialog)
        RouteMeasurementDialog._emit_pause_requested(dialog)
        RouteMeasurementDialog._emit_interrupt_or_resume_requested(dialog)

        self.assertEqual(dialog._pause_button.text, "Resume")
        self.assertEqual(dialog._interrupt_button.text, "Resume")
        self.assertTrue(dialog._pause_button.enabled)
        self.assertFalse(dialog._interrupt_button.enabled)
        self.assertEqual(len(resumed), 2)
        self.assertEqual(paused, [])
        self.assertEqual(interrupted, [])

    def test_running_dialog_pause_button_becomes_interrupt_until_waiting(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        resumed: list[bool] = []
        interrupted: list[bool] = []
        paused: list[bool] = []
        dialog._running = True
        dialog._waiting = False
        dialog._waiting_reason = ""
        dialog._pause_request_pending = False
        dialog._interrupt_request_pending = False
        dialog._pause_button = _FakeButton()
        dialog._interrupt_button = _FakeButton()
        dialog.next_requested = types.SimpleNamespace(
            emit=lambda: resumed.append(True)
        )
        dialog.pause_requested = types.SimpleNamespace(
            emit=lambda: paused.append(True)
        )
        dialog.interrupt_requested = types.SimpleNamespace(
            emit=lambda: interrupted.append(True)
        )

        RouteMeasurementDialog._update_pause_interrupt_buttons(dialog)
        self.assertEqual(dialog._pause_button.text, "Pause")

        RouteMeasurementDialog._emit_pause_requested(dialog)
        self.assertEqual(paused, [True])
        self.assertEqual(dialog._pause_button.text, "Interrupt")
        self.assertTrue(dialog._pause_button.enabled)

        RouteMeasurementDialog._emit_pause_requested(dialog)
        self.assertEqual(interrupted, [True])
        self.assertEqual(resumed, [])
        self.assertEqual(dialog._pause_button.text, "Interrupt")
        self.assertFalse(dialog._pause_button.enabled)

        dialog._waiting = True
        dialog._waiting_reason = "paused"
        dialog._pause_request_pending = False
        dialog._interrupt_request_pending = False
        RouteMeasurementDialog._update_pause_interrupt_buttons(dialog)
        self.assertEqual(dialog._pause_button.text, "Resume")
        self.assertTrue(dialog._pause_button.enabled)

    def test_external_measurement_waiting_keeps_interrupt_control(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        dialog._running = True
        dialog._waiting = False
        dialog._waiting_reason = ""
        dialog._pause_request_pending = False
        dialog._interrupt_request_pending = False
        dialog._pause_button = _FakeButton()
        dialog._interrupt_button = _FakeButton()
        dialog._stop_button = _FakeButton()
        dialog._save_shift_button = _FakeButton()
        dialog._remeasure_button = _FakeButton()
        dialog._skip_button = _FakeButton()
        dialog._next_button = _FakeButton()
        dialog._operation_combo = _FakeButton()
        dialog._jump_point_spin = _FakeButton()
        dialog._move_button = _FakeButton()
        dialog._jump_button = _FakeButton()
        dialog._set_runtime_settings_enabled = lambda _enabled: None
        dialog._update_operation_state = lambda: None
        dialog._update_session_buttons = (
            lambda: RouteMeasurementDialog._update_session_buttons(dialog)
        )
        dialog._start_session_button = _FakeButton()
        dialog._cancel_session_button = _FakeButton()
        dialog._measurement_session_active = True
        dialog._measure_button = _FakeButton()

        RouteMeasurementDialog.set_waiting(
            dialog,
            True,
            reason="external_measurement",
        )

        self.assertEqual(dialog._pause_button.text, "Interrupt")
        self.assertTrue(dialog._pause_button.enabled)
        self.assertFalse(dialog._next_button.enabled)
        self.assertFalse(dialog._measure_button.enabled)
        self.assertFalse(dialog._save_shift_button.enabled)
        self.assertFalse(dialog._skip_button.enabled)

    def test_waiting_dialog_enables_runtime_output_and_meter_fields(self) -> None:
        class _FakeEnabledWidget:
            def __init__(self, *, checked: bool = False) -> None:
                self.enabled = False
                self._checked = bool(checked)

            def setEnabled(self, enabled: bool) -> None:  # noqa: N802
                self.enabled = bool(enabled)

            def isChecked(self) -> bool:  # noqa: N802
                return self._checked

        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        dialog._running = True
        dialog._waiting = False
        dialog._operation_combo = _FakeEnabledWidget()
        dialog._operation_combo.currentData = lambda: (
            route_measurement_dialog_module.ROUTE_OPERATION_PHOTO_THEN_MEASURE
        )
        dialog._photo_dir_edit = _FakeEnabledWidget()
        dialog._photo_browse_button = _FakeEnabledWidget()
        dialog._photo_settle_spin = _FakeEnabledWidget()
        dialog._photo_autofocus_checkbox = _FakeEnabledWidget(checked=True)
        dialog._photo_autofocus_range_spin = _FakeEnabledWidget()
        dialog._csv_path_edit = _FakeEnabledWidget()
        dialog._csv_browse_button = _FakeEnabledWidget()
        dialog._previous_ok_only_checkbox = _FakeEnabledWidget(checked=True)
        dialog._previous_csv_path_edit = _FakeEnabledWidget()
        dialog._previous_csv_browse_button = _FakeEnabledWidget()
        dialog._meter_combo = _FakeEnabledWidget()
        dialog._gwinstek_page = _FakeEnabledWidget()
        dialog._keithley_page = _FakeEnabledWidget()
        dialog._initial_measurement_count_spin = _FakeEnabledWidget()
        dialog._followup_measurement_count_spin = _FakeEnabledWidget()
        dialog._max_relative_rms_spin = _FakeEnabledWidget()
        dialog._contact_settle_spin = _FakeEnabledWidget()
        dialog._contact_seek_range_spin = _FakeEnabledWidget()
        dialog._contact_seek_step_spin = _FakeEnabledWidget()
        dialog._contact_max_mad_sigma_spin = _FakeEnabledWidget()
        dialog._contact_max_p95_step_spin = _FakeEnabledWidget()
        dialog._contact_max_relative_mad_spin = _FakeEnabledWidget()
        dialog._contact_max_relative_p95_step_spin = _FakeEnabledWidget()
        dialog._pause_button = _FakeButton()
        dialog._interrupt_button = _FakeButton()
        dialog._stop_button = _FakeButton()
        dialog._save_shift_button = _FakeButton()
        dialog._remeasure_button = _FakeButton()
        dialog._skip_button = _FakeButton()
        dialog._next_button = _FakeButton()
        dialog._jump_point_spin = _FakeEnabledWidget()
        dialog._move_button = _FakeButton()
        dialog._jump_button = _FakeButton()
        dialog._set_runtime_settings_enabled = lambda _enabled: None
        dialog._update_session_buttons = lambda: None

        RouteMeasurementDialog.set_waiting(dialog, True)

        self.assertTrue(dialog._csv_path_edit.enabled)
        self.assertTrue(dialog._operation_combo.enabled)
        self.assertTrue(dialog._photo_dir_edit.enabled)
        self.assertTrue(dialog._photo_autofocus_checkbox.enabled)
        self.assertTrue(dialog._photo_autofocus_range_spin.enabled)
        self.assertTrue(dialog._previous_ok_only_checkbox.enabled)
        self.assertTrue(dialog._previous_csv_path_edit.enabled)
        self.assertTrue(dialog._meter_combo.enabled)
        self.assertTrue(dialog._gwinstek_page.enabled)
        self.assertTrue(dialog._keithley_page.enabled)

    def test_running_dialog_close_is_ignored(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        statuses: list[str] = []

        class _CloseEvent:
            def __init__(self) -> None:
                self.ignored = False

            def ignore(self) -> None:
                self.ignored = True

        event = _CloseEvent()
        dialog._running = True
        dialog.set_status = lambda message: statuses.append(str(message))

        RouteMeasurementDialog.closeEvent(dialog, event)

        self.assertTrue(event.ignored)
        self.assertEqual(statuses, ["Stop route measurement before closing."])

    def test_main_shutdown_forces_route_dialog_close(self) -> None:
        window = Main.__new__(Main)
        calls: list[tuple[str, bool | None]] = []
        window._route_measurement_dialog = types.SimpleNamespace(
            set_running=lambda value: calls.append(("running", bool(value))),
            close=lambda: calls.append(("close", None)),
        )
        window.design_layout_window = None
        window.contact_calibration_window = None
        window.surface_map_window = None
        window.microscope_scan_dialog = None

        Main._close_auxiliary_windows(window, force_route_dialog=True)

        self.assertEqual(calls, [("running", False), ("close", None)])

    def test_api_route_start_takes_over_waiting_gui_runner(self) -> None:
        class _FakeEmit:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def emit(self, *args: object) -> None:
                self.calls.append(tuple(args))

        class _FakeAliveThread:
            def __init__(self) -> None:
                self.joined = False

            def is_alive(self) -> bool:
                return not self.joined

            def join(self, timeout: float | None = None) -> None:
                _ = timeout
                self.joined = True

        class _FakeConnectedLcr:
            def __init__(self) -> None:
                self.configurations: list[RouteMeterConfiguration] = []

            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                configuration: RouteMeterConfiguration,
            ) -> None:
                self.configurations.append(configuration)

        point = RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )
        stage = types.SimpleNamespace()
        old_runner = RouteMeasurementRunner(
            points=[point],
            csv_path="NUL",
            stage_controller=stage,
            lcr_controller=types.SimpleNamespace(),
            needle_feedrate=None,
            wait_before_first_point=True,
        )
        old_runner.set_route_offset_xy((0.125, -0.25))
        old_thread = _FakeAliveThread()
        lcr = _FakeConnectedLcr()
        window = Main.__new__(Main)
        window._route_measurement_thread = old_thread
        window._route_measurement_runner = old_runner
        window._route_measurement_waiting = True
        window._last_route_measurement_result = None
        window._route_measurement_current_point = 1
        window._route_measurement_session_active = True
        window._route_measurement_dialog = None
        window._api_route_lcr_controller = None
        window._api_route_last_status = None
        window._api_route_session_id = None
        window._api_route_artifacts = {}
        window._api_route_artifacts_lock = threading.Lock()
        window._telegram_photo_lock = threading.Lock()
        window._telegram_pending_contact_photo = None
        window._telegram_pending_contact_before_photo = None
        window._last_route_pre_contact_photo = None
        window._last_route_contact_failure_photo = None
        window._last_route_contact_failure_before_photo = None
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(points=[object()], name="route"),
            registration=types.SimpleNamespace(valid=True),
        )
        window.serial_connection = types.SimpleNamespace(is_open=True)
        window.stage_controller = stage
        window.lcr_controller = lcr
        window.route_measurement_status = _FakeEmit()
        window.route_measurement_progress = _FakeEmit()
        window.route_measurement_result = _FakeEmit()
        window.route_measurement_waiting_changed = _FakeEmit()
        window.route_measurement_started = _FakeEmit()
        window.route_measurement_finished = _FakeEmit()
        window._route_measurement_points = lambda _route: [point]
        window._wait_for_camera_frame = lambda timeout_s=0.1: (None, None)
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window.settings_manager = types.SimpleNamespace(
            needle_calibration_configuration=lambda: types.SimpleNamespace(
                feedrate_mm_min=2.0,
            )
        )
        window._capture_api_route_photo_artifact = (
            lambda _point, _position, _total, _focus_result: []
        )
        window._api_route_photo_autofocus = (
            lambda _point, _position, _total, *, range_mm: None
        )
        window._capture_route_contact_photo = lambda *args, **kwargs: None
        window._capture_route_pre_contact_photo = lambda *args, **kwargs: None
        window._send_telegram_alert = lambda *args, **kwargs: None

        response = Main._api_start_route_session(
            window,
            {},
        )
        new_runner = window._route_measurement_runner

        self.assertTrue(response["accepted"], response)
        self.assertTrue(old_thread.joined)
        self.assertIsInstance(new_runner, RouteExternalMeasurementSessionRunner)
        self.assertEqual(new_runner.route_offset_xy(), (0.125, -0.25))
        self.assertEqual(lcr.configurations, [RouteMeterConfiguration()])
        self.assertEqual(response["state"], "waiting_paused")

        Main._on_route_measurement_finished(
            window,
            old_runner,
            False,
            "Old route stopped.",
            "",
        )
        self.assertIs(window._route_measurement_runner, new_runner)

        new_runner.stop()
        window._route_measurement_thread.join(timeout=2.0)

    def test_prestart_route_measurement_defers_camera_frame_check(self) -> None:
        class _FakeEmit:
            def emit(self, *_args: object) -> None:
                pass

        class _FakeLcr:
            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                _configuration: RouteMeterConfiguration,
            ) -> None:
                pass

        point = RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )
        window = Main.__new__(Main)
        statuses: list[str] = []
        camera_calls: list[float] = []
        dialog_calls: list[tuple[str, object]] = []
        window._route_measurement_thread = None
        window.serial_connection = types.SimpleNamespace(is_open=True)
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
        window._active_microscope_scale = lambda: None

        def wait_for_camera_frame(*, timeout_s: float = 0.1, **_kwargs):
            camera_calls.append(float(timeout_s))
            return None, None

        window._wait_for_camera_frame = wait_for_camera_frame
        window.lcr_controller = _FakeLcr()
        window.stage_controller = types.SimpleNamespace()
        window._current_needle_feedrate = lambda: None
        window.route_measurement_status = _FakeEmit()
        window.route_measurement_progress = _FakeEmit()
        window.route_measurement_recorded = _FakeEmit()
        window.route_measurement_result = _FakeEmit()
        window.route_measurement_waiting_changed = _FakeEmit()
        window.design_navigator_panel = None
        window._route_measurement_dialog = types.SimpleNamespace(
            set_running=lambda value: dialog_calls.append(("running", bool(value))),
            reset_progress=lambda total: dialog_calls.append(("progress", int(total))),
            set_status=lambda message: dialog_calls.append(("status", str(message))),
        )
        window._show_status = lambda message, *_args: statuses.append(str(message))
        window._send_telegram_alert = lambda *args, **kwargs: None
        window._telegram_photo_lock = threading.Lock()
        window._update_stage_coordinate_apply_state = lambda: None

        configuration = RouteMeasurementRunConfiguration(
            csv_path="route.csv",
            previous_csv_path="route.csv",
            operation_mode=route_measurement_dialog_module.ROUTE_OPERATION_MEASURE,
            photo_output_dir="photos",
            photo_settle_s=0.0,
            photo_autofocus_enabled=True,
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
        original_thread = main_module.threading.Thread
        _FakeThread.instances = []
        main_module.threading.Thread = _FakeThread
        try:
            Main._start_route_measurement(
                window,
                configuration,
                wait_before_first_point=True,
            )
        finally:
            main_module.threading.Thread = original_thread

        self.assertEqual(camera_calls, [])
        self.assertEqual(len(_FakeThread.instances), 1)
        self.assertTrue(_FakeThread.instances[0].started)
        self.assertIsNotNone(window._route_measurement_runner)
        self.assertEqual(
            window._route_measurement_runner.contact_quality_limits().as_dict(),
            {
                "max_mad_sigma_ohm": 1_500.0,
                "max_p95_abs_step_ohm": 2_500.0,
                "max_relative_mad_sigma": 0.08,
                "max_relative_p95_abs_step": 0.12,
            },
        )
        self.assertEqual(dialog_calls[0], ("running", True))
        self.assertFalse(
            any("Camera frame is unavailable" in status for status in statuses)
        )

    def test_submit_restarts_waiting_runner_after_route_setup_change(self) -> None:
        class _FakeRunner:
            def __init__(self, offset=(0.0, 0.0)) -> None:
                self.offset = offset
                self.updated = False
                self.applied = False
                self.confirmations: list[str] = []
                self.runtime_settings: dict[str, object] = {}

            def route_offset_xy(self):
                return self.offset

            def update_runtime_settings(self, **kwargs) -> None:
                self.updated = True
                self.runtime_settings = dict(kwargs)

            def apply_meter_configuration(self, _configuration) -> None:
                self.applied = True

            def submit_confirmation(self, action: str) -> bool:
                self.confirmations.append(str(action))
                return True

        def configuration(
            *,
            previous_ok_only: bool,
            previous_csv_path: str,
        ) -> RouteMeasurementRunConfiguration:
            return RouteMeasurementRunConfiguration(
                csv_path="route.csv",
                previous_csv_path=previous_csv_path,
                operation_mode=route_measurement_dialog_module.ROUTE_OPERATION_MEASURE,
                photo_output_dir="photos",
                photo_settle_s=0.0,
                photo_autofocus_enabled=False,
                photo_autofocus_range_mm=0.03,
                initial_measurement_count=10,
                followup_measurement_count=240,
                current_point=1,
                max_relative_rms=0.01,
                contact_settle_s=0.0,
                contact_seek_range_mm=0.01,
                contact_seek_step_mm=0.001,
                previous_ok_only=previous_ok_only,
                meter=RouteMeterConfiguration(),
                contact_quality_limits=RouteContactQualityLimits(
                    max_mad_sigma_ohm=1_200.0,
                    max_p95_abs_step_ohm=1_800.0,
                    max_relative_mad_sigma=0.07,
                    max_relative_p95_abs_step=0.11,
                ),
            )

        previous = configuration(
            previous_ok_only=False,
            previous_csv_path="old.csv",
        )
        current = configuration(
            previous_ok_only=True,
            previous_csv_path="new.csv",
        )
        old_runner = _FakeRunner(offset=(0.125, -0.25))
        new_runner = _FakeRunner()
        restart_calls: list[tuple[RouteMeasurementRunConfiguration, tuple[float, float]]] = []
        window = Main.__new__(Main)
        window._route_measurement_runner = old_runner
        window._route_measurement_waiting = True
        window._route_measurement_runtime_configuration = previous
        window._route_measurement_dialog = types.SimpleNamespace(
            current_configuration=lambda: current,
            set_waiting=lambda _waiting: None,
            set_status=lambda _message: None,
        )
        window._route_contact_move_thread = None
        window._save_route_measurement_session_metadata = lambda _configuration: None
        window._show_status = lambda *_args: None
        window.design_navigator_panel = None

        def restart(
            config: RouteMeasurementRunConfiguration,
            *,
            route_offset_xy: tuple[float, float],
        ) -> bool:
            restart_calls.append((config, route_offset_xy))
            window._route_measurement_runner = new_runner
            return True

        window._restart_waiting_route_measurement = restart

        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(restart_calls, [(current, (0.125, -0.25))])
        self.assertEqual(old_runner.confirmations, [])
        self.assertTrue(new_runner.updated)
        self.assertEqual(
            new_runner.runtime_settings["contact_quality_limits"].as_dict(),
            {
                "max_mad_sigma_ohm": 1_200.0,
                "max_p95_abs_step_ohm": 1_800.0,
                "max_relative_mad_sigma": 0.07,
                "max_relative_p95_abs_step": 0.11,
            },
        )
        self.assertTrue(new_runner.applied)
        self.assertEqual(new_runner.confirmations, ["next"])

    def test_record_route_contact_height_writes_height_map_csv(self) -> None:
        window = Main.__new__(Main)
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(name="route-a")
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "measurements" / "route.csv"
            record = RouteContactHeightRecord(
                timestamp="2026-05-29T12:00:00+03:00",
                structure_number=1,
                point_index=1,
                point_id="p001",
                label="P001",
                design_center=(10.0, 20.0),
                stage_xy=(1.0, 2.0),
                measurement_status="ok",
                resistance_ohm=1000.5,
                resistance_rms_ohm=0.5,
                relative_rms=0.0005,
                contact_quality=RouteContactQuality(
                    assessed=True,
                    good=True,
                    status="good",
                    median_ohm=1000.5,
                    mad_sigma_ohm=1.2,
                    p95_abs_step_ohm=2.0,
                    span_ohm=3.0,
                ),
                contact_found=True,
                contact_depth_below_down_mm=0.002,
                contact_axis_a_lowering_mm=1.002,
                contact_seek=RouteContactSeekResult(
                    found=True,
                    status="found",
                    attempts=3,
                    initial_status="bad_contact",
                    final_status="good",
                    depth_below_down_mm=0.002,
                    axis_a_lowering_mm=1.002,
                    step_mm=0.001,
                    max_depth_mm=0.002,
                ),
            )

            Main._record_route_contact_height(
                window,
                record,
                1,
                3,
                csv_path=csv_path,
            )

            height_map_path = csv_path.parent / "route-contact-height-map.csv"
            with height_map_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route_name"], "route-a")
        self.assertEqual(rows[0]["design_x"], "10.0")
        self.assertEqual(rows[0]["stage_y"], "2.0")
        self.assertEqual(rows[0]["measurement_status"], "ok")
        self.assertEqual(rows[0]["contact_found"], "true")
        self.assertEqual(rows[0]["contact_depth_below_down_mm"], "0.002")
        self.assertEqual(rows[0]["contact_axis_a_lowering_mm"], "1.002")
        self.assertEqual(rows[0]["contact_seek_status"], "found")
        self.assertEqual(rows[0]["contact_seek_attempts"], "3")

    def test_saving_needle_down_target_does_not_reapply_full_settings(self) -> None:
        window = Main.__new__(Main)
        stage_controller = _FakeStageController()
        joystick = _FakeJoystick(120.0)
        settings_manager = _FakeSettingsManager()
        statuses: list[str] = []
        full_apply_called: list[bool] = []

        window.stage_controller = stage_controller
        window.joystick_panel = joystick
        window.settings_manager = settings_manager
        window.contact_calibration_window = None
        window._apply_settings = lambda: full_apply_called.append(True)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._save_needle_down_position_from_lowering(window, 2.885)

        self.assertEqual(full_apply_called, [])
        self.assertEqual(settings_manager.saved_count, 1)
        self.assertEqual(
            settings_manager.settings.needle_calibration.down_position_mm,
            2.885,
        )
        self.assertTrue(
            settings_manager.settings.needle_calibration.down_position_configured
        )
        self.assertEqual(
            stage_controller.needle_calibrations[-1]["down_position_mm"],
            2.885,
        )
        self.assertIn(("lower", 2.885), joystick.needle_contacts)
        self.assertIn(
            "Saved needle down target and set current A position to A0.",
            statuses,
        )

    def test_coordinate_move_records_programmed_feedrate(self) -> None:
        window, stage_controller, _joystick, timer, _statuses = _make_main(120.0)

        accepted = Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )

        self.assertTrue(accepted)
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0, "Y": -2.0}, 120.0)],
        )
        self.assertEqual(window._coordinate_move_programmed_feedrate, 120.0)
        self.assertEqual(window._coordinate_move_effective_feedrate, 120.0)
        self.assertTrue(timer.started)
        self.assertEqual(_joystick.common_targets, [])
        self.assertEqual(_joystick.common_cleared, 1)

    def test_mixed_axis_coordinate_move_shows_common_feedrate(self) -> None:
        window, _stage_controller, joystick, _timer, _statuses = _make_main(120.0)

        accepted = Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Z": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )

        self.assertTrue(accepted)
        self.assertEqual(joystick.common_targets, [(120.0, 100.0)])

    def test_api_move_without_feedrate_uses_current_gui_feedrate(self) -> None:
        window, _stage_controller, _joystick, _timer, _statuses = _make_main(77.0)

        self.assertEqual(Main._api_move_feedrate(window, None), 77.0)

    def test_single_axis_coordinate_move_does_not_show_common_feedrate(self) -> None:
        window, _stage_controller, joystick, _timer, _statuses = _make_main(120.0)

        accepted = Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )

        self.assertTrue(accepted)
        self.assertEqual(joystick.common_targets, [])
        self.assertEqual(joystick.common_cleared, 1)

    def test_pending_xy_coordinate_move_uses_xy_feedrate(self) -> None:
        window, stage_controller, joystick, _timer, _statuses = _make_main(999.0)
        joystick.coordinate_feedrate = 42.0
        window._pending_stage_axis_targets = {
            "X": (5.0, 5.0),
            "Y": (-2.0, -2.0),
        }

        Main._apply_pending_stage_coordinate_targets(window)

        self.assertEqual(joystick.coordinate_axes, [("X", "Y")])
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0, "Y": -2.0}, 42.0)],
        )
        self.assertEqual(joystick.common_targets, [])

    def test_coordinate_apply_switches_step_mode_to_jog_before_feedrate(self) -> None:
        window, stage_controller, joystick, _timer, _statuses = _make_main(99.0)
        joystick.mode = "step"
        window._pending_stage_axis_targets = {"X": (5.0, 5.0)}

        Main._apply_pending_stage_coordinate_targets(window)

        self.assertEqual(joystick.mode_changes, [("jog", True)])
        self.assertEqual(joystick.coordinate_modes, ["jog"])
        self.assertEqual(stage_controller.requests, [({"X": 5.0}, 99.0)])

    def test_coordinate_move_feedrate_change_reissues_absolute_jog(self) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        advances = []
        window._advance_coordinate_move_prediction = lambda: advances.append(True)
        stage_controller.busy = True

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(stage_controller.jog_stops, 1)
        self.assertEqual(stage_controller.absolute_jog_replace_flags, [True])
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0}, 120.0), ({"X": 5.0}, 180.0)],
        )
        self.assertEqual(window._coordinate_move_programmed_feedrate, 180.0)
        self.assertEqual(window._coordinate_move_effective_feedrate, 180.0)
        self.assertTrue(advances)
        self.assertTrue(
            any(
                "Active coordinate move feedrate: F180.0." in item
                for item in statuses
            )
        )

    def test_coordinate_move_feedrate_change_uses_active_axis_set(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        window._coordinate_move_axis = None
        window._advance_coordinate_move_prediction = lambda: None
        stage_controller.busy = True

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(stage_controller.jog_stops, 1)
        self.assertEqual(stage_controller.absolute_jog_replace_flags, [True])
        self.assertEqual(
            stage_controller.requests,
            [
                ({"X": 5.0, "Y": -2.0}, 120.0),
                ({"X": 5.0, "Y": -2.0}, 180.0),
            ],
        )
        self.assertEqual(window._coordinate_move_programmed_feedrate, 180.0)
        self.assertEqual(window._coordinate_move_effective_feedrate, 180.0)

    def test_coordinate_move_feedrate_change_keeps_tracking_after_busy_reissue_cancel(
        self,
    ) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.busy = True
        window._advance_coordinate_move_prediction = lambda: None

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertTrue(window._coordinate_move_reissue_cancel_pending)
        self.assertEqual(stage_controller.absolute_jog_replace_flags, [True])
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0}, 120.0), ({"X": 5.0}, 180.0)],
        )

        Main.on_move_finished(window, False, "Operation cancelled.")

        self.assertFalse(window._coordinate_move_reissue_cancel_pending)
        self.assertEqual(window._coordinate_move_axis, "X")
        self.assertEqual(window._coordinate_move_axes, {"X"})
        self.assertEqual(window._coordinate_move_programmed_feedrate, 180.0)
        self.assertFalse(
            any("Operation cancelled." in item for item in statuses)
        )

    def test_coordinate_move_feedrate_reissue_failure_keeps_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.next_absolute_jog_accept = False
        stage_controller.busy = True
        scheduled_delays: list[tuple[int, ...]] = []
        window._schedule_status_refreshes = (
            lambda delays: scheduled_delays.append(tuple(delays))
        )
        window._advance_coordinate_move_prediction = lambda: None

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(window._coordinate_move_axis, "X")
        self.assertEqual(window._coordinate_move_axes, {"X"})
        self.assertEqual(window._coordinate_move_programmed_feedrate, 120.0)
        self.assertEqual(window._coordinate_move_effective_feedrate, 120.0)
        self.assertEqual(stage_controller.requests, [({"X": 5.0}, 120.0)])
        self.assertEqual(stage_controller.jog_stops, 0)
        self.assertTrue(scheduled_delays)
        self.assertTrue(
            any(
                "Unable to update coordinate move feedrate." in item
                for item in statuses
            )
        )

    def test_manual_jog_clears_active_coordinate_move_tracking(self) -> None:
        window, _stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Z": (3.84, 3.84)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )

        Main._on_manual_jog_command_changed(window, (("X", -250.0),), 60.0)

        self.assertIsNone(window._coordinate_move_axis)
        self.assertEqual(window._coordinate_move_axes, set())
        self.assertEqual(window._motion_axes, {"X"})

    def test_feedrate_change_does_not_reissue_stale_idle_coordinate_move(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Z": (3.84, 3.84)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.busy = False
        stage_controller.latest_state = "Idle"

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertIsNone(window._coordinate_move_axis)
        self.assertEqual(window._coordinate_move_axes, set())
        self.assertEqual(stage_controller.requests, [({"X": 5.0, "Z": 3.84}, 120.0)])
        self.assertEqual(stage_controller.jog_stops, 0)

    def test_cancel_button_is_enabled_for_generic_busy_stage_task(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.busy = True

        Main._update_stage_coordinate_apply_state(window)

        self.assertTrue(cancel_button.enabled)

    def test_cancel_button_is_enabled_for_reported_controller_motion(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"

        Main._update_stage_coordinate_apply_state(window)

        self.assertTrue(cancel_button.enabled)

    def test_stale_reported_controller_motion_does_not_enable_cancel(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"
        stage_controller.last_status_time = (
            time.monotonic() - Main.CONTROLLER_ACTIVE_STATE_STALE_S - 0.5
        )

        Main._update_stage_coordinate_apply_state(window)

        self.assertFalse(cancel_button.enabled)

    def test_cancel_button_cancels_generic_busy_stage_task(self) -> None:
        window, stage_controller, _cancel_button, statuses = _make_cancel_main()
        stage_controller.busy = True

        Main._cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_tasks,
            ["Operation cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_motions, [])
        self.assertIn("Cancel requested.", statuses)

    def test_cancel_button_keeps_coordinate_move_on_jog_cancel_path(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        window._coordinate_move_axis = "X"
        window._coordinate_move_axes = {"X"}
        window._clear_coordinate_move_tracking = (
            lambda *, clear_pending, reset_override: setattr(
                window,
                "_coordinate_move_axis",
                None,
            )
        )

        Main._cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_motions,
            ["Coordinate move cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_tasks, [])

    def test_cancel_button_cancels_reported_controller_motion(self) -> None:
        window, stage_controller, _cancel_button, statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"

        Main._cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_motions,
            ["Motion cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_tasks, [])
        self.assertIn("Cancel requested.", statuses)

    def test_home_all_ignores_stale_jog_state_after_cancel(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"
        stage_controller.last_status_time = (
            time.monotonic() - Main.CONTROLLER_ACTIVE_STATE_STALE_S - 0.5
        )
        window._refresh_pending_homing_ui = lambda: None

        Main._request_home_all_from_ui(window)

        self.assertEqual(stage_controller.home_all_requests, 1)
        self.assertEqual(stage_controller.status_message.messages, [])

    def test_home_all_sends_all_home_during_fresh_jog_state(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"
        stage_controller.last_status_time = time.monotonic()
        window._refresh_pending_homing_ui = lambda: None

        Main._request_home_all_from_ui(window)

        self.assertEqual(stage_controller.home_all_requests, 1)
        self.assertEqual(stage_controller.home_axis_requests, [])
        self.assertEqual(window._pending_homing_axes, [])
        self.assertEqual(stage_controller.status_message.messages, [])

    def test_manual_axis_home_queues_second_axis_while_first_is_active(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        window._refresh_pending_homing_ui = lambda: None

        Main._request_home_axis_from_ui(window, "X")
        window._homing_active_key = "X"
        Main._request_home_axis_from_ui(window, "Z")

        self.assertEqual(stage_controller.home_all_requests, 0)
        self.assertEqual(stage_controller.home_axis_requests, ["X"])
        self.assertEqual(window._pending_homing_axes, ["Z"])

    def test_idle_status_before_motion_does_not_clear_coordinate_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.latest_state = "Idle"
        window._coordinate_move_started_at = None

        Main._finish_coordinate_move_if_idle(window, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

        self.assertEqual(window._coordinate_move_axis, "X")
        self.assertEqual(window._coordinate_move_axes, {"X", "Y"})

    def test_idle_status_after_motion_clears_coordinate_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.latest_state = "Idle"
        window._coordinate_move_started_at = None
        window._coordinate_move_seen_active_state = True

        Main._finish_coordinate_move_if_idle(window, (5.0, -2.0, 0.0, 0.0, 0.0, 0.0))

        self.assertIsNone(window._coordinate_move_axis)

    def test_previous_micron_step_status_does_not_clear_coordinate_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"A": (-0.010, -0.010)},
            feedrate_mm_min=1.0,
            source_label="coordinate field",
        )
        stage_controller.latest_state = "Idle"
        window._coordinate_move_started_at = None
        window._coordinate_move_seen_active_state = True

        Main._finish_coordinate_move_if_idle(
            window,
            (0.0, 0.0, 0.0, -0.004, 0.0, 0.0),
        )

        self.assertEqual(window._coordinate_move_axis, "A")


if __name__ == "__main__":
    unittest.main()
