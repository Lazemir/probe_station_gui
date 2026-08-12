from __future__ import annotations

import threading
import time
import unittest

try:
    from .lcr_test_support import (
        LCRMeterController,
        ROUTE_METER_GWINSTEK,
        _FakeLCRSession,
        _connect_direct,
        _replace_live_session_configuration,
        lcr_module,
    )
except ImportError:
    from lcr_test_support import (
        LCRMeterController,
        ROUTE_METER_GWINSTEK,
        _FakeLCRSession,
        _connect_direct,
        _replace_live_session_configuration,
        lcr_module,
    )


class _TrackedOutputSession(_FakeLCRSession):
    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__()
        self.events = events
        self.close_count = 0
        self.exit_count = 0

    def output(self, enabled: bool = True):
        session = self

        class _OutputContext:
            def __enter__(self):
                session.events.append(
                    (f"enter:{bool(enabled)}", threading.current_thread().name)
                )
                return session

            def __exit__(self, _exc_type, _exc, _tb) -> None:
                session.exit_count += 1
                session.events.append(("exit", threading.current_thread().name))

        return _OutputContext()

    def close(self) -> None:
        self.close_count += 1
        self.closed = True
        self.events.append(("close", threading.current_thread().name))


class _NestedOutputSession(_TrackedOutputSession):
    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__(events)
        self._context_count = 0

    def output(self, enabled: bool = True):
        session = self
        self._context_count += 1
        context_index = self._context_count

        class _OutputContext:
            def __enter__(self):
                session.events.append(
                    (
                        f"enter:{context_index}:{bool(enabled)}",
                        threading.current_thread().name,
                    )
                )
                return session

            def __exit__(self, _exc_type, _exc, _tb) -> None:
                session.exit_count += 1
                session.events.append(
                    (f"exit:{context_index}", threading.current_thread().name)
                )

        return _OutputContext()


class _BlockedExitSession(_TrackedOutputSession):
    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__(events)
        self.exit_entered = threading.Event()
        self.release_exit = threading.Event()

    def output(self, enabled: bool = True):
        session = self

        class _OutputContext:
            def __enter__(self):
                session.events.append(
                    (f"enter:{bool(enabled)}", threading.current_thread().name)
                )
                return session

            def __exit__(self, _exc_type, _exc, _tb) -> None:
                session.exit_count += 1
                session.events.append(("exit", threading.current_thread().name))
                session.exit_entered.set()
                session.release_exit.wait(timeout=5.0)

        return _OutputContext()


class _BlockedReadSession(_TrackedOutputSession):
    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__(events)
        self.read_entered = threading.Event()
        self.release_read = threading.Event()
        self.abort_count = 0

    def read_primary_value(self, *, trigger: bool = False) -> float:
        self.events.append(("read", threading.current_thread().name))
        self.read_entered.set()
        self.release_read.wait(timeout=5.0)
        return 42.0

    def abort_measurement(self) -> None:
        self.abort_count += 1
        self.events.append(("abort", threading.current_thread().name))


class _ExitFailingSession(_TrackedOutputSession):
    def output(self, enabled: bool = True):
        session = self

        class _OutputContext:
            def __enter__(self):
                session.events.append(("enter", threading.current_thread().name))
                return session

            def __exit__(self, _exc_type, _exc, _tb) -> None:
                session.exit_count += 1
                session.events.append(("exit", threading.current_thread().name))
                raise RuntimeError("output exit failed")

        return _OutputContext()


class _PartialEnterSession(_TrackedOutputSession):
    def output(self, enabled: bool = True):
        session = self

        class _OutputContext:
            def __enter__(self):
                session.events.append(("enter", threading.current_thread().name))
                raise RuntimeError("output enter failed")

            def __exit__(self, _exc_type, _exc, _tb) -> None:
                session.exit_count += 1
                session.events.append(("exit", threading.current_thread().name))

        return _OutputContext()


class _RuntimeConfigureFailure(_TrackedOutputSession):
    def configure_measurement(self, **_kwargs) -> None:
        self.events.append(("configure", threading.current_thread().name))
        raise RuntimeError("configure exploded")


class _IdentifyFailure(_TrackedOutputSession):
    backend_name = "replacement"

    def identify(self) -> str:
        self.events.append(("identify", threading.current_thread().name))
        raise RuntimeError("identify exploded")


class LCRControllerLifecycleTests(unittest.TestCase):
    def test_shutdown_inside_output_drains_context_before_session_on_worker(
        self,
    ) -> None:
        events: list[tuple[str, str]] = []
        session = _TrackedOutputSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session

        with controller.output(True):
            controller.shutdown()

        self.assertEqual(session.exit_count, 1)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(
            events,
            [
                ("enter:True", "LCRMeterWorker"),
                ("exit", "LCRMeterWorker"),
                ("close", "LCRMeterWorker"),
            ],
        )

    def test_atexit_shutdown_drains_nested_outputs_lifo_exactly_once(self) -> None:
        events: list[tuple[str, str]] = []
        session = _NestedOutputSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session

        with controller.output(True):
            with controller.output(False):
                lcr_module._shutdown_lcr_meter_controllers()

        self.assertEqual(session.exit_count, 2)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(
            events,
            [
                ("enter:1:True", "LCRMeterWorker"),
                ("enter:2:False", "LCRMeterWorker"),
                ("exit:2", "LCRMeterWorker"),
                ("exit:1", "LCRMeterWorker"),
                ("close", "LCRMeterWorker"),
            ],
        )

    def test_concurrent_atexit_owns_context_until_exit_and_preserves_body_error(
        self,
    ) -> None:
        events: list[tuple[str, str]] = []
        session = _BlockedExitSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session
        shutdown_done = threading.Event()
        body_done = threading.Event()
        body_error: list[BaseException] = []
        shutdown_thread: threading.Thread | None = None

        def run_body() -> None:
            nonlocal shutdown_thread
            try:
                with controller.output(True):
                    shutdown_thread = threading.Thread(
                        target=lambda: (
                            lcr_module._shutdown_lcr_meter_controllers(),
                            shutdown_done.set(),
                        ),
                        name="atexit-caller",
                        daemon=True,
                    )
                    shutdown_thread.start()
                    self.assertTrue(session.exit_entered.wait(timeout=1.0))
                    self.assertEqual(
                        len(controller._live_session._manual_output_contexts),
                        1,
                    )
                    self.assertFalse(shutdown_done.is_set())
                    raise ValueError("body failed")
            except BaseException as exc:
                body_error.append(exc)
            finally:
                body_done.set()

        body_thread = threading.Thread(
            target=run_body,
            name="output-body",
            daemon=True,
        )
        body_thread.start()
        try:
            self.assertTrue(body_done.wait(timeout=1.0))
            self.assertEqual(len(body_error), 1)
            self.assertIsInstance(body_error[0], ValueError)
            self.assertEqual(str(body_error[0]), "body failed")
            self.assertFalse(shutdown_done.is_set())
            self.assertEqual(
                len(controller._live_session._manual_output_contexts),
                1,
            )
        finally:
            session.release_exit.set()

        self.assertTrue(shutdown_done.wait(timeout=1.0))
        body_thread.join(timeout=1.0)
        if shutdown_thread is not None:
            shutdown_thread.join(timeout=1.0)

        self.assertEqual(session.exit_count, 1)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(
            events,
            [
                ("enter:True", "LCRMeterWorker"),
                ("exit", "LCRMeterWorker"),
                ("close", "LCRMeterWorker"),
            ],
        )

    def test_output_finalizer_does_not_hide_non_shutdown_worker_error(self) -> None:
        events: list[tuple[str, str]] = []
        session = _TrackedOutputSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session
        original_run = controller._run_on_meter_worker
        entered = False

        def fail_finish(operation):
            nonlocal entered
            if not entered:
                entered = True
                return original_run(operation)
            raise RuntimeError("Measurement instrument worker is stopping.")

        controller._run_on_meter_worker = fail_finish
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "Measurement instrument worker is stopping",
            ):
                with controller.output(True):
                    pass
        finally:
            controller._run_on_meter_worker = original_run
            controller.shutdown()

        self.assertEqual(session.exit_count, 1)
        self.assertEqual(session.close_count, 1)

    def test_disabling_live_polling_serializes_output_exit_after_inflight_read(
        self,
    ) -> None:
        events: list[tuple[str, str]] = []
        session = _BlockedReadSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session
        controller._live_session._stop_polling.clear()
        self.assertTrue(
            controller._worker_runtime.submit(controller._run_meter_poll_once)
        )
        self.assertTrue(session.read_entered.wait(timeout=1.0))

        try:
            started = time.monotonic()
            controller.set_live_polling_enabled(False)

            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(controller.live_polling_enabled())
            self.assertEqual(session.exit_count, 0)
        finally:
            session.release_read.set()
            controller.wait_until_idle(timeout_s=1.0)
            controller.shutdown()

        self.assertEqual(
            [
                (event, thread_name)
                for event, thread_name in events
                if event in {"enter:True", "read", "exit"}
            ],
            [
                ("enter:True", "LCRMeterWorker"),
                ("read", "LCRMeterWorker"),
                ("exit", "LCRMeterWorker"),
            ],
        )

    def test_blocked_read_shutdown_is_bounded_and_retires_on_worker(self) -> None:
        events: list[tuple[str, str]] = []
        session = _BlockedReadSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session
        controller._live_session._stop_polling.clear()
        summaries: list[tuple[float, bool, int]] = []
        _connect_direct(
            controller.reading_summary_updated,
            lambda value, is_short, count: summaries.append(
                (float(value), bool(is_short), int(count))
            ),
        )
        self.assertTrue(
            controller._worker_runtime.submit(controller._run_meter_poll_once)
        )
        self.assertTrue(session.read_entered.wait(timeout=1.0))
        shutdown_returned = threading.Event()
        shutdown_started = time.monotonic()
        shutdown_thread = threading.Thread(
            target=lambda: (controller.shutdown(), shutdown_returned.set()),
            name="shutdown-caller",
            daemon=True,
        )
        shutdown_thread.start()

        returned_in_time = shutdown_returned.wait(timeout=2.2)
        elapsed = time.monotonic() - shutdown_started
        if not returned_in_time:
            session.release_read.set()
            shutdown_thread.join(timeout=1.0)
            self.fail("shutdown waited behind the blocked measurement")

        self.assertLess(elapsed, 2.2)
        self.assertEqual(session.abort_count, 1)
        self.assertEqual(session.close_count, 0)
        self.assertEqual(session.exit_count, 0)

        session.release_read.set()
        shutdown_thread.join(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while session.close_count == 0 and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertEqual(summaries, [])
        self.assertEqual(session.exit_count, 1)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(
            [name for event, name in events if event in {"exit", "close"}],
            ["LCRMeterWorker", "LCRMeterWorker"],
        )
        abort_threads = [name for event, name in events if event == "abort"]
        self.assertEqual(abort_threads, ["shutdown-caller"])

    def test_output_body_failure_exits_once_and_preserves_body_error(self) -> None:
        events: list[tuple[str, str]] = []
        session = _TrackedOutputSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session

        with self.assertRaisesRegex(ValueError, "body failed"):
            with controller.output(True):
                raise ValueError("body failed")

        self.assertEqual(session.exit_count, 1)
        controller.shutdown()

    def test_partial_output_enter_failure_runs_exit_cleanup_once(self) -> None:
        events: list[tuple[str, str]] = []
        session = _PartialEnterSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session

        with self.assertRaisesRegex(RuntimeError, "output enter failed"):
            with controller.output(True):
                self.fail("body must not run")

        self.assertEqual(session.exit_count, 1)
        controller.shutdown()

    def test_disconnect_closes_session_when_live_output_exit_raises(self) -> None:
        events: list[tuple[str, str]] = []
        session = _ExitFailingSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session
        controller._run_on_meter_worker(
            lambda: controller._live_session._ensure_live_output_context(session)
        )

        controller.request_disconnect()
        self.assertTrue(controller.wait_until_idle(timeout_s=1.0))

        self.assertEqual(session.exit_count, 1)
        self.assertEqual(session.close_count, 1)
        self.assertFalse(controller.is_connected())
        controller.shutdown()

    def test_read_success_signal_order_is_stable(self) -> None:
        events: list[str] = []

        class _Session(_FakeLCRSession):
            def read_primary_value(self, *, trigger: bool = False) -> float:
                events.append("backend-read")
                return 42.0

        controller = LCRMeterController()
        controller._live_session._session = _Session()
        _connect_direct(
            controller.reading_started,
            lambda count: events.append(f"started:{int(count)}"),
        )
        _connect_direct(
            controller.reading_updated,
            lambda value, is_short: events.append(
                f"updated:{float(value)}:{bool(is_short)}"
            ),
        )
        _connect_direct(
            controller.reading_summary_updated,
            lambda value, is_short, count: events.append(
                f"summary:{float(value)}:{bool(is_short)}:{int(count)}"
            ),
        )

        self.assertEqual(controller.read_primary_value_now(), 42.0)

        self.assertEqual(
            events,
            [
                "started:1",
                "backend-read",
                "updated:42.0:False",
                "summary:42.0:False:1",
            ],
        )
        controller.shutdown()

    def test_poll_failure_signal_and_cleanup_order_is_stable(self) -> None:
        events: list[str] = []

        class _Session(_TrackedOutputSession):
            def read_primary_value(self, *, trigger: bool = False) -> float:
                events.append("backend-read")
                raise RuntimeError("poll exploded")

            def close(self) -> None:
                super().close()
                events.append("close")

        controller = LCRMeterController()
        controller._live_session._session = _Session([])
        controller._live_session._stop_polling.clear()
        _connect_direct(
            controller.status_message,
            lambda message: events.append(f"status:{message}"),
        )
        _connect_direct(
            controller.connection_changed,
            lambda connected, _backend, message: events.append(
                f"connection:{bool(connected)}:{message}"
            ),
        )

        self.assertFalse(controller._run_meter_poll_once())

        self.assertEqual(
            events,
            [
                "backend-read",
                "status:Instrument read failed: poll exploded",
                "connection:False:poll exploded",
                "close",
            ],
        )
        controller.shutdown()

    def test_same_resource_runtime_reconfigure_failure_disconnects_and_worker_survives(
        self,
    ) -> None:
        events: list[str] = []
        session_events: list[tuple[str, str]] = []
        session = _RuntimeConfigureFailure(session_events)
        controller = LCRMeterController()
        _replace_live_session_configuration(
            controller,
            meter_type=ROUTE_METER_GWINSTEK,
            resource_name="COM4",
        )
        controller._live_session._connected_resource_name = (
            controller._live_session.snapshot().configuration.connection_key
        )
        controller._live_session._session = session
        _connect_direct(
            controller.connection_changed,
            lambda connected, _backend, message: events.append(
                f"connection:{bool(connected)}:{message}"
            ),
        )
        _connect_direct(
            controller.status_message,
            lambda message: events.append(f"status:{message}"),
        )

        controller.request_reconfigure()
        self.assertTrue(controller.wait_until_idle(timeout_s=1.0))

        self.assertEqual(session.close_count, 1)
        self.assertFalse(controller.is_connected())
        self.assertEqual(
            events,
            [
                "connection:False:configure exploded",
                "status:Instrument reconfiguration failed: configure exploded",
            ],
        )
        self.assertEqual(controller._run_on_meter_worker(lambda: "alive"), "alive")
        controller.shutdown()

    def test_changed_resource_identify_failure_closes_uncommitted_replacement(
        self,
    ) -> None:
        events: list[str] = []
        old_events: list[tuple[str, str]] = []
        replacement_events: list[tuple[str, str]] = []
        old_session = _TrackedOutputSession(old_events)
        replacement = _IdentifyFailure(replacement_events)
        controller = LCRMeterController()
        _replace_live_session_configuration(
            controller,
            meter_type=ROUTE_METER_GWINSTEK,
            resource_name="COM5",
        )
        controller._live_session._connected_resource_name = "COM4"
        controller._live_session._session = old_session
        controller._live_session._session_opener = (
            lambda _configuration, *, timeout_ms: replacement
        )
        _connect_direct(controller.status_message, events.append)
        _connect_direct(
            controller.connection_changed,
            lambda connected, _backend, message: events.append(
                f"connection:{bool(connected)}:{message}"
            ),
        )

        controller.request_reconfigure()
        self.assertTrue(controller.wait_until_idle(timeout_s=1.0))

        self.assertEqual(old_session.close_count, 1)
        self.assertEqual(replacement.close_count, 1)
        self.assertFalse(controller.is_connected())
        self.assertEqual(
            events,
            [
                "Instrument connection settings changed. Reconnecting.",
                "connection:False:identify exploded",
                "Instrument reconfiguration failed: identify exploded",
            ],
        )
        controller.shutdown()

    def test_atexit_shutdown_is_weak_idempotent_and_continues_after_failure(
        self,
    ) -> None:
        calls: list[str] = []

        class _TrackedController:
            def __init__(self, name: str, *, fail: bool = False) -> None:
                self.name = name
                self.fail = fail

            def shutdown(self) -> None:
                calls.append(self.name)
                if self.fail:
                    raise RuntimeError("shutdown failed")

        first = _TrackedController("first", fail=True)
        second = _TrackedController("second")
        lcr_module._LCR_METER_CONTROLLERS.add(first)
        lcr_module._LCR_METER_CONTROLLERS.add(second)
        try:
            lcr_module._shutdown_lcr_meter_controllers()
            self.assertCountEqual(calls, ["first", "second"])
        finally:
            lcr_module._LCR_METER_CONTROLLERS.discard(first)
            lcr_module._LCR_METER_CONTROLLERS.discard(second)

        events: list[tuple[str, str]] = []
        session = _TrackedOutputSession(events)
        controller = LCRMeterController()
        controller._live_session._session = session
        controller.shutdown()
        controller.shutdown()
        lcr_module._shutdown_lcr_meter_controllers()

        self.assertEqual(session.close_count, 1)


if __name__ == "__main__":
    unittest.main()
