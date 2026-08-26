import csv
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
from pathlib import Path

from probe_station_gui.route.measurement import RouteMeasurementPoint
from probe_station_gui.route.point_execution import PointPhotoSettings
from tests.app.main_coordinate_feedrate_support import (
    Main,
    ObjectiveCalibrationSettings,
    RouteMeasurementPointRequestCallbacks,
    RouteContactHeightRecord,
    RouteContactQuality,
    RouteContactSeekResult,
    RouteMeasurementRecord,
    RoutePhotoRecord,
    Settings,
    _FakeAliveThread,
    _FakeFrame,
    _FakeRouteMeasurementRunner,
    _FakeThread,
    _telegram_runtime_stub,
    main_module,
    request_route_measurement_for_point,
)
from tests.app.main_route_session_support import _telegram_test_photo_bytes
from tests.app.route_run_execution_support import (
    activate_route_run,
    install_route_run_execution,
)
from probe_station_gui.views import (
    main_window_needle_calibration as needle_calibration_ui,
)
from probe_station_gui.views import main_window_shutdown as shutdown_ui
from probe_station_gui.route.telegram_adapter import (
    RouteTelegramPhotoState,
)


class _StageLease:
    def __init__(
        self,
        label: str,
        calls: list[tuple[object, ...]] | None,
    ) -> None:
        self._calls = calls
        self._released = False
        if calls is not None:
            calls.append(("begin", label))

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._calls is not None:
            self._calls.append(("finish",))

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        self.release()
        return False


def _stage_lease(
    label: str,
    calls: list[tuple[object, ...]] | None = None,
) -> _StageLease:
    return _StageLease(label, calls)


class MainCoordinateFeedrateTest(unittest.TestCase):
    def test_combine_telegram_contact_photos_side_by_side(self) -> None:
        script = r"""
import struct
import zlib
from PySide6.QtGui import QImage
from main import Main
from probe_station_gui.route.telegram_adapter import combine_telegram_contact_photos

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

combined = combine_telegram_contact_photos(
    png_bytes(8, 4, 0x00FF0000),
    png_bytes(2, 4, 0x0000FF00),
    encode_image=Main._qimage_telegram_photo,
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
        install_route_run_execution(window)
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
        window._telegram_runtime = _telegram_runtime_stub(
            route_photos=types.SimpleNamespace(
                take_pending_contact_photos=lambda: (before, after)
            ),
            send_bot_message=lambda message, *, photo=None, reply_markup=None: (
                sent.append((message, photo, reply_markup))
            ),
        )
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
        runner = types.SimpleNamespace(
            status_payload=lambda: {"waiting_reason": "contact_attention"}
        )
        activate_route_run(window, runner)
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

        window._last_route_measurement_result = (record, 1, 2, True)
        window._pending_route_measure_point = None
        route_photos = RouteTelegramPhotoState()
        route_photos._last_contact_failure_before_photo = before
        route_photos._last_contact_failure_photo = after
        window._telegram_runtime = _telegram_runtime_stub(
            route_photos=route_photos,
            send_alert=lambda key, text, **kwargs: alerts.append((key, text, kwargs)),
            route_actions_markup="actions",
        )
        window.design_navigator_panel = None
        window._route_measurement_dialog = None

        Main._on_route_measurement_waiting_changed(window, runner, True)

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
        requests: list[tuple[float, str]] = []
        window._route_measurement_optical_session_token = "route-token"

        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda message: emitted.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            run_external_local_autofocus=lambda *, range_mm, parent_token: (
                requests.append((float(range_mm), str(parent_token))) or "focus-result"
            )
        )
        settings = PointPhotoSettings(autofocus_range_mm=0.03)

        result = Main._route_photo_autofocus(
            window,
            types.SimpleNamespace(),
            2,
            5,
            settings=settings,
        )

        self.assertEqual(result, "focus-result")
        self.assertEqual(requests, [(0.03, "route-token")])
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
            reserve_external_task=lambda label: _stage_lease(label),
            run_external_local_autofocus=lambda *, range_mm, step_mm=None: (
                calls.append((float(range_mm), step_mm)) or _FocusResult()
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
        install_route_run_execution(window)
        window._telegram_runtime = _telegram_runtime_stub()
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

    def test_record_route_photo_keeps_empty_route_name_when_route_name_is_none(
        self,
    ) -> None:
        window = Main.__new__(Main)
        install_route_run_execution(window)
        window._telegram_runtime = _telegram_runtime_stub()
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(name=None)
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
        self.assertEqual(rows[0]["route_name"], "")

    def test_route_points_keep_contact_xy_separate_from_photo_objective_xy(
        self,
    ) -> None:
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
        usability = types.SimpleNamespace(usable=True, rejection_reason=None)
        window._coordinate_system_coordinator = types.SimpleNamespace(
            current_design_lease=lambda: usability,
            project_design_to_camera_stage=lambda snapshot, _design_xy: (
                (1.0, 2.0) if snapshot is usability else None
            ),
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
        window._coordinate_system_coordinator = types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(
                registration=types.SimpleNamespace(registration_valid=True)
            )
        )
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._stage_motion = types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(cancelable=False)
        )
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
            with mock.patch.object(
                main_module.stage_move_lifecycle,
                "has_application_cancelable_operation",
                return_value=False,
            ):
                needle_calibration_ui.request_sample_unload(
                    window,
                    message_box=main_module.QMessageBox,
                    thread_factory=main_module.threading.Thread,
                )
        finally:
            main_module.QMessageBox = original_box
            main_module.threading.Thread = original_thread

        self.assertEqual(invalidations, [])
        self.assertEqual(remembered, [])
        self.assertEqual(_FakeThread.instances, [])

    def test_sample_unload_confirm_clears_registration_before_start(self) -> None:
        window = Main.__new__(Main)
        events: list[object] = []
        window._coordinate_system_coordinator = types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(
                registration=types.SimpleNamespace(registration_valid=True)
            )
        )
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._stage_motion = types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(cancelable=False)
        )
        window._invalidate_design_registration = lambda reason: events.append(
            ("invalidate", reason)
        )
        window._remember_sample_focus_from_latest = lambda: events.append("remember")
        window._current_needle_feedrate = lambda: 7.0
        window._run_sample_unload = lambda *_args: None
        window.stage_controller = types.SimpleNamespace(
            max_feedrate_for_axes=lambda axes: (
                900.0 if tuple(axes) == ("X", "Y") else 1.0
            )
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
            with mock.patch.object(
                main_module.stage_move_lifecycle,
                "has_application_cancelable_operation",
                return_value=False,
            ):
                needle_calibration_ui.request_sample_unload(
                    window,
                    message_box=main_module.QMessageBox,
                    thread_factory=main_module.threading.Thread,
                )
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
        window._coordinate_system_coordinator = types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(
                registration=types.SimpleNamespace(registration_valid=False)
            )
        )
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._stage_motion = types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(cancelable=False)
        )
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
            with mock.patch.object(
                main_module.stage_move_lifecycle,
                "has_application_cancelable_operation",
                return_value=False,
            ):
                needle_calibration_ui.request_sample_unload(
                    window,
                    message_box=main_module.QMessageBox,
                    thread_factory=main_module.threading.Thread,
                )
        finally:
            main_module.QMessageBox = original_box
            main_module.threading.Thread = original_thread

        self.assertEqual(invalidations, [])
        self.assertEqual(remembered, [True])
        self.assertEqual(len(_FakeThread.instances), 1)
        self.assertTrue(_FakeThread.instances[0].started)

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
            reserve_external_task=lambda label: _stage_lease(label, calls),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(
                ("move", x_mm, y_mm)
            ),
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
            reserve_external_task=lambda label: _stage_lease(label, calls),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(
                ("move", x_mm, y_mm)
            ),
        )
        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda _message: None
        )
        window.route_contact_move_finished = types.SimpleNamespace(
            emit=lambda _success, _message: None
        )

        Main._run_route_contact_move(window, point, 75.0)

        self.assertIn(("move", 1.35, 2.3), calls)

    def test_measure_selected_route_point_while_running_queues_interrupt(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        statuses: list[str] = []

        activate_route_run(window, runner, _FakeAliveThread())
        window._pending_route_measure_point = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        cancellations: list[str] = []
        window.stage_controller = types.SimpleNamespace(
            cancel_active_task=lambda reason: cancellations.append(str(reason))
        )
        window._clear_stage_motion_axes = lambda: None
        window._schedule_status_refreshes = lambda _delays: None
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )

        request_route_measurement_for_point(
            point_number=91,
            thread_active=True,
            waiting=False,
            callbacks=RouteMeasurementPointRequestCallbacks(
                open_dialog=window._open_route_measurement_dialog,
                current_dialog=lambda: window._route_measurement_dialog,
                submit_confirmation=window._submit_route_measurement_confirmation,
                request_point_correction=(
                    window._request_route_measurement_point_correction
                ),
                start_measurement=window._start_route_measurement,
                set_pending_point=lambda point: setattr(
                    window, "_pending_route_measure_point", point
                ),
                clear_pending_point=lambda: setattr(
                    window, "_pending_route_measure_point", None
                ),
            ),
        )

        self.assertTrue(runner.correction_requested)
        self.assertEqual(cancellations, ["Route measurement interrupt requested."])
        self.assertEqual(window._pending_route_measure_point, 91)
        self.assertEqual(
            statuses,
            ["Stopping contact measurement, then measuring point 91."],
        )
        self.assertEqual(runner.confirmations, [])

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
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )

        Main._cancel_contact_seek(window)

        self.assertTrue(window._contact_seek_stop_requested.is_set())
        self.assertEqual(aborts, [])
        self.assertEqual(cancelled_motions, ["Contact seek cancel requested."])
        self.assertEqual(statuses, ["Contact seek cancel requested."])

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

        shutdown_ui.close_auxiliary_windows(window, force_route_dialog=True)

        self.assertEqual(calls, [("running", False), ("close", None)])

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


if __name__ == "__main__":
    unittest.main()
