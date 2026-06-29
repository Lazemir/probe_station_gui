import csv
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from probe_station_gui.route.measurement import RouteMeasurementRunner


def _calls_named(calls: list[tuple[Any, ...]], name: str) -> list[tuple[Any, ...]]:
    return [call for call in calls if call[0] == name]


try:
    from .measurement_test_support import (
        _FailingPreparedBatchRouteLCR,
        _FakeBatchRouteLCR,
        _FakeStage,
        _PausingBatchRouteLCR,
        _point,
        _read_csv_rows_if_exists,
    )
except ImportError:
    from measurement_test_support import (
        _FailingPreparedBatchRouteLCR,
        _FakeBatchRouteLCR,
        _FakeStage,
        _PausingBatchRouteLCR,
        _point,
        _read_csv_rows_if_exists,
    )


class RouteMeasurementContactSeekTest(unittest.TestCase):
    def test_auto_contact_seek_presses_deeper_without_lift_lower_retry(self) -> None:
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
        depth_index = stage.calls.index(("lower_to_depth", 0.001, 75.0))
        final_lift_index = stage.calls.index(
            ("needles", "lift", 75.0),
            depth_index + 1,
        )
        self.assertLess(first_lower_index, depth_index)
        self.assertLess(depth_index, final_lift_index)
        self.assertEqual(
            _calls_named(
                stage.calls[first_lower_index + 1 : final_lift_index],
                "needles",
            ),
            [],
        )
        self.assertEqual(_calls_named(stage.calls, "adjust"), [])

    def test_stop_during_auto_contact_seek_readout_writes_no_csv_row(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        runner_holder: dict[str, RouteMeasurementRunner] = {}

        def stop_on_second_batch(batch_number: int) -> None:
            if batch_number == 2:
                runner_holder["runner"].stop()

        lcr = _PausingBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (200000.0, 210000.0, 1000.0, 1001.0)
            ],
            stop_on_second_batch,
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
            runner_holder["runner"] = runner

            success, message = runner.run()

            self.assertFalse(success)
            self.assertEqual(message, "Route measurement stopped by user.")
            self.assertEqual(_read_csv_rows_if_exists(csv_path), [])

        depth_index = stage.calls.index(("lower_to_depth", 0.001, 75.0))
        final_lift_index = stage.calls.index(
            ("needles", "lift", 75.0),
            depth_index + 1,
        )
        self.assertLess(depth_index, final_lift_index)
        self.assertEqual(stage.calls[-1], ("finish",))

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
        self.assertEqual(stage.axis_a_lowering_mm, 1.001)
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

        self.assertEqual(lcr.batch_counts, [2, 2])
        self.assertEqual(stage.calls[-2:], [("needles", "lift", None), ("finish",)])

    def test_prepare_external_contact_uses_photo_prelude_then_places_contact(
        self,
    ) -> None:
        point = replace(_point(1), photo_stage_xy=(20.0, 30.0))
        stage = _FakeStage()
        records = []

        def focus(_point, _position, _total) -> dict[str, object]:
            stage.calls.append(("focus", _point.index))
            return {"focus_best_z_mm": 1.02}

        def capture(_point, _position, _total, focus_result) -> str:
            stage.calls.append(("focus_result", focus_result))
            stage.calls.append(("photo", _point.index))
            return "photo.png"

        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 1000.0},
                {"differential_resistance_ohm": 1001.0},
            ]
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
                photo_callback=capture,
                photo_focus_callback=focus,
                photo_record_callback=lambda record, _position, _total: records.append(
                    record
                ),
                photo_settle_s=0.0,
                contact_settle_s=0.0,
            )

            result = runner.prepare_external_contact(
                point,
                photo_enabled=True,
                photo_focus_enabled=True,
            )

            self.assertTrue(result.placement.success, result.placement.message)
            self.assertEqual(result.photo_path, "photo.png")
            self.assertEqual(result.focus, {"focus_best_z_mm": 1.02})

        photo_move_index = stage.calls.index(("move", 20.0, 30.0))
        focus_index = stage.calls.index(("focus", 1))
        photo_index = stage.calls.index(("photo", 1))
        contact_move_index = stage.calls.index(("move", 1.0, 11.0))
        lower_index = stage.calls.index(("needles", "lower", 75.0))
        self.assertLess(photo_move_index, focus_index)
        self.assertLess(focus_index, photo_index)
        self.assertLess(photo_index, contact_move_index)
        self.assertLess(contact_move_index, lower_index)
        self.assertEqual(
            _calls_named(stage.calls, "move"),
            [("move", 20.0, 30.0), ("move", 1.0, 11.0)],
        )
        self.assertNotIn(("needles", "lift", 75.0), stage.calls)
        self.assertEqual(records[0].stage_xy, (20.0, 30.0))
        self.assertEqual(lcr.batch_counts, [2])

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
        self.assertEqual(_calls_named(stage.calls, "needles"), [])
        self.assertEqual(stage.calls[-1], ("finish",))

    def test_check_contact_finishes_stage_task_when_prepare_fails(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FailingPreparedBatchRouteLCR(RuntimeError("prepare failed"))

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
                contact_settle_s=0.0,
            )

            with self.assertRaisesRegex(RuntimeError, "prepare failed"):
                runner.check_contact(point)

            self.assertFalse(csv_path.exists())

        self.assertEqual(lcr.prepare_calls, [(2, 2)])
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
        self.assertEqual(_calls_named(stage.calls, "needles"), [])
        self.assertEqual(
            _calls_named(stage.calls, "lower_to_depth"),
            [("lower_to_depth", 0.001, 75.0)],
        )
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
            self.assertEqual(lcr.batch_counts, [2, 2, 2, 2, 2])
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
            self.assertEqual(contact_heights[0].contact_seek.attempts, 2)
            self.assertEqual(
                contact_heights[0].contact_seek.initial_status,
                "bad_contact",
            )
            self.assertEqual(contact_heights[0].contact_seek.final_status, "good")

        self.assertEqual(
            _calls_named(stage.calls, "lower_to_depth"),
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
            _calls_named(stage.calls, "lower_to_depth"),
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
            self.assertEqual(lcr.batch_counts, [2, 2, 2])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "bad_contact")
            self.assertEqual(rows[0]["n_measurements"], "2")

        self.assertEqual(
            _calls_named(stage.calls, "lower_to_depth"),
            [
                ("lower_to_depth", 0.0005, None),
                ("lower_to_depth", 0.001, None),
            ],
        )
        self.assertEqual(_calls_named(stage.calls, "adjust"), [])

        first_lower_index = stage.calls.index(("needles", "lower", None))
        first_depth_index = stage.calls.index(("lower_to_depth", 0.0005, None))
        final_lift_index = stage.calls.index(
            ("needles", "lift", None),
            first_depth_index + 1,
        )
        self.assertEqual(
            _calls_named(
                stage.calls[first_lower_index + 1 : final_lift_index],
                "needles",
            ),
            [],
        )

    def test_auto_contact_seek_adjust_fallback_uses_incremental_steps(self) -> None:
        class _AdjustOnlyStage(_FakeStage):
            def __init__(self) -> None:
                super().__init__()
                self.run_external_needles_lower_to_depth_below_down = None

        point = _point(1)
        stage = _AdjustOnlyStage()
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
                auto_contact_seek_max_total_mm=0.002,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(lcr.batch_counts, [2, 2, 2])

        first_lower_index = stage.calls.index(("needles", "lower", None))
        final_lift_index = stage.calls.index(
            ("needles", "lift", None),
            first_lower_index + 1,
        )
        self.assertEqual(
            _calls_named(stage.calls, "adjust"),
            [("adjust", -0.001, None), ("adjust", -0.001, None)],
        )
        self.assertEqual(
            _calls_named(
                stage.calls[first_lower_index + 1 : final_lift_index],
                "needles",
            ),
            [],
        )

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

    def test_measure_after_exhausted_contact_seek_measures_current_point(self) -> None:
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
                    12000.0,
                    12010.0,
                    11990.0,
                    12005.0,
                )
            ]
        )
        results = []
        results_changed = threading.Condition()
        statuses: list[str] = []

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
                measurement_count=4,
                initial_measurement_count=2,
                short_threshold_ohm=10.0,
                confirm_each_point=True,
                auto_contact_seek_on_bad_contact=True,
                auto_contact_seek_step_mm=0.001,
                auto_contact_seek_max_total_mm=0.001,
                contact_settle_s=0.0,
                result_callback=on_result,
                status_callback=statuses.append,
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

            runner.submit_confirmation("measure")
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][0], True, result[0][1])
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual([row["structure_number"] for row in rows], ["1", "1"])
        self.assertEqual(rows[1]["n_measurements"], "4")
        self.assertTrue(
            any("point 1/1 measuring current contact" in item for item in statuses)
        )

    def test_exhausted_contact_seek_message_includes_failed_criterion(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        statuses: list[str] = []
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    100000.0,
                    500000.0,
                    120000.0,
                    520000.0,
                )
            ]
        )
        runner = RouteMeasurementRunner(
            points=[point],
            csv_path=Path(os.devnull),
            stage_controller=stage,
            lcr_controller=lcr,
            needle_feedrate=None,
            measurement_count=2,
            initial_measurement_count=2,
            auto_contact_seek_on_bad_contact=True,
            auto_contact_seek_step_mm=0.001,
            auto_contact_seek_max_total_mm=0.001,
            contact_settle_s=0.0,
            status_callback=statuses.append,
        )

        result = runner.seek_contact(point)

        self.assertFalse(result.success)
        self.assertIn("failed criterion:", result.message)
        self.assertIn("MAD sigma", result.message)
        self.assertIn("failed criterion:", statuses[-1])

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
            self.assertEqual(
                _calls_named(stage.calls, "lower_to_depth"),
                [("lower_to_depth", 0.001, 75.0)],
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


if __name__ == "__main__":
    unittest.main()
