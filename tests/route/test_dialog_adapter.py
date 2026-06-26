import types
import unittest
from pathlib import Path

from probe_station_gui.route.dialog_adapter import (
    RouteDialogRuntimeSink,
    current_route_measurement_configuration,
    request_route_measurement_for_point,
    restart_waiting_route_measurement,
    route_dialog_defaults,
    route_dialog_restore_plan,
    route_measurement_setup_changed,
    route_measurement_session_cancel_plan,
    route_measurement_session_start_plan,
)


class RouteDialogAdapterTest(unittest.TestCase):
    def test_runtime_sink_is_no_op_without_dialog(self) -> None:
        sink = RouteDialogRuntimeSink(lambda: None)

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

    def test_runtime_sink_forwards_runtime_updates_to_dialog(self) -> None:
        calls: list[tuple[object, ...]] = []
        dialog = types.SimpleNamespace(
            set_status=lambda message: calls.append(("status", message)),
            set_running=lambda active: calls.append(("running", active)),
            set_waiting=lambda waiting, reason="": calls.append(
                ("waiting", waiting, reason)
            ),
            reset_progress=lambda total: calls.append(("reset_progress", total)),
            finish_progress=lambda success: calls.append(("finish_progress", success)),
            set_progress=lambda position, total, point_number: calls.append(
                ("progress", position, total, point_number)
            ),
            set_result=lambda record, position, total, saved: calls.append(
                ("result", record, position, total, saved)
            ),
            set_current_point=lambda point_number, *, save=True: calls.append(
                ("current_point", point_number, save)
            ),
            set_measurement_session_active=lambda active, *, save=True: calls.append(
                ("session_active", active, save)
            ),
        )
        sink = RouteDialogRuntimeSink(lambda: dialog)

        self.assertTrue(sink.set_status("status"))
        self.assertTrue(sink.set_running(True))
        self.assertTrue(sink.set_waiting(True, reason="paused"))
        self.assertTrue(sink.reset_progress(5))
        self.assertTrue(sink.finish_progress(False))
        self.assertTrue(sink.set_progress(2, 5, 4))
        self.assertTrue(sink.set_result("record", 2, 5, True))
        self.assertTrue(sink.set_current_point(4, save=False))
        self.assertTrue(sink.set_measurement_session_active(False, save=False))

        self.assertEqual(
            calls,
            [
                ("status", "status"),
                ("running", True),
                ("waiting", True, "paused"),
                ("reset_progress", 5),
                ("finish_progress", False),
                ("progress", 2, 5, 4),
                ("result", "record", 2, 5, True),
                ("current_point", 4, False),
                ("session_active", False, False),
            ],
        )

    def test_runtime_sink_guards_optional_pending_methods(self) -> None:
        calls: list[tuple[object, ...]] = []
        dialog = types.SimpleNamespace(
            set_status=lambda message: calls.append(("status", message)),
        )
        sink = RouteDialogRuntimeSink(lambda: dialog)

        self.assertTrue(sink.set_pause_request_pending(True))
        self.assertTrue(sink.set_interrupt_request_pending(True))
        self.assertTrue(sink.set_status("kept"))

        self.assertEqual(calls, [("status", "kept")])

    def test_runtime_sink_helper_sequences_match_dialog_runtime_updates(self) -> None:
        calls: list[tuple[object, ...]] = []
        dialog = types.SimpleNamespace(
            set_status=lambda message: calls.append(("status", message)),
            set_running=lambda active: calls.append(("running", active)),
            set_waiting=lambda waiting, reason="": calls.append(
                ("waiting", waiting, reason)
            ),
            set_pause_request_pending=lambda pending: calls.append(
                ("pause_pending", pending)
            ),
            set_interrupt_request_pending=lambda pending: calls.append(
                ("interrupt_pending", pending)
            ),
            reset_progress=lambda total: calls.append(("reset_progress", total)),
            finish_progress=lambda success: calls.append(("finish_progress", success)),
        )
        sink = RouteDialogRuntimeSink(lambda: dialog)
        ui_state = types.SimpleNamespace(
            active=True,
            pause_pending=True,
            waiting=True,
            control_waiting_reason="pause requested",
        )

        sink.apply_api_control_update(ui_state, "api status")
        sink.route_runner_started("runner started", 8)
        sink.route_started("api started", 9, waiting=True, waiting_reason="paused")
        sink.pause_requested("pause requested")
        sink.stop_requested("Stopping route measurement.")
        sink.shift_status("shift saved", mark_interrupt_pending=True)
        sink.finished_ui(False, "finished")

        self.assertEqual(
            calls,
            [
                ("running", True),
                ("pause_pending", True),
                ("waiting", True, "pause requested"),
                ("status", "api status"),
                ("running", True),
                ("reset_progress", 8),
                ("status", "runner started"),
                ("running", True),
                ("waiting", True, "paused"),
                ("reset_progress", 9),
                ("status", "api started"),
                ("pause_pending", True),
                ("status", "pause requested"),
                ("waiting", False, ""),
                ("status", "Stopping route measurement."),
                ("interrupt_pending", True),
                ("status", "shift saved"),
                ("running", False),
                ("finish_progress", False),
                ("status", "finished"),
            ],
        )

    def test_route_dialog_defaults_use_route_path_siblings(self) -> None:
        route = types.SimpleNamespace(
            path=Path("C:/data/routes/chip-17.route.json"),
        )

        defaults = route_dialog_defaults(route, document=None)

        self.assertEqual(
            defaults.csv_path,
            "C:\\data\\routes\\chip-17.route-measurements.csv",
        )
        self.assertEqual(
            defaults.photo_dir,
            "C:\\data\\routes\\chip-17.route-photos",
        )

    def test_route_dialog_defaults_use_document_directory_when_route_path_missing(
        self,
    ) -> None:
        document = types.SimpleNamespace(
            path=Path("C:/designs/probe/chip.gds"),
        )

        defaults = route_dialog_defaults(route=types.SimpleNamespace(path=None), document=document)

        self.assertEqual(
            defaults.csv_path,
            "C:\\designs\\probe\\probe_route_measurements.csv",
        )
        self.assertEqual(
            defaults.photo_dir,
            "C:\\designs\\probe\\probe_route_photos",
        )

    def test_route_dialog_defaults_fall_back_to_plain_names(self) -> None:
        defaults = route_dialog_defaults(
            route=types.SimpleNamespace(path=None),
            document=None,
        )

        self.assertEqual(defaults.csv_path, "probe_route_measurements.csv")
        self.assertEqual(defaults.photo_dir, "probe_route_photos")

    def test_restore_plan_invalidates_stale_route_settings(self) -> None:
        route = types.SimpleNamespace(
            name="route-a",
            path=Path("C:/routes/route-a.json"),
            points=[object(), object()],
        )
        state = {
            "measurement_session_active": True,
            "current_point": 5,
            "session_route_name": "route-b",
            "session_route_point_count": 2,
        }

        plan = route_dialog_restore_plan(state, route)

        self.assertFalse(plan.session_active)
        self.assertEqual(plan.current_point, 5)
        self.assertFalse(plan.open_dialog)

    def test_session_start_plan_rejects_active_thread(self) -> None:
        plan = route_measurement_session_start_plan(
            thread_active=True,
            dialog_configuration=None,
            current_point=None,
        )

        self.assertFalse(plan.accepted)
        self.assertEqual(plan.status_message, "Route measurement is already active.")
        self.assertEqual(plan.status_timeout_ms, 4000)

    def test_session_start_plan_uses_dialog_point_before_saved_point(self) -> None:
        configuration = types.SimpleNamespace(current_point=7)

        plan = route_measurement_session_start_plan(
            thread_active=False,
            dialog_configuration=configuration,
            current_point=3,
        )

        self.assertTrue(plan.accepted)
        self.assertEqual(plan.point_number, 7)
        self.assertTrue(plan.session_active)
        self.assertTrue(plan.pending)
        self.assertEqual(plan.status_message, "Route point set to point 7.")
        self.assertEqual(plan.status_timeout_ms, 5000)

    def test_session_cancel_plan_rejects_active_thread(self) -> None:
        plan = route_measurement_session_cancel_plan(thread_active=True)

        self.assertFalse(plan.accepted)
        self.assertEqual(
            plan.status_message,
            "Stop route measurement before canceling the session.",
        )
        self.assertEqual(plan.status_timeout_ms, 5000)

    def test_session_cancel_plan_resets_state_when_idle(self) -> None:
        plan = route_measurement_session_cancel_plan(thread_active=False)

        self.assertTrue(plan.accepted)
        self.assertFalse(plan.session_active)
        self.assertFalse(plan.pending)
        self.assertEqual(plan.point_number, 1)
        self.assertEqual(plan.status_message, "Route measurement session cancelled.")
        self.assertEqual(plan.status_timeout_ms, 5000)

    def test_current_route_measurement_configuration_prefers_runtime(self) -> None:
        fallback = types.SimpleNamespace(csv_path="fallback.csv")
        runtime = types.SimpleNamespace(csv_path="runtime.csv")

        active = current_route_measurement_configuration(runtime, fallback)

        self.assertIs(active, runtime)

    def test_route_measurement_setup_changed_detects_runtime_relevant_changes(self) -> None:
        previous = types.SimpleNamespace(
            operation_mode="measure",
            previous_ok_only=False,
            previous_csv_path="old.csv",
        )
        current = types.SimpleNamespace(
            operation_mode="photo",
            previous_ok_only=False,
            previous_csv_path="old.csv",
        )

        changed = route_measurement_setup_changed(previous, current)

        self.assertTrue(changed)

    def test_request_route_measurement_for_point_opens_dialog_when_idle(self) -> None:
        calls: list[object] = []
        dialog = types.SimpleNamespace(
            set_current_point=lambda point: calls.append(("point", int(point))),
            current_configuration=lambda: "config",
        )

        request_route_measurement_for_point(
            point_number=9,
            thread_active=False,
            waiting=False,
            open_dialog=lambda *, start_context: calls.append(("open", start_context)),
            current_dialog=lambda: dialog,
            submit_confirmation=lambda _action: None,
            request_point_correction=lambda **_kwargs: None,
            start_measurement=lambda config: calls.append(("start", config)),
            set_pending_point=lambda _point: None,
            clear_pending_point=lambda: None,
        )

        self.assertEqual(
            calls,
            [("open", False), ("point", 9), ("start", "config")],
        )

    def test_request_route_measurement_for_point_jumps_when_waiting(self) -> None:
        calls: list[object] = []

        request_route_measurement_for_point(
            point_number=11,
            thread_active=True,
            waiting=True,
            open_dialog=lambda *, start_context: calls.append(("open", start_context)),
            current_dialog=lambda: None,
            submit_confirmation=lambda action: calls.append(("jump", action)),
            request_point_correction=lambda **_kwargs: calls.append(("interrupt", _kwargs)),
            start_measurement=lambda config: calls.append(("start", config)),
            set_pending_point=lambda point: calls.append(("pending", point)),
            clear_pending_point=lambda: calls.append(("clear",)),
        )

        self.assertEqual(calls, [("clear",), ("jump", "jump:11")])

    def test_restart_waiting_route_measurement_reports_stop_timeout(self) -> None:
        old_runner = types.SimpleNamespace(stop=lambda: None)
        old_thread = types.SimpleNamespace(
            is_alive=lambda: True,
            join=lambda timeout=None: None,
        )
        statuses: list[tuple[str, int]] = []

        restarted = restart_waiting_route_measurement(
            configuration="config",
            route_offset_xy=(0.5, -0.25),
            old_runner=old_runner,
            old_thread=old_thread,
            clear_waiting_state=lambda: None,
            start_measurement=lambda config, *, wait_before_first_point: None,
            current_runner=lambda: None,
            current_thread=lambda: old_thread,
            show_status=lambda message, timeout_ms: statuses.append(
                (str(message), int(timeout_ms))
            ),
        )

        self.assertFalse(restarted)
        self.assertEqual(
            statuses,
            [("Waiting route measurement did not stop.", 8000)],
        )


if __name__ == "__main__":
    unittest.main()
