import csv
import tempfile
import threading
import unittest
from pathlib import Path

from probe_station_gui.settings.precision_approach import (
    PrecisionApproachProfile,
    PrecisionApproachSettings,
)
from probe_station_gui.route.measurement import (
    CSV_FIELDS,
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    RouteMeasurementPoint,
    RouteMeasurementRunner,
    filter_route_points_by_previous_status,
    latest_route_measurement_statuses,
)
from probe_station_gui.route.measurement_csv import RouteMeasurementCsvWriter
from tests.stage.controller_test_support import StageController, _LineFakeSerial

try:
    from .measurement_test_support import (
        _FakeLCR,
        _FakeStage,
        _NotifyingStage,
        _OpeningLCR,
        _point,
        _read_csv_rows_if_exists,
    )
except ImportError:
    from measurement_test_support import (
        _FakeLCR,
        _FakeStage,
        _NotifyingStage,
        _OpeningLCR,
        _point,
        _read_csv_rows_if_exists,
    )


class _PermissionThenAppendWriter(RouteMeasurementCsvWriter):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.attempts = 0

    def append(self, record: object) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise PermissionError(13, "Permission denied", str(self.path))
        super().append(record)


class RouteMeasurementRunnerTest(unittest.TestCase):
    def test_latest_statuses_use_last_csv_row_per_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerow({"structure_number": "1", "status": "open"})
                writer.writerow({"structure_number": "2", "status": "short"})
                writer.writerow({"structure_number": "1", "status": "ok"})
                writer.writerow({"structure_number": "3", "status": "OK"})

            statuses = latest_route_measurement_statuses(csv_path)

        self.assertEqual(statuses, {1: "ok", 2: "short", 3: "ok"})

    def test_filter_route_points_by_previous_ok_status(self) -> None:
        points = [_point(1), _point(2), _point(3), _point(4)]
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerow({"structure_number": "1", "status": "ok"})
                writer.writerow({"structure_number": "2", "status": "short"})
                writer.writerow({"structure_number": "3", "status": "ok"})
                writer.writerow({"structure_number": "3", "status": "open"})

            filtered = filter_route_points_by_previous_status(
                points,
                csv_path,
                allowed_statuses={"ok"},
            )

        self.assertEqual([point.index for point in filtered], [1])

    def test_stop_before_run_does_not_raise_needles(self) -> None:
        points = [
            RouteMeasurementPoint(
                index=1,
                point_id="p001",
                label="P001",
                design_center=(100.0, 200.0),
                stage_xy=(1.0, 2.0),
                needle_1_design=(101.0, 201.0),
                needle_2_design=(99.0, 199.0),
            )
        ]
        stage = _FakeStage()
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([10.0]),
                needle_feedrate=80.0,
            )
            runner.stop()

            success, message = runner.run()

        self.assertFalse(success)
        self.assertEqual(message, "Route measurement stopped by user.")
        self.assertEqual(stage.calls, [("begin", "route measurement"), ("finish",)])

    def test_runner_writes_csv_and_lifts_between_points(self) -> None:
        points = [
            RouteMeasurementPoint(
                index=1,
                point_id="p001",
                label="P001",
                design_center=(100.0, 200.0),
                stage_xy=(1.0, 2.0),
                needle_1_design=(101.0, 201.0),
                needle_2_design=(99.0, 199.0),
            ),
            RouteMeasurementPoint(
                index=2,
                point_id="p002",
                label="P002",
                design_center=(110.0, 200.0),
                stage_xy=(1.5, 2.0),
                needle_1_design=(111.0, 201.0),
                needle_2_design=(109.0, 199.0),
            ),
        ]
        stage = _FakeStage()
        lcr = _FakeLCR([5.0, 25.0])
        records = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(record),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertIn("2 measurements saved", message)
            self.assertEqual(len(records), 2)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(list(rows[0].keys()), CSV_FIELDS)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["structure_number"], "1")
            self.assertEqual(rows[0]["nplc"], "")
            self.assertEqual(rows[0]["n_measurements"], "1")
            self.assertEqual(rows[0]["resistance_ohm"], "5")
            self.assertEqual(rows[0]["resistance_rms_ohm"], "0")
            self.assertEqual(rows[0]["relative_rms"], "0")
            self.assertEqual(rows[0]["status"], "ok")
            self.assertTrue(rows[0]["timestamp"])
            self.assertEqual(rows[1]["structure_number"], "2")
            self.assertEqual(rows[1]["resistance_ohm"], "25")

        self.assertEqual(stage.calls[0], ("begin", "route measurement"))
        self.assertIn(("move", 1.0, 2.0), stage.calls)
        self.assertIn(("move", 1.5, 2.0), stage.calls)
        self.assertEqual(stage.calls[-1], ("finish",))
        lower_calls = [
            call for call in stage.calls if call == ("needles", "lower", 75.0)
        ]
        self.assertEqual(len(lower_calls), 2)
        lift_calls = [
            call for call in stage.calls if call == ("needles", "lift", 75.0)
        ]
        self.assertEqual(len(lift_calls), 3)
        self.assertNotIn(("needles", "raise", 75.0), stage.calls)

    def test_runner_retries_csv_append_after_permission_error(self) -> None:
        stage = _FakeStage()
        lcr = _FakeLCR([42.0])
        statuses: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[_point(1)],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                contact_settle_s=0.0,
                status_callback=statuses.append,
            )
            writer = _PermissionThenAppendWriter(csv_path)
            runner._csv_writer = writer
            runner._csv_write_retry_interval_s = 0.0

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(writer.attempts, 2)
            self.assertTrue(
                any("Close the CSV" in status for status in statuses),
                statuses,
            )
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["structure_number"], "1")
            self.assertEqual(rows[0]["resistance_ohm"], "42")

    def test_runner_uses_updated_csv_path_after_initial_wait(self) -> None:
        stage = _FakeStage()
        lcr = _FakeLCR([100.0])
        waiting = threading.Event()

        with tempfile.TemporaryDirectory() as tmpdir:
            old_csv_path = Path(tmpdir) / "old-route.csv"
            new_csv_path = Path(tmpdir) / "new-route.csv"
            runner = RouteMeasurementRunner(
                points=[_point(1)],
                csv_path=old_csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=1,
                initial_measurement_count=1,
                contact_settle_s=0.0,
                wait_before_first_point=True,
                waiting_callback=lambda value: waiting.set() if value else None,
            )
            finished: list[tuple[bool, str]] = []
            thread = threading.Thread(
                target=lambda: finished.append(runner.run()),
                daemon=True,
            )

            thread.start()
            self.assertTrue(waiting.wait(timeout=2.0))
            self.assertFalse(old_csv_path.exists())
            runner.update_runtime_settings(
                measurement_count=1,
                initial_measurement_count=1,
                max_relative_rms=None,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.001,
                contact_settle_s=0.0,
                csv_path=new_csv_path,
                nplc_label="1",
                measurement_type="DCR",
            )
            self.assertEqual(runner.csv_path, new_csv_path.resolve())
            self.assertTrue(runner.submit_confirmation("next"))
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(finished[0][0], True, finished[0][1])
            self.assertFalse(old_csv_path.exists())
            with new_csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["structure_number"], "1")
            self.assertEqual(rows[0]["nplc"], "1")
            self.assertEqual(rows[0]["measurement_type"], "DCR")

    def test_photo_only_route_raises_needles_and_skips_meter_and_csv(self) -> None:
        points = [_point(1), _point(2)]
        stage = _FakeStage()
        lcr = _OpeningLCR([5.0])
        photos: list[tuple[int, int, int]] = []

        def capture(point, position, total, _focus_result) -> str:
            photos.append((point.index, position, total))
            return f"photo-{point.index}.png"

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_PHOTO,
                photo_callback=capture,
                photo_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertIn("2 photos", message)
            self.assertFalse(csv_path.exists())

        self.assertFalse(lcr.opened)
        self.assertFalse(lcr.closed)
        self.assertEqual(photos, [(1, 1, 2), (2, 2, 2)])
        self.assertEqual(stage.calls[0], ("begin", "route photo capture"))
        self.assertEqual(stage.calls[-1], ("finish",))
        self.assertNotIn(("needles", "lower", 75.0), stage.calls)
        self.assertGreaterEqual(
            len([call for call in stage.calls if call == ("needles", "raise", 75.0)]),
            3,
        )

    def test_photo_then_measure_captures_before_lowering_needles(self) -> None:
        point = _point(1)
        stage = _FakeStage()

        def capture(_point, _position, _total, _focus_result) -> str:
            stage.calls.append(("photo", _point.index))
            return "photo.png"

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
                photo_callback=capture,
                photo_settle_s=0.0,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        photo_index = stage.calls.index(("photo", 1))
        lower_index = stage.calls.index(("needles", "lower", 75.0))
        self.assertLess(photo_index, lower_index)

    def test_photo_then_measure_raises_needles_before_next_point_move(self) -> None:
        points = [_point(1), _point(2)]
        stage = _FakeStage()

        def capture(_point, _position, _total, _focus_result) -> str:
            stage.calls.append(("photo", _point.index))
            return "photo.png"

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0, 7.0]),
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
                photo_callback=capture,
                photo_settle_s=0.0,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        second_move_index = stage.calls.index(("move", 2.0, 12.0))
        self.assertEqual(
            stage.calls[second_move_index - 1],
            ("needles", "raise", 75.0),
        )
        second_photo_index = stage.calls.index(("photo", 2))
        self.assertNotIn(
            ("needles", "raise", 75.0),
            stage.calls[second_move_index + 1 : second_photo_index],
        )

    def test_photo_then_measure_uses_photo_xy_then_contact_xy(self) -> None:
        point = RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
            photo_stage_xy=(1.25, 1.75),
        )
        stage = _FakeStage()

        def capture(_point, _position, _total, _focus_result) -> str:
            stage.calls.append(("photo", _point.index))
            return "photo.png"

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
                photo_callback=capture,
                photo_settle_s=0.0,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        photo_move_index = stage.calls.index(("move", 1.25, 1.75))
        photo_index = stage.calls.index(("photo", 1))
        contact_move_index = stage.calls.index(("move", 1.0, 2.0))
        lower_index = stage.calls.index(("needles", "lower", 75.0))
        self.assertLess(photo_move_index, photo_index)
        self.assertLess(photo_index, contact_move_index)
        self.assertLess(contact_move_index, lower_index)

    def test_interrupted_cancelled_contact_move_waits_for_shift_and_confirmation(
        self,
    ) -> None:
        point = RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
            photo_stage_xy=(1.25, 1.75),
        )
        statuses: list[str] = []
        runner_holder: dict[str, RouteMeasurementRunner] = {}

        class _InterruptingContactMoveStage(_FakeStage):
            def run_external_move_to_xy(self, x_mm: float, y_mm: float) -> str:
                self.calls.append(("move", x_mm, y_mm))
                if len([call for call in self.calls if call[0] == "move"]) == 2:
                    runner_holder["runner"].request_current_point_correction()
                    raise RuntimeError("Operation cancelled.")
                return "moved"

        stage = _InterruptingContactMoveStage()

        def capture(_point, _position, _total, _focus_result) -> str:
            stage.calls.append(("photo", _point.index))
            return "photo.png"

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
                photo_callback=capture,
                photo_settle_s=0.0,
                contact_settle_s=0.0,
                status_callback=statuses.append,
            )
            runner_holder["runner"] = runner
            finished: list[tuple[bool, str]] = []
            thread = threading.Thread(
                target=lambda: finished.append(runner.run()),
                daemon=True,
            )

            thread.start()
            try:
                self.assertTrue(runner.wait_until_waiting(timeout_s=2.0))
                self.assertEqual(finished, [])
                self.assertNotIn(("needles", "lower", 75.0), stage.calls)
                selected, message = runner.set_current_adjustment_point(1)
                self.assertTrue(selected, message)
                saved, message = runner.save_current_position_adjustment((1.5, 1.75))
                self.assertTrue(saved, message)
                self.assertEqual(runner.route_offset_xy(), (0.5, -0.25))
                self.assertTrue(
                    any("interrupted; correct position" in item for item in statuses)
                )
            finally:
                if thread.is_alive():
                    runner.submit_confirmation("skip")
                    thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0][0], finished[0][1])

    def test_interrupt_after_precision_preparation_skips_final_and_contact_work(
        self,
    ) -> None:
        point = _point(1)
        stage = StageController()
        serial_connection = _LineFakeSerial([b"ok\n", b"ok\n"])
        stage._serial = serial_connection
        stage._motion_safety_disabled = True
        stage._move_safety_check = lambda: None
        stage.queue_jog_stop = lambda: None
        settings = PrecisionApproachSettings()
        for axis in settings.profiles:
            settings.profiles[axis] = PrecisionApproachProfile()
        settings.profiles["X"] = PrecisionApproachProfile(True, 0.1, 1)
        stage.apply_precision_approach_configuration(settings)

        current = {"X": point.stage_xy[0], "Y": point.stage_xy[1]}

        def status_for_current(**_kwargs: object):
            values = tuple(current.get(axis, 0.0) for axis in stage.AXIS_INDEX)
            return type(
                "Status",
                (),
                {
                    "state": "Idle",
                    "position": values,
                    "display_position": values,
                    "work_position": values,
                    "work_offset": tuple(0.0 for _axis in stage.AXIS_INDEX),
                    "coordinate_system": "G54",
                    "homed_axes": set(stage.AXIS_INDEX),
                    "values": dict(current),
                },
            )()

        stage._query_synced_status_for_absolute_motion = status_for_current
        stage._query_current_status_with_required_coordinates = status_for_current
        stage._require_homed_axes = lambda *_args, **_kwargs: None
        stage._require_position_for_absolute_motion = (
            lambda status, **_kwargs: status.display_position
        )
        stage._axis_value_for_configured_mode = (
            lambda status, axis: status.values.get(axis)
        )
        needle_calls: list[tuple[str, float | None]] = []
        stage.run_external_needles_action = (
            lambda action, feedrate=None: needle_calls.append((action, feedrate))
            or f"{action} done"
        )

        lcr = _FakeLCR([5.0])
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_MEASURE,
                contact_settle_s=0.0,
            )
            accepted_segments: list[dict[str, float]] = []

            def accept_segment_then_interrupt(
                targets: dict[str, float],
                **_kwargs: object,
            ) -> None:
                accepted_segments.append(dict(targets))
                current.update(targets)
                if len(accepted_segments) == 1:
                    runner.request_current_point_correction()
                    stage.cancel_active_task()

            stage._wait_for_idle_at_targets = accept_segment_then_interrupt
            finished: list[tuple[bool, str]] = []
            thread = threading.Thread(
                target=lambda: finished.append(runner.run()),
                daemon=True,
            )

            thread.start()
            try:
                self.assertTrue(runner.wait_until_waiting(timeout_s=2.0))
                jog_writes = [
                    payload
                    for payload in serial_connection.writes
                    if payload.startswith(b"$J=G90 G21 ")
                ]
                self.assertEqual(
                    accepted_segments,
                    [{"X": point.stage_xy[0] - 0.1, "Y": point.stage_xy[1]}],
                )
                self.assertEqual(len(jog_writes), 1)
                self.assertIn(b"X0.9 Y11", jog_writes[0])
                self.assertNotIn(b"X1 Y11", b"".join(jog_writes))
                self.assertNotIn(("lower", 75.0), needle_calls)
                self.assertEqual(lcr.values, [5.0])
            finally:
                if thread.is_alive():
                    runner.submit_confirmation("skip")
                    thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0][0], finished[0][1])
        stage.shutdown()

    def test_interrupted_cancelled_lower_waits_for_shift_and_confirmation(self) -> None:
        point = RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
            photo_stage_xy=(1.0, 2.0),
        )
        statuses: list[str] = []
        runner_holder: dict[str, RouteMeasurementRunner] = {}

        class _InterruptingLowerStage(_FakeStage):
            def run_external_needles_action(
                self,
                action: str,
                feedrate: float | None = None,
            ) -> str:
                self.calls.append(("needles", action, feedrate))
                if action == "lower":
                    runner_holder["runner"].request_current_point_correction()
                    raise RuntimeError("Operation cancelled.")
                return f"{action} done"

        stage = _InterruptingLowerStage()

        def capture(_point, _position, _total, _focus_result) -> str:
            stage.calls.append(("photo", _point.index))
            return "photo.png"

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
                photo_callback=capture,
                photo_settle_s=0.0,
                contact_settle_s=0.0,
                status_callback=statuses.append,
            )
            runner_holder["runner"] = runner
            finished: list[tuple[bool, str]] = []
            thread = threading.Thread(
                target=lambda: finished.append(runner.run()),
                daemon=True,
            )

            thread.start()
            try:
                self.assertTrue(runner.wait_until_waiting(timeout_s=2.0))
                self.assertEqual(finished, [])
                selected, message = runner.set_current_adjustment_point(1)
                self.assertTrue(selected, message)
                saved, message = runner.save_current_position_adjustment((1.5, 1.75))
                self.assertTrue(saved, message)
                self.assertEqual(runner.route_offset_xy(), (0.5, -0.25))
                self.assertTrue(
                    any("interrupted; correct position" in item for item in statuses)
                )
            finally:
                if thread.is_alive():
                    runner.submit_confirmation("skip")
                    thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0][0], finished[0][1])

    def test_photo_focus_runs_after_raise_and_before_capture(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        records = []

        def focus(_point, _position, _total) -> dict[str, object]:
            stage.calls.append(("focus", _point.index))
            return {
                "focus_start_z_mm": 1.0,
                "focus_best_z_mm": 1.02,
                "focus_delta_um": 20.0,
            }

        def capture(_point, _position, _total, focus_result) -> str:
            stage.calls.append(("focus_result", focus_result))
            stage.calls.append(("photo", _point.index))
            return "photo.png"

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_OpeningLCR([5.0]),
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_PHOTO,
                photo_callback=capture,
                photo_focus_callback=focus,
                photo_record_callback=lambda record, _position, _total: records.append(
                    record
                ),
                photo_focus_enabled=True,
                photo_settle_s=0.0,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        raise_index = stage.calls.index(("needles", "raise", 75.0), 2)
        focus_index = stage.calls.index(("focus", 1))
        photo_index = stage.calls.index(("photo", 1))
        self.assertLess(raise_index, focus_index)
        self.assertLess(focus_index, photo_index)
        self.assertEqual(records[0].focus["focus_best_z_mm"], 1.02)
        self.assertEqual(records[0].design_center, point.design_center)
        self.assertEqual(records[0].stage_xy, point.stage_xy)

    def test_interrupted_cancelled_focus_waits_for_shift_and_confirmation(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        statuses: list[str] = []
        runner_holder: dict[str, RouteMeasurementRunner] = {}

        def focus(_point, _position, _total) -> str:
            stage.calls.append(("focus", _point.index))
            runner_holder["runner"].request_current_point_correction()
            raise RuntimeError("Operation cancelled.")

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_MEASURE,
                photo_focus_callback=focus,
                photo_focus_enabled=True,
                contact_settle_s=0.0,
                status_callback=statuses.append,
            )
            runner_holder["runner"] = runner
            finished: list[tuple[bool, str]] = []
            thread = threading.Thread(
                target=lambda: finished.append(runner.run()),
                daemon=True,
            )

            thread.start()
            try:
                self.assertTrue(runner.wait_until_waiting(timeout_s=2.0))
                self.assertEqual(finished, [])
                self.assertNotIn(("needles", "lower", 75.0), stage.calls)
                selected, message = runner.set_current_adjustment_point(1)
                self.assertTrue(selected, message)
                saved, message = runner.save_current_position_adjustment((1.5, 10.75))
                self.assertTrue(saved, message)
                self.assertEqual(runner.route_offset_xy(), (0.5, -0.25))
                self.assertTrue(
                    any("interrupted; correct position" in item for item in statuses)
                )
            finally:
                if thread.is_alive():
                    runner.submit_confirmation("skip")
                    thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0][0], finished[0][1])
        self.assertIn("0 measurements saved", finished[0][1])

    def test_measure_only_focus_raises_needles_before_focus_and_then_moves_to_contact(self) -> None:
        point = RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
            photo_stage_xy=(1.25, 1.75),
        )
        stage = _FakeStage()

        def focus(_point, _position, _total) -> str:
            stage.calls.append(("focus", _point.index))
            return "focused"

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                operation_mode=ROUTE_OPERATION_MEASURE,
                photo_focus_callback=focus,
                photo_focus_enabled=True,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        raise_index = stage.calls.index(("needles", "raise", 75.0), 2)
        focus_move_index = stage.calls.index(("move", 1.25, 1.75))
        focus_index = stage.calls.index(("focus", 1))
        contact_move_index = stage.calls.index(("move", 1.0, 2.0))
        lower_index = stage.calls.index(("needles", "lower", 75.0))
        self.assertLess(raise_index, focus_move_index)
        self.assertLess(focus_move_index, focus_index)
        self.assertLess(focus_index, contact_move_index)
        self.assertLess(contact_move_index, lower_index)

    def test_runner_reports_progress_with_route_point_number(self) -> None:
        points = [
            RouteMeasurementPoint(
                index=2,
                point_id="p002",
                label="P002",
                design_center=(100.0, 200.0),
                stage_xy=(1.0, 2.0),
                needle_1_design=(101.0, 201.0),
                needle_2_design=(99.0, 199.0),
            ),
            RouteMeasurementPoint(
                index=5,
                point_id="p005",
                label="P005",
                design_center=(110.0, 200.0),
                stage_xy=(1.5, 2.0),
                needle_1_design=(111.0, 201.0),
                needle_2_design=(109.0, 199.0),
            ),
        ]
        progress: list[tuple[int, int, int]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([5.0, 25.0]),
                needle_feedrate=None,
                contact_settle_s=0.0,
                progress_callback=lambda position, total, point_number: progress.append(
                    (position, total, point_number)
                ),
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        self.assertEqual(progress, [(1, 2, 2), (2, 2, 5)])

    def test_runner_keeps_progress_out_of_status_log(self) -> None:
        statuses: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[_point(1), _point(2)],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([5.0, 7.0]),
                needle_feedrate=None,
                contact_settle_s=0.01,
                status_callback=statuses.append,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        self.assertFalse(
            any("%|" in status or "remaining" in status for status in statuses)
        )

    def test_post_measurement_lift_runs_before_result_callback(self) -> None:
        point = _point(1)
        stage = _FakeStage()

        def on_result(_record, _position, _total, saved) -> None:
            stage.calls.append(("result", bool(saved)))

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                contact_settle_s=0.0,
                result_callback=on_result,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        result_index = stage.calls.index(("result", True))
        lower_index = stage.calls.index(("needles", "lower", 75.0))
        post_measurement_lift_index = next(
            index
            for index, call in enumerate(stage.calls)
            if index > lower_index and call == ("needles", "lift", 75.0)
        )
        self.assertLess(post_measurement_lift_index, result_index)

    def test_post_measurement_lift_runs_before_contact_photo_callback(self) -> None:
        point = _point(1)
        stage = _FakeStage()

        def on_contact_photo(_point, _record, _position, _total, saved) -> None:
            stage.calls.append(("contact_photo", bool(saved), stage.axis_a_lowering_mm))

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                contact_settle_s=0.0,
                contact_photo_callback=on_contact_photo,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        callback_index = next(
            index
            for index, call in enumerate(stage.calls)
            if isinstance(call, tuple) and call[:2] == ("contact_photo", True)
        )
        lower_index = stage.calls.index(("needles", "lower", 75.0))
        post_measurement_lift_index = next(
            index
            for index, call in enumerate(stage.calls)
            if index > lower_index and call == ("needles", "lift", 75.0)
        )
        self.assertLess(post_measurement_lift_index, callback_index)
        self.assertEqual(stage.calls[callback_index][2], 0.0)

    def test_pre_contact_photo_callback_runs_before_needle_lower(self) -> None:
        point = _point(1)
        stage = _FakeStage()

        def on_pre_contact_photo(_point, _position, _total) -> None:
            stage.calls.append(("pre_contact_photo", stage.axis_a_lowering_mm))

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0]),
                needle_feedrate=75.0,
                contact_settle_s=0.0,
                pre_contact_photo_callback=on_pre_contact_photo,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        callback_index = next(
            index
            for index, call in enumerate(stage.calls)
            if isinstance(call, tuple) and call[0] == "pre_contact_photo"
        )
        lower_index = stage.calls.index(("needles", "lower", 75.0))
        self.assertLess(callback_index, lower_index)
        self.assertEqual(stage.calls[callback_index][1], 0.0)

    def test_interactive_wait_releases_stage_and_shift_moves_following_points(self) -> None:
        points = [
            RouteMeasurementPoint(
                index=1,
                point_id="p001",
                label="P001",
                design_center=(100.0, 200.0),
                stage_xy=(1.0, 2.0),
                needle_1_design=(101.0, 201.0),
                needle_2_design=(99.0, 199.0),
            ),
            RouteMeasurementPoint(
                index=2,
                point_id="p002",
                label="P002",
                design_center=(110.0, 200.0),
                stage_xy=(3.0, 4.0),
                needle_1_design=(111.0, 201.0),
                needle_2_design=(109.0, 199.0),
            ),
        ]
        stage = _FakeStage()
        records = []
        records_changed = threading.Condition()

        def on_record(record, _position, _total) -> None:
            with records_changed:
                records.append(record)
                records_changed.notify_all()

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=_FakeLCR([5.0, 7.0]),
                needle_feedrate=70.0,
                confirm_each_point=True,
                contact_settle_s=0.0,
                record_callback=on_record,
            )
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            with records_changed:
                self.assertTrue(
                    records_changed.wait_for(lambda: len(records) >= 1, timeout=2.0)
                )
            self.assertEqual(stage.calls[-1], ("finish",))

            saved, message = runner.save_current_position_adjustment((1.25, 1.75))
            self.assertTrue(saved, message)
            runner.submit_confirmation("next")
            with records_changed:
                self.assertTrue(
                    records_changed.wait_for(lambda: len(records) >= 2, timeout=2.0)
                )
            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], True, result[0][1])
        self.assertIn(("move", 1.0, 2.0), stage.calls)
        self.assertIn(("move", 3.25, 3.75), stage.calls)

    def test_runner_starts_from_configured_point_number(self) -> None:
        points = [_point(1), _point(2), _point(3)]
        stage = _FakeStage()

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=_FakeLCR([20.0, 30.0]),
                needle_feedrate=None,
                start_point_number=2,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["structure_number"] for row in rows], ["2", "3"])

        self.assertNotIn(("move", 1.0, 11.0), stage.calls)
        self.assertIn(("move", 2.0, 12.0), stage.calls)
        self.assertIn(("move", 3.0, 13.0), stage.calls)

    def test_runner_preserves_existing_csv_rows_when_starting_later(self) -> None:
        points = [_point(1), _point(2)]

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp": "2026-05-27T10:00:00",
                        "structure_number": "1",
                        "nplc": "10",
                        "n_measurements": "5",
                        "resistance_ohm": "11",
                        "resistance_rms_ohm": "0",
                        "relative_rms": "0",
                        "status": "ok",
                    }
                )
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([22.0]),
                needle_feedrate=None,
                start_point_number=2,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["structure_number"] for row in rows], ["1", "2"])
            self.assertEqual(rows[0]["resistance_ohm"], "11")
            self.assertEqual(rows[1]["resistance_ohm"], "22")

    def test_interactive_jump_to_point_number_continues_from_target(self) -> None:
        points = [_point(1), _point(2), _point(3)]
        records = []
        records_changed = threading.Condition()

        def on_record(record, _position, _total) -> None:
            with records_changed:
                records.append(record)
                records_changed.notify_all()

        def wait_for_record_count(count: int) -> None:
            with records_changed:
                self.assertTrue(
                    records_changed.wait_for(
                        lambda: len(records) >= count,
                        timeout=2.0,
                    )
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([10.0, 30.0]),
                needle_feedrate=None,
                confirm_each_point=True,
                contact_settle_s=0.0,
                record_callback=on_record,
            )
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            wait_for_record_count(1)
            runner.submit_jump(3)
            wait_for_record_count(2)
            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["structure_number"] for row in rows], ["1", "3"])
            self.assertEqual(rows[1]["resistance_ohm"], "30")

    def test_current_point_correction_waits_without_writing_partial_row(self) -> None:
        point = _point(1)
        stage = _NotifyingStage()
        lcr = _FakeLCR([8.0])
        records = []
        records_changed = threading.Condition()
        waiting_values: list[bool] = []
        waiting_changed = threading.Condition()

        def on_record(record, _position, _total) -> None:
            with records_changed:
                records.append(record)
                records_changed.notify_all()

        def on_waiting(waiting: bool) -> None:
            with waiting_changed:
                waiting_values.append(bool(waiting))
                waiting_changed.notify_all()

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                confirm_each_point=True,
                contact_settle_s=0.5,
                record_callback=on_record,
                waiting_callback=on_waiting,
            )
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            self.assertTrue(stage.wait_for_needles_action("lower"))
            runner.request_current_point_correction()
            with waiting_changed:
                self.assertTrue(
                    waiting_changed.wait_for(
                        lambda: waiting_values and waiting_values[-1],
                        timeout=2.0,
                    )
                )

            self.assertEqual(lcr.abort_count, 0)
            self.assertEqual(_read_csv_rows_if_exists(csv_path), [])
            self.assertEqual(stage.calls[-1], ("finish",))

            saved, message = runner.save_current_position_adjustment((1.25, 11.75))
            self.assertTrue(saved, message)
            runner.submit_confirmation("remeasure")
            with records_changed:
                self.assertTrue(
                    records_changed.wait_for(
                        lambda: len(records) >= 1,
                        timeout=2.0,
                    )
                )
            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["structure_number"], "1")
            self.assertEqual(rows[0]["resistance_ohm"], "8")

    def test_interactive_remeasure_appends_same_structure_row(self) -> None:
        point = RouteMeasurementPoint(
            index=9,
            point_id="p009",
            label="P009",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )
        records = []
        records_changed = threading.Condition()

        def on_record(record, _position, _total) -> None:
            with records_changed:
                records.append(record)
                records_changed.notify_all()

        def wait_for_record_count(count: int) -> None:
            with records_changed:
                self.assertTrue(
                    records_changed.wait_for(
                        lambda: len(records) >= count,
                        timeout=2.0,
                    )
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([5.0, 7.0]),
                needle_feedrate=None,
                confirm_each_point=True,
                contact_settle_s=0.0,
                record_callback=on_record,
            )
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            wait_for_record_count(1)
            runner.submit_confirmation("remeasure")
            wait_for_record_count(2)
            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["structure_number"] for row in rows], ["9", "9"])
            self.assertEqual([row["resistance_ohm"] for row in rows], ["5", "7"])

    def test_stop_during_measurement_batch_does_not_write_partial_row(self) -> None:
        point = RouteMeasurementPoint(
            index=4,
            point_id="p004",
            label="P004",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner_holder = {}

            def stop_runner() -> None:
                runner_holder["runner"].stop()

            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([5.0, 7.0], on_read=stop_runner),
                needle_feedrate=None,
                measurement_count=2,
                contact_settle_s=0.0,
            )
            runner_holder["runner"] = runner

            success, message = runner.run()

            self.assertFalse(success)
            self.assertEqual(message, "Route measurement stopped by user.")
            self.assertEqual(_read_csv_rows_if_exists(csv_path), [])


if __name__ == "__main__":
    unittest.main()
