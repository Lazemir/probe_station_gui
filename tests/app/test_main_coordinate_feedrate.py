import csv
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

from main_coordinate_feedrate_support import (
    Main,
    ObjectiveCalibrationSettings,
    RouteContactHeightRecord,
    RouteContactQuality,
    RouteContactSeekResult,
    RouteMeasurementDialog,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RoutePhotoRecord,
    Settings,
    _FakeAliveThread,
    _FakeButton,
    _FakeFrame,
    _FakeRouteMeasurementRunner,
    _FakeThread,
    _telegram_test_photo_bytes,
    main_module,
    request_route_measurement_for_point,
    route_measurement_dialog_module,
)

class MainCoordinateFeedrateTest(unittest.TestCase):
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

    def test_record_route_photo_keeps_empty_route_name_when_route_name_is_none(
        self,
    ) -> None:
        window = Main.__new__(Main)
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

        request_route_measurement_for_point(
            point_number=91,
            thread_active=True,
            waiting=False,
            open_dialog=window._open_route_measurement_dialog,
            current_dialog=lambda: window._route_measurement_dialog,
            submit_confirmation=window._submit_route_measurement_confirmation,
            request_point_correction=window._request_route_measurement_point_correction,
            start_measurement=window._start_route_measurement,
            set_pending_point=lambda point: setattr(
                window, "_pending_route_measure_point", point
            ),
            clear_pending_point=lambda: setattr(
                window, "_pending_route_measure_point", None
            ),
        )

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
