import threading
import types
import unittest

from tests.app.main_coordinate_feedrate_support import (
    Main,
    RouteMeasurementDialog,
    RouteMeasurementPoint,
    StageControllerError,
    _FakeAliveThread,
    _FakeButton,
    _FakeJoinableThread,
    _FakeRouteMeasurementRunner,
    _FakeVisibleDialog,
    _make_main,
    api_route_adjusted_stage_xy,
    main_module,
)


class MainRouteControlTest(unittest.TestCase):
    def test_gui_serialized_stage_command_runs_hardware_dispatch_in_worker(self) -> None:
        window = Main.__new__(Main)
        dispatch_threads: list[int] = []
        dispatch_started = threading.Event()
        release_dispatch = threading.Event()
        gui_thread_id = threading.get_ident()
        window._probe_route_api_window_guard = lambda _action, _payload: None

        def dispatch(_request, *, apply_route_control_guard):
            dispatch_threads.append(threading.get_ident())
            dispatch_started.set()
            self.assertTrue(release_dispatch.wait(timeout=1.0))
            return {"accepted": True, "status_code": 200}

        window._dispatch_api_command_request = dispatch
        response = Main._submit_api_command_request(
            window,
            {
                "action": "stage_local_focus",
                "payload": {"range_mm": 0.03},
            },
        )

        self.assertIsInstance(response, main_module.DeferredApiResponse)
        self.assertTrue(dispatch_started.wait(timeout=1.0))
        self.assertEqual(len(dispatch_threads), 1)
        self.assertNotEqual(dispatch_threads[0], gui_thread_id)
        self.assertTrue(Main._api_stage_command_worker_active(window))
        self.assertFalse(
            Main._wait_for_api_stage_command_workers(window, timeout_s=0.0)
        )

        release_dispatch.set()

        self.assertEqual(
            response.wait(timeout_s=1.0),
            {"accepted": True, "status_code": 200},
        )
        self.assertFalse(Main._api_stage_command_worker_active(window))
        self.assertTrue(
            Main._wait_for_api_stage_command_workers(window, timeout_s=0.0)
        )

    def test_concurrent_route_session_starts_are_single_flight(self) -> None:
        window = Main.__new__(Main)
        dispatch_started = threading.Event()
        release_dispatch = threading.Event()
        dispatched: list[str] = []
        window._probe_route_api_window_guard = lambda _action, _payload: None

        def dispatch(request, *, apply_route_control_guard):
            dispatched.append(str(request["action"]))
            dispatch_started.set()
            self.assertTrue(release_dispatch.wait(timeout=1.0))
            return {"accepted": True, "status_code": 202}

        window._dispatch_api_command_request = dispatch
        first = Main._submit_api_command_request(
            window,
            {"action": "start_route_session", "payload": {"start": 1}},
        )
        self.assertTrue(dispatch_started.wait(timeout=1.0))
        try:
            second = Main._submit_api_command_request(
                window,
                {"action": "start_route_session", "payload": {"start": 2}},
            )
            self.assertEqual(
                second,
                {
                    "accepted": False,
                    "status_code": 409,
                    "message": (
                        "API stage command is already running: start_route_session."
                    ),
                },
            )
            self.assertEqual(dispatched, ["start_route_session"])
        finally:
            release_dispatch.set()
        self.assertEqual(
            first.wait(timeout_s=1.0),
            {"accepted": True, "status_code": 202},
        )

    def test_route_start_rejects_concurrent_different_stage_command(self) -> None:
        window = Main.__new__(Main)
        dispatch_started = threading.Event()
        release_dispatch = threading.Event()
        dispatched: list[str] = []
        window._probe_route_api_window_guard = lambda _action, _payload: None

        def dispatch(request, *, apply_route_control_guard):
            dispatched.append(str(request["action"]))
            dispatch_started.set()
            self.assertTrue(release_dispatch.wait(timeout=1.0))
            return {"accepted": True, "status_code": 202}

        window._dispatch_api_command_request = dispatch
        first = Main._submit_api_command_request(
            window,
            {"action": "start_route_session", "payload": {}},
        )
        self.assertTrue(dispatch_started.wait(timeout=1.0))
        try:
            second = Main._submit_api_command_request(
                window,
                {"action": "stage_local_focus", "payload": {}},
            )
            self.assertEqual(second.get("accepted"), False)
            self.assertEqual(second.get("status_code"), 409)
            self.assertIn("start_route_session", str(second.get("message")))
            self.assertEqual(dispatched, ["start_route_session"])
        finally:
            release_dispatch.set()
        self.assertEqual(
            first.wait(timeout_s=1.0),
            {"accepted": True, "status_code": 202},
        )

    def test_worker_reservation_is_held_through_completion_publication(self) -> None:
        window = Main.__new__(Main)
        mutation_finished = threading.Event()
        publication_started = threading.Event()
        release_publication = threading.Event()
        reservation_released = threading.Event()
        releases: list[str] = []
        test_case = self
        window._probe_route_api_window_guard = lambda _action, _payload: None

        def release(reservation):
            releases.append(reservation.operation_id)
            result = Main._release_api_stage_command_reservation(window, reservation)
            reservation_released.set()
            return result

        class _BlockingCompletion(main_module.DeferredApiResponse):
            def complete(self, result):
                publication_started.set()
                test_case.assertTrue(release_publication.wait(timeout=1.0))
                return super().complete(result)

        def dispatch(_request, *, apply_route_control_guard):
            mutation_finished.set()
            return {"accepted": True, "status_code": 202}

        original_completion = main_module.DeferredApiResponse
        main_module.DeferredApiResponse = _BlockingCompletion
        window._release_api_stage_command_reservation = release
        window._dispatch_api_command_request = dispatch
        try:
            response = Main._submit_api_command_request(
                window,
                {"action": "start_route_session", "payload": {}},
            )
            self.assertTrue(mutation_finished.wait(timeout=1.0))
            self.assertTrue(publication_started.wait(timeout=1.0))
            self.assertTrue(Main._api_stage_command_worker_active(window))
            self.assertFalse(
                Main._wait_for_api_stage_command_workers(window, timeout_s=0.0)
            )
        finally:
            release_publication.set()
            main_module.DeferredApiResponse = original_completion

        self.assertEqual(
            response.wait(timeout_s=1.0),
            {"accepted": True, "status_code": 202},
        )
        self.assertTrue(reservation_released.wait(timeout=1.0))
        self.assertEqual(len(releases), 1)
        self.assertFalse(Main._api_stage_command_worker_active(window))

    def test_stage_command_start_failure_releases_reservation_once(self) -> None:
        window = Main.__new__(Main)
        releases: list[str] = []
        window._probe_route_api_window_guard = lambda _action, _payload: None

        def release(reservation):
            releases.append(reservation.operation_id)
            return Main._release_api_stage_command_reservation(window, reservation)

        class _FailingThread:
            def __init__(self, **_kwargs) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("thread unavailable")

        window._release_api_stage_command_reservation = release
        original_thread = main_module.threading.Thread
        main_module.threading.Thread = _FailingThread
        try:
            response = Main._submit_api_command_request(
                window,
                {"action": "stage_local_focus", "payload": {}},
            )
        finally:
            main_module.threading.Thread = original_thread

        self.assertEqual(response.get("accepted"), False)
        self.assertEqual(response.get("status_code"), 500)
        self.assertEqual(len(releases), 1)
        self.assertFalse(Main._api_stage_command_worker_active(window))

    def test_stage_command_exception_releases_reservation_once(self) -> None:
        window = Main.__new__(Main)
        releases: list[str] = []
        released = threading.Event()
        window._probe_route_api_window_guard = lambda _action, _payload: None

        def release(reservation):
            releases.append(reservation.operation_id)
            result = Main._release_api_stage_command_reservation(window, reservation)
            released.set()
            return result

        def dispatch(_request, *, apply_route_control_guard):
            raise RuntimeError("dispatch failed")

        window._release_api_stage_command_reservation = release
        window._dispatch_api_command_request = dispatch
        response = Main._submit_api_command_request(
            window,
            {"action": "stage_local_focus", "payload": {}},
        )

        self.assertEqual(response.wait(timeout_s=1.0).get("status_code"), 500)
        self.assertTrue(released.wait(timeout=1.0))
        self.assertEqual(len(releases), 1)
        self.assertFalse(Main._api_stage_command_worker_active(window))

    def test_cancelled_stage_command_response_releases_reservation_once(self) -> None:
        window = Main.__new__(Main)
        releases: list[str] = []
        released = threading.Event()
        window._probe_route_api_window_guard = lambda _action, _payload: None

        def release(reservation):
            releases.append(reservation.operation_id)
            result = Main._release_api_stage_command_reservation(window, reservation)
            released.set()
            return result

        window._release_api_stage_command_reservation = release
        window._dispatch_api_command_request = (
            lambda _request, *, apply_route_control_guard: {
                "accepted": False,
                "status_code": 409,
                "message": "Operation cancelled.",
            }
        )
        response = Main._submit_api_command_request(
            window,
            {"action": "stage_local_focus", "payload": {}},
        )

        self.assertEqual(
            response.wait(timeout_s=1.0),
            {
                "accepted": False,
                "status_code": 409,
                "message": "Operation cancelled.",
            },
        )
        self.assertTrue(released.wait(timeout=1.0))
        self.assertEqual(len(releases), 1)
        self.assertFalse(Main._api_stage_command_worker_active(window))

    def test_api_route_session_opens_measurement_controls_without_starting_gui_run(self) -> None:
        window = Main.__new__(Main)
        calls: list[bool] = []
        window._open_route_measurement_dialog = (
            lambda *, start_context=True: calls.append(bool(start_context))
        )

        Main._show_route_measurement_dialog_for_api_session(window)

        self.assertEqual(calls, [False])

    def test_api_route_session_started_slot_updates_controls_on_gui_thread(self) -> None:
        window = Main.__new__(Main)
        calls: list[object] = []
        panel = types.SimpleNamespace(
            set_route_measurement_running=lambda value: calls.append(
                ("panel_running", value)
            ),
            set_route_measurement_waiting=lambda value, reason="": calls.append(
                ("panel_waiting", value, reason)
            ),
            set_route_measurement_status=lambda value: calls.append(
                ("panel_status", value)
            ),
        )
        dialog = types.SimpleNamespace(
            set_running=lambda value: calls.append(("dialog_running", value)),
            set_waiting=lambda value, reason="": calls.append(
                ("dialog_waiting", value, reason)
            ),
            reset_progress=lambda value: calls.append(("dialog_progress", value)),
            set_status=lambda value: calls.append(("dialog_status", value)),
        )
        window._route_measurement_waiting = False
        window.design_navigator_panel = panel
        window._route_measurement_dialog = dialog
        window._set_route_measurement_resume_point = lambda value: calls.append(
            ("resume_point", value)
        )
        window._set_route_measurement_pending = lambda value: calls.append(
            ("pending", value)
        )
        window._show_route_measurement_dialog_for_api_session = lambda: calls.append(
            ("open_controls",)
        )
        window._show_status = lambda message: calls.append(("status", message))
        window._update_stage_coordinate_apply_state = lambda: calls.append(
            ("update_stage_controls",)
        )

        Main._on_route_measurement_started(window, "started", 420, 7, True)

        self.assertIn(("resume_point", 7), calls)
        self.assertIn(("pending", True), calls)
        self.assertIn(("open_controls",), calls)
        self.assertIn(("dialog_progress", 420), calls)
        self.assertIn(("status", "started"), calls)

    def test_route_started_preserves_initial_waiting_state_for_api_controls(
        self,
    ) -> None:
        window = Main.__new__(Main)
        calls: list[object] = []
        panel = types.SimpleNamespace(
            set_route_measurement_running=lambda value: calls.append(
                ("panel_running", value)
            ),
            set_route_measurement_waiting=lambda value, reason="": calls.append(
                ("panel_waiting", value, reason)
            ),
            set_route_measurement_status=lambda value: calls.append(
                ("panel_status", value)
            ),
        )
        dialog = types.SimpleNamespace(
            set_running=lambda value: calls.append(("dialog_running", value)),
            set_waiting=lambda value, reason="": calls.append(
                ("dialog_waiting", value, reason)
            ),
            reset_progress=lambda value: calls.append(("dialog_progress", value)),
            set_status=lambda value: calls.append(("dialog_status", value)),
        )
        window._route_measurement_waiting = True
        window.design_navigator_panel = panel
        window._route_measurement_dialog = dialog
        window._set_route_measurement_resume_point = lambda value: calls.append(
            ("resume_point", value)
        )
        window._set_route_measurement_pending = lambda value: calls.append(
            ("pending", value)
        )
        window._show_route_measurement_dialog_for_api_session = lambda: calls.append(
            ("open_controls",)
        )
        window._show_status = lambda message: calls.append(("status", message))
        window._update_stage_coordinate_apply_state = lambda: calls.append(
            ("update_stage_controls",)
        )

        Main._on_route_measurement_started(window, "started", 420, 7, True)

        self.assertIn(("panel_waiting", True, "paused"), calls)
        self.assertIn(("dialog_running", True), calls)
        self.assertIn(("dialog_waiting", True, "paused"), calls)

    def test_api_route_control_save_shift_updates_direct_route_offset(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        window._route_measurement_runner = None
        window._route_measurement_thread = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_offset_xy = (0.0, 0.0)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
        }
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (1.75, 2.25, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(window._api_route_offset_xy, (0.5, -0.25))
        self.assertEqual(
            api_route_adjusted_stage_xy(point, route_offset_xy=window._api_route_offset_xy),
            (1.75, 2.25),
        )
        self.assertEqual(
            statuses,
            ["Route shift saved: dX=+0.5000 mm, dY=-0.2500 mm."],
        )

    def test_api_route_control_interrupt_cancels_gui_work_and_pauses(self) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main()
        aborts: list[str] = []

        window._route_measurement_runner = None
        window._route_measurement_waiting = False
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._pending_route_measure_point = 91
        window._contact_seek_stop_requested = threading.Event()
        window._api_route_control_active = True
        window._api_route_control_pause_requested = True
        window._api_route_control_paused = False
        window._api_route_control_stop_requested = True
        window._api_route_control_label = "chip 163"
        window._api_route_control_updated_utc = ""
        window.lcr_controller = types.SimpleNamespace(
            abort_current_measurement=lambda: aborts.append("lcr")
        )
        window._api_route_lcr_controller = None
        window._controller_reports_active_motion = lambda: True

        Main._request_route_measurement_point_correction(window)

        self.assertIsNone(window._pending_route_measure_point)
        self.assertTrue(window._contact_seek_stop_requested.is_set())
        self.assertEqual(aborts, [])
        self.assertEqual(
            stage_controller.cancelled_tasks,
            ["API route control interrupt requested."],
        )
        self.assertEqual(
            stage_controller.cancelled_motions,
            ["API route control interrupt requested."],
        )
        self.assertTrue(window._api_route_control_active)
        self.assertFalse(window._api_route_control_pause_requested)
        self.assertTrue(window._api_route_control_paused)
        self.assertFalse(window._api_route_control_stop_requested)
        self.assertEqual(statuses[-1], "chip 163: interrupted; paused.")

    def test_api_route_control_confirmation_preserves_requested_action(self) -> None:
        window = Main.__new__(Main)
        actions: list[dict[str, object]] = []

        window._route_measurement_runner = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = lambda *_args: None

        Main._submit_route_measurement_confirmation(window, "skip")

        self.assertEqual(actions, [{"action": "skip"}])

    def test_api_route_control_action_is_consumed_explicitly(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        window._api_route_control_active = True
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = True
        window._api_route_control_stop_requested = False
        window._api_route_control_pending_action = ""
        window._api_route_control_label = "chip 163"
        window._api_route_control_updated_utc = ""
        window.design_navigator_panel = None
        window._route_measurement_dialog = _FakeVisibleDialog(True)
        window._api_timestamp_utc = lambda: "now"
        window._show_status = lambda message, _timeout_ms=None: statuses.append(str(message))

        response = Main._api_route_control_action(window, {"action": "skip"})

        self.assertFalse(window._api_route_control_pause_requested)
        self.assertFalse(window._api_route_control_paused)
        self.assertEqual(window._api_route_control_pending_action, "skip")
        self.assertEqual(response["pending_action"], "skip")

        ack = Main._api_route_control_action(window, {"action": "ack"})

        self.assertEqual(window._api_route_control_pending_action, "")
        self.assertEqual(ack["pending_action"], "")
        self.assertEqual(statuses[-1], "chip 163: skip.")

    def test_api_route_control_start_clears_waiting_route_runner(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        opened: list[bool] = []
        runner = _FakeRouteMeasurementRunner()
        thread = _FakeJoinableThread()

        window._route_measurement_runner = runner
        window._route_measurement_thread = thread
        window._route_measurement_waiting = True
        window._route_measurement_waiting_reason = "paused"
        window._route_measurement_session_active = True
        window._pending_route_measure_point = 33
        window._api_route_control_active = False
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = False
        window._api_route_control_stop_requested = False
        window._api_route_control_pending_action = ""
        window._api_route_control_label = ""
        window._api_route_control_updated_utc = ""
        window.design_navigator_panel = None
        window._route_measurement_dialog = None
        window._api_timestamp_utc = lambda: "now"
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )

        def open_controls() -> bool:
            opened.append(True)
            window._route_measurement_dialog = _FakeVisibleDialog(True)
            return True

        window._show_route_measurement_dialog_for_api_session = open_controls

        response = Main._api_route_control_action(
            window,
            {"action": "start", "label": "chip 163"},
        )

        self.assertTrue(response["accepted"])
        self.assertTrue(runner.stop_requested)
        self.assertEqual(thread.join_calls, [2.0])
        self.assertIsNone(window._route_measurement_runner)
        self.assertIsNone(window._route_measurement_thread)
        self.assertFalse(window._route_measurement_waiting)
        self.assertFalse(window._route_measurement_session_active)
        self.assertIsNone(window._pending_route_measure_point)
        self.assertTrue(window._api_route_control_active)
        self.assertEqual(statuses[-1], "chip 163: running.")
        self.assertEqual(opened, [True])

    def test_api_route_control_start_rejects_when_controls_do_not_open(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        opened: list[bool] = []

        window._route_measurement_runner = None
        window._route_measurement_thread = None
        window._api_route_control_active = False
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = False
        window._api_route_control_stop_requested = False
        window._api_route_control_pending_action = ""
        window._api_route_control_label = ""
        window._api_route_control_updated_utc = ""
        window.design_navigator_panel = None
        window._route_measurement_dialog = None
        window._api_timestamp_utc = lambda: "now"
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )
        window._show_route_measurement_dialog_for_api_session = (
            lambda: opened.append(True) or False
        )

        response = Main._api_route_control_action(
            window,
            {"action": "start", "label": "chip 163"},
        )

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertFalse(window._api_route_control_active)
        self.assertEqual(opened, [True])
        self.assertIn("window did not open", response["message"])

    def test_api_route_control_start_rejects_busy_route_runner(self) -> None:
        window = Main.__new__(Main)

        window._route_measurement_runner = _FakeRouteMeasurementRunner()
        window._route_measurement_thread = _FakeJoinableThread()
        window._route_measurement_waiting = False
        window._api_route_control_active = False

        response = Main._api_route_control_action(
            window,
            {"action": "start", "label": "chip 163"},
        )

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertIn("already active", response["message"])
        self.assertFalse(window._api_route_control_active)

    def test_api_route_control_start_rejects_external_route_session(self) -> None:
        window = Main.__new__(Main)
        runner = types.SimpleNamespace(
            stop=lambda: None,
            submit_external_result=lambda _result: True,
        )

        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeJoinableThread()
        window._route_measurement_waiting = True
        window._api_route_control_active = False

        response = Main._api_route_control_action(
            window,
            {"action": "start", "label": "chip 163"},
        )

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertFalse(window._api_route_control_active)

    def test_probe_route_api_rejects_route_command_without_control_window(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        window._route_measurement_dialog = None
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        response = Main._submit_api_command_request(
            window,
            {
                "action": "move_to_contact",
                "payload": {"contact_number": 33},
            },
        )

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertFalse(response["route_control_window_open"])
        self.assertIn("Probe route control window is closed", response["message"])
        self.assertEqual(statuses, [response["message"]])

    def test_api_route_control_command_uses_gui_thread_bridge(self) -> None:
        window = Main.__new__(Main)
        requests: list[tuple[dict[str, object], float]] = []

        def submit(request, *, timeout_s=5.0):
            requests.append((dict(request), float(timeout_s)))
            return {"accepted": True, "bridged": True}

        window._api_bridge = types.SimpleNamespace(submit=submit)

        response = Main._submit_api_command_request_from_api_thread(
            window,
            {
                "action": "api_route_control_action",
                "payload": {"action": "start"},
            },
        )

        self.assertTrue(response["accepted"])
        self.assertTrue(response["bridged"])
        self.assertEqual(len(requests), 1)
        bridged_request, timeout_s = requests[0]
        self.assertEqual(bridged_request["action"], "command")
        self.assertEqual(
            bridged_request["command"],
            {
                "action": "api_route_control_action",
                "payload": {"action": "start"},
            },
        )
        self.assertEqual(timeout_s, 10.0)

    def test_stage_starting_api_command_is_submitted_to_gui_bridge(self) -> None:
        window = Main.__new__(Main)
        bridge_requests: list[tuple[dict[str, object], float]] = []
        dispatch_calls: list[tuple[dict[str, object], bool]] = []

        def submit(request, *, timeout_s=5.0):
            bridge_requests.append((dict(request), float(timeout_s)))
            return {"accepted": True, "route_control_window_open": True}

        def dispatch(command_request, *, apply_route_control_guard):
            dispatch_calls.append(
                (dict(command_request), bool(apply_route_control_guard))
            )
            return {"accepted": True, "dispatched": True}

        window._api_bridge = types.SimpleNamespace(submit=submit)
        window._dispatch_api_command_request = dispatch

        command = {
            "action": "move_to_contact",
            "payload": {"contact_number": 33},
        }
        response = Main._submit_api_command_request_from_api_thread(window, command)

        self.assertTrue(response["accepted"])
        self.assertEqual(len(bridge_requests), 1)
        bridge_request, timeout_s = bridge_requests[0]
        self.assertEqual(bridge_request["action"], "command")
        self.assertEqual(bridge_request["command"], command)
        self.assertEqual(timeout_s, 10.0)
        self.assertEqual(dispatch_calls, [])

    def test_probe_route_api_guard_does_not_cover_bare_stage_move(self) -> None:
        window = Main.__new__(Main)

        self.assertFalse(
            Main._probe_route_api_requires_window(
                window,
                "move_to_coordinates",
                {"x": 1.0},
            )
        )

    def test_api_route_control_resume_rejects_closed_control_window(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        window._route_measurement_dialog = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        response = Main._api_route_control_action(window, {"action": "resume"})

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertFalse(response["route_control_window_open"])
        self.assertEqual(statuses, [response["message"]])

    def test_api_route_control_pause_request_waits_for_ack_before_resume(self) -> None:
        window = Main.__new__(Main)
        calls: list[tuple[str, object]] = []
        statuses: list[str] = []

        panel = types.SimpleNamespace(
            set_route_measurement_running=lambda value: calls.append(("running", value)),
            set_route_measurement_pause_request_pending=lambda value: calls.append(
                ("pending", value)
            ),
            set_route_measurement_waiting=lambda value, reason="": calls.append(
                ("waiting", value, reason)
            ),
            set_route_measurement_status=lambda value: calls.append(("status", value)),
        )
        window.design_navigator_panel = panel
        window._route_measurement_dialog = None
        window._api_route_control_active = True
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = False
        window._api_route_control_stop_requested = False
        window._api_route_control_pending_action = ""
        window._api_route_control_label = "chip 163"
        window._api_route_control_updated_utc = ""
        window._api_timestamp_utc = lambda: "now"
        window._show_status = lambda message, _timeout_ms=None: statuses.append(str(message))

        pause = Main._api_route_control_action(window, {"action": "pause"})

        self.assertTrue(pause["pause_requested"])
        self.assertFalse(pause["paused"])
        self.assertTrue(window._api_route_control_pause_requested)
        self.assertFalse(window._api_route_control_paused)
        self.assertFalse(window._route_measurement_waiting)
        self.assertIn(("pending", True), calls)
        self.assertIn(("waiting", False, "paused"), calls)

        paused = Main._api_route_control_action(window, {"action": "pause_ack"})

        self.assertFalse(paused["pause_requested"])
        self.assertTrue(paused["paused"])
        self.assertFalse(window._api_route_control_pause_requested)
        self.assertTrue(window._api_route_control_paused)
        self.assertTrue(window._route_measurement_waiting)
        self.assertIn(("pending", False), calls)

    def test_api_route_control_pending_pause_click_interrupts(self) -> None:
        window = Main.__new__(Main)
        interrupts: list[str] = []

        window._route_measurement_runner = None
        window._api_route_control_active = True
        window._api_route_control_pause_requested = True
        window._api_route_control_paused = False
        window._interrupt_api_route_controlled_operation = (
            lambda reason: interrupts.append(str(reason))
        )
        window._show_status = lambda *_args: None

        Main._request_pause_route_measurement(window)

        self.assertEqual(interrupts, ["API route control interrupt requested."])

    def test_api_route_control_pause_uses_control_state_with_existing_route_runner(self) -> None:
        window = Main.__new__(Main)
        actions: list[dict[str, object]] = []
        runner = types.SimpleNamespace(
            request_pause_after_current_point=lambda: actions.append(
                {"action": "runner_pause"}
            )
        )

        window._route_measurement_runner = runner
        window._api_route_control_active = True
        window._api_route_control_pause_requested = False
        window._api_route_control_paused = False
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = lambda *_args: None

        Main._request_pause_route_measurement(window)

        self.assertEqual(actions, [{"action": "pause"}])

    def test_api_route_control_interrupt_uses_control_state_with_existing_route_runner(self) -> None:
        window = Main.__new__(Main)
        interrupts: list[str] = []
        runner = _FakeRouteMeasurementRunner()

        window._route_measurement_runner = runner
        window._api_route_control_active = True
        window._api_route_control_pause_requested = True
        window._api_route_control_paused = False
        window._interrupt_api_route_controlled_operation = (
            lambda reason: interrupts.append(str(reason))
        )
        window._show_status = lambda *_args: None

        Main._request_route_measurement_point_correction(window)

        self.assertFalse(runner.correction_requested)
        self.assertEqual(interrupts, ["API route control interrupt requested."])

    def test_api_route_control_resume_uses_control_state_with_existing_route_runner(self) -> None:
        window = Main.__new__(Main)
        actions: list[dict[str, object]] = []
        runner = _FakeRouteMeasurementRunner()

        window._route_measurement_runner = runner
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = lambda *_args: None

        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(runner.confirmations, [])
        self.assertEqual(actions, [{"action": "resume"}])

    def test_api_route_control_save_shift_uses_control_state_with_existing_route_runner(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        runner_calls: list[object] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda point_number: runner_calls.append(
                ("select", point_number)
            )
            or (True, "selected"),
            save_current_position_adjustment=lambda stage_xy: runner_calls.append(
                ("save", stage_xy)
            )
            or (True, "saved by runner"),
        )

        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_offset_xy = (0.0, 0.0)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
        }
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (1.75, 2.25, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(runner_calls, [])
        self.assertEqual(window._api_route_offset_xy, (0.5, -0.25))
        self.assertEqual(
            statuses,
            ["Route shift saved: dX=+0.5000 mm, dY=-0.2500 mm."],
        )

    def test_blocked_route_shift_save_does_not_read_dialog_configuration(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        dialog_calls: list[str] = []

        class _Dialog:
            def current_configuration(self) -> object:
                dialog_calls.append("current_configuration")
                return types.SimpleNamespace(current_point=7)

        window._route_measurement_runner = None
        window._route_measurement_thread = None
        window._route_measurement_dialog = _Dialog()
        window._route_measurement_current_point = None
        window._api_route_control_active = True
        window._api_route_control_paused = False
        window._show_status = lambda message, _timeout_ms=0: statuses.append(str(message))
        window._route_runtime_presenter = lambda: types.SimpleNamespace(
            set_status=lambda _message: None
        )

        Main._save_route_measurement_shift(window)

        self.assertEqual(dialog_calls, [])
        self.assertEqual(statuses, ["Pause API route control before saving shift."])

    def test_route_shift_save_stage_error_while_busy_does_not_save(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        dialog_statuses: list[str] = []
        runner_calls: list[object] = []

        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda point_number: runner_calls.append(
                ("select", point_number)
            )
            or (True, "selected"),
            save_current_position_adjustment=lambda stage_xy: runner_calls.append(
                ("save", stage_xy)
            )
            or (True, "saved by runner"),
        )
        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_waiting = True
        window._route_measurement_current_point = None
        window._route_measurement_dialog = types.SimpleNamespace(
            set_status=lambda message: dialog_statuses.append(str(message))
        )
        window.design_navigator_panel = None
        window._api_route_control_active = False
        window._api_route_offset_xy = (0.0, 0.0)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (_ for _ in ()).throw(
                StageControllerError("Stage is busy.")
            ),
            latest_stage_position=lambda: (1.0, 2.0, 0.0),
            is_busy=lambda: True,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(runner_calls, [("select", 7)])
        self.assertEqual(statuses, ["Stage is busy."])
        self.assertEqual(dialog_statuses, ["Stage is busy."])
        self.assertEqual(window._api_route_offset_xy, (0.0, 0.0))

    def test_route_shift_save_stage_error_without_latest_position_does_not_save(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        runner_calls: list[object] = []

        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda point_number: runner_calls.append(
                ("select", point_number)
            )
            or (True, "selected"),
            save_current_position_adjustment=lambda stage_xy: runner_calls.append(
                ("save", stage_xy)
            )
            or (True, "saved by runner"),
        )
        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_waiting = True
        window._route_measurement_current_point = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = False
        window._api_route_offset_xy = (0.0, 0.0)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (_ for _ in ()).throw(
                StageControllerError("Unable to read stage position.")
            ),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(runner_calls, [("select", 7)])
        self.assertEqual(statuses, ["Unable to read stage position."])
        self.assertEqual(window._api_route_offset_xy, (0.0, 0.0))

    def test_route_shift_save_stage_error_with_latest_position_falls_back(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        runner_calls: list[object] = []

        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda point_number: runner_calls.append(
                ("select", point_number)
            )
            or (True, "selected"),
            save_current_position_adjustment=lambda stage_xy: runner_calls.append(
                ("save", stage_xy)
            )
            or (True, "saved by runner"),
            route_offset_xy=lambda: (0.5, -0.25),
        )
        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_waiting = True
        window._route_measurement_current_point = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = False
        window._api_route_offset_xy = (0.0, 0.0)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (_ for _ in ()).throw(
                StageControllerError("Unable to read stage position.")
            ),
            latest_stage_position=lambda: (1.75, 2.25, 0.0),
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(runner_calls, [("select", 7), ("save", (1.75, 2.25))])
        self.assertEqual(statuses, ["saved by runner"])
        self.assertEqual(window._api_route_offset_xy, (0.5, -0.25))

    def test_route_shift_save_without_usable_stage_xy_shows_unavailable(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        dialog_statuses: list[str] = []
        runner_calls: list[object] = []

        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda point_number: runner_calls.append(
                ("select", point_number)
            )
            or (True, "selected"),
            save_current_position_adjustment=lambda stage_xy: runner_calls.append(
                ("save", stage_xy)
            )
            or (True, "saved by runner"),
        )
        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_waiting = True
        window._route_measurement_current_point = None
        window._route_measurement_dialog = types.SimpleNamespace(
            set_status=lambda message: dialog_statuses.append(str(message))
        )
        window.design_navigator_panel = None
        window._api_route_control_active = False
        window._api_route_offset_xy = (0.0, 0.0)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (None, 2.25, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(runner_calls, [("select", 7)])
        self.assertEqual(statuses, ["Current stage X/Y position is unavailable."])
        self.assertEqual(dialog_statuses, ["Current stage X/Y position is unavailable."])
        self.assertEqual(window._api_route_offset_xy, (0.0, 0.0))

    def test_runner_route_shift_save_updates_api_offset_only_when_usable(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        runner_calls: list[object] = []

        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda point_number: runner_calls.append(
                ("select", point_number)
            )
            or (True, "selected"),
            save_current_position_adjustment=lambda stage_xy: runner_calls.append(
                ("save", stage_xy)
            )
            or (True, "saved by runner"),
            route_offset_xy=lambda: ("bad", -0.25),
        )
        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_waiting = True
        window._route_measurement_current_point = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = False
        window._api_route_offset_xy = (9.0, 8.0)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (1.75, 2.25, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(runner_calls, [("select", 7), ("save", (1.75, 2.25))])
        self.assertEqual(statuses, ["saved by runner"])
        self.assertEqual(window._api_route_offset_xy, (9.0, 8.0))

    def test_runner_route_shift_save_ignores_route_offset_exception(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        runner_calls: list[object] = []

        def route_offset_xy() -> tuple[float, float]:
            raise ValueError("bad offset")

        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda point_number: runner_calls.append(
                ("select", point_number)
            )
            or (True, "selected"),
            save_current_position_adjustment=lambda stage_xy: runner_calls.append(
                ("save", stage_xy)
            )
            or (True, "saved by runner"),
            route_offset_xy=route_offset_xy,
        )
        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_waiting = True
        window._route_measurement_current_point = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = False
        window._api_route_offset_xy = (9.0, 8.0)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (1.75, 2.25, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(runner_calls, [("select", 7), ("save", (1.75, 2.25))])
        self.assertEqual(statuses, ["saved by runner"])
        self.assertEqual(window._api_route_offset_xy, (9.0, 8.0))

    def test_runner_route_shift_save_marks_interrupt_pending_in_route_controls(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        panel_calls: list[tuple[str, object]] = []
        dialog_calls: list[tuple[str, object]] = []

        runner = types.SimpleNamespace(
            set_current_adjustment_point=lambda _point_number: (True, "selected"),
            save_current_position_adjustment=lambda _stage_xy: (True, "saved by runner"),
            route_offset_xy=lambda: (0.5, -0.25),
        )
        window._route_measurement_runner = runner
        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_waiting = True
        window._route_measurement_current_point = None
        window._route_measurement_dialog = types.SimpleNamespace(
            set_interrupt_request_pending=lambda value: dialog_calls.append(
                ("interrupt", value)
            ),
            set_status=lambda message: dialog_calls.append(("status", message)),
        )
        window.design_navigator_panel = types.SimpleNamespace(
            set_route_measurement_interrupt_request_pending=lambda value: panel_calls.append(
                ("interrupt", value)
            ),
            set_route_measurement_status=lambda message: panel_calls.append(
                ("status", message)
            ),
        )
        window._api_route_control_active = False
        window._api_route_offset_xy = (0.0, 0.0)
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (1.75, 2.25, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)

        self.assertEqual(statuses, ["saved by runner"])
        self.assertIn(("interrupt", True), panel_calls)
        self.assertIn(("status", "saved by runner"), panel_calls)
        self.assertIn(("interrupt", True), dialog_calls)
        self.assertIn(("status", "saved by runner"), dialog_calls)

    def test_api_route_control_save_shift_then_resume_does_not_skip_contact(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []
        actions: list[dict[str, object]] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )

        window._route_measurement_runner = None
        window._route_measurement_thread = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._api_route_offset_xy = (0.0, 0.0)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
        }
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            current_stage_position=lambda: (1.75, 2.25, 0.0),
            latest_stage_position=lambda: None,
            is_busy=lambda: False,
        )

        Main._save_route_measurement_shift(window, 7)
        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(window._api_route_offset_xy, (0.5, -0.25))
        self.assertEqual(actions, [{"action": "resume"}])

    def test_route_contact_move_requires_paused_api_route_control(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        window._route_contact_move_thread = None
        window._route_measurement_thread = None
        window._api_route_control_active = True
        window._api_route_control_paused = False
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._request_route_contact_move(window, 33)

        self.assertEqual(
            statuses,
            ["Pause API route control before moving to a contact."],
        )

    def test_telegram_measure_submits_waiting_route_action(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        statuses: list[str] = []

        window._route_measurement_waiting = True
        window._route_measurement_runner = runner
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._telegram_default_markup = lambda: "markup"
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        response = Main._telegram_route_action_response(window, "measure")

        self.assertEqual(runner.confirmations, ["measure"])
        self.assertEqual(response.callback_answer, "measure submitted.")
        self.assertEqual(response.reply_markup, "markup")
        self.assertEqual(statuses, ["Route measurement: measure."])

    def test_telegram_remeasure_action_is_disabled(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()

        window._route_measurement_waiting = True
        window._route_measurement_runner = runner
        window._telegram_default_markup = lambda: "markup"

        response = Main._telegram_route_action_response(window, "remeasure")

        self.assertEqual(runner.confirmations, [])
        self.assertEqual(response.callback_answer, "Unknown action.")
        self.assertEqual(response.reply_markup, "markup")

    def test_telegram_route_actions_markup_omits_remeasure(self) -> None:
        original = main_module.telegram_inline_keyboard
        main_module.telegram_inline_keyboard = lambda rows: rows
        try:
            markup = Main._telegram_route_actions_markup()
        finally:
            main_module.telegram_inline_keyboard = original

        labels = [
            label
            for row in markup
            for label, _callback in row
        ]
        self.assertIn("Measure", labels)
        self.assertIn("Skip", labels)
        self.assertNotIn("Remeasure", labels)

    def test_telegram_skip_submits_paused_api_route_control_action(self) -> None:
        window = Main.__new__(Main)
        actions: list[dict[str, object]] = []

        window._route_measurement_waiting = True
        window._route_measurement_runner = None
        window._api_route_control_active = True
        window._api_route_control_paused = True
        window._telegram_default_markup = lambda: "markup"
        window._api_route_control_action = lambda payload: actions.append(dict(payload))
        window._show_status = lambda *_args: None

        response = Main._telegram_route_action_response(window, "skip")

        self.assertEqual(actions, [{"action": "skip"}])
        self.assertEqual(response.callback_answer, "skip submitted.")
        self.assertEqual(response.reply_markup, "markup")

    def test_waiting_dialog_pause_and_interrupt_buttons_resume(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        resumed: list[bool] = []
        interrupted: list[bool] = []
        paused: list[bool] = []
        dialog._running = True
        dialog._waiting = True
        dialog._waiting_reason = "paused"
        dialog._pause_request_pending = False
        dialog._interrupt_request_pending = False
        dialog._pause_button = _FakeButton()
        dialog._interrupt_button = _FakeButton()
        dialog.next_requested = types.SimpleNamespace(
            emit=lambda: resumed.append(True)
        )
        dialog.pause_requested = types.SimpleNamespace(
            emit=lambda: paused.append(True)
        )
        dialog.interrupt_requested = types.SimpleNamespace(
            emit=lambda: interrupted.append(True)
        )

        RouteMeasurementDialog._update_pause_interrupt_buttons(dialog)
        RouteMeasurementDialog._emit_pause_requested(dialog)
        RouteMeasurementDialog._emit_interrupt_or_resume_requested(dialog)

        self.assertEqual(dialog._pause_button.text, "Resume")
        self.assertEqual(dialog._interrupt_button.text, "Resume")
        self.assertTrue(dialog._pause_button.enabled)
        self.assertFalse(dialog._interrupt_button.enabled)
        self.assertEqual(len(resumed), 2)
        self.assertEqual(paused, [])
        self.assertEqual(interrupted, [])

    def test_running_dialog_pause_button_becomes_interrupt_until_waiting(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        resumed: list[bool] = []
        interrupted: list[bool] = []
        paused: list[bool] = []
        dialog._running = True
        dialog._waiting = False
        dialog._waiting_reason = ""
        dialog._pause_request_pending = False
        dialog._interrupt_request_pending = False
        dialog._pause_button = _FakeButton()
        dialog._interrupt_button = _FakeButton()
        dialog.next_requested = types.SimpleNamespace(
            emit=lambda: resumed.append(True)
        )
        dialog.pause_requested = types.SimpleNamespace(
            emit=lambda: paused.append(True)
        )
        dialog.interrupt_requested = types.SimpleNamespace(
            emit=lambda: interrupted.append(True)
        )

        RouteMeasurementDialog._update_pause_interrupt_buttons(dialog)
        self.assertEqual(dialog._pause_button.text, "Pause")

        RouteMeasurementDialog._emit_pause_requested(dialog)
        self.assertEqual(paused, [True])
        self.assertEqual(dialog._pause_button.text, "Interrupt")
        self.assertTrue(dialog._pause_button.enabled)

        RouteMeasurementDialog._emit_pause_requested(dialog)
        self.assertEqual(interrupted, [True])
        self.assertEqual(resumed, [])
        self.assertEqual(dialog._pause_button.text, "Interrupt")
        self.assertFalse(dialog._pause_button.enabled)

        dialog._waiting = True
        dialog._waiting_reason = "paused"
        dialog._pause_request_pending = False
        dialog._interrupt_request_pending = False
        RouteMeasurementDialog._update_pause_interrupt_buttons(dialog)
        self.assertEqual(dialog._pause_button.text, "Resume")
        self.assertTrue(dialog._pause_button.enabled)

    def test_external_measurement_waiting_keeps_interrupt_control(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        dialog._running = True
        dialog._waiting = False
        dialog._waiting_reason = ""
        dialog._pause_request_pending = False
        dialog._interrupt_request_pending = False
        dialog._pause_button = _FakeButton()
        dialog._interrupt_button = _FakeButton()
        dialog._stop_button = _FakeButton()
        dialog._save_shift_button = _FakeButton()
        dialog._remeasure_button = _FakeButton()
        dialog._skip_button = _FakeButton()
        dialog._next_button = _FakeButton()
        dialog._operation_combo = _FakeButton()
        dialog._jump_point_spin = _FakeButton()
        dialog._move_button = _FakeButton()
        dialog._jump_button = _FakeButton()
        dialog._set_runtime_settings_enabled = lambda _enabled: None
        dialog._update_operation_state = lambda: None
        dialog._update_session_buttons = (
            lambda: RouteMeasurementDialog._update_session_buttons(dialog)
        )
        dialog._start_session_button = _FakeButton()
        dialog._cancel_session_button = _FakeButton()
        dialog._measurement_session_active = True
        dialog._measure_button = _FakeButton()

        RouteMeasurementDialog.set_waiting(
            dialog,
            True,
            reason="external_measurement",
        )

        self.assertEqual(dialog._pause_button.text, "Interrupt")
        self.assertTrue(dialog._pause_button.enabled)
        self.assertFalse(dialog._next_button.enabled)
        self.assertFalse(dialog._measure_button.enabled)
        self.assertFalse(dialog._save_shift_button.enabled)
        self.assertFalse(dialog._skip_button.enabled)

if __name__ == "__main__":
    unittest.main()
