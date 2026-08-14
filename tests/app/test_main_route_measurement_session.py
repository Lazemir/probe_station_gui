import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import probe_station_gui.application.api_route_scan as api_route_scan_owner
import probe_station_gui.application.design_edit_dialog as design_edit_dialog_owner
import probe_station_gui.application.route_launch_setup as route_launch_owner
from probe_station_gui.application.route_run_execution import (
    RouteRunKind,
    RouteRunReleaseCause,
    RouteRunReleaseRequest,
)
from probe_station_gui.design.session import DesignSession
from probe_station_gui.instruments.meters.lcr_session_backend import LCRMeterError
from probe_station_gui.route.measurement import RouteExternalMeasurementSessionRunner
from probe_station_gui.route.session_start import snapshot_route_design_frame
from probe_station_gui.route.operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
)
from tests.app.main_coordinate_feedrate_support import (
    Main,
    RouteMeasurementPointRequestCallbacks,
    RouteMeasurementRunner,
    RouteMeasurementSettingsStore,
    _FakeAliveThread,
    _FakeRouteDialogOpenState,
    _FakeRouteMeasurementRunner,
    _FakeThread,
    _telegram_runtime_stub,
    main_module,
    request_route_measurement_for_point,
)
from tests.app.main_route_session_support import (
    RouteContactQualityLimits,
    RouteMeasurementPoint,
    RouteMeasurementRunConfiguration,
    RouteMeterConfiguration,
    _RouteStartLcr,
    _make_route_start_main,
    _route_start_configuration,
    _route_start_point,
    _telegram_test_photo_bytes,
)
from tests.app.route_run_execution_support import (
    activate_route_run,
    install_route_run_execution,
)


def _install_usable_design_frame(window: Main) -> object:
    usability = types.SimpleNamespace(
        usable=True,
        rejection_reason=None,
        frame_id="design-a",
        frame_version=4,
    )
    route_frame = snapshot_route_design_frame(
        frame_id="design-a",
        frame_version=4,
    )
    window._coordinate_system_coordinator = types.SimpleNamespace(
        current_design_lease=lambda: usability,
        design_lease_is_current=lambda snapshot: snapshot is usability,
    )
    window._snapshot_active_route_design_frame = lambda _usability=None: route_frame
    window._design_contact_success_callback = lambda _frame: None
    return usability


class MainRouteMeasurementSessionTest(unittest.TestCase):
    def test_restore_active_session_loads_saved_route_when_design_route_is_missing(
        self,
    ) -> None:
        route_path = Path("C:/routes/stripes.probe-route.json")
        route = types.SimpleNamespace(
            name="stripes",
            path=route_path,
            points=[object()] * 450,
        )
        state = {
            "measurement_session_active": True,
            "current_point": 223,
            "session_route_name": "stripes",
            "session_route_point_count": 450,
            "session_route_path": str(route_path),
        }
        events: list[object] = []
        window = Main.__new__(Main)
        window._design_session = types.SimpleNamespace(route=None)
        window._route_measurement_session_active = False
        window._route_measurement_settings_store = lambda: types.SimpleNamespace(
            load=lambda: dict(state)
        )
        window._apply_route_edit_plan = lambda plan: (
            events.append(("apply", plan)) or True
        )
        window._set_route_measurement_resume_point = lambda point: events.append(
            ("point", int(point))
        )

        def load_route(session, path):
            events.append(("load", str(path)))
            session.route = route
            return "route-plan"

        with (
            mock.patch.object(
                design_edit_dialog_owner.route_editing,
                "load_measurement_route",
                side_effect=load_route,
            ),
            mock.patch.object(
                main_module.QTimer,
                "singleShot",
                side_effect=lambda delay, callback: events.append(
                    ("open", int(delay), callback)
                ),
            ),
        ):
            Main._restore_route_measurement_state_after_design_load(window)

        self.assertEqual(events[0], ("load", str(route_path)))
        self.assertEqual(events[1], ("apply", "route-plan"))
        self.assertEqual(events[2], ("point", 223))
        self.assertEqual(events[3][0:2], ("open", 0))
        self.assertTrue(window._route_measurement_session_active)

    def test_route_run_holds_one_optical_session_around_runner(self) -> None:
        events: list[object] = []

        class _Lease:
            token = "route-token"

            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append(("exit", exc_type))
                return False

        class _Manager:
            def open(self, operation: str):
                events.append(("open", operation))
                return _Lease()

        window = Main.__new__(Main)
        window._optical_session_manager = _Manager()
        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda message: None
        )
        emitted: list[tuple[object, ...]] = []
        window.route_measurement_finished = types.SimpleNamespace(
            emit=lambda *args: emitted.append(args)
        )

        class _Runner:
            csv_path = Path("route.csv")

            @staticmethod
            def requires_optical_session() -> bool:
                return True

            @staticmethod
            def run() -> tuple[bool, str]:
                events.append(("run", window._route_measurement_optical_session_token))
                return True, "complete"

        runner = _Runner()
        activate_route_run(window, runner)

        Main._run_route_measurement(window, runner, RouteRunKind.GUI)

        self.assertEqual(
            events,
            [
                ("open", "route photography"),
                "enter",
                ("run", "route-token"),
                ("exit", None),
            ],
        )
        self.assertIsNone(window._route_measurement_optical_session_token)
        self.assertEqual(emitted, [(runner, True, "complete", "route.csv")])

    def test_route_run_closes_optical_session_when_runner_raises(self) -> None:
        events: list[object] = []

        class _Lease:
            token = "route-token"

            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append(("exit", exc_type))
                return False

        window = Main.__new__(Main)
        window._optical_session_manager = types.SimpleNamespace(
            open=lambda operation: events.append(("open", operation)) or _Lease()
        )
        statuses: list[str] = []
        window.route_measurement_status = types.SimpleNamespace(emit=statuses.append)
        emitted: list[tuple[object, ...]] = []
        window.route_measurement_finished = types.SimpleNamespace(
            emit=lambda *args: emitted.append(args)
        )

        class _Runner:
            csv_path = Path("route.csv")

            @staticmethod
            def requires_optical_session() -> bool:
                return True

            @staticmethod
            def run() -> tuple[bool, str]:
                raise RuntimeError("capture failed")

        runner = _Runner()
        activate_route_run(window, runner)

        Main._run_route_measurement(window, runner, RouteRunKind.GUI)

        self.assertEqual(events[-1], ("exit", RuntimeError))
        self.assertIsNone(window._route_measurement_optical_session_token)
        self.assertEqual(statuses, ["Route measurement failed: capture failed"])
        self.assertEqual(
            emitted,
            [(runner, False, "capture failed", "route.csv")],
        )

    def test_delayed_external_worker_keeps_bound_kind_after_replacement(self) -> None:
        window = Main.__new__(Main)
        window._optical_session_manager = mock.MagicMock()
        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda _message: None
        )
        emitted: list[tuple[object, ...]] = []
        window.route_measurement_finished = types.SimpleNamespace(
            emit=lambda *args: emitted.append(args)
        )

        class _Runner:
            @staticmethod
            def requires_optical_session() -> bool:
                return False

            @staticmethod
            def run() -> tuple[bool, str]:
                return False, "start failed"

        runner = _Runner()
        slot = activate_route_run(
            window,
            runner,
            kind=RouteRunKind.EXTERNAL_RESULT_SESSION,
        )

        def delayed_worker_target() -> None:
            Main._run_route_measurement(
                window,
                runner,
                RouteRunKind.EXTERNAL_RESULT_SESSION,
            )

        release = slot.release(
            RouteRunReleaseRequest(
                cause=RouteRunReleaseCause.FAILED_START,
                expected_runner=runner,
                join_timeout_s=0.0,
            )
        )
        replacement = object()
        replacement_thread = types.SimpleNamespace(
            is_alive=lambda: True,
            join=lambda timeout=None: None,
        )
        slot.activate(
            replacement,
            replacement_thread,
            kind=RouteRunKind.GUI,
        )

        delayed_worker_target()

        self.assertTrue(release.released)
        self.assertEqual(emitted, [(runner, False, "start failed", "")])
        self.assertIs(slot.snapshot().runner, replacement)

    def test_route_autofocus_uses_outer_optical_session(self) -> None:
        calls: list[dict[str, object]] = []
        window = Main.__new__(Main)
        window._route_measurement_optical_session_token = "route-token"
        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda message: None
        )
        window.stage_controller = types.SimpleNamespace(
            run_external_local_autofocus=lambda **kwargs: (
                calls.append(kwargs) or "focused"
            )
        )
        settings = types.SimpleNamespace(autofocus_range_mm=0.03)

        result = Main._route_photo_autofocus(
            window,
            object(),
            1,
            10,
            settings=settings,
        )

        self.assertEqual(result, "focused")
        self.assertEqual(
            calls,
            [{"range_mm": 0.03, "parent_token": "route-token"}],
        )

    def test_api_route_autofocus_uses_outer_optical_session(self) -> None:
        calls: list[dict[str, object]] = []
        window = Main.__new__(Main)
        window._route_measurement_optical_session_token = "route-token"
        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda message: None
        )
        window.stage_controller = types.SimpleNamespace(
            run_external_local_autofocus=lambda **kwargs: (
                calls.append(kwargs) or "focused"
            )
        )

        result = Main._api_route_photo_autofocus(
            window,
            object(),
            1,
            10,
            range_mm=0.04,
        )

        self.assertEqual(result, "focused")
        self.assertEqual(
            calls,
            [{"range_mm": 0.04, "parent_token": "route-token"}],
        )

    def test_api_route_session_reports_unexpected_instrument_setup_error(self) -> None:
        class _FailingLcr:
            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                _configuration: RouteMeterConfiguration,
            ) -> None:
                raise RuntimeError(
                    "Invalid session handle. The resource might be closed."
                )

        point = RouteMeasurementPoint(
            index=12,
            point_id="p012",
            label="P012",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )
        window = Main.__new__(Main)
        install_route_run_execution(window)
        window._last_route_measurement_result = None
        window._route_measurement_current_point = 12
        window.serial_connection = types.SimpleNamespace(is_open=True)
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(points=[object()], name="route"),
            registration=types.SimpleNamespace(valid=True),
        )
        _install_usable_design_frame(window)
        window._route_measurement_points = (
            lambda _route, *, frame_usability_snapshot=None: [point]
        )
        window._api_route_meter_configuration = lambda _payload, voltages_v=None: (
            RouteMeterConfiguration()
        )
        window.lcr_controller = _FailingLcr()

        response = Main._api_start_route_session(window, {})

        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["error_type"], "RuntimeError")
        self.assertIn("Route measurement instrument setup failed", response["message"])
        self.assertIn("Invalid session handle", response["message"])

    def test_api_route_session_rejects_unverified_design_provenance(self) -> None:
        for status, reason in (
            ("pending", "Design coordinate provenance is being checked."),
            ("blocked", "Design source changed since registration."),
        ):
            with self.subTest(status=status):
                unsafe_point_calls: list[object] = []
                window = Main.__new__(Main)
                window._api_route_session_thread_preflight = lambda _payload: (
                    None,
                    (0.0, 0.0),
                )
                window.serial_connection = types.SimpleNamespace(is_open=True)
                window._design_session = types.SimpleNamespace(
                    active_frame_id="design-a",
                    route=types.SimpleNamespace(points=[object()], name="route"),
                    registration=types.SimpleNamespace(valid=True),
                )
                window._route_measurement_current_point = 1
                window._snapshot_active_route_design_frame = lambda: (
                    snapshot_route_design_frame(
                        frame_id="design-a",
                        frame_version=4,
                    )
                )
                frame_usability = types.SimpleNamespace(
                    usable=False,
                    rejection_reason=reason,
                    frame_id="design-a",
                    frame_version=4,
                )
                window._coordinate_system_coordinator = types.SimpleNamespace(
                    current_design_lease=lambda current=frame_usability: current,
                    design_lease_is_current=lambda _snapshot: True,
                )

                def unsafe_points(route: object) -> list[RouteMeasurementPoint]:
                    unsafe_point_calls.append(route)
                    raise AssertionError("Unverified route points were materialized.")

                window._route_measurement_points = unsafe_points

                response = Main._api_start_route_session(window, {})

                self.assertFalse(response["accepted"], response)
                self.assertEqual(response["status_code"], 409)
                self.assertEqual(response["message"], reason)
                self.assertEqual(unsafe_point_calls, [])

    def test_api_route_session_rejects_verified_missing_reference_draft(self) -> None:
        unsafe_point_calls: list[object] = []
        window = Main.__new__(Main)
        window._api_route_session_thread_preflight = lambda _payload: (
            None,
            (0.0, 0.0),
        )
        window.serial_connection = types.SimpleNamespace(is_open=True)
        window._design_session = types.SimpleNamespace(
            active_frame_id="design-a",
            route=types.SimpleNamespace(points=[object()], name="route"),
            registration=types.SimpleNamespace(valid=True),
        )
        window._route_measurement_current_point = 1
        frame_usability = types.SimpleNamespace(
            usable=False,
            rejection_reason="Design X reference is not registered.",
            frame_id="design-a",
            frame_version=4,
        )
        window._coordinate_system_coordinator = types.SimpleNamespace(
            current_design_lease=lambda: frame_usability,
            design_lease_is_current=lambda _snapshot: True,
        )

        def unsafe_points(route: object) -> list[RouteMeasurementPoint]:
            unsafe_point_calls.append(route)
            raise AssertionError("Missing-reference route points were materialized.")

        window._route_measurement_points = unsafe_points

        response = Main._api_start_route_session(window, {})

        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(unsafe_point_calls, [])

    def test_route_session_active_reads_new_and_legacy_settings(self) -> None:
        self.assertTrue(
            RouteMeasurementSettingsStore.session_active(
                {"measurement_session_active": True}
            )
        )
        self.assertTrue(
            RouteMeasurementSettingsStore.session_active({"measurement_pending": True})
        )
        self.assertFalse(
            RouteMeasurementSettingsStore.session_active(
                {
                    "measurement_session_active": False,
                    "measurement_pending": True,
                }
            )
        )

    def test_route_completion_telegram_reports_csv_session_total(self) -> None:
        window = Main.__new__(Main)
        alerts: list[tuple[str, str, dict[str, object]]] = []
        statuses: list[str] = []
        pending: list[bool] = []
        resumed: list[int] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "route.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write("timestamp,point_index,status\n")
                handle.write("2026-05-31T20:00:00+03:00,1,ok\n")
                handle.write("2026-05-31T20:01:00+03:00,2,ok\n")

            activate_route_run(window, object(), waiting=True)
            window._route_measurement_photo_enabled = False
            window._route_measurement_measure_enabled = True
            window._route_measurement_point_numbers = [1, 2]
            window._route_measurement_current_point = 2
            window.design_navigator_panel = None
            window._route_measurement_dialog = None
            window._update_stage_coordinate_apply_state = lambda: None
            window._set_route_measurement_resume_point = resumed.append
            window._set_route_measurement_pending = pending.append
            window._show_status = lambda message, _timeout_ms=None: statuses.append(
                str(message)
            )
            window._telegram_runtime = _telegram_runtime_stub(
                send_alert=lambda key, text, **kwargs: alerts.append(
                    (key, text, kwargs)
                )
            )

            Main._on_route_measurement_finished(
                window,
                True,
                "Route measurement complete: 1 measurements saved to route.csv.",
                str(csv_path),
            )

        self.assertEqual(resumed, [1])
        self.assertEqual(pending, [False])
        self.assertEqual(len(alerts), 1)
        key, text, kwargs = alerts[0]
        self.assertEqual(key, "route_completed")
        self.assertIn("Session total: 2 measurements in CSV.", text)
        self.assertEqual(kwargs["document_path"], csv_path)

    def test_route_completion_stores_final_api_status_payload(self) -> None:
        class _Runner:
            def status_payload(self) -> dict[str, object]:
                return {"state": "completed", "accepted": True}

        runner = _Runner()
        window = Main.__new__(Main)
        activate_route_run(
            window,
            runner,
            kind=RouteRunKind.EXTERNAL_RESULT_SESSION,
            waiting=True,
        )
        window._api_route_session_id = "session-1"
        window._api_route_last_status = None
        window._api_route_lcr_controller = object()
        window._route_measurement_runtime_configuration = object()
        window._last_route_measurement_result = object()
        window._pending_route_measure_point = object()
        window._route_measurement_photo_enabled = True
        window._route_measurement_measure_enabled = False
        window._route_measurement_context_close_requested = False
        window._route_measurement_point_numbers = [1]
        window._route_measurement_current_point = 1
        window.design_navigator_panel = None
        window._route_measurement_dialog = None
        window._resume_resistance_standby_polling = lambda: None
        window._update_stage_coordinate_apply_state = lambda: None
        window._set_route_measurement_pending = lambda _pending: None
        window._show_status = lambda *_args, **_kwargs: None
        window._telegram_runtime = _telegram_runtime_stub()

        Main._on_route_measurement_finished(
            window,
            runner,
            True,
            "Route photo capture complete.",
            "",
        )

        self.assertEqual(
            window._api_route_last_status,
            {"state": "completed", "accepted": True},
        )

    def test_api_route_session_status_includes_public_artifacts(self) -> None:
        class _Runner:
            def status_payload(self) -> dict[str, object]:
                return {"accepted": True, "state": "running"}

        window = Main.__new__(Main)
        activate_route_run(
            window,
            _Runner(),
            kind=RouteRunKind.EXTERNAL_RESULT_SESSION,
        )
        window._api_route_last_status = None
        window._api_route_artifacts = {
            "artifact-1": {
                "artifact_id": "artifact-1",
                "filename": "photo.png",
                "content_type": "image/png",
                "kind": "route_photo",
                "metadata": {"position": 1},
                "created_at_utc": "2026-06-26T20:00:00Z",
                "size_bytes": 2,
                "data": b"\x01\x02",
            }
        }
        window._api_route_artifacts_lock = threading.Lock()

        response = Main._api_route_session_status(window)

        self.assertEqual(response["accepted"], True)
        self.assertEqual(response["state"], "running")
        self.assertEqual(
            response["artifacts"],
            [
                {
                    "artifact_id": "artifact-1",
                    "filename": "photo.png",
                    "content_type": "image/png",
                    "kind": "route_photo",
                    "metadata": {"position": 1},
                    "created_at_utc": "2026-06-26T20:00:00Z",
                    "size_bytes": 2,
                }
            ],
        )

    def test_api_route_session_artifact_returns_binary_payload(self) -> None:
        window = Main.__new__(Main)
        window._api_route_artifacts = {
            "artifact-1": {
                "artifact_id": "artifact-1",
                "filename": "photo.png",
                "content_type": "image/png",
                "kind": "route_photo",
                "metadata": {"position": 1},
                "created_at_utc": "2026-06-26T20:00:00Z",
                "size_bytes": 2,
                "data": b"\x01\x02",
            }
        }
        window._api_route_artifacts_lock = threading.Lock()

        response = Main._api_route_session_artifact(
            window,
            {"artifact_id": " artifact-1 "},
        )

        self.assertEqual(response["accepted"], True)
        self.assertEqual(response["artifact_id"], "artifact-1")
        self.assertEqual(response["data"], b"\x01\x02")

    def test_api_route_photo_artifact_sends_requested_telegram_photo_once(self) -> None:
        sent: list[tuple[str, tuple[bytes, str] | None, object | None]] = []

        class _RouteTelegram:
            def __init__(self) -> None:
                self.calls = 0

            def consume_route_photo_request(self) -> bool:
                self.calls += 1
                return self.calls == 1

        route_telegram = _RouteTelegram()
        point = RouteMeasurementPoint(
            index=3,
            point_id="p003",
            label="P003",
            design_center=(0.0, 0.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(0.0, 0.0),
            needle_2_design=(0.0, 0.0),
        )
        window = Main.__new__(Main)
        install_route_run_execution(window)
        window._api_route_artifacts = {}
        window._api_route_artifacts_lock = threading.Lock()
        window._latest_camera_counter = lambda: 4
        window._wait_for_camera_frame = lambda *, after_counter, timeout_s: (
            object(),
            after_counter + 1,
        )
        window._qimage_telegram_photo = lambda _frame: (
            _telegram_test_photo_bytes(3, 2, 0x0000FF00),
            "route.jpg",
        )
        window._latest_camera_frame_photo = lambda: None
        window._api_timestamp_utc = lambda: "2026-06-26T20:00:00Z"
        window._api_structure_number_for_measurement_point = lambda _point: 17
        window._telegram_runtime = _telegram_runtime_stub(
            route_photos=route_telegram,
            send_bot_message=lambda message, *, photo=None, reply_markup=None: (
                sent.append((message, photo, reply_markup))
            ),
        )

        first_artifact = Main._capture_api_route_photo_artifact(
            window,
            point,
            1,
            2,
            {"focus_best_z_mm": 1.2},
        )
        second_artifact = Main._capture_api_route_photo_artifact(
            window,
            point,
            1,
            2,
            {"focus_best_z_mm": 1.2},
        )

        self.assertNotEqual(first_artifact, second_artifact)
        self.assertEqual(len(sent), 1)
        message, photo, reply_markup = sent[0]
        self.assertIn("Point 1/2", message)
        self.assertIn("structure 17", message)
        self.assertEqual(
            photo, (_telegram_test_photo_bytes(3, 2, 0x0000FF00), "route.jpg")
        )
        self.assertEqual(reply_markup, "markup")
        self.assertEqual(
            window._api_route_artifacts[first_artifact]["metadata"],
            {
                "position": 1,
                "total": 2,
                "point_index": 3,
                "contact_number": 17,
                "label": "P003",
                "focus": {"focus_best_z_mm": 1.2},
            },
        )

    def test_context_close_failure_suppresses_route_failure_telegram(self) -> None:
        window = Main.__new__(Main)
        alerts: list[tuple[object, ...]] = []
        pending: list[bool] = []
        resumed: list[int] = []
        statuses: list[str] = []

        activate_route_run(window, object(), waiting=True)
        window._api_route_session_id = None
        window._api_route_lcr_controller = None
        window._route_measurement_runtime_configuration = None
        window._last_route_measurement_result = object()
        window._pending_route_measure_point = object()
        window._route_measurement_photo_enabled = True
        window._route_measurement_measure_enabled = True
        window._route_measurement_context_close_requested = True
        window._route_measurement_point_numbers = [1, 2]
        window._route_measurement_current_point = 2
        window.design_navigator_panel = None
        window._route_measurement_dialog = None
        window._resume_resistance_standby_polling = lambda: None
        window._update_stage_coordinate_apply_state = lambda: None
        window._set_route_measurement_resume_point = resumed.append
        window._set_route_measurement_pending = pending.append
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )
        window._telegram_runtime = _telegram_runtime_stub(
            send_alert=lambda *args, **_kwargs: alerts.append(tuple(args))
        )

        Main._on_route_measurement_finished(
            window,
            False,
            "Route window closed.",
            "partial.csv",
        )

        self.assertEqual(resumed, [2])
        self.assertEqual(pending, [True])
        self.assertEqual(statuses, ["Route window closed."])
        self.assertEqual(alerts, [])
        self.assertFalse(window._route_measurement_context_close_requested)

    def test_cancel_route_measurement_session_clears_persisted_state(self) -> None:
        window = Main.__new__(Main)
        statuses: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            route_point = types.SimpleNamespace(camera_center=(10.0, 20.0))
            route = types.SimpleNamespace(points=[route_point])

            install_route_run_execution(window)
            window._route_measurement_dialog = None
            window._route_measurement_session_active = True
            window._route_measurement_current_point = 5
            window.settings_manager = types.SimpleNamespace(
                config_dir=lambda: config_dir
            )
            window._design_session = DesignSession(route=route)
            window._last_selected_design_point = None
            window._refresh_design_panel = lambda: None
            window._controller_state_persistence_suspended = True
            window._show_status = lambda message, _timeout_ms=None: statuses.append(
                str(message)
            )

            Main._cancel_route_measurement_session(window)

            settings_path = config_dir / "route-measurement-settings.json"
            with settings_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)

        self.assertFalse(window._route_measurement_session_active)
        self.assertEqual(window._route_measurement_current_point, 1)
        self.assertEqual(window._design_session.selected_route_point_index, 0)
        self.assertFalse(data["measurement_session_active"])
        self.assertFalse(data["measurement_pending"])
        self.assertEqual(data["current_point"], 1)
        self.assertIn("Route measurement session cancelled.", statuses)

    def test_start_route_measurement_session_keeps_status_dialog_only(self) -> None:
        window = Main.__new__(Main)
        dialog = _FakeRouteDialogOpenState(
            configuration=types.SimpleNamespace(current_point=7)
        )
        statuses: list[tuple[str, int | None]] = []
        panel_statuses: list[str] = []

        install_route_run_execution(window)
        window._route_measurement_dialog = dialog
        window.design_navigator_panel = types.SimpleNamespace(
            set_route_measurement_status=lambda message: panel_statuses.append(
                str(message)
            )
        )
        window._route_measurement_session_active = False
        window._route_measurement_current_point = 3
        window._select_route_point_for_measurement = lambda _point_number: None
        window._save_route_measurement_session_metadata = lambda _configuration: None
        window._show_status = lambda message, timeout_ms=0: statuses.append(
            (str(message), timeout_ms)
        )

        Main._start_route_measurement_session(window)

        self.assertEqual(
            statuses,
            [("Route point set to point 7.", 5000)],
        )
        self.assertEqual(dialog.status_calls, ["Route point set to point 7."])
        self.assertEqual(panel_statuses, [])

    def test_cancel_route_measurement_session_keeps_status_dialog_only(self) -> None:
        window = Main.__new__(Main)
        dialog = _FakeRouteDialogOpenState()
        statuses: list[tuple[str, int | None]] = []
        panel_statuses: list[str] = []

        install_route_run_execution(window)
        window._route_measurement_dialog = dialog
        window.design_navigator_panel = types.SimpleNamespace(
            set_route_measurement_status=lambda message: panel_statuses.append(
                str(message)
            )
        )
        window._route_measurement_session_active = True
        window._route_measurement_current_point = 5
        window._pending_route_measure_point = 11
        window._select_route_point_for_measurement = lambda _point_number: None
        window._show_status = lambda message, timeout_ms=0: statuses.append(
            (str(message), timeout_ms)
        )

        Main._cancel_route_measurement_session(window)

        self.assertEqual(statuses, [("Route measurement session cancelled.", 5000)])
        self.assertEqual(dialog.status_calls, ["Route measurement session cancelled."])
        self.assertEqual(panel_statuses, [])

    def test_open_route_measurement_dialog_without_route_shows_status_only(
        self,
    ) -> None:
        window = Main.__new__(Main)
        statuses: list[tuple[str, int | None]] = []

        window._design_session = types.SimpleNamespace(route=None, document=None)
        window._route_measurement_dialog = None
        window._show_status = lambda message, timeout_ms=None: statuses.append(
            (str(message), timeout_ms)
        )

        Main._open_route_measurement_dialog(window, start_context=False)

        self.assertEqual(
            statuses,
            [("Create or load a probe route before measuring.", 5000)],
        )
        self.assertIsNone(window._route_measurement_dialog)

    def test_open_route_measurement_dialog_updates_existing_dialog_before_sync(
        self,
    ) -> None:
        window = Main.__new__(Main)
        dialog = _FakeRouteDialogOpenState()
        route = types.SimpleNamespace(
            name="route-a",
            path=Path("C:/routes/route-a.json"),
            points=[object(), object(), object()],
        )

        window._design_session = types.SimpleNamespace(route=route, document=None)
        window._route_measurement_dialog = dialog
        window._route_measurement_session_active = True
        window._route_measurement_current_point = 6
        install_route_run_execution(window)
        window.lcr_controller = types.SimpleNamespace(meter_type=lambda: "keysight")
        window.settings_manager = types.SimpleNamespace(
            config_dir=lambda: Path("C:/config")
        )

        Main._open_route_measurement_dialog(window, start_context=False)

        self.assertEqual(len(dialog.route_updates), 1)
        self.assertEqual(dialog.route_updates[0]["route_name"], "route-a")
        self.assertEqual(dialog.session_active_calls, [(True, False)])
        self.assertEqual(dialog.current_point_calls, [(6, False)])
        self.assertEqual(dialog.running_calls, [])
        self.assertEqual(dialog.waiting_calls, [])
        self.assertEqual(dialog.status_calls, ["Choose a point, then Measure or Move."])

    def test_open_route_measurement_dialog_start_context_launches_waiting_run(
        self,
    ) -> None:
        window = Main.__new__(Main)
        dialog = _FakeRouteDialogOpenState(
            configuration=types.SimpleNamespace(current_point=9)
        )
        route = types.SimpleNamespace(name="route-a", path=None, points=[object()])
        started: list[tuple[object, bool]] = []

        window._design_session = types.SimpleNamespace(route=route, document=None)
        window._route_measurement_dialog = dialog
        window._route_measurement_session_active = False
        window._route_measurement_current_point = None
        install_route_run_execution(window)
        window.lcr_controller = types.SimpleNamespace(meter_type=lambda: "keysight")
        window.settings_manager = types.SimpleNamespace(
            config_dir=lambda: Path("C:/config")
        )
        window._start_route_measurement = (
            lambda configuration, *, wait_before_first_point=False: started.append(
                (configuration, bool(wait_before_first_point))
            )
        )

        Main._open_route_measurement_dialog(window, start_context=True)

        self.assertEqual(started, [(dialog.configuration, True)])

    def test_clear_route_measurement_dialog_requests_runner_stop_while_active(
        self,
    ) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()

        window._route_measurement_dialog = _FakeRouteDialogOpenState()
        activate_route_run(window, runner, _FakeAliveThread())
        window._route_measurement_context_close_requested = False

        Main._clear_route_measurement_dialog(window)

        self.assertIsNone(window._route_measurement_dialog)
        self.assertTrue(window._route_measurement_context_close_requested)
        self.assertTrue(runner.stop_requested)

    def test_measure_selected_route_point_while_waiting_submits_jump(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        statuses: list[str] = []

        activate_route_run(window, runner, _FakeAliveThread(), waiting=True)
        window._route_measurement_current_point = 38
        window._pending_route_measure_point = 123
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )

        request_route_measurement_for_point(
            point_number=91,
            thread_active=True,
            waiting=True,
            callbacks=RouteMeasurementPointRequestCallbacks(
                open_dialog=window._open_route_measurement_dialog,
                current_dialog=lambda: window._route_measurement_dialog,
                submit_confirmation=window._submit_route_measurement_confirmation,
                request_point_correction=(
                    window._request_route_measurement_point_correction
                ),
                start_measurement=window._start_route_measurement,
                set_pending_point=lambda point: setattr(
                    window, "_pending_route_measure_point", point
                ),
                clear_pending_point=lambda: setattr(
                    window, "_pending_route_measure_point", None
                ),
            ),
        )

        self.assertEqual(runner.confirmations, ["jump:91"])
        self.assertIsNone(window._pending_route_measure_point)
        self.assertEqual(statuses, ["Route measurement: measure from point 91."])

    def test_resume_after_waiting_point_change_jumps_to_selected_point(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        resumed: list[int] = []
        statuses: list[str] = []

        activate_route_run(window, runner, waiting=True)
        window._pending_route_measure_point = None
        window._route_contact_move_thread = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        window._api_route_control_active = False
        window._set_route_measurement_resume_point = resumed.append
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )

        Main._on_route_measurement_current_point_changed(window, 42)
        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(resumed, [42])
        self.assertIsNone(window._pending_route_measure_point)
        self.assertEqual(runner.confirmations, ["jump:42"])
        self.assertEqual(statuses, ["Route measurement: measure from point 42."])

    def test_api_route_start_takes_over_waiting_gui_runner(self) -> None:
        class _FakeEmit:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def emit(self, *args: object) -> None:
                self.calls.append(tuple(args))

        class _FakeAliveThread:
            def __init__(self) -> None:
                self.joined = False

            def is_alive(self) -> bool:
                return not self.joined

            def join(self, timeout: float | None = None) -> None:
                _ = timeout
                self.joined = True

        class _FakeConnectedLcr:
            def __init__(self) -> None:
                self.configurations: list[RouteMeterConfiguration] = []

            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                configuration: RouteMeterConfiguration,
            ) -> None:
                self.configurations.append(configuration)

        point = RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )
        stage = types.SimpleNamespace()
        old_runner = RouteMeasurementRunner(
            points=[point],
            csv_path="NUL",
            stage_controller=stage,
            lcr_controller=types.SimpleNamespace(),
            needle_feedrate=None,
            wait_before_first_point=True,
        )
        old_runner.set_route_offset_xy((0.125, -0.25))
        old_thread = _FakeAliveThread()
        lcr = _FakeConnectedLcr()
        window = Main.__new__(Main)
        activate_route_run(window, old_runner, old_thread, waiting=True)
        window._last_route_measurement_result = None
        window._route_measurement_current_point = 1
        window._route_measurement_session_active = True
        window._route_measurement_dialog = None
        window._api_route_lcr_controller = None
        window._api_route_last_status = None
        window._api_route_session_id = None
        window._api_route_artifacts = {}
        window._api_route_artifacts_lock = threading.Lock()
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(points=[object()], name="route"),
            registration=types.SimpleNamespace(valid=True),
        )
        _install_usable_design_frame(window)
        window.serial_connection = types.SimpleNamespace(is_open=True)
        window.stage_controller = stage
        window.lcr_controller = lcr
        window.route_measurement_status = _FakeEmit()
        window.route_measurement_progress = _FakeEmit()
        window.route_measurement_result = _FakeEmit()
        window.route_measurement_waiting_changed = _FakeEmit()
        window.route_measurement_started = _FakeEmit()
        window.route_measurement_finished = _FakeEmit()
        optical_lease = mock.MagicMock()
        optical_lease.token = "route-token"
        optical_lease.__enter__.return_value = optical_lease
        window._optical_session_manager = mock.MagicMock()
        window._optical_session_manager.open.return_value = optical_lease
        window._route_measurement_points = (
            lambda _route, *, frame_usability_snapshot=None: [point]
        )
        window._wait_for_camera_frame = lambda timeout_s=0.1: (None, None)
        window._api_route_meter_configuration = lambda _payload, voltages_v=None: (
            RouteMeterConfiguration()
        )
        window.settings_manager = types.SimpleNamespace(
            needle_calibration_configuration=lambda: types.SimpleNamespace(
                feedrate_mm_min=2.0,
            )
        )
        window._capture_api_route_photo_artifact = (
            lambda _point, _position, _total, _focus_result: []
        )
        window._api_route_photo_autofocus = (
            lambda _point, _position, _total, *, range_mm: None
        )
        window._capture_route_contact_photo = lambda *args, **kwargs: None
        window._capture_route_pre_contact_photo = lambda *args, **kwargs: None
        window._telegram_runtime = _telegram_runtime_stub()

        response = Main._api_start_route_session(
            window,
            {},
        )
        new_runner = window._route_run_execution.snapshot().runner
        window._route_measurement_context_close_requested = True

        self.assertTrue(response["accepted"], response)
        self.assertTrue(old_thread.joined)
        self.assertIsInstance(new_runner, RouteExternalMeasurementSessionRunner)
        self.assertEqual(new_runner.route_offset_xy(), (0.125, -0.25))
        self.assertEqual(lcr.configurations, [RouteMeterConfiguration()])
        self.assertEqual(response["state"], "waiting_paused")

        Main._on_route_measurement_finished(
            window,
            old_runner,
            False,
            "Old route stopped.",
            "",
        )
        self.assertIs(window._route_run_execution.snapshot().runner, new_runner)
        self.assertTrue(window._route_measurement_context_close_requested)

        new_runner.stop()
        window._route_run_execution.snapshot().thread.join(timeout=2.0)

    def test_api_route_start_attaches_existing_external_session(self) -> None:
        active_status = {
            "accepted": True,
            "session_id": "session-123",
            "state": "waiting_paused",
            "message": "Route API session ready.",
            "position": 4,
            "current_contact": {"label": "P004"},
        }

        class _FakeExternalRunner(RouteExternalMeasurementSessionRunner):
            def __init__(self) -> None:
                pass

            def status_payload(self) -> dict[str, object]:
                return dict(active_status)

        for alias in ("attach_existing_session", "attach_existing", "resume_existing"):
            with self.subTest(alias=alias):
                window = Main.__new__(Main)
                activate_route_run(
                    window,
                    _FakeExternalRunner(),
                    _FakeAliveThread(),
                    kind=RouteRunKind.EXTERNAL_RESULT_SESSION,
                )

                response = Main._api_start_route_session(
                    window,
                    {alias: True},
                )

                self.assertEqual(response, active_status)

    def test_api_route_start_rejects_existing_external_session_without_attach(
        self,
    ) -> None:
        active_status = {
            "accepted": True,
            "session_id": "session-123",
            "state": "waiting_paused",
            "message": "Route API session ready.",
            "position": 4,
            "current_contact": {"label": "P004"},
        }

        class _FakeExternalRunner(RouteExternalMeasurementSessionRunner):
            def __init__(self) -> None:
                pass

            def status_payload(self) -> dict[str, object]:
                return dict(active_status)

        window = Main.__new__(Main)
        activate_route_run(
            window,
            _FakeExternalRunner(),
            _FakeAliveThread(),
            kind=RouteRunKind.EXTERNAL_RESULT_SESSION,
        )

        response = Main._api_start_route_session(window, {})

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(
            response["message"],
            "External route session is already active at P004. Stop it first or pass "
            "attach_existing_session=true to attach explicitly.",
        )
        self.assertEqual(response["active_session"], active_status)

    def test_api_route_start_rejects_when_waiting_gui_runner_does_not_stop(
        self,
    ) -> None:
        point = _route_start_point()
        runner = RouteMeasurementRunner(
            points=[point],
            csv_path="NUL",
            stage_controller=types.SimpleNamespace(),
            lcr_controller=types.SimpleNamespace(),
            needle_feedrate=None,
            wait_before_first_point=True,
        )

        class _NeverStopsThread:
            def __init__(self) -> None:
                self.join_calls: list[float | None] = []

            def is_alive(self) -> bool:
                return True

            def join(self, timeout: float | None = None) -> None:
                self.join_calls.append(timeout)

        thread = _NeverStopsThread()
        window = Main.__new__(Main)
        activate_route_run(window, runner, thread, waiting=True)
        window._last_route_measurement_result = None
        window._route_measurement_session_active = True

        response = Main._api_start_route_session(window, {})

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(
            response["message"], "Waiting GUI route measurement did not stop."
        )
        self.assertEqual(thread.join_calls, [2.0])
        self.assertIs(window._route_run_execution.snapshot().runner, runner)
        self.assertTrue(window._route_run_execution.snapshot().waiting)
        self.assertTrue(window._route_measurement_session_active)

    def test_api_route_start_initial_pause_timeout_cleans_up_session_state(
        self,
    ) -> None:
        class _FakeApiThread:
            def __init__(self, *, target, args=(), daemon=None, name=None) -> None:
                self.target = target
                self.args = args
                self.daemon = daemon
                self.name = name
                self.started = False
                self.alive = True
                self.join_calls: list[float | None] = []

            def start(self) -> None:
                self.started = True

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float | None = None) -> None:
                self.join_calls.append(timeout)
                self.alive = False

        class _FakeExternalRunner:
            def __init__(self, **kwargs) -> None:
                self.kwargs = kwargs
                self.route_offset = None
                self.stop_calls = 0

            def set_route_offset_xy(self, offset_xy: tuple[float, float]) -> None:
                self.route_offset = tuple(offset_xy)

            def wait_until_initial_pause(self, timeout_s: float) -> bool:
                return False

            def status_payload(self) -> dict[str, object]:
                return {
                    "accepted": True,
                    "state": "running",
                    "message": "Initial pause timed out.",
                }

            def stop(self) -> None:
                self.stop_calls += 1

        window, _statuses, telegrams, _dialog, lcr, _camera_calls = (
            _make_route_start_main()
        )
        window._route_measurement_current_point = 1
        window._last_route_measurement_result = None
        window._route_measurement_point_numbers = [99]
        window._api_route_meter_configuration = lambda _payload, voltages_v=None: (
            RouteMeterConfiguration()
        )
        window.settings_manager = types.SimpleNamespace(
            needle_calibration_configuration=lambda: types.SimpleNamespace(
                feedrate_mm_min=2.0,
            )
        )
        window._api_route_artifacts = {"old": object()}
        window._api_route_artifacts_lock = threading.Lock()
        with (
            mock.patch.object(
                api_route_scan_owner,
                "RouteExternalMeasurementSessionRunner",
                _FakeExternalRunner,
            ),
            mock.patch.object(api_route_scan_owner.threading, "Thread", _FakeApiThread),
        ):
            response = Main._api_start_route_session(window, {})

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["message"], "Initial pause timed out.")
        self.assertEqual(
            response["status"],
            {
                "accepted": True,
                "state": "running",
                "message": "Initial pause timed out.",
            },
        )
        self.assertFalse(window._route_run_execution.snapshot().active)
        self.assertIsNone(window._api_route_lcr_controller)
        self.assertFalse(window._route_run_execution.snapshot().waiting)
        self.assertFalse(window._route_measurement_session_active)
        self.assertEqual(telegrams, [])
        self.assertEqual(lcr.configurations, [RouteMeterConfiguration()])

    def test_route_start_rejects_active_thread_with_existing_status_timeout(
        self,
    ) -> None:
        window, statuses, _telegrams, _dialog, lcr, _camera_calls = (
            _make_route_start_main(thread=_FakeAliveThread())
        )

        Main._start_route_measurement(window, _route_start_configuration())

        self.assertEqual(statuses, [("Route measurement is already active.", 4000)])
        self.assertEqual(lcr.configurations, [])
        self.assertIsInstance(
            window._route_run_execution.snapshot().thread,
            _FakeAliveThread,
        )

    def test_route_start_rejects_disconnected_serial_with_existing_status_timeout(
        self,
    ) -> None:
        window, statuses, _telegrams, _dialog, lcr, _camera_calls = (
            _make_route_start_main(serial_open=False)
        )

        Main._start_route_measurement(window, _route_start_configuration())

        self.assertEqual(
            statuses,
            [("Connect the stage controller before measuring a route.", 5000)],
        )
        self.assertEqual(lcr.configurations, [])
        self.assertFalse(window._route_run_execution.snapshot().active)

    def test_photo_route_without_objective_scale_does_not_configure_meter_or_start(
        self,
    ) -> None:
        window, statuses, _telegrams, dialog, lcr, camera_calls = (
            _make_route_start_main(objective_scale=None)
        )
        configuration = _route_start_configuration(
            operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        )

        Main._start_route_measurement(window, configuration)

        message = (
            "Calibrate click-to-move for the active objective before saving "
            "microscope photos with a scale bar."
        )
        self.assertEqual(statuses, [(message, 8000)])
        self.assertEqual(dialog.statuses, [message])
        self.assertEqual(camera_calls, [])
        self.assertEqual(lcr.configurations, [])
        self.assertFalse(window._route_run_execution.snapshot().active)

    def test_immediate_photo_route_without_camera_frame_reports_telegram_failure(
        self,
    ) -> None:
        window, statuses, telegrams, dialog, lcr, camera_calls = _make_route_start_main(
            camera_frame=None
        )
        configuration = _route_start_configuration(
            operation_mode=ROUTE_OPERATION_PHOTO,
        )

        Main._start_route_measurement(window, configuration)

        message = "Camera frame is unavailable; cannot capture route photos."
        self.assertEqual(camera_calls, [0.1])
        self.assertEqual(statuses, [(message, 8000)])
        self.assertEqual(dialog.statuses, [message])
        self.assertEqual(
            telegrams,
            [
                (
                    "route_failed",
                    (f"Probe route could not start:\n{message}",),
                    {"attach_photo": True},
                )
            ],
        )
        self.assertEqual(lcr.configurations, [])
        self.assertFalse(window._route_run_execution.snapshot().active)

    def test_immediate_autofocus_route_without_camera_frame_reports_failure(
        self,
    ) -> None:
        window, statuses, telegrams, dialog, lcr, camera_calls = _make_route_start_main(
            camera_frame=None
        )
        configuration = _route_start_configuration(photo_autofocus_enabled=True)

        Main._start_route_measurement(window, configuration)

        message = "Camera frame is unavailable; cannot autofocus route points."
        self.assertEqual(camera_calls, [0.1])
        self.assertEqual(statuses, [(message, 8000)])
        self.assertEqual(dialog.statuses, [message])
        self.assertEqual(
            telegrams,
            [
                (
                    "route_failed",
                    (f"Probe route could not start:\n{message}",),
                    {"attach_photo": True},
                )
            ],
        )
        self.assertEqual(lcr.configurations, [])
        self.assertFalse(window._route_run_execution.snapshot().active)

    def test_photo_only_route_uses_dummy_lcr_without_meter_configuration(self) -> None:
        window, _statuses, _telegrams, _dialog, lcr, _camera_calls = (
            _make_route_start_main()
        )
        configuration = _route_start_configuration(
            operation_mode=ROUTE_OPERATION_PHOTO,
        )
        original_thread = route_launch_owner.threading.Thread
        _FakeThread.instances = []
        route_launch_owner.threading.Thread = _FakeThread
        try:
            Main._start_route_measurement(window, configuration)
        finally:
            route_launch_owner.threading.Thread = original_thread

        self.assertEqual(lcr.configurations, [])
        self.assertEqual(lcr.runtime_configurations, [])
        self.assertIsNot(
            window._route_run_execution.snapshot().runner._lcr_controller,
            lcr,
        )
        self.assertEqual(len(_FakeThread.instances), 1)
        self.assertTrue(_FakeThread.instances[0].started)

    def test_gui_runner_keeps_start_frame_lineage_after_design_window_switch(
        self,
    ) -> None:
        window, _statuses, _telegrams, _dialog, _lcr, _camera_calls = (
            _make_route_start_main()
        )
        start_frame = snapshot_route_design_frame(
            frame_id="design-a",
            frame_version=4,
        )
        switched_frame = snapshot_route_design_frame(
            frame_id="design-b",
            frame_version=9,
        )
        captured: list[RouteMeasurementRunner] = []
        window._snapshot_active_route_design_frame = lambda _usability=None: start_frame
        window._start_route_measurement_runner = lambda runner, *_args, **_kwargs: (
            captured.append(runner)
        )

        Main._start_route_measurement(window, _route_start_configuration())
        window._active_route_design_frame_snapshot = switched_frame

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0].status_payload()["design_frame"],
            {"frame_id": "design-a", "frame_version": 4},
        )
        self.assertIs(captured[0].design_frame_snapshot, start_frame)

    def test_route_start_meter_setup_failure_clears_dialog_running_state(self) -> None:
        lcr = _RouteStartLcr(error=LCRMeterError("meter offline"))
        window, statuses, _telegrams, dialog, _lcr, _camera_calls = (
            _make_route_start_main(lcr_controller=lcr)
        )

        Main._start_route_measurement(window, _route_start_configuration())

        message = "Route measurement instrument setup failed: meter offline"
        self.assertEqual(statuses, [(message, 8000)])
        self.assertEqual(dialog.running, [False])
        self.assertEqual(dialog.statuses, [message])
        self.assertEqual(lcr.configurations, [])
        self.assertFalse(window._route_run_execution.snapshot().active)

    def test_prestart_route_measurement_defers_camera_frame_check(self) -> None:
        class _FakeEmit:
            def emit(self, *_args: object) -> None:
                pass

        class _FakeLcr:
            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                _configuration: RouteMeterConfiguration,
            ) -> None:
                pass

        point = RouteMeasurementPoint(
            index=1,
            point_id="p001",
            label="P001",
            design_center=(100.0, 200.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(101.0, 201.0),
            needle_2_design=(99.0, 199.0),
        )
        window = Main.__new__(Main)
        statuses: list[str] = []
        telegrams: list[tuple[object, ...]] = []
        camera_calls: list[float] = []
        dialog_calls: list[tuple[str, object]] = []
        install_route_run_execution(window)
        window.serial_connection = types.SimpleNamespace(is_open=True)
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(points=[object()], name="route"),
            registration=types.SimpleNamespace(valid=True),
        )
        _install_usable_design_frame(window)
        window._route_measurement_points = (
            lambda _route, *, frame_usability_snapshot=None: [point]
        )
        window._set_route_measurement_resume_point = lambda _point: None
        window._route_measurement_session_active = False
        window._set_route_measurement_pending = lambda _pending: None
        window._route_measurement_runtime_configuration = None
        window._save_route_measurement_session_metadata = lambda _configuration: None
        window._active_microscope_scale = lambda: None

        def wait_for_camera_frame(*, timeout_s: float = 0.1, **_kwargs):
            camera_calls.append(float(timeout_s))
            return None, None

        window._wait_for_camera_frame = wait_for_camera_frame
        window.lcr_controller = _FakeLcr()
        window.stage_controller = types.SimpleNamespace()
        window._current_needle_feedrate = lambda: None
        window.route_measurement_status = _FakeEmit()
        window.route_measurement_progress = _FakeEmit()
        window.route_measurement_recorded = _FakeEmit()
        window.route_measurement_result = _FakeEmit()
        window.route_measurement_waiting_changed = _FakeEmit()
        window.design_navigator_panel = None
        window._route_measurement_dialog = types.SimpleNamespace(
            set_running=lambda value: dialog_calls.append(("running", bool(value))),
            reset_progress=lambda total: dialog_calls.append(("progress", int(total))),
            set_status=lambda message: dialog_calls.append(("status", str(message))),
        )
        window._show_status = lambda message, *_args: statuses.append(str(message))
        window._telegram_runtime = _telegram_runtime_stub(
            send_alert=lambda *args, **kwargs: telegrams.append(tuple(args))
        )
        window._update_stage_coordinate_apply_state = lambda: None

        configuration = RouteMeasurementRunConfiguration(
            csv_path="route.csv",
            previous_csv_path="route.csv",
            operation_mode=ROUTE_OPERATION_MEASURE,
            photo_output_dir="photos",
            photo_settle_s=0.0,
            photo_autofocus_enabled=True,
            photo_autofocus_range_mm=0.03,
            initial_measurement_count=10,
            followup_measurement_count=240,
            current_point=1,
            max_relative_rms=0.01,
            contact_settle_s=0.0,
            contact_seek_range_mm=0.01,
            contact_seek_step_mm=0.001,
            previous_ok_only=False,
            meter=RouteMeterConfiguration(),
            contact_quality_limits=RouteContactQualityLimits(
                max_mad_sigma_ohm=1_500.0,
                max_p95_abs_step_ohm=2_500.0,
                max_relative_mad_sigma=0.08,
                max_relative_p95_abs_step=0.12,
            ),
        )
        original_thread = route_launch_owner.threading.Thread
        _FakeThread.instances = []
        route_launch_owner.threading.Thread = _FakeThread
        try:
            Main._start_route_measurement(
                window,
                configuration,
                wait_before_first_point=True,
            )
        finally:
            route_launch_owner.threading.Thread = original_thread

        self.assertEqual(camera_calls, [])
        self.assertEqual(telegrams, [])
        self.assertEqual(len(_FakeThread.instances), 1)
        self.assertTrue(_FakeThread.instances[0].started)
        self.assertIsNotNone(window._route_run_execution.snapshot().runner)
        self.assertEqual(
            window._route_run_execution.snapshot()
            .runner.contact_quality_limits()
            .as_dict(),
            {
                "max_mad_sigma_ohm": 1_500.0,
                "max_p95_abs_step_ohm": 2_500.0,
                "max_relative_mad_sigma": 0.08,
                "max_relative_p95_abs_step": 0.12,
            },
        )
        self.assertEqual(dialog_calls[0], ("running", True))
        self.assertFalse(
            any("Camera frame is unavailable" in status for status in statuses)
        )

    def test_submit_restarts_waiting_runner_after_route_setup_change(self) -> None:
        class _FakeRunner:
            def __init__(self, offset=(0.0, 0.0)) -> None:
                self.offset = offset
                self.updated = False
                self.applied = False
                self.confirmations: list[str] = []
                self.runtime_settings: dict[str, object] = {}
                self.stop_requested = False

            def route_offset_xy(self):
                return self.offset

            def set_route_offset_xy(self, offset: tuple[float, float]) -> None:
                self.offset = offset

            def wait_until_waiting(self, timeout_s: float) -> bool:
                _ = timeout_s
                return True

            def update_runtime_settings(self, **kwargs) -> None:
                self.updated = True
                self.runtime_settings = dict(kwargs)

            def apply_meter_configuration(self, _configuration) -> None:
                self.applied = True

            def submit_confirmation(self, action: str) -> bool:
                self.confirmations.append(str(action))
                return True

            def stop(self) -> None:
                self.stop_requested = True

        def configuration(
            *,
            previous_ok_only: bool,
            previous_csv_path: str,
        ) -> RouteMeasurementRunConfiguration:
            return RouteMeasurementRunConfiguration(
                csv_path="route.csv",
                previous_csv_path=previous_csv_path,
                operation_mode=ROUTE_OPERATION_MEASURE,
                photo_output_dir="photos",
                photo_settle_s=0.0,
                photo_autofocus_enabled=False,
                photo_autofocus_range_mm=0.03,
                initial_measurement_count=10,
                followup_measurement_count=240,
                current_point=1,
                max_relative_rms=0.01,
                contact_settle_s=0.0,
                contact_seek_range_mm=0.01,
                contact_seek_step_mm=0.001,
                previous_ok_only=previous_ok_only,
                meter=RouteMeterConfiguration(),
                contact_quality_limits=RouteContactQualityLimits(
                    max_mad_sigma_ohm=1_200.0,
                    max_p95_abs_step_ohm=1_800.0,
                    max_relative_mad_sigma=0.07,
                    max_relative_p95_abs_step=0.11,
                ),
            )

        previous = configuration(
            previous_ok_only=False,
            previous_csv_path="old.csv",
        )
        current = configuration(
            previous_ok_only=True,
            previous_csv_path="new.csv",
        )
        old_runner = _FakeRunner(offset=(0.125, -0.25))
        new_runner = _FakeRunner()
        restart_calls: list[tuple[RouteMeasurementRunConfiguration, bool]] = []
        window = Main.__new__(Main)
        activate_route_run(window, old_runner, waiting=True)
        window._route_measurement_runtime_configuration = previous
        window._route_measurement_dialog = types.SimpleNamespace(
            current_configuration=lambda: current,
            set_waiting=lambda _waiting: None,
            set_status=lambda _message: None,
        )
        window._route_contact_move_thread = None
        window._save_route_measurement_session_metadata = lambda _configuration: None
        window._show_status = lambda *_args: None
        window.design_navigator_panel = None

        def restart(
            config: RouteMeasurementRunConfiguration,
            *,
            wait_before_first_point: bool,
        ) -> None:
            restart_calls.append((config, bool(wait_before_first_point)))
            activate_route_run(window, new_runner, waiting=True)

        window._start_route_measurement = restart

        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(restart_calls, [(current, True)])
        self.assertEqual(old_runner.confirmations, [])
        self.assertTrue(old_runner.stop_requested)
        self.assertTrue(new_runner.updated)
        self.assertEqual(new_runner.offset, (0.125, -0.25))
        self.assertEqual(
            new_runner.runtime_settings["contact_quality_limits"].as_dict(),
            {
                "max_mad_sigma_ohm": 1_200.0,
                "max_p95_abs_step_ohm": 1_800.0,
                "max_relative_mad_sigma": 0.07,
                "max_relative_p95_abs_step": 0.11,
            },
        )
        self.assertTrue(new_runner.applied)
        self.assertEqual(new_runner.confirmations, ["next"])

    def test_submit_confirmation_reports_meter_configuration_failure(self) -> None:
        class _FakeRunner:
            def __init__(self) -> None:
                self.confirmations: list[str] = []
                self.runtime_settings: dict[str, object] = {}

            def update_runtime_settings(self, **kwargs) -> None:
                self.runtime_settings = dict(kwargs)

            def apply_meter_configuration(self, _configuration) -> None:
                raise LCRMeterError("meter offline")

            def submit_confirmation(self, action: str) -> bool:
                self.confirmations.append(str(action))
                return True

        configuration = RouteMeasurementRunConfiguration(
            csv_path="route.csv",
            previous_csv_path="route.csv",
            operation_mode=ROUTE_OPERATION_MEASURE,
            photo_output_dir="photos",
            photo_settle_s=0.0,
            photo_autofocus_enabled=False,
            photo_autofocus_range_mm=0.03,
            initial_measurement_count=10,
            followup_measurement_count=240,
            current_point=1,
            max_relative_rms=0.01,
            contact_settle_s=0.0,
            contact_seek_range_mm=0.01,
            contact_seek_step_mm=0.001,
            previous_ok_only=False,
            meter=RouteMeterConfiguration(),
            contact_quality_limits=RouteContactQualityLimits(
                max_mad_sigma_ohm=1_200.0,
                max_p95_abs_step_ohm=1_800.0,
                max_relative_mad_sigma=0.07,
                max_relative_p95_abs_step=0.11,
            ),
        )
        runner = _FakeRunner()
        statuses: list[str] = []
        dialog_statuses: list[str] = []
        window = Main.__new__(Main)
        activate_route_run(window, runner, waiting=True)
        window._route_measurement_runtime_configuration = configuration
        window._route_measurement_dialog = types.SimpleNamespace(
            current_configuration=lambda: configuration,
            set_waiting=lambda _waiting: None,
            set_status=lambda message: dialog_statuses.append(str(message)),
        )
        window._route_contact_move_thread = None
        window._save_route_measurement_session_metadata = lambda _configuration: None
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )
        window.design_navigator_panel = None

        Main._submit_route_measurement_confirmation(window, "next")

        self.assertEqual(
            statuses,
            ["Route measurement instrument setup failed: meter offline"],
        )
        self.assertEqual(
            dialog_statuses,
            ["Route measurement instrument setup failed: meter offline"],
        )
        self.assertEqual(runner.confirmations, [])


if __name__ == "__main__":
    unittest.main()
