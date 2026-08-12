from __future__ import annotations

import threading
import time
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
                self.assertIsNone(meter_worker_poll_timeout(**(base_args | override)))

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
    @staticmethod
    def _wait_for_pending_calls(
        runtime: MeterWorkerRuntime,
        expected: int,
        *,
        timeout_s: float = 1.0,
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with runtime._state:
                if runtime._pending_calls == expected:
                    return True
            time.sleep(0.005)
        return False

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
            self.assertTrue(
                runtime.wait_until_idle(timeout_s=1.0, default_timeout_s=1.0)
            )
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

    def test_synchronous_calls_run_fifo_on_one_worker_and_correlate_results(
        self,
    ) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        executions: list[tuple[int, str]] = []
        results: dict[int, object] = {}

        def invoke(index: int) -> None:
            def operation() -> str:
                executions.append((index, threading.current_thread().name))
                if index == 0:
                    first_entered.set()
                    release_first.wait(timeout=1.0)
                if index == 1:
                    raise ValueError("second failed")
                return f"result-{index}"

            try:
                results[index] = runtime.run(operation)
            except BaseException as exc:
                results[index] = exc

        callers = [
            threading.Thread(target=invoke, args=(index,), daemon=True)
            for index in range(3)
        ]
        try:
            callers[0].start()
            self.assertTrue(first_entered.wait(timeout=1.0))
            callers[1].start()
            self.assertTrue(self._wait_for_pending_calls(runtime, 2))
            callers[2].start()
            self.assertTrue(self._wait_for_pending_calls(runtime, 3))
            release_first.set()
            for caller in callers:
                caller.join(timeout=1.0)

            self.assertFalse(any(caller.is_alive() for caller in callers))
            self.assertEqual(
                executions,
                [
                    (0, "test-meter-worker"),
                    (1, "test-meter-worker"),
                    (2, "test-meter-worker"),
                ],
            )
            self.assertEqual(results[0], "result-0")
            self.assertIsInstance(results[1], ValueError)
            self.assertEqual(str(results[1]), "second failed")
            self.assertEqual(results[2], "result-2")
        finally:
            release_first.set()
            runtime.shutdown(join_timeout_s=1.0)

    def test_shutdown_cancels_queued_waiter_and_does_not_run_its_operation(
        self,
    ) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        queued_executed = threading.Event()
        queued_outcome: list[BaseException | object] = []

        def first_operation() -> None:
            first_entered.set()
            release_first.wait(timeout=1.0)

        def queued_caller() -> None:
            try:
                queued_outcome.append(
                    runtime.run(lambda: queued_executed.set() or "unexpected")
                )
            except BaseException as exc:
                queued_outcome.append(exc)

        first_caller = threading.Thread(
            target=lambda: runtime.run(first_operation), daemon=True
        )
        second_caller = threading.Thread(target=queued_caller, daemon=True)
        first_caller.start()
        self.assertTrue(first_entered.wait(timeout=1.0))
        second_caller.start()
        self.assertTrue(self._wait_for_pending_calls(runtime, 2))

        runtime.request_shutdown()
        release_first.set()
        first_caller.join(timeout=1.0)
        second_caller.join(timeout=1.0)

        self.assertFalse(second_caller.is_alive())
        self.assertFalse(queued_executed.is_set())
        self.assertEqual(len(queued_outcome), 1)
        self.assertIsInstance(queued_outcome[0], RuntimeError)
        self.assertEqual(
            str(queued_outcome[0]),
            "Measurement instrument worker is stopping.",
        )

    def test_post_shutdown_requests_reject_without_restarting_worker(self) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        self.assertEqual(runtime.run(lambda: 1), 1)
        runtime.shutdown(join_timeout_s=1.0)
        original_thread = runtime._thread

        with self.assertRaisesRegex(
            RuntimeError,
            "Measurement instrument worker is stopping",
        ):
            runtime.run(lambda: 2)
        self.assertFalse(runtime.submit(lambda: None))
        runtime.wake()

        self.assertIs(runtime._thread, original_thread)
        self.assertIsNotNone(original_thread)
        self.assertFalse(original_thread.is_alive())

    def test_shutdown_runs_retirement_once_on_worker_after_running_call(
        self,
    ) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        operation_entered = threading.Event()
        release_operation = threading.Event()
        retired = threading.Event()
        events: list[tuple[str, str]] = []

        def operation() -> None:
            events.append(("operation", threading.current_thread().name))
            operation_entered.set()
            release_operation.wait(timeout=1.0)

        def retire() -> None:
            events.append(("retire", threading.current_thread().name))
            retired.set()

        self.assertTrue(runtime.submit(operation))
        self.assertTrue(operation_entered.wait(timeout=1.0))

        runtime.shutdown(join_timeout_s=0.01, retire=retire)
        runtime.shutdown(join_timeout_s=0.01, retire=retire)

        self.assertFalse(retired.is_set())
        release_operation.set()
        self.assertTrue(retired.wait(timeout=1.0))
        runtime.shutdown(join_timeout_s=1.0, retire=retire)
        self.assertEqual(
            events,
            [
                ("operation", "test-meter-worker"),
                ("retire", "test-meter-worker"),
            ],
        )

    def test_retire_registered_during_worker_finalization_is_not_lost(
        self,
    ) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        original_batch = runtime._run_retire_callbacks
        first_batch_finished = threading.Event()
        release_finalization = threading.Event()
        retired = threading.Event()
        retire_threads: list[str] = []
        batch_count = 0

        def paused_batch() -> bool:
            nonlocal batch_count
            result = original_batch()
            batch_count += 1
            if batch_count == 1:
                first_batch_finished.set()
                release_finalization.wait(timeout=1.0)
            return result

        runtime._run_retire_callbacks = paused_batch
        self.assertEqual(runtime.run(lambda: "started"), "started")
        runtime.request_shutdown(
            retire=lambda: retire_threads.append(threading.current_thread().name)
        )
        self.assertTrue(first_batch_finished.wait(timeout=1.0))

        try:
            runtime.shutdown(
                join_timeout_s=0.01,
                retire=lambda: (
                    retire_threads.append(threading.current_thread().name),
                    retired.set(),
                ),
            )
        finally:
            release_finalization.set()

        self.assertTrue(retired.wait(timeout=1.0))
        self.assertEqual(
            retire_threads,
            ["test-meter-worker", "test-meter-worker"],
        )
        self.assertEqual(runtime._retire_callbacks, [])

        late_retired = threading.Event()
        runtime.shutdown(retire=late_retired.set)
        self.assertFalse(late_retired.is_set())

    def test_worker_shutdown_from_worker_does_not_join_itself(self) -> None:
        runtime = MeterWorkerRuntime(
            name="test-meter-worker",
            poll_timeout=lambda: None,
            poll_once=lambda: False,
        )
        outcomes: list[str] = []

        def operation() -> None:
            runtime.shutdown(join_timeout_s=1.0)
            outcomes.append("returned")

        self.assertTrue(runtime.submit(operation))
        self.assertTrue(runtime.wait_until_idle(timeout_s=1.0, default_timeout_s=1.0))
        self.assertEqual(outcomes, ["returned"])

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
            self.assertTrue(
                runtime.wait_until_idle(timeout_s=1.0, default_timeout_s=1.0)
            )
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
            self.assertFalse(
                runtime.wait_until_idle(timeout_s=0, default_timeout_s=1.0)
            )
            release.set()
            self.assertTrue(
                runtime.wait_until_idle(timeout_s=1.0, default_timeout_s=1.0)
            )
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
