"""Worker queue primitives for measurement instrument controllers."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable


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

        self._ensure_started()
        self._queue.put(None)

    def run(self, func: Callable[[], object]) -> object:
        """Run a callable on the worker thread and return its result."""

        if self.is_current_worker_thread():
            return func()
        done = threading.Event()
        call = MeterWorkerCall(func=func, done=done)
        with self._state:
            self._pending_calls += 1
        self._queue.put(call)
        self._ensure_started()
        done.wait()
        if call.error is not None:
            raise call.error
        return call.result

    def submit(self, func: Callable[[], object]) -> bool:
        """Queue a background call when no other call is pending or running."""

        with self._state:
            if self._pending_calls > 0 or self._running_call:
                return False
            self._pending_calls += 1
        self._queue.put(MeterWorkerCall(func=func))
        self._ensure_started()
        return True

    def request_shutdown(self) -> None:
        """Signal the worker loop to stop and wake any blocking wait."""

        self.shutdown_event.set()
        self._queue.put(None)
        with self._state:
            self._state.notify_all()

    @property
    def shutdown_requested(self) -> bool:
        return self.shutdown_event.is_set()

    def shutdown(self, *, join_timeout_s: float | None = None) -> None:
        """Stop the worker and optionally wait for the thread to exit."""

        self.request_shutdown()
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

    def _ensure_started(self) -> None:
        with self._start_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            self.shutdown_event.clear()
            thread = threading.Thread(
                target=self._loop,
                name=self._name,
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def _loop(self) -> None:
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
