import csv
import math
import tempfile
import threading
import unittest
from pathlib import Path

from probe_station_gui.route_measurement import (
    CSV_FIELDS,
    RouteMeasurementPoint,
    RouteMeasurementRunner,
)


class _FakeStage:
    def __init__(self) -> None:
        self.calls: list[object] = []

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
        return f"{action} done"


class _FakeLCR:
    def __init__(self, values: list[float], on_read=None) -> None:
        self.values = list(values)
        self.on_read = on_read

    def read_primary_value_now(self) -> float:
        value = self.values.pop(0)
        if self.on_read is not None:
            self.on_read()
        return value


class RouteMeasurementRunnerTest(unittest.TestCase):
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

    def test_runner_writes_csv_and_raises_between_points(self) -> None:
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
            self.assertEqual(rows[0]["junction"], "1")
            self.assertEqual(rows[0]["nplc"], "")
            self.assertEqual(rows[0]["n_measurements"], "1")
            self.assertEqual(rows[0]["resistance_ohm"], "5")
            self.assertEqual(rows[0]["resistance_rms_ohm"], "0")
            self.assertEqual(rows[0]["relative_rms"], "0")
            self.assertEqual(rows[0]["status"], "ok")
            self.assertTrue(rows[0]["timestamp"])
            self.assertEqual(rows[1]["junction"], "2")
            self.assertEqual(rows[1]["resistance_ohm"], "25")

        self.assertEqual(stage.calls[0], ("begin", "route measurement"))
        self.assertIn(("move", 1.0, 2.0), stage.calls)
        self.assertIn(("move", 1.5, 2.0), stage.calls)
        self.assertEqual(stage.calls[-1], ("finish",))
        lower_calls = [
            call for call in stage.calls if call == ("needles", "lower", 75.0)
        ]
        self.assertEqual(len(lower_calls), 2)

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
            self.assertEqual(rows[0]["junction"], "7")
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
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["n_measurements"], "2")
            self.assertEqual(rows[0]["resistance_ohm"], "6")
            self.assertEqual(rows[0]["resistance_rms_ohm"], "1")
            self.assertEqual(rows[0]["relative_rms"], "0.166666666667")

    def test_interactive_remeasure_replaces_same_junction_row(self) -> None:
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
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["junction"], "9")
            self.assertEqual(rows[0]["resistance_ohm"], "7")

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
