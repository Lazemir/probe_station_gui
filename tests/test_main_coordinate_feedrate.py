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
from probe_station_gui.dialogs.route_measurement_dialog import RouteMeasurementDialog
from probe_station_gui.route_measurement import (
    RouteContactHeightRecord,
    RouteContactQuality,
    RouteContactSeekResult,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RoutePhotoRecord,
)
from probe_station_gui.settings_manager import ObjectiveCalibrationSettings, Settings


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


class _FakeRouteMeasurementRunner:
    def __init__(self) -> None:
        self.confirmations: list[str] = []
        self.correction_requested = False

    def submit_confirmation(self, action: str) -> bool:
        self.confirmations.append(str(action))
        return True

    def request_current_point_correction(self) -> None:
        self.correction_requested = True


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
            cwd=Path(__file__).resolve().parents[1],
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
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._request_route_measurement_for_point(window, 91)

        self.assertTrue(runner.correction_requested)
        self.assertEqual(window._pending_route_measure_point, 91)
        self.assertEqual(
            statuses,
            [
                "Stopping contact measurement, then measuring point 91."
            ],
        )
        self.assertEqual(runner.confirmations, [])

        Main._on_route_measurement_waiting_changed(window, True)

        self.assertIsNone(window._pending_route_measure_point)
        self.assertEqual(runner.confirmations, ["jump:91"])
        self.assertEqual(
            statuses[-1],
            "Route measurement: measure from point 91.",
        )

    def test_waiting_dialog_measure_uses_selected_point(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        jumped: list[int] = []
        remeasured: list[bool] = []
        measured: list[bool] = []

        dialog._running = True
        dialog._waiting = True
        dialog._jump_point_spin = types.SimpleNamespace(value=lambda: 91)
        dialog.jump_requested = types.SimpleNamespace(
            emit=lambda point: jumped.append(int(point))
        )
        dialog.remeasure_requested = types.SimpleNamespace(
            emit=lambda: remeasured.append(True)
        )
        dialog._emit_measure_requested = lambda: measured.append(True)

        RouteMeasurementDialog._emit_next_or_measure_requested(dialog)

        self.assertEqual(jumped, [91])
        self.assertEqual(remeasured, [])
        self.assertEqual(measured, [])

    def test_waiting_dialog_pause_and_interrupt_buttons_resume(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        resumed: list[bool] = []
        interrupted: list[bool] = []
        paused: list[bool] = []
        dialog._running = True
        dialog._waiting = True
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
        self.assertTrue(dialog._interrupt_button.enabled)
        self.assertEqual(len(resumed), 2)
        self.assertEqual(paused, [])
        self.assertEqual(interrupted, [])

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
