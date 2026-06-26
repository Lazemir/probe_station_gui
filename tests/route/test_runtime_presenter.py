import types
import unittest

from probe_station_gui.route.runtime_presenter import RouteRuntimePresentationSink


class RouteRuntimePresenterTest(unittest.TestCase):
    def test_sink_is_no_op_without_surfaces(self) -> None:
        sink = RouteRuntimePresentationSink(
            current_dialog=lambda: None,
            current_navigator=lambda: None,
        )

        self.assertFalse(sink.set_status("status"))
        self.assertFalse(sink.set_running(True))
        self.assertFalse(sink.set_waiting(True, reason="paused"))
        self.assertFalse(sink.set_pause_request_pending(True))
        self.assertFalse(sink.set_interrupt_request_pending(True))
        self.assertFalse(sink.reset_progress(4))
        self.assertFalse(sink.finish_progress(True))
        self.assertFalse(sink.set_progress(1, 4, 2))
        self.assertFalse(sink.set_result("record", 1, 4, False))
        self.assertFalse(sink.set_current_point(3, save=False))
        self.assertFalse(sink.set_measurement_session_active(True, save=False))

    def test_sink_forwards_runtime_updates_to_dialog_and_navigator(self) -> None:
        calls: list[tuple[object, ...]] = []
        dialog = types.SimpleNamespace(
            set_status=lambda message: calls.append(("dialog_status", message)),
            set_running=lambda active: calls.append(("dialog_running", active)),
            set_waiting=lambda waiting, reason="": calls.append(
                ("dialog_waiting", waiting, reason)
            ),
            reset_progress=lambda total: calls.append(("dialog_progress_reset", total)),
            finish_progress=lambda success: calls.append(
                ("dialog_progress_finish", success)
            ),
            set_progress=lambda position, total, point_number: calls.append(
                ("dialog_progress", position, total, point_number)
            ),
            set_result=lambda record, position, total, saved: calls.append(
                ("dialog_result", record, position, total, saved)
            ),
            set_current_point=lambda point_number, *, save=True: calls.append(
                ("dialog_point", point_number, save)
            ),
            set_measurement_session_active=lambda active, *, save=True: calls.append(
                ("dialog_session", active, save)
            ),
        )
        navigator = types.SimpleNamespace(
            set_route_measurement_status=lambda message: calls.append(
                ("panel_status", message)
            ),
            set_route_measurement_running=lambda active: calls.append(
                ("panel_running", active)
            ),
            set_route_measurement_waiting=lambda waiting, reason="": calls.append(
                ("panel_waiting", waiting, reason)
            ),
        )
        sink = RouteRuntimePresentationSink(
            current_dialog=lambda: dialog,
            current_navigator=lambda: navigator,
        )

        self.assertTrue(sink.set_status("status"))
        self.assertTrue(sink.set_running(True))
        self.assertTrue(sink.set_waiting(True, reason="paused"))
        self.assertTrue(sink.reset_progress(5))
        self.assertTrue(sink.finish_progress(False))
        self.assertTrue(sink.set_progress(2, 5, 4))
        self.assertTrue(sink.set_result("record", 2, 5, True))
        self.assertTrue(sink.set_current_point(4, save=False))
        self.assertTrue(sink.set_measurement_session_active(False, save=False))

        self.assertIn(("dialog_status", "status"), calls)
        self.assertIn(("panel_status", "status"), calls)
        self.assertIn(("dialog_running", True), calls)
        self.assertIn(("panel_running", True), calls)
        self.assertIn(("dialog_waiting", True, "paused"), calls)
        self.assertIn(("panel_waiting", True, "paused"), calls)
        self.assertIn(("dialog_progress_reset", 5), calls)
        self.assertIn(("dialog_progress_finish", False), calls)
        self.assertIn(("dialog_progress", 2, 5, 4), calls)
        self.assertIn(("dialog_result", "record", 2, 5, True), calls)
        self.assertIn(("dialog_point", 4, False), calls)
        self.assertIn(("dialog_session", False, False), calls)

    def test_sink_guards_optional_pending_methods(self) -> None:
        calls: list[tuple[object, ...]] = []
        sink = RouteRuntimePresentationSink(
            current_dialog=lambda: types.SimpleNamespace(
                set_status=lambda message: calls.append(("dialog_status", message))
            ),
            current_navigator=lambda: types.SimpleNamespace(
                set_route_measurement_status=lambda message: calls.append(
                    ("panel_status", message)
                )
            ),
        )

        self.assertTrue(sink.set_pause_request_pending(True))
        self.assertTrue(sink.set_interrupt_request_pending(True))
        self.assertTrue(sink.set_status("kept"))

        self.assertEqual(
            calls,
            [("panel_status", "kept"), ("dialog_status", "kept")],
        )

    def test_helper_sequences_match_combined_runtime_updates(self) -> None:
        calls: list[tuple[object, ...]] = []
        dialog = types.SimpleNamespace(
            set_status=lambda message: calls.append(("dialog_status", message)),
            set_running=lambda active: calls.append(("dialog_running", active)),
            set_waiting=lambda waiting, reason="": calls.append(
                ("dialog_waiting", waiting, reason)
            ),
            set_pause_request_pending=lambda pending: calls.append(
                ("dialog_pause_pending", pending)
            ),
            set_interrupt_request_pending=lambda pending: calls.append(
                ("dialog_interrupt_pending", pending)
            ),
            reset_progress=lambda total: calls.append(("dialog_reset_progress", total)),
            finish_progress=lambda success: calls.append(
                ("dialog_finish_progress", success)
            ),
        )
        navigator = types.SimpleNamespace(
            set_route_measurement_status=lambda message: calls.append(
                ("panel_status", message)
            ),
            set_route_measurement_running=lambda active: calls.append(
                ("panel_running", active)
            ),
            set_route_measurement_waiting=lambda waiting, reason="": calls.append(
                ("panel_waiting", waiting, reason)
            ),
            set_route_measurement_pause_request_pending=lambda pending: calls.append(
                ("panel_pause_pending", pending)
            ),
            set_route_measurement_interrupt_request_pending=lambda pending: calls.append(
                ("panel_interrupt_pending", pending)
            ),
        )
        sink = RouteRuntimePresentationSink(
            current_dialog=lambda: dialog,
            current_navigator=lambda: navigator,
        )
        ui_state = types.SimpleNamespace(
            active=True,
            pause_pending=True,
            waiting=True,
            control_waiting_reason="pause requested",
        )

        sink.apply_api_control_update(ui_state, "api status")
        sink.route_runner_started("runner started", 8)
        sink.route_started("api started", 9, waiting=True, waiting_reason="paused")
        sink.stop_requested("Stopping route measurement.")
        sink.clear_waiting()
        sink.pause_requested("pause requested")
        sink.shift_status("shift saved", mark_interrupt_pending=True)
        sink.unsaved_result_status("unsaved result")
        sink.recorded_result_status("recorded result")
        sink.finished_ui(False, "finished")

        self.assertEqual(
            calls,
            [
                ("panel_running", True),
                ("dialog_running", True),
                ("panel_pause_pending", True),
                ("dialog_pause_pending", True),
                ("panel_waiting", True, "pause requested"),
                ("dialog_waiting", True, "pause requested"),
                ("panel_status", "api status"),
                ("dialog_status", "api status"),
                ("panel_running", True),
                ("panel_waiting", False, ""),
                ("panel_status", "runner started"),
                ("dialog_running", True),
                ("dialog_reset_progress", 8),
                ("dialog_status", "runner started"),
                ("panel_running", True),
                ("panel_waiting", True, "paused"),
                ("panel_status", "api started"),
                ("dialog_running", True),
                ("dialog_waiting", True, "paused"),
                ("dialog_reset_progress", 9),
                ("dialog_status", "api started"),
                ("panel_waiting", False, ""),
                ("dialog_waiting", False, ""),
                ("panel_status", "Stopping route measurement."),
                ("dialog_status", "Stopping route measurement."),
                ("panel_waiting", False, ""),
                ("dialog_waiting", False, ""),
                ("panel_pause_pending", True),
                ("dialog_pause_pending", True),
                ("panel_status", "pause requested"),
                ("dialog_status", "pause requested"),
                ("panel_interrupt_pending", True),
                ("dialog_interrupt_pending", True),
                ("panel_status", "shift saved"),
                ("dialog_status", "shift saved"),
                ("panel_status", "unsaved result"),
                ("dialog_status", "unsaved result"),
                ("panel_status", "recorded result"),
                ("dialog_status", "recorded result"),
                ("panel_running", False),
                ("panel_waiting", False, ""),
                ("panel_status", "finished"),
                ("dialog_running", False),
                ("dialog_finish_progress", False),
                ("dialog_status", "finished"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
