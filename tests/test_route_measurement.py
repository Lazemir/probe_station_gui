import csv
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path

from probe_station_gui.route_measurement import (
    CSV_FIELDS,
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    RouteMeasurementPoint,
    RouteMeasurementRunner,
    RouteExternalMeasurementSessionRunner,
    filter_route_points_by_previous_status,
    latest_route_measurement_statuses,
)


class _FakeStage:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.axis_a_lowering_mm = math.nan

    def begin_external_task(self, label: str) -> None:
        self.calls.append(("begin", label))

    def finish_external_task(self) -> None:
        self.calls.append(("finish",))

    def run_external_move_to_xy(self, x_mm: float, y_mm: float) -> str:
        self.calls.append(("move", x_mm, y_mm))
        return "moved"

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        self.calls.append(("needles", action, feedrate))
        if action == "lower":
            self.axis_a_lowering_mm = 1.0
        elif action in {"lift", "raise"}:
            self.axis_a_lowering_mm = 0.0
        return f"{action} done"

    def run_external_needles_adjust(
        self,
        step_mm: float,
        feedrate: float | None = None,
    ) -> str:
        self.calls.append(("adjust", step_mm, feedrate))
        if math.isfinite(self.axis_a_lowering_mm):
            self.axis_a_lowering_mm += abs(float(step_mm))
        return "adjusted"

    def run_external_needles_lower_to_depth_below_down(
        self,
        depth_mm: float,
        feedrate: float | None = None,
    ) -> str:
        self.calls.append(("lower_to_depth", depth_mm, feedrate))
        self.axis_a_lowering_mm = 1.0 + float(depth_mm)
        return "lowered to depth"

    def run_external_local_autofocus(self, *, range_mm: float, step_mm=None) -> str:
        self.calls.append(("autofocus", range_mm, step_mm))
        return "local autofocus done"

    def latest_axis_a_lowering(self) -> float:
        return self.axis_a_lowering_mm


class _NotifyingStage(_FakeStage):
    def __init__(self) -> None:
        super().__init__()
        self.changed = threading.Condition()

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        result = super().run_external_needles_action(action, feedrate)
        with self.changed:
            self.changed.notify_all()
        return result

    def wait_for_needles_action(self, action: str) -> bool:
        with self.changed:
            return self.changed.wait_for(
                lambda: any(
                    call[0] == "needles" and call[1] == action
                    for call in self.calls
                    if isinstance(call, tuple)
                ),
                timeout=2.0,
            )


class _FakeLCR:
    def __init__(self, values: list[float], on_read=None) -> None:
        self.values = list(values)
        self.on_read = on_read
        self.abort_count = 0

    def read_primary_value_now(self) -> float:
        value = self.values.pop(0)
        if self.on_read is not None:
            self.on_read()
        return value

    def abort_current_measurement(self) -> None:
        self.abort_count += 1


class _FakeRouteLCR:
    def __init__(self, measurements: list[dict[str, object]]) -> None:
        self.measurements = list(measurements)

    def read_route_measurement_now(self) -> dict[str, object]:
        return dict(self.measurements.pop(0))


class _FakeBatchRouteLCR:
    def __init__(self, measurements: list[dict[str, object]]) -> None:
        self.measurements = list(measurements)
        self.batch_counts: list[int] = []

    def read_route_measurement_batch_now(self, count: int) -> list[dict[str, object]]:
        self.batch_counts.append(int(count))
        batch = self.measurements[:count]
        del self.measurements[:count]
        return [dict(item) for item in batch]


class _PreparedBatchRouteLCR(_FakeBatchRouteLCR):
    def __init__(self, measurements: list[dict[str, object]]) -> None:
        super().__init__(measurements)
        self.prepare_calls: list[tuple[int, int | None]] = []
        self.prepare_started = threading.Event()
        self.allow_prepare_finish = threading.Event()
        self.lift_started = threading.Event()
        self.read_after_measurement: list[bool] = []

    def prepare_route_measurement_batch_now(
        self,
        count: int,
        *,
        source_list_count: int | None = None,
    ) -> None:
        self.prepare_calls.append((int(count), source_list_count))
        self.prepare_started.set()
        self.allow_prepare_finish.wait(timeout=2.0)

    def read_route_measurement_batch_now(
        self,
        count: int,
        *,
        after_measurement=None,
    ) -> list[dict[str, object]]:
        self.read_after_measurement.append(after_measurement is not None)
        if after_measurement is not None:
            after_measurement()
            self.lift_started.wait(timeout=2.0)
        return super().read_route_measurement_batch_now(count)


class _PrepareAwareStage(_FakeStage):
    def __init__(self, lcr: _PreparedBatchRouteLCR) -> None:
        super().__init__()
        self.lcr = lcr
        self.prepare_started_before_lower = False

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        if action == "lower":
            self.prepare_started_before_lower = self.lcr.prepare_started.wait(
                timeout=2.0
            )
            self.lcr.allow_prepare_finish.set()
        result = super().run_external_needles_action(action, feedrate)
        if action == "lift":
            self.lcr.lift_started.set()
        return result


class _PausingBatchRouteLCR(_FakeBatchRouteLCR):
    def __init__(self, measurements: list[dict[str, object]], pause_on_batch) -> None:
        super().__init__(measurements)
        self.pause_on_batch = pause_on_batch

    def read_route_measurement_batch_now(self, count: int) -> list[dict[str, object]]:
        batch = super().read_route_measurement_batch_now(count)
        self.pause_on_batch(len(self.batch_counts))
        return batch


class _OpeningLCR(_FakeLCR):
    def __init__(self, values: list[float]) -> None:
        super().__init__(values)
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True


def _point(index: int) -> RouteMeasurementPoint:
    return RouteMeasurementPoint(
        index=index,
        point_id=f"p{index:03d}",
        label=f"P{index:03d}",
        design_center=(100.0 + index, 200.0),
        stage_xy=(float(index), 10.0 + float(index)),
        needle_1_design=(101.0 + index, 201.0),
        needle_2_design=(99.0 + index, 199.0),
    )


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

    def test_external_session_waits_for_notebook_result_without_csv(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 100.0 + index}
                for index in range(5)
            ]
        )
        records = []
        finished: list[tuple[bool, str]] = []
        runner = RouteExternalMeasurementSessionRunner(
            session_id="session-1",
            points=[point],
            stage_controller=stage,
            lcr_controller=lcr,
            needle_feedrate=75.0,
            measurement_count=5,
            initial_measurement_count=2,
            contact_settle_s=0.0,
            photo_enabled=False,
            photo_focus_enabled=False,
            result_callback=lambda record, position, total, saved: records.append(
                (record, position, total, saved)
            ),
        )
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        self.assertTrue(
            self._wait_for_state(runner, "waiting_external_measurement")
        )
        status = runner.status_payload()
        preparation = status["last_preparation"]
        self.assertEqual(preparation["measurement"]["status"], "ok")
        self.assertEqual(len(preparation["measurement"]["raw_samples"]), 5)
        self.assertEqual(lcr.batch_counts, [2, 3])

        self.assertTrue(
            runner.submit_external_result(
                {
                    "status": "ok",
                    "summary": {"iv_points": 31},
                    "files": [{"kind": "iv", "path": "iv.csv"}],
                }
            )
        )
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0][3])
        self.assertIn(("needles", "lift", 75.0), stage.calls)
        final_status = runner.status_payload()
        self.assertEqual(final_status["history"][0]["external_result"]["summary"], {"iv_points": 31})

    def test_external_session_short_skips_external_wait_and_followup(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {
                    "differential_resistance_ohm": 2.0,
                    "compliance_hit": True,
                },
                {
                    "differential_resistance_ohm": 3.0,
                    "compliance_hit": True,
                },
            ]
        )
        runner = RouteExternalMeasurementSessionRunner(
            session_id="session-1",
            points=[point],
            stage_controller=stage,
            lcr_controller=lcr,
            needle_feedrate=75.0,
            measurement_count=5,
            initial_measurement_count=2,
            contact_settle_s=0.0,
            photo_enabled=False,
            photo_focus_enabled=False,
        )

        success, message = runner.run()

        self.assertTrue(success, message)
        self.assertEqual(message, "Route API session complete.")
        self.assertEqual(lcr.batch_counts, [2])
        status = runner.status_payload()
        self.assertEqual(status["history"][0]["status"], "short")
        self.assertEqual(
            status["history"][0]["preparation"]["measurement"]["n_measurements"],
            2,
        )
        self.assertIn(("needles", "lift", 75.0), stage.calls)

    def test_external_session_waiting_action_from_callback_is_not_lost(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 100.0 + index}
                for index in range(5)
            ]
        )
        runner_holder: dict[str, RouteExternalMeasurementSessionRunner] = {}

        def on_waiting(waiting: bool) -> None:
            if waiting:
                runner_holder["runner"].submit_confirmation("skip")

        runner = RouteExternalMeasurementSessionRunner(
            session_id="session-1",
            points=[point],
            stage_controller=stage,
            lcr_controller=lcr,
            needle_feedrate=75.0,
            measurement_count=5,
            initial_measurement_count=2,
            contact_settle_s=0.0,
            photo_enabled=False,
            photo_focus_enabled=False,
            waiting_callback=on_waiting,
        )
        runner_holder["runner"] = runner
        finished: list[tuple[bool, str]] = []
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])
        self.assertEqual(runner.status_payload()["history"][0]["status"], "skipped")

    @staticmethod
    def _wait_for_state(
        runner: RouteExternalMeasurementSessionRunner,
        state: str,
        *,
        timeout_s: float = 2.0,
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if runner.status_payload().get("state") == state:
                return True
            time.sleep(0.01)
        return False

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

    def test_runner_records_overload_for_nonfinite_lcr_reading(self) -> None:
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([math.inf]),
                needle_feedrate=None,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["structure_number"], "7")
            self.assertEqual(rows[0]["resistance_ohm"], "")
            self.assertEqual(rows[0]["resistance_rms_ohm"], "")
            self.assertEqual(rows[0]["relative_rms"], "")
            self.assertEqual(rows[0]["status"], "overload")

    def test_runner_averages_configured_measurement_count(self) -> None:
        point = RouteMeasurementPoint(
            index=3,
            point_id="p003",
            label="P003",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([5.0, 7.0]),
                needle_feedrate=None,
                measurement_count=2,
                measurement_type="Keithley voltage sweep +/-0.03 V",
                contact_settle_s=0.0,
                nplc_label="10",
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "2")
            self.assertEqual(rows[0]["nplc"], "10")
            self.assertEqual(
                rows[0]["measurement_type"],
                "Keithley voltage sweep +/-0.03 V",
            )
            self.assertEqual(rows[0]["resistance_ohm"], "6")
            self.assertEqual(rows[0]["resistance_rms_ohm"], "1")
            self.assertEqual(rows[0]["relative_rms"], "0.166666666667")

    def test_runner_prepares_batch_before_lower_and_lifts_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            lcr = _PreparedBatchRouteLCR(
                [
                    {"differential_resistance_ohm": 10.0},
                    {"differential_resistance_ohm": 12.0},
                ]
            )
            stage = _PrepareAwareStage(lcr)
            result_events: list[object] = []
            runner = RouteMeasurementRunner(
                points=[_point(1)],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=2,
                initial_measurement_count=2,
                contact_settle_s=0.0,
                result_callback=lambda *_args: result_events.append(("result",)),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertTrue(stage.prepare_started_before_lower)
            self.assertEqual(lcr.prepare_calls, [(2, 2)])
            self.assertEqual(lcr.read_after_measurement, [True])
            self.assertTrue(lcr.lift_started.is_set())
            self.assertIn(("needles", "lift", 75.0), stage.calls)
            self.assertEqual(result_events, [("result",)])

    def test_runner_prepares_initial_batch_with_followup_source_list_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            lcr = _FakeBatchRouteLCR(
                [
                    {"differential_resistance_ohm": 10.0},
                    {"differential_resistance_ohm": 12.0},
                    {"differential_resistance_ohm": 11.0},
                    {"differential_resistance_ohm": 13.0},
                    {"differential_resistance_ohm": 14.0},
                ]
            )
            prepare_calls: list[tuple[int, int | None]] = []

            def prepare(count: int, *, source_list_count: int | None = None) -> None:
                prepare_calls.append((int(count), source_list_count))

            lcr.prepare_route_measurement_batch_now = prepare
            runner = RouteMeasurementRunner(
                points=[_point(1)],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=5,
                initial_measurement_count=2,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(prepare_calls[:2], [(2, 3), (3, 3)])
            self.assertEqual(lcr.batch_counts, [2, 3])

    def test_runner_uses_batch_route_reader_when_available(self) -> None:
        point = _point(3)
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 5.0},
                {"differential_resistance_ohm": 6.0},
                {"differential_resistance_ohm": 7.0},
            ]
        )
        records = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=3,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(record),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [3])
            self.assertEqual(lcr.measurements, [])
            self.assertEqual(
                [sample.differential_resistance_ohm for sample in records[0].raw_samples],
                [5.0, 6.0, 7.0],
            )
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "3")
            self.assertEqual(rows[0]["resistance_ohm"], "6")

    def test_large_batch_route_reader_is_delegated_to_driver(self) -> None:
        point = _point(3)
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": float(value)}
                for value in range(1, 126)
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=125,
                initial_measurement_count=125,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [125])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "125")

    def test_low_resistance_without_compliance_is_not_short(self) -> None:
        point = _point(1)
        lcr = _FakeLCR([0.4, 0.5, 0.6, 0.5, 0.4])
        records = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=5,
                short_threshold_ohm=1.0,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(record),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.values, [])
            self.assertEqual(
                [sample.differential_resistance_ohm for sample in records[0].raw_samples],
                [0.4, 0.5, 0.6, 0.5, 0.4],
            )
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "5")
            self.assertEqual(rows[0]["resistance_ohm"], "0.48")
            self.assertEqual(rows[0]["status"], "ok")

    def test_non_short_check_does_not_discard_first_sample(self) -> None:
        point = _point(1)
        lcr = _FakeLCR([100.0, 5.0])
        records = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=2,
                short_threshold_ohm=1.0,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(record),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.values, [])
            self.assertEqual(len(records), 1)
            self.assertEqual(
                [sample.differential_resistance_ohm for sample in records[0].raw_samples],
                [100.0, 5.0],
            )
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "2")
            self.assertEqual(rows[0]["resistance_ohm"], "52.5")
            self.assertEqual(rows[0]["status"], "ok")

    def test_non_short_check_tops_up_initial_batch(self) -> None:
        point = _point(1)
        lcr = _FakeBatchRouteLCR(
            [{"differential_resistance_ohm": value} for value in range(10, 22)]
        )
        records = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=12,
                short_threshold_ohm=1.0,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(record),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [10, 2])
            self.assertEqual(
                [sample.differential_resistance_ohm for sample in records[0].raw_samples],
                list(range(10, 22)),
            )
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "12")
            self.assertEqual(rows[0]["status"], "ok")

    def test_bad_contact_initial_batch_skips_followup_batch(self) -> None:
        point = _point(1)
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    520000.0,
                    610000.0,
                    480000.0,
                    570000.0,
                    540000.0,
                    620000.0,
                    500000.0,
                    590000.0,
                    560000.0,
                    630000.0,
                    10.0,
                    11.0,
                )
            ]
        )
        records = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=12,
                short_threshold_ohm=1.0,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(record),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [10])
            self.assertEqual(len(records[0].raw_samples), 10)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "10")
            self.assertEqual(rows[0]["status"], "bad_contact")

    def test_auto_contact_seek_retries_lift_lower_before_full_measurement(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    200000.0,
                    210000.0,
                    1000.0,
                    1001.0,
                    1002.0,
                    1003.0,
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=4,
                initial_measurement_count=2,
                short_threshold_ohm=10.0,
                auto_contact_seek_on_bad_contact=True,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [2, 2, 2])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["n_measurements"], "4")

        first_lower_index = stage.calls.index(("needles", "lower", 75.0))
        retry_lift_index = next(
            index
            for index, call in enumerate(stage.calls)
            if index > first_lower_index and call == ("needles", "lift", 75.0)
        )
        retry_lower_index = next(
            index
            for index, call in enumerate(stage.calls)
            if index > retry_lift_index and call == ("needles", "lower", 75.0)
        )
        final_lift_index = next(
            index
            for index, call in enumerate(stage.calls)
            if index > retry_lower_index and call == ("needles", "lift", 75.0)
        )
        self.assertLess(first_lower_index, retry_lift_index)
        self.assertLess(retry_lift_index, retry_lower_index)
        self.assertLess(retry_lower_index, final_lift_index)
        self.assertEqual([call for call in stage.calls if call[0] == "adjust"], [])

    def test_place_contact_reuses_contact_seek_without_csv_and_leaves_needles_down(
        self,
    ) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (200000.0, 210000.0, 1000.0, 1001.0)
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=2,
                initial_measurement_count=2,
                auto_contact_seek_on_bad_contact=True,
                contact_settle_s=0.0,
            )

            result = runner.place_contact(point)

            self.assertTrue(result.success, result.message)
            self.assertEqual(result.record.status, "ok")
            self.assertEqual(result.record.n_measurements, 2)
            self.assertFalse(csv_path.exists())

        self.assertEqual(lcr.batch_counts, [2, 2])
        self.assertEqual(stage.axis_a_lowering_mm, 1.0)
        self.assertEqual(stage.calls[-1], ("finish",))
        self.assertNotIn(("needles", "lift", 75.0), stage.calls[-2:])

    def test_place_contact_failure_lifts_needles_without_csv(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    200000.0,
                    210000.0,
                    220000.0,
                    230000.0,
                    240000.0,
                    250000.0,
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=2,
                initial_measurement_count=2,
                auto_contact_seek_on_bad_contact=True,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.001,
                contact_settle_s=0.0,
            )

            result = runner.place_contact(point)

            self.assertFalse(result.success)
            self.assertEqual(result.record.status, "bad_contact")
            self.assertFalse(csv_path.exists())

        self.assertEqual(lcr.batch_counts, [2, 2, 2])
        self.assertEqual(stage.calls[-2:], [("needles", "lift", None), ("finish",)])

    def test_check_contact_measures_current_position_without_seek_or_csv(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 200000.0},
                {"differential_resistance_ohm": 210000.0},
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=2,
                initial_measurement_count=2,
                auto_contact_seek_on_bad_contact=True,
                contact_settle_s=0.0,
            )

            result = runner.check_contact(point)

            self.assertFalse(result.success)
            self.assertEqual(result.record.status, "bad_contact")
            self.assertIsNone(result.contact_seek)
            self.assertFalse(csv_path.exists())

        self.assertEqual(lcr.batch_counts, [2])
        self.assertNotIn(("move", 1.0, 2.0), stage.calls)
        self.assertEqual([call for call in stage.calls if call[0] == "needles"], [])
        self.assertEqual(stage.calls[-1], ("finish",))

    def test_seek_contact_uses_current_position_without_xy_move_or_csv(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (200000.0, 210000.0, 1000.0, 1001.0)
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=2,
                initial_measurement_count=2,
                auto_contact_seek_on_bad_contact=False,
                contact_settle_s=0.0,
            )

            result = runner.seek_contact(point)

            self.assertTrue(result.success, result.message)
            self.assertEqual(result.record.status, "ok")
            self.assertIsNotNone(result.contact_seek)
            self.assertTrue(result.contact_seek.found)
            self.assertFalse(csv_path.exists())

        self.assertEqual(lcr.batch_counts, [2, 2])
        self.assertNotIn(("move", 1.0, 2.0), stage.calls)
        self.assertIn(("needles", "lift", 75.0), stage.calls)
        self.assertIn(("needles", "lower", 75.0), stage.calls)
        self.assertEqual(stage.calls[-1], ("finish",))

    def test_auto_contact_seek_presses_deeper_when_full_batch_turns_bad(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        contact_heights = []
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    200000.0,
                    210000.0,
                    220000.0,
                    230000.0,
                    1000.0,
                    1001.0,
                    1000.0,
                    5000.0,
                    1000.0,
                    1001.0,
                    1002.0,
                    1003.0,
                )
            ]
        )

        def on_contact_height(record, _position, _total) -> None:
            contact_heights.append(record)

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=4,
                initial_measurement_count=2,
                auto_contact_seek_on_bad_contact=True,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.002,
                contact_settle_s=0.0,
                contact_height_record_callback=on_contact_height,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [2, 2, 2, 2, 2, 2])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["n_measurements"], "4")
            self.assertEqual(len(contact_heights), 1)
            self.assertTrue(contact_heights[0].contact_found)
            self.assertAlmostEqual(
                contact_heights[0].contact_depth_below_down_mm,
                0.002,
            )
            self.assertAlmostEqual(
                contact_heights[0].contact_axis_a_lowering_mm,
                1.002,
            )
            self.assertIsNotNone(contact_heights[0].contact_seek)
            self.assertTrue(contact_heights[0].contact_seek.found)
            self.assertEqual(contact_heights[0].contact_seek.status, "found")
            self.assertEqual(contact_heights[0].contact_seek.attempts, 3)
            self.assertEqual(
                contact_heights[0].contact_seek.initial_status,
                "bad_contact",
            )
            self.assertEqual(contact_heights[0].contact_seek.final_status, "good")

        self.assertEqual(
            [call for call in stage.calls if call[0] == "lower_to_depth"],
            [
                ("lower_to_depth", 0.001, None),
                ("lower_to_depth", 0.002, None),
            ],
        )

    def test_auto_contact_seek_starts_deeper_after_initial_full_batch_fails(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    1000.0,
                    1001.0,
                    1000.0,
                    5000.0,
                    1000.0,
                    1001.0,
                    1002.0,
                    1003.0,
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=4,
                initial_measurement_count=2,
                auto_contact_seek_on_bad_contact=True,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.002,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [2, 2, 2, 2])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["n_measurements"], "4")

        self.assertEqual(
            [call for call in stage.calls if call[0] == "lower_to_depth"],
            [("lower_to_depth", 0.001, None)],
        )

    def test_auto_contact_seek_stops_at_configured_depth(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    200000.0,
                    210000.0,
                    220000.0,
                    230000.0,
                    240000.0,
                    250000.0,
                    260000.0,
                    270000.0,
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=4,
                initial_measurement_count=2,
                short_threshold_ohm=10.0,
                auto_contact_seek_on_bad_contact=True,
                auto_contact_seek_step_mm=0.0005,
                auto_contact_seek_max_total_mm=0.001,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [2, 2, 2, 2])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "bad_contact")
            self.assertEqual(rows[0]["n_measurements"], "2")

        self.assertEqual(
            [call for call in stage.calls if call[0] == "lower_to_depth"],
            [
                ("lower_to_depth", 0.0005, None),
                ("lower_to_depth", 0.001, None),
            ],
        )
        self.assertEqual([call for call in stage.calls if call[0] == "adjust"], [])

        first_depth_index = stage.calls.index(("lower_to_depth", 0.0005, None))
        self.assertEqual(stage.calls[first_depth_index - 1], ("needles", "lift", None))

    def test_exhausted_auto_contact_seek_saves_bad_contact_in_confirm_mode(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    200000.0,
                    210000.0,
                    220000.0,
                    230000.0,
                    240000.0,
                    250000.0,
                )
            ]
        )
        results = []
        results_changed = threading.Condition()
        contact_heights = []

        def on_result(record, _position, _total, saved) -> None:
            with results_changed:
                results.append((record, saved))
                results_changed.notify_all()

        def on_contact_height(record, _position, _total) -> None:
            contact_heights.append(record)

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=4,
                initial_measurement_count=2,
                short_threshold_ohm=10.0,
                confirm_each_point=True,
                auto_contact_seek_on_bad_contact=True,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.001,
                contact_settle_s=0.0,
                result_callback=on_result,
                contact_height_record_callback=on_contact_height,
            )
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            with results_changed:
                self.assertTrue(
                    results_changed.wait_for(
                        lambda: len(results) >= 1,
                        timeout=2.0,
                    )
                )
            self.assertEqual(results[0][0].status, "bad_contact")
            self.assertTrue(results[0][1])

            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "bad_contact")
            self.assertEqual(rows[0]["n_measurements"], "2")

        self.assertEqual(len(contact_heights), 1)
        self.assertFalse(contact_heights[0].contact_found)
        self.assertIsNotNone(contact_heights[0].contact_seek)
        self.assertFalse(contact_heights[0].contact_seek.found)
        self.assertEqual(contact_heights[0].contact_seek.status, "not_found")
        self.assertAlmostEqual(
            contact_heights[0].contact_seek.depth_below_down_mm,
            0.001,
        )

    def test_pause_during_auto_contact_seek_waits_for_saved_point(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        waiting_values: list[bool] = []
        waiting_changed = threading.Condition()
        statuses: list[str] = []
        statuses_changed = threading.Condition()
        runner_holder: dict[str, RouteMeasurementRunner] = {}

        def maybe_pause(batch_number: int) -> None:
            if batch_number == 2:
                runner_holder["runner"].request_pause_after_current_point()

        def on_waiting(waiting: bool) -> None:
            with waiting_changed:
                waiting_values.append(bool(waiting))
                waiting_changed.notify_all()

        def on_status(status: str) -> None:
            with statuses_changed:
                statuses.append(status)
                statuses_changed.notify_all()

        lcr = _PausingBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    200000.0,
                    210000.0,
                    1000.0,
                    1001.0,
                )
            ],
            maybe_pause,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=2,
                initial_measurement_count=2,
                confirm_each_point=True,
                auto_contact_seek_on_bad_contact=True,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.003,
                contact_settle_s=0.0,
                status_callback=on_status,
                waiting_callback=on_waiting,
            )
            runner_holder["runner"] = runner
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            with waiting_changed:
                self.assertTrue(
                    waiting_changed.wait_for(
                        lambda: waiting_values and waiting_values[-1],
                        timeout=2.0,
                    )
                )
            with statuses_changed:
                self.assertTrue(
                    statuses_changed.wait_for(
                        lambda: any("paused" in status for status in statuses),
                        timeout=2.0,
                    )
                )

            self.assertTrue(thread.is_alive())
            self.assertEqual(lcr.batch_counts, [2, 2])
            self.assertGreaterEqual(
                len([call for call in stage.calls if call == ("needles", "lift", 75.0)]),
                2,
            )

            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], True, result[0][1])

    def test_runtime_settings_update_applies_to_remeasure(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    200000.0,
                    210000.0,
                    220000.0,
                    230000.0,
                    240000.0,
                    250000.0,
                    260000.0,
                    270000.0,
                    280000.0,
                    290000.0,
                    300000.0,
                    310000.0,
                    1000.0,
                    1001.0,
                )
            ]
        )
        results = []
        results_changed = threading.Condition()

        def on_result(record, _position, _total, saved) -> None:
            with results_changed:
                results.append((record, saved))
                results_changed.notify_all()

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=2,
                initial_measurement_count=2,
                confirm_each_point=True,
                auto_contact_seek_on_bad_contact=True,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.001,
                contact_settle_s=0.0,
                result_callback=on_result,
            )
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            with results_changed:
                self.assertTrue(
                    results_changed.wait_for(
                        lambda: len(results) >= 1,
                        timeout=2.0,
                    )
                )
            self.assertEqual(results[0][0].status, "bad_contact")
            self.assertTrue(results[0][1])

            runner.update_runtime_settings(
                measurement_count=2,
                initial_measurement_count=2,
                max_relative_rms=0.01,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.002,
                contact_settle_s=0.0,
            )
            runner.submit_confirmation("remeasure")
            with results_changed:
                self.assertTrue(
                    results_changed.wait_for(
                        lambda: len(results) >= 2,
                        timeout=2.0,
                    )
                )
            self.assertEqual(results[1][0].status, "ok")
            self.assertTrue(results[1][1])
            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["status"], "bad_contact")
            self.assertEqual(rows[1]["status"], "ok")

        self.assertIn(("lower_to_depth", 0.002, None), stage.calls)

    def test_auto_contact_seek_compliance_short_is_saved_without_followup(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 200000.0},
                {"differential_resistance_ohm": 400000.0},
                {
                    "differential_resistance_ohm": 1100000.0,
                    "compliance_hit": True,
                },
                {"differential_resistance_ohm": 1200000.0},
                {"differential_resistance_ohm": 10.0},
                {"differential_resistance_ohm": 11.0},
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=4,
                initial_measurement_count=2,
                short_threshold_ohm=10.0,
                auto_contact_seek_on_bad_contact=True,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [2, 2])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "short")
            self.assertEqual(rows[0]["n_measurements"], "2")

    def test_compliance_hit_marks_short_even_above_threshold(self) -> None:
        point = _point(1)
        lcr = _FakeRouteLCR(
            [
                {
                    "differential_resistance_ohm": 100.0,
                    "compliance_hit": index == 0,
                }
                for index in range(5)
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=5,
                short_threshold_ohm=1.0,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "5")
            self.assertEqual(rows[0]["resistance_ohm"], "100")
            self.assertEqual(rows[0]["status"], "short")
            self.assertEqual(rows[0]["contact_compliance_hits"], "1")

    def test_interactive_auto_next_continues_after_ok_and_short(self) -> None:
        points = [_point(1), _point(2)]
        records = []
        statuses = []
        waiting_values: list[bool] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeRouteLCR(
                    [
                        {
                            "differential_resistance_ohm": 5.0,
                            "compliance_hit": True,
                        },
                        {"differential_resistance_ohm": 20.0},
                    ]
                ),
                needle_feedrate=None,
                measurement_count=1,
                short_threshold_ohm=10.0,
                confirm_each_point=True,
                auto_next_ok_or_short=True,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(
                    record
                ),
                status_callback=statuses.append,
                waiting_callback=lambda waiting: waiting_values.append(bool(waiting)),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["status"] for row in rows], ["short", "ok"])

        self.assertEqual([record.status for record in records], ["short", "ok"])
        self.assertNotIn(True, waiting_values)
        self.assertTrue(any("continuing" in status for status in statuses))

    def test_interactive_pause_waits_after_current_saved_point(self) -> None:
        points = [_point(1), _point(2)]
        statuses = []
        statuses_changed = threading.Condition()
        waiting_values: list[bool] = []
        waiting_changed = threading.Condition()

        def on_status(status: str) -> None:
            with statuses_changed:
                statuses.append(status)
                statuses_changed.notify_all()

        def on_waiting(waiting: bool) -> None:
            with waiting_changed:
                waiting_values.append(bool(waiting))
                waiting_changed.notify_all()

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=points,
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeRouteLCR(
                    [
                        {"differential_resistance_ohm": 20.0},
                        {"differential_resistance_ohm": 30.0},
                    ]
                ),
                needle_feedrate=None,
                measurement_count=1,
                confirm_each_point=True,
                auto_next_ok_or_short=True,
                contact_settle_s=0.0,
                status_callback=on_status,
                waiting_callback=on_waiting,
            )
            result = []
            runner.request_pause_after_current_point()
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            with waiting_changed:
                self.assertTrue(
                    waiting_changed.wait_for(
                        lambda: waiting_values and waiting_values[-1],
                        timeout=2.0,
                    )
                )
            self.assertTrue(thread.is_alive())
            with statuses_changed:
                self.assertTrue(
                    statuses_changed.wait_for(
                        lambda: any("paused" in status for status in statuses),
                        timeout=2.0,
                    )
                )
            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["status"] for row in rows], ["ok", "ok"])

    def test_interactive_short_ignores_relative_rms_limit(self) -> None:
        point = _point(1)
        waiting_values: list[bool] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeRouteLCR(
                    [
                        {
                            "differential_resistance_ohm": 1.0,
                            "compliance_hit": True,
                        },
                        {
                            "differential_resistance_ohm": 3.0,
                            "compliance_hit": True,
                        },
                    ]
                ),
                needle_feedrate=None,
                measurement_count=2,
                short_threshold_ohm=10.0,
                max_relative_rms=0.01,
                confirm_each_point=True,
                auto_next_ok_or_short=True,
                contact_settle_s=0.0,
                waiting_callback=lambda waiting: waiting_values.append(bool(waiting)),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "short")
            self.assertEqual(rows[0]["n_measurements"], "2")

        self.assertNotIn(True, waiting_values)

    def test_interactive_auto_next_still_waits_after_rejected_result(self) -> None:
        point = _point(1)
        waiting_values: list[bool] = []
        waiting_changed = threading.Condition()

        def on_waiting(waiting: bool) -> None:
            with waiting_changed:
                waiting_values.append(bool(waiting))
                waiting_changed.notify_all()

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([5.0, 7.0]),
                needle_feedrate=None,
                measurement_count=2,
                max_relative_rms=0.01,
                confirm_each_point=True,
                auto_next_ok_or_short=True,
                contact_settle_s=0.0,
                waiting_callback=on_waiting,
            )
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            with waiting_changed:
                self.assertTrue(
                    waiting_changed.wait_for(
                        lambda: waiting_values and waiting_values[-1],
                        timeout=2.0,
                    )
                )
            self.assertTrue(thread.is_alive())
            runner.submit_confirmation("skip")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_interactive_quality_limit_rejects_noisy_result_without_csv_row(self) -> None:
        point = _point(1)
        records = []
        records_changed = threading.Condition()
        results = []
        results_changed = threading.Condition()

        def on_record(record, _position, _total) -> None:
            with records_changed:
                records.append(record)
                records_changed.notify_all()

        def on_result(record, _position, _total, saved) -> None:
            with results_changed:
                results.append((record, saved))
                results_changed.notify_all()

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=_FakeLCR([5.0, 7.0, 10.0, 10.0]),
                needle_feedrate=None,
                measurement_count=2,
                max_relative_rms=0.01,
                confirm_each_point=True,
                contact_settle_s=0.0,
                record_callback=on_record,
                result_callback=on_result,
            )
            result = []
            thread = threading.Thread(
                target=lambda: result.append(runner.run()),
                daemon=True,
            )

            thread.start()
            with results_changed:
                self.assertTrue(
                    results_changed.wait_for(
                        lambda: len(results) >= 1,
                        timeout=2.0,
                    )
                )
            self.assertFalse(results[0][1])
            self.assertEqual(results[0][0].status, "unstable")
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

            runner.submit_confirmation("remeasure")
            with results_changed:
                self.assertTrue(
                    results_changed.wait_for(
                        lambda: len(results) >= 2,
                        timeout=2.0,
                    )
                )
            runner.submit_confirmation("next")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            self.assertTrue(results[1][1])
            self.assertEqual(len(records), 1)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["structure_number"], "1")
            self.assertEqual(rows[0]["resistance_ohm"], "10")

    def test_contact_quality_marks_noisy_open_contact(self) -> None:
        point = _point(1)
        lcr = _FakeBatchRouteLCR(
            [
                {
                    "differential_resistance_ohm": value,
                    "negative": {"current_a": -0.001},
                    "positive": {"current_a": 0.001},
                }
                for value in (520000.0, 610000.0, 480000.0, 570000.0)
            ]
        )
        records = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=4,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(record),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(records[0].status, "bad_contact")
            self.assertIsNotNone(records[0].contact_quality)
            self.assertFalse(records[0].contact_quality.good)
            self.assertIn("mad_sigma_too_high", records[0].contact_quality.reasons)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "bad_contact")
            self.assertEqual(rows[0]["contact_quality"], "bad_contact")
            self.assertEqual(rows[0]["contact_polarity_sign_mismatches"], "0")

    def test_high_stable_resistance_is_measurement_not_bad_contact(self) -> None:
        point = _point(1)
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (106000.0, 106050.0, 105980.0, 106020.0)
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=_FakeStage(),
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=4,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["contact_quality"], "good")

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
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
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
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
