"""Thread-safe route-run control mailbox."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class _RouteRunControlMailbox:
    """Own route stop and confirmation synchronization."""

    def __init__(
        self,
        *,
        waiting_changed: Callable[[bool], None] | None = None,
    ) -> None:
        self._stop_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._interrupt_requested = threading.Event()
        self._confirmation_condition = threading.Condition()
        self._pending_confirmation: str | None = None
        self._waiting_condition = threading.Condition(threading.Lock())
        self._waiting = False
        self._waiting_changed = waiting_changed

    def request_stop(self) -> None:
        self._stop_requested.set()
        with self._confirmation_condition:
            self._confirmation_condition.notify_all()

    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def wait_for_stop(self, timeout_s: float) -> bool:
        return self._stop_requested.wait(max(0.0, float(timeout_s)))

    def request_pause(self) -> None:
        self._pause_requested.set()

    def consume_pause(self) -> bool:
        requested = self._pause_requested.is_set()
        if requested:
            self._pause_requested.clear()
        return requested

    def request_interrupt(self) -> None:
        self._interrupt_requested.set()
        with self._confirmation_condition:
            self._confirmation_condition.notify_all()

    def interrupt_requested(self) -> bool:
        return self._interrupt_requested.is_set()

    def clear_interrupt(self) -> None:
        self._interrupt_requested.clear()

    def point_stop_requested(self) -> bool:
        return self.stop_requested() or self.interrupt_requested()

    def is_waiting(self) -> bool:
        with self._waiting_condition:
            return self._waiting

    def set_waiting(self, waiting: bool) -> None:
        published = bool(waiting)
        with self._waiting_condition:
            self._waiting = published
            self._waiting_condition.notify_all()
        if self._waiting_changed is not None:
            self._waiting_changed(published)

    def wait_until_waiting(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._waiting_condition:
            while True:
                if self._waiting:
                    return True
                if self.stop_requested():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._waiting_condition.wait(timeout=min(0.05, remaining))

    def submit_confirmation(self, action: str) -> bool:
        normalized = str(action).strip().lower()
        if normalized.isdigit():
            normalized = f"jump:{int(normalized)}"
        elif normalized.startswith("jump:"):
            try:
                normalized = f"jump:{int(normalized.split(':', 1)[1].strip())}"
            except ValueError:
                return False
        elif normalized not in {"next", "measure", "remeasure", "skip"}:
            return False
        with self._confirmation_condition:
            self._pending_confirmation = normalized
            self._confirmation_condition.notify_all()
        return True

    def clear_confirmation(self) -> None:
        with self._confirmation_condition:
            self._pending_confirmation = None

    def wait_for_confirmation(self) -> str:
        with self._confirmation_condition:
            while not self._stop_requested.is_set():
                if self._pending_confirmation is not None:
                    decision = self._pending_confirmation
                    self._pending_confirmation = None
                    return decision
                self._confirmation_condition.wait(timeout=0.2)
        return "stop"
