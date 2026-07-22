import threading
import time
import unittest

from probe_station_gui.route.measurement import RouteExternalMeasurementSessionRunner
from probe_station_gui.route.session_start import snapshot_route_design_frame

try:
    from .measurement_test_support import (
        _EventStage,
        _FakeBatchRouteLCR,
        _FakeStage,
        _OutputTrackingBatchRouteLCR,
        _point,
    )
except ImportError:
    from measurement_test_support import (
        _EventStage,
        _FakeBatchRouteLCR,
        _FakeStage,
        _OutputTrackingBatchRouteLCR,
        _point,
    )


class RouteApiControlTest(unittest.TestCase):
    def test_external_session_waits_for_api_result_without_csv(self) -> None:
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
            design_frame_snapshot=snapshot_route_design_frame(
                frame_id="design-a",
                frame_version=4,
            ),
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
        self.assertEqual(
            final_status["design_frame"],
            {"frame_id": "design-a", "frame_version": 4},
        )
        self.assertEqual(
            final_status["history"][0]["design_frame"],
            {"frame_id": "design-a", "frame_version": 4},
        )
        self.assertEqual(final_status["history"][0]["external_result"]["summary"], {"iv_points": 31})

    def test_external_session_uses_contact_runner_output_lifecycle(self) -> None:
        events: list[object] = []
        point = _point(1)
        stage = _EventStage(events)
        lcr = _OutputTrackingBatchRouteLCR(
            [
                {"differential_resistance_ohm": 50.0},
                {"differential_resistance_ohm": 60.0},
            ],
            events,
        )
        finished: list[tuple[bool, str]] = []
        runner = RouteExternalMeasurementSessionRunner(
            session_id="session-1",
            points=[point],
            stage_controller=stage,
            lcr_controller=lcr,
            needle_feedrate=75.0,
            measurement_count=2,
            initial_measurement_count=2,
            contact_settle_s=0.0,
            photo_enabled=False,
            photo_focus_enabled=False,
        )
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        self.assertTrue(self._wait_for_state(runner, "waiting_external_measurement"))
        self.assertTrue(runner.submit_external_result({"status": "ok"}))
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])
        output_on_index = events.index(("output", True))
        lower_index = events.index(("needles", "lower"))
        reversed_events = list(reversed(events))
        lift_index = len(events) - 1 - reversed_events.index(
            ("needles", "lift")
        )
        output_off_index = len(events) - 1 - reversed_events.index(
            ("output", False)
        )
        self.assertLess(output_on_index, lower_index)
        self.assertLess(output_on_index, output_off_index)
        self.assertGreater(output_off_index, lift_index)
        self.assertEqual(events.count(("output", True)), 1)
        self.assertEqual(events.count(("output", False)), 1)

    def test_pause_during_contact_acks_only_after_lift_and_keeps_interrupt_path(
        self,
    ) -> None:
        point = _point(1)
        runner_holder: dict[str, RouteExternalMeasurementSessionRunner] = {}
        references: list[object] = []

        class _PauseOnFirstLowerStage(_FakeStage):
            def __init__(self) -> None:
                super().__init__()
                self.pause_requested = False

            def run_external_needles_action(
                self,
                action: str,
                feedrate: float | None = None,
            ) -> str:
                result = super().run_external_needles_action(action, feedrate)
                if action == "lower" and not self.pause_requested:
                    self.pause_requested = True
                    runner_holder["runner"].request_pause_after_current_point()
                return result

        stage = _PauseOnFirstLowerStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 100.0 + index}
                for index in range(10)
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
            post_success_contact=references.append,
        )
        runner_holder["runner"] = runner
        finished: list[tuple[bool, str]] = []
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        self.assertTrue(self._wait_for_external_request(runner, 1))
        pending_status = runner.status_payload()
        self.assertEqual(pending_status["state"], "waiting_external_measurement")
        self.assertNotEqual(pending_status["waiting_reason"], "paused")
        self.assertEqual(len(references), 1)

        runner.request_current_point_correction()
        self.assertTrue(self._wait_for_external_request(runner, 2))
        self.assertEqual(
            stage.calls.count(("needles", "lower", 75.0)),
            2,
        )
        self.assertEqual(runner.status_payload()["history"], [])

        self.assertTrue(runner.submit_external_result({"status": "ok"}))
        self.assertTrue(self._wait_for_state(runner, "waiting_paused"))
        paused_status = runner.status_payload()
        self.assertEqual(paused_status["waiting_reason"], "paused")
        self.assertGreaterEqual(
            stage.calls.count(("needles", "lift", 75.0)),
            2,
        )

        self.assertTrue(runner.submit_confirmation("next"))
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])

    def test_pending_pause_plus_interrupt_suppresses_contact_reference(self) -> None:
        point = _point(1)
        runner_holder: dict[str, RouteExternalMeasurementSessionRunner] = {}
        references: list[object] = []

        class _PauseAndInterruptOnLowerStage(_FakeStage):
            def run_external_needles_action(
                self,
                action: str,
                feedrate: float | None = None,
            ) -> str:
                result = super().run_external_needles_action(action, feedrate)
                if action == "lower":
                    runner_holder["runner"].request_pause_after_current_point()
                    runner_holder["runner"].request_current_point_correction()
                return result

        runner = RouteExternalMeasurementSessionRunner(
            session_id="session-pause-interrupt",
            points=[point],
            stage_controller=_PauseAndInterruptOnLowerStage(),
            lcr_controller=_FakeBatchRouteLCR(
                [{"differential_resistance_ohm": 100.0} for _ in range(10)]
            ),
            needle_feedrate=75.0,
            measurement_count=5,
            initial_measurement_count=2,
            contact_settle_s=0.0,
            photo_enabled=False,
            photo_focus_enabled=False,
            post_success_contact=references.append,
        )
        runner_holder["runner"] = runner
        thread = threading.Thread(target=runner.run, daemon=True)

        thread.start()
        self.assertTrue(self._wait_for_state(runner, "waiting_interrupted"))

        self.assertEqual(references, [])
        runner.stop()
        thread.join(timeout=2.0)

    def test_stop_during_contact_suppresses_contact_reference(self) -> None:
        point = _point(1)
        runner_holder: dict[str, RouteExternalMeasurementSessionRunner] = {}
        references: list[object] = []

        class _StopOnLowerStage(_FakeStage):
            def run_external_needles_action(
                self,
                action: str,
                feedrate: float | None = None,
            ) -> str:
                result = super().run_external_needles_action(action, feedrate)
                if action == "lower":
                    runner_holder["runner"].stop()
                return result

        runner = RouteExternalMeasurementSessionRunner(
            session_id="session-stop",
            points=[point],
            stage_controller=_StopOnLowerStage(),
            lcr_controller=_FakeBatchRouteLCR(
                [{"differential_resistance_ohm": 100.0} for _ in range(10)]
            ),
            needle_feedrate=75.0,
            measurement_count=5,
            initial_measurement_count=2,
            contact_settle_s=0.0,
            photo_enabled=False,
            photo_focus_enabled=False,
            post_success_contact=references.append,
        )
        runner_holder["runner"] = runner

        success, _message = runner.run()

        self.assertFalse(success)
        self.assertEqual(references, [])

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

    def test_external_session_measure_waits_for_api_result(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (520000.0, 610000.0, 480000.0, 570000.0)
            ]
        )
        runner = RouteExternalMeasurementSessionRunner(
            session_id="session-1",
            points=[point],
            stage_controller=stage,
            lcr_controller=lcr,
            needle_feedrate=75.0,
            measurement_count=4,
            initial_measurement_count=4,
            auto_contact_seek_max_total_mm=0.0,
            contact_settle_s=0.0,
            photo_enabled=False,
            photo_focus_enabled=False,
        )
        finished: list[tuple[bool, str]] = []
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        self.assertTrue(self._wait_for_state(runner, "waiting_contact"))
        status = runner.status_payload()
        self.assertEqual(status["last_preparation"]["measurement"]["status"], "bad_contact")
        calls_before_measure = list(stage.calls)
        self.assertTrue(runner.submit_confirmation("measure"))
        self.assertTrue(self._wait_for_state(runner, "waiting_external_measurement"))
        self.assertEqual(stage.calls, calls_before_measure)
        self.assertTrue(
            runner.submit_external_result(
                {
                    "status": "ok",
                    "summary": {"manual_contact": True},
                }
            )
        )
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])
        final_status = runner.status_payload()
        self.assertEqual(final_status["history"][0]["status"], "ok")
        self.assertEqual(
            final_status["history"][0]["external_result"]["summary"],
            {"manual_contact": True},
        )
        self.assertIn(("needles", "lift", 75.0), stage.calls)

    def test_external_session_measure_failed_result_waits_on_same_point(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": value}
                for value in (520000.0, 610000.0, 480000.0, 570000.0)
            ]
        )
        runner = RouteExternalMeasurementSessionRunner(
            session_id="session-1",
            points=[point],
            stage_controller=stage,
            lcr_controller=lcr,
            needle_feedrate=75.0,
            measurement_count=4,
            initial_measurement_count=4,
            auto_contact_seek_max_total_mm=0.0,
            contact_settle_s=0.0,
            photo_enabled=False,
            photo_focus_enabled=False,
        )
        finished: list[tuple[bool, str]] = []
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        self.assertTrue(self._wait_for_state(runner, "waiting_contact"))
        self.assertTrue(runner.submit_confirmation("measure"))
        self.assertTrue(self._wait_for_state(runner, "waiting_external_measurement"))
        first_status = runner.status_payload()
        first_request_id = int(first_status["external_measurement_request_id"])
        lift_count_before_failed_result = stage.calls.count(
            ("needles", "lift", 75.0)
        )
        self.assertTrue(
            runner.submit_external_result(
                {
                    "status": "failed",
                    "message": "source compliance error",
                    "external_measurement_request_id": first_request_id,
                }
            )
        )
        self.assertTrue(
            self._wait_for_waiting_reason(
                runner,
                "external_measurement_failed",
            )
        )
        failed_status = runner.status_payload()

        self.assertTrue(thread.is_alive())
        self.assertEqual(failed_status["waiting_reason"], "external_measurement_failed")
        self.assertEqual(failed_status["position"], 1)
        self.assertEqual(failed_status["last_external_result"]["status"], "failed")
        self.assertEqual(failed_status["history"], [])
        self.assertEqual(
            stage.calls.count(("needles", "lift", 75.0)),
            lift_count_before_failed_result,
        )
        retry_request_id = int(failed_status["external_measurement_request_id"])
        self.assertGreater(retry_request_id, first_request_id)

        self.assertTrue(
            runner.submit_external_result(
                {
                    "status": "ok",
                    "summary": {"retry": True},
                    "external_measurement_request_id": retry_request_id,
                }
            )
        )
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])
        final_status = runner.status_payload()
        self.assertEqual(final_status["history"][0]["status"], "ok")
        self.assertEqual(
            final_status["history"][0]["external_result"]["summary"],
            {"retry": True},
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

    def test_external_session_starts_waiting_before_first_point(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 100.0 + index}
                for index in range(5)
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
            wait_before_first_point=True,
        )
        finished: list[tuple[bool, str]] = []
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        self.assertTrue(self._wait_for_state(runner, "waiting_paused"))
        self.assertTrue(runner.wait_until_initial_pause(timeout_s=0.1))
        self.assertEqual(stage.calls, [])
        self.assertEqual(lcr.batch_counts, [])
        selected, message = runner.set_current_adjustment_point(1)
        self.assertTrue(selected, message)
        saved, message = runner.save_current_position_adjustment((1.25, 11.75))
        self.assertTrue(saved, message)

        self.assertTrue(runner.submit_confirmation("next"))
        self.assertTrue(
            self._wait_for_state(runner, "waiting_external_measurement")
        )
        self.assertTrue(runner.submit_external_result({"status": "ok"}))
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])
        self.assertEqual(lcr.batch_counts, [2, 3])

    def test_external_session_interrupt_during_focus_does_not_lower_needles(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 100.0 + index}
                for index in range(5)
            ]
        )
        runner_holder: dict[str, RouteExternalMeasurementSessionRunner] = {}

        def focus(_point, _position, _total) -> str:
            runner_holder["runner"].request_current_point_correction()
            return "focus interrupted"

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
            photo_focus_enabled=True,
            photo_focus_callback=focus,
        )
        runner_holder["runner"] = runner
        finished: list[tuple[bool, str]] = []
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        self.assertTrue(self._wait_for_state(runner, "waiting_interrupted"))
        self.assertNotIn(("needles", "lower", 75.0), stage.calls)
        self.assertEqual(lcr.batch_counts, [])

        self.assertTrue(runner.submit_confirmation("skip"))
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])

    def test_external_session_resume_after_interrupted_focus_retries_point(self) -> None:
        point = _point(1)
        stage = _FakeStage()
        lcr = _FakeBatchRouteLCR(
            [
                {"differential_resistance_ohm": 100.0 + index}
                for index in range(5)
            ]
        )
        runner_holder: dict[str, RouteExternalMeasurementSessionRunner] = {}
        focus_calls: list[int] = []

        def focus(_point, position, _total) -> str:
            focus_calls.append(int(position))
            if len(focus_calls) == 1:
                runner_holder["runner"].request_current_point_correction()
                return "focus interrupted"
            return "focus ok"

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
            photo_focus_enabled=True,
            photo_focus_callback=focus,
        )
        runner_holder["runner"] = runner
        finished: list[tuple[bool, str]] = []
        thread = threading.Thread(
            target=lambda: finished.append(runner.run()),
            daemon=True,
        )

        thread.start()
        self.assertTrue(self._wait_for_state(runner, "waiting_interrupted"))
        self.assertNotIn(("needles", "lower", 75.0), stage.calls)
        self.assertEqual(lcr.batch_counts, [])

        self.assertTrue(runner.submit_confirmation("next"))
        self.assertTrue(
            self._wait_for_state(runner, "waiting_external_measurement")
        )
        self.assertEqual(focus_calls, [1, 1])
        self.assertIn(("needles", "lower", 75.0), stage.calls)
        self.assertEqual(lcr.batch_counts, [2, 3])

        self.assertTrue(runner.submit_external_result({"status": "ok"}))
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(finished, [(True, "Route API session complete.")])

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

    @staticmethod
    def _wait_for_external_request(
        runner: RouteExternalMeasurementSessionRunner,
        request_id: int,
        *,
        timeout_s: float = 2.0,
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = runner.status_payload()
            if (
                status.get("state") == "waiting_external_measurement"
                and status.get("external_measurement_request_id") == request_id
            ):
                return True
            time.sleep(0.01)
        return False

    @staticmethod
    def _wait_for_waiting_reason(
        runner: RouteExternalMeasurementSessionRunner,
        reason: str,
        *,
        timeout_s: float = 2.0,
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if runner.status_payload().get("waiting_reason") == reason:
                return True
            time.sleep(0.01)
        return False


if __name__ == "__main__":
    unittest.main()
