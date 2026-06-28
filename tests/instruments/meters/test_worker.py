from __future__ import annotations

import threading
import unittest

from probe_station_gui.instruments.meters.worker import (
    MeterWorkerCall,
    MeterWorkerRuntime,
    meter_worker_poll_timeout,
)


class MeterWorkerCallTests(unittest.TestCase):
    def test_worker_call_defaults_to_pending_state(self) -> None:
        call = MeterWorkerCall(func=lambda: 42)

        self.assertIsNone(call.done)
        self.assertIsNone(call.result)
        self.assertIsNone(call.error)
        self.assertEqual(call.func(), 42)

    def test_worker_call_can_hold_completion_state(self) -> None:
        done = threading.Event()
        error = RuntimeError("boom")

        call = MeterWorkerCall(
            func=lambda: None,
            done=done,
            result=12,
            error=error,
        )

        self.assertIs(call.done, done)
        self.assertEqual(call.result, 12)
        self.assertIs(call.error, error)


class MeterWorkerPollTimeoutTests(unittest.TestCase):
    def test_poll_timeout_is_disabled_without_live_polling_session_or_stop_clear(
        self,
    ) -> None:
        base_args = {
            "live_polling_enabled": True,
            "stop_polling": False,
            "has_session": True,
            "poll_interval_ms": 250,
        }

        for override in (
            {"live_polling_enabled": False},
            {"stop_polling": True},
            {"has_session": False},
        ):
            with self.subTest(override=override):
                self.assertIsNone(
                    meter_worker_poll_timeout(**(base_args | override))
                )

    def test_poll_timeout_uses_poll_interval_with_minimum(self) -> None:
        self.assertEqual(
            meter_worker_poll_timeout(
                live_polling_enabled=True,
                stop_polling=False,
                has_session=True,
                poll_interval_ms=250,
            ),
            0.25,
        )
        self.assertEqual(
            meter_worker_poll_timeout(
                live_polling_enabled=True,
                stop_polling=False,
                has_session=True,
                poll_interval_ms=20,
            ),
            0.05,
        )


class MeterWorkerRuntimeTests(unittest.TestCase):
    def test_run_executes_call_on_worker_and_waits_until_idle(self) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        try:
            thread_names: list[str] = []

            def read_value() -> int:
                thread_names.append(threading.current_thread().name)
                return 42

            self.assertEqual(runtime.run(read_value), 42)
            self.assertTrue(runtime.wait_until_idle(timeout_s=1.0, default_timeout_s=1.0))
            self.assertEqual(thread_names, ["test-meter-worker"])
        finally:
            runtime.shutdown(join_timeout_s=1.0)

    def test_run_from_worker_thread_executes_inline(self) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        try:
            def outer_call() -> str:
                return str(runtime.run(lambda: threading.current_thread().name))

            self.assertEqual(runtime.run(outer_call), "test-meter-worker")
        finally:
            runtime.shutdown(join_timeout_s=1.0)

    def test_submit_rejects_second_call_while_worker_is_busy(self) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        try:
            def blocking_call() -> None:
                calls.append("first")
                entered.set()
                release.wait(timeout=1.0)

            self.assertTrue(runtime.submit(blocking_call))
            self.assertTrue(entered.wait(timeout=1.0))
            self.assertFalse(runtime.submit(lambda: calls.append("second")))
            release.set()
            self.assertTrue(runtime.wait_until_idle(timeout_s=1.0, default_timeout_s=1.0))
            self.assertEqual(calls, ["first"])
        finally:
            runtime.shutdown(join_timeout_s=1.0)

    def test_wait_until_idle_reports_blocked_call(self) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        entered = threading.Event()
        release = threading.Event()
        try:
            def blocking_call() -> None:
                entered.set()
                release.wait(timeout=1.0)

            self.assertTrue(runtime.submit(blocking_call))
            self.assertTrue(entered.wait(timeout=1.0))
            self.assertFalse(runtime.wait_until_idle(timeout_s=0, default_timeout_s=1.0))
            release.set()
            self.assertTrue(runtime.wait_until_idle(timeout_s=1.0, default_timeout_s=1.0))
        finally:
            runtime.shutdown(join_timeout_s=1.0)

    def test_worker_polls_when_idle_timeout_expires(self) -> None:
        polled = threading.Event()
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: 0.01,
            poll_once=lambda: polled.set() or True,
        )
        try:
            runtime.wake()
            self.assertTrue(polled.wait(timeout=1.0))
        finally:
            runtime.shutdown(join_timeout_s=1.0)

    def test_request_shutdown_sets_shutdown_event(self) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )

        runtime.request_shutdown()

        self.assertTrue(runtime.shutdown_requested)
