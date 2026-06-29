import csv
import math
import tempfile
import threading
import unittest
from pathlib import Path

from probe_station_gui.route.measurement import (
    RouteContactQualityLimits,
    RouteMeasurementPoint,
    RouteMeasurementRunner,
    RouteMeasurementSample,
    summarize_route_contact_quality,
)


def _events_named(events: list[object], name: str) -> list[object]:
    return [event for event in events if event[0] == name]

try:
    from .measurement_test_support import (
        _EventStage,
        _FakeBatchRouteLCR,
        _FakeLCR,
        _FakeRouteLCR,
        _FakeStage,
        _OutputTrackingBatchRouteLCR,
        _PrepareAwareStage,
        _PreparedBatchRouteLCR,
        _point,
        _read_csv_rows_if_exists,
    )
except ImportError:
    from measurement_test_support import (
        _EventStage,
        _FakeBatchRouteLCR,
        _FakeLCR,
        _FakeRouteLCR,
        _FakeStage,
        _OutputTrackingBatchRouteLCR,
        _PrepareAwareStage,
        _PreparedBatchRouteLCR,
        _point,
        _read_csv_rows_if_exists,
    )


class RouteMeasurementReadoutQualityTest(unittest.TestCase):
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

    def test_runner_keeps_meter_output_enabled_until_final_lift(self) -> None:
        events: list[object] = []
        stage = _EventStage(events)
        lcr = _OutputTrackingBatchRouteLCR(
            [
                {"differential_resistance_ohm": 10.0},
                {"differential_resistance_ohm": 12.0},
            ],
            events,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = RouteMeasurementRunner(
                points=[_point(1)],
                csv_path=Path(tmpdir) / "route.csv",
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=75.0,
                measurement_count=2,
                initial_measurement_count=2,
                contact_settle_s=0.0,
            )

            success, message = runner.run()

        self.assertTrue(success, message)
        output_on_index = events.index(("output", True))
        lower_index = events.index(("needles", "lower"))
        lift_index = len(events) - 1 - list(reversed(events)).index(("needles", "lift"))
        output_off_index = (
            len(events) - 1 - list(reversed(events)).index(("output", False))
        )
        self.assertLess(output_on_index, lower_index)
        self.assertGreater(output_off_index, lift_index)
        self.assertEqual(
            _events_named(events, "output"),
            [("output", True), ("output", False)],
        )
        self.assertEqual(_events_named(events, "read"), [("read", 2, True)])

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

    def test_confirm_rejected_measure_reads_current_contact_and_saves_record(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (
                    520000.0,
                    610000.0,
                    480000.0,
                    570000.0,
                    1000.0,
                    1001.0,
                    1002.0,
                    1001.0,
                )
            ]
        )
        results = []
        records = []
        result_changed = threading.Condition()

        def on_result(record, _position, _total, saved) -> None:
            with result_changed:
                results.append((record, saved))
                result_changed.notify_all()

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            runner = RouteMeasurementRunner(
                points=[point],
                csv_path=csv_path,
                stage_controller=stage,
                lcr_controller=lcr,
                needle_feedrate=None,
                measurement_count=4,
                confirm_each_point=True,
                contact_settle_s=0.0,
                result_callback=on_result,
                record_callback=lambda record, _position, _total: records.append(record),
            )
            finished: list[tuple[bool, str]] = []
            thread = threading.Thread(
                target=lambda: finished.append(runner.run()),
                daemon=True,
            )

            thread.start()
            with result_changed:
                self.assertTrue(
                    result_changed.wait_for(
                        lambda: len(results) == 1,
                        timeout=2.0,
                    )
                )
            self.assertEqual(results[0][0].status, "bad_contact")
            self.assertFalse(results[0][1])
            calls_before_measure = list(stage.calls)

            self.assertTrue(runner.submit_confirmation("measure"))
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual(finished[0][0], True, finished[0][1])
            self.assertEqual(lcr.batch_counts, [4, 4])
            self.assertEqual(len(results), 2)
            self.assertTrue(results[1][1])
            self.assertEqual(results[1][0].status, "ok")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, "ok")
            self.assertEqual(stage.calls, calls_before_measure)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "ok")

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
            self.assertEqual(_read_csv_rows_if_exists(csv_path), [])

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
            self.assertEqual(_read_csv_rows_if_exists(csv_path), [])

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

    def test_contact_quality_allows_stable_relative_spread(self) -> None:
        point = _point(1)
        lcr = _FakeBatchRouteLCR(
            [
                {
                    "differential_resistance_ohm": value,
                    "negative": {"current_a": -0.001},
                    "positive": {"current_a": 0.001},
                }
                for value in (
                    29400.0,
                    29500.0,
                    29600.0,
                    29700.0,
                    29800.0,
                    30000.0,
                    30200.0,
                    30300.0,
                    30400.0,
                    30500.0,
                    30600.0,
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
                measurement_count=11,
                initial_measurement_count=11,
                contact_settle_s=0.0,
                record_callback=lambda record, _position, _total: records.append(record),
            )

            success, message = runner.run()

            self.assertTrue(success, message)
            self.assertEqual(records[0].status, "ok")
            self.assertIsNotNone(records[0].contact_quality)
            self.assertTrue(records[0].contact_quality.good)
            self.assertGreater(records[0].contact_quality.mad_sigma_ohm, 300.0)

    def test_contact_quality_thresholds_are_configurable(self) -> None:
        samples = [
            RouteMeasurementSample(index, value)
            for index, value in enumerate(
                (18_600.0, 19_800.0, 18_900.0, 20_100.0),
                start=1,
            )
        ]

        default_quality = summarize_route_contact_quality(samples)
        relaxed_quality = summarize_route_contact_quality(
            samples,
            contact_quality_limits=RouteContactQualityLimits(
                max_mad_sigma_ohm=2_000.0,
                max_p95_abs_step_ohm=3_000.0,
                max_relative_mad_sigma=0.20,
                max_relative_p95_abs_step=0.25,
            ),
        )

        self.assertFalse(default_quality.good)
        self.assertIn("mad_sigma_too_high", default_quality.reasons)
        self.assertTrue(default_quality.failure_criteria)
        self.assertTrue(relaxed_quality.good)
        self.assertEqual(relaxed_quality.reasons, ())

    def test_contact_quality_treats_polarity_mismatch_as_diagnostic(self) -> None:
        point = _point(1)
        lcr = _FakeBatchRouteLCR(
            [
                {
                    "differential_resistance_ohm": value,
                    "negative": {"current_a": -2.0e-6},
                    "positive": {"current_a": -5.0e-8},
                }
                for value in (30000.0, 30100.0, 29950.0, 30050.0)
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
            self.assertEqual(records[0].status, "ok")
            self.assertIsNotNone(records[0].contact_quality)
            self.assertTrue(records[0].contact_quality.good)
            self.assertEqual(records[0].contact_quality.polarity_sign_mismatch_count, 4)
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["contact_polarity_sign_mismatches"], "4")

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


if __name__ == "__main__":
    unittest.main()
