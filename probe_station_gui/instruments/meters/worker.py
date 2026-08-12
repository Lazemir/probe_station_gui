"""Worker queue primitives for measurement instrument controllers."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable


_WORKER_STOPPING_MESSAGE = "Measurement instrument worker is stopping."


@dataclass
class MeterWorkerCall:
    func: Callable[[], object]
    done: threading.Event | None = None
    result: object = None
    error: BaseException | None = None


def meter_worker_poll_timeout(
    *,
    live_polling_enabled: bool,
    stop_polling: bool,
    has_session: bool,
    poll_interval_ms: int,
) -> float | None:
    if not live_polling_enabled or stop_polling or not has_session:
        return None
    return max(0.05, poll_interval_ms / 1000.0)


class MeterWorkerRuntime:
    """Own the serialized worker thread used by a measurement instrument."""

    def __init__(
        self,
        *,
        name: str,
        poll_timeout: Callable[[], float | None],
        poll_once: Callable[[], object],
        logger: logging.Logger | None = None,
    ) -> None:
        self._name = str(name)
        self._poll_timeout = poll_timeout
        self._poll_once = poll_once
        self._logger = logger
        self._queue: queue.Queue[MeterWorkerCall | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self._state = threading.Condition()
        self._pending_calls = 0
        self._running_call = False
        self._accepting = True
        self._stopped = False
        self._retire_callbacks: list[Callable[[], object]] = []

    def wait_until_idle(
        self,
        *,
        timeout_s: float | None,
        default_timeout_s: float,
    ) -> bool:
        """Wait until queued and running calls finish."""

        if self.is_current_worker_thread():
            return True
        timeout = (
            float(default_timeout_s)
            if timeout_s is None
            else max(0.0, float(timeout_s))
        )
        deadline = time.monotonic() + timeout
        with self._state:
            while True:
                if self._pending_calls <= 0 and not self._running_call:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._state.wait(timeout=min(0.05, remaining))

    def wake(self) -> None:
        """Start the worker and wake it from a blocking queue wait."""

        with self._start_lock:
            with self._state:
                if not self._accepting:
                    return
                self._queue.put(None)
                self._ensure_started_locked()

    def run(self, func: Callable[[], object]) -> object:
        """Run a callable on the worker thread and return its result."""

        if self.is_current_worker_thread():
            return func()
        done = threading.Event()
        call = MeterWorkerCall(func=func, done=done)
        if not self._enqueue(call, reject_when_busy=False):
            raise RuntimeError(_WORKER_STOPPING_MESSAGE)
        done.wait()
        if call.error is not None:
            raise call.error
        return call.result

    def submit(
        self,
        func: Callable[[], object],
        *,
        queue_if_busy: bool = False,
    ) -> bool:
        """Queue a background call, optionally behind accepted work."""

        return self._enqueue(
            MeterWorkerCall(func=func),
            reject_when_busy=not queue_if_busy,
        )

    def request_shutdown(
        self,
        *,
        retire: Callable[[], object] | None = None,
    ) -> None:
        """Signal the worker loop to stop and wake any blocking wait."""

        cancelled: list[MeterWorkerCall] = []
        with self._start_lock:
            with self._state:
                if self._stopped:
                    return
                if retire is not None and not any(
                    callback is retire for callback in self._retire_callbacks
                ):
                    self._retire_callbacks.append(retire)
                self._accepting = False
                self.shutdown_event.set()
                cancelled = self._cancel_queued_calls_locked()
                self._queue.put(None)
                if self._thread is None:
                    if self._retire_callbacks:
                        self._ensure_started_locked(allow_stopping=True)
                    else:
                        self._stopped = True
                self._state.notify_all()
        self._finish_cancelled_calls(cancelled)

    @property
    def shutdown_requested(self) -> bool:
        return self.shutdown_event.is_set()

    def shutdown(
        self,
        *,
        join_timeout_s: float | None = None,
        retire: Callable[[], object] | None = None,
    ) -> None:
        """Stop the worker and optionally wait for the thread to exit."""

        self.request_shutdown(retire=retire)
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and not self.is_current_worker_thread()
        ):
            thread.join(timeout=join_timeout_s)

    def is_current_worker_thread(self) -> bool:
        thread = self._thread
        return thread is not None and threading.current_thread() is thread

    def has_live_worker_other_than_current(self) -> bool:
        thread = self._thread
        return (
            thread is not None
            and thread.is_alive()
            and threading.current_thread() is not thread
        )

    def _enqueue(
        self,
        call: MeterWorkerCall,
        *,
        reject_when_busy: bool,
    ) -> bool:
        with self._start_lock:
            with self._state:
                if not self._accepting:
                    return False
                if reject_when_busy and (self._pending_calls > 0 or self._running_call):
                    return False
                self._pending_calls += 1
                self._queue.put(call)
                self._ensure_started_locked()
                return True

    def _ensure_started_locked(self, *, allow_stopping: bool = False) -> None:
        thread = self._thread
        if thread is not None:
            return
        if not allow_stopping and not self._accepting:
            return
        thread = threading.Thread(
            target=self._loop,
            name=self._name,
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _loop(self) -> None:
        try:
            while not self.shutdown_event.is_set():
                timeout = self._poll_timeout()
                try:
                    call = self._queue.get(timeout=timeout)
                except queue.Empty:
                    self._run_idle_poll()
                    continue
                if call is None:
                    continue
                self._run_call(call)
        finally:
            self.shutdown_event.set()
            cancelled = self._cancel_queued_calls()
            self._finish_cancelled_calls(cancelled)
            self._finish_retirement()

    def _cancel_queued_calls(self) -> list[MeterWorkerCall]:
        with self._state:
            return self._cancel_queued_calls_locked()

    def _cancel_queued_calls_locked(self) -> list[MeterWorkerCall]:
        cancelled: list[MeterWorkerCall] = []
        while True:
            try:
                call = self._queue.get_nowait()
            except queue.Empty:
                break
            if call is None:
                continue
            call.error = RuntimeError(_WORKER_STOPPING_MESSAGE)
            self._pending_calls = max(0, self._pending_calls - 1)
            cancelled.append(call)
        return cancelled

    @staticmethod
    def _finish_cancelled_calls(calls: list[MeterWorkerCall]) -> None:
        for call in calls:
            if call.done is not None:
                call.done.set()

    def _finish_retirement(self) -> None:
        while self._run_retire_callbacks():
            pass

    def _run_retire_callbacks(self) -> bool:
        with self._state:
            callbacks = self._retire_callbacks
            self._retire_callbacks = []
            if not callbacks:
                self._accepting = False
                self._stopped = True
                self._state.notify_all()
                return False
        for callback in callbacks:
            try:
                callback()
            except BaseException:
                if self._logger is not None:
                    self._logger.exception(
                        "Measurement instrument worker retirement failed."
                    )
        return True

    def _run_idle_poll(self) -> None:
        with self._state:
            self._running_call = True
        try:
            self._poll_once()
        finally:
            with self._state:
                self._running_call = False
                self._state.notify_all()

    def _run_call(self, call: MeterWorkerCall) -> None:
        with self._state:
            self._running_call = True
        try:
            call.result = call.func()
        except BaseException as exc:
            call.error = exc
            if call.done is None and self._logger is not None:
                self._logger.exception("Measurement instrument worker task failed.")
        finally:
            if call.done is not None:
                call.done.set()
            with self._state:
                self._running_call = False
                self._pending_calls = max(0, self._pending_calls - 1)
                self._state.notify_all()
