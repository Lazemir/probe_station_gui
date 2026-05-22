import csv
import tempfile
import unittest
from pathlib import Path

from probe_station_gui.route_measurement import (
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
    def __init__(self, values: list[float]) -> None:
        self.values = list(values)

    def read_primary_value_now(self) -> float:
        return self.values.pop(0)


class RouteMeasurementRunnerTest(unittest.TestCase):
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
                rows = list(csv.reader(handle))
            self.assertEqual(rows, [["5.0"], ["25.0"]])

        self.assertEqual(stage.calls[0], ("begin", "route measurement"))
        self.assertIn(("move", 1.0, 2.0), stage.calls)
        self.assertIn(("move", 1.5, 2.0), stage.calls)
        self.assertEqual(stage.calls[-1], ("finish",))
        lower_calls = [
            call for call in stage.calls if call == ("needles", "lower", 75.0)
        ]
        self.assertEqual(len(lower_calls), 2)


if __name__ == "__main__":
    unittest.main()
