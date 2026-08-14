"""Application-owned registration for the active Route Measurement run."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RouteRunKind(str, Enum):
    """Explicit application workflow that owns the active runner."""

    GUI = "gui"
    EXTERNAL_RESULT_SESSION = "external_result_session"


class RouteRunReleaseCause(str, Enum):
    """Reason the application is attempting to release an active run."""

    FINISHED = "finished"
    TAKEOVER = "takeover"
    FAILED_START = "failed_start"


@dataclass(frozen=True)
class RouteRunSnapshot:
    """Immutable view of the execution state owned by the slot."""

    runner: Optional[object]
    thread: Optional[object]
    kind: Optional[RouteRunKind]
    waiting: bool
    waiting_reason: str

    @property
    def active(self) -> bool:
        return self.runner is not None

    @property
    def thread_alive(self) -> bool:
        thread = self.thread
        if thread is None:
            return False
        try:
            return bool(thread.is_alive())
        except Exception:
            return False


@dataclass(frozen=True)
class RouteInterruptDirective:
    """Application work required after forwarding an Interrupt."""

    runner: Optional[object]
    cancel_stage: bool


@dataclass(frozen=True)
class RouteRunReleaseRequest:
    """Typed request to release the current runner and worker thread."""

    cause: RouteRunReleaseCause
    expected_runner: Optional[object]
    join_timeout_s: float


@dataclass(frozen=True)
class RouteRunReleaseOutcome:
    """Result of a release attempt, including both state boundaries."""

    released: bool
    stale: bool
    timed_out: bool
    rejected: bool
    prior: RouteRunSnapshot
    current: RouteRunSnapshot


class _RouteRunExecutionSlot:
    """Own runner/thread identity and published safe-waiting state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runner: Optional[object] = None
        self._thread: Optional[object] = None
        self._kind: Optional[RouteRunKind] = None
        self._waiting = False
        self._waiting_reason = ""

    def snapshot(self) -> RouteRunSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def activate(
        self,
        runner: object,
        thread: object,
        *,
        kind: RouteRunKind,
    ) -> RouteRunSnapshot:
        with self._lock:
            if self._runner is not None or self._thread is not None:
                raise RuntimeError("Route Measurement run is already active.")
            self._runner = runner
            self._thread = thread
            self._kind = kind
            self._waiting = False
            self._waiting_reason = ""
            return self._snapshot_unlocked()

    def publish_waiting(
        self,
        waiting: bool,
        *,
        expected_runner: object,
    ) -> RouteRunSnapshot:
        waiting = bool(waiting)
        with self._lock:
            runner = self._runner
            if runner is None or runner is not expected_runner:
                return self._snapshot_unlocked()
            if not waiting:
                self._waiting = False
                self._waiting_reason = ""
                return self._snapshot_unlocked()

        waiting_reason = self._waiting_reason_from_runner(runner)
        with self._lock:
            if self._runner is not runner:
                return self._snapshot_unlocked()
            self._waiting = True
            self._waiting_reason = waiting_reason
            return self._snapshot_unlocked()

    def request_interrupt(self) -> RouteInterruptDirective:
        with self._lock:
            runner = self._runner
            waiting = self._waiting
        if runner is None:
            return RouteInterruptDirective(runner=None, cancel_stage=False)
        request_correction = getattr(
            runner,
            "request_current_point_correction",
            None,
        )
        if callable(request_correction):
            request_correction()
        return RouteInterruptDirective(
            runner=runner,
            cancel_stage=not waiting,
        )

    def release(
        self,
        request: RouteRunReleaseRequest,
    ) -> RouteRunReleaseOutcome:
        with self._lock:
            prior = self._snapshot_unlocked()
            if (
                request.expected_runner is not None
                and request.expected_runner is not prior.runner
            ):
                return self._outcome_unlocked(prior, stale=True)
            if not prior.active:
                return self._outcome_unlocked(prior, rejected=True)
            if not self._release_guard_allows(request.cause, prior):
                return self._outcome_unlocked(prior, rejected=True)

        timed_out = self._perform_release_callbacks(request, prior)

        with self._lock:
            if self._runner is not prior.runner or self._thread is not prior.thread:
                return self._outcome_unlocked(prior, stale=True)
            if request.cause is RouteRunReleaseCause.TAKEOVER and timed_out:
                return self._outcome_unlocked(prior, timed_out=True)
            self._clear_unlocked()
            return self._outcome_unlocked(
                prior,
                released=True,
                timed_out=timed_out,
            )

    def _snapshot_unlocked(self) -> RouteRunSnapshot:
        return RouteRunSnapshot(
            runner=self._runner,
            thread=self._thread,
            kind=self._kind,
            waiting=self._waiting,
            waiting_reason=self._waiting_reason,
        )

    def _outcome_unlocked(
        self,
        prior: RouteRunSnapshot,
        *,
        released: bool = False,
        stale: bool = False,
        timed_out: bool = False,
        rejected: bool = False,
    ) -> RouteRunReleaseOutcome:
        return RouteRunReleaseOutcome(
            released=released,
            stale=stale,
            timed_out=timed_out,
            rejected=rejected,
            prior=prior,
            current=self._snapshot_unlocked(),
        )

    @staticmethod
    def _release_guard_allows(
        cause: RouteRunReleaseCause,
        snapshot: RouteRunSnapshot,
    ) -> bool:
        if cause is RouteRunReleaseCause.TAKEOVER:
            return snapshot.kind is RouteRunKind.GUI and snapshot.waiting
        if cause is RouteRunReleaseCause.FAILED_START:
            return snapshot.kind is RouteRunKind.EXTERNAL_RESULT_SESSION
        return cause is RouteRunReleaseCause.FINISHED

    @staticmethod
    def _perform_release_callbacks(
        request: RouteRunReleaseRequest,
        prior: RouteRunSnapshot,
    ) -> bool:
        runner = prior.runner
        thread = prior.thread
        if request.cause is RouteRunReleaseCause.FINISHED:
            if thread is not None and not thread.is_alive():
                thread.join(timeout=request.join_timeout_s)
            return False

        if request.cause is RouteRunReleaseCause.TAKEOVER:
            if runner is not None:
                runner.stop()
            if thread is None or not thread.is_alive():
                return False
            thread.join(timeout=request.join_timeout_s)
            return bool(thread.is_alive())

        if thread is None or not thread.is_alive():
            return False
        if runner is not None:
            runner.stop()
        thread.join(timeout=request.join_timeout_s)
        return bool(thread.is_alive())

    @staticmethod
    def _waiting_reason_from_runner(runner: object) -> str:
        status_payload = getattr(runner, "status_payload", None)
        if not callable(status_payload):
            return "paused"
        try:
            status = status_payload()
            waiting_reason = str(status.get("waiting_reason") or "").strip()
        except Exception:
            return "paused"
        return waiting_reason or "paused"

    def _clear_unlocked(self) -> None:
        self._runner = None
        self._thread = None
        self._kind = None
        self._waiting = False
        self._waiting_reason = ""
