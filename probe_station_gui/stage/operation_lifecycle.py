"""Thread-owned operation leases and calibration handoff for Stage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import threading

from probe_station_gui.stage.types import StageTaskToken


class StageOperationBusyError(RuntimeError):
    """Raised when an external caller tries to reserve a busy Stage."""


@dataclass(frozen=True)
class StageOperationSnapshot:
    """Immutable view of current Stage operation ownership."""

    active: bool
    label: str | None
    owner_thread: threading.Thread | None
    token: StageTaskToken | None
    cancelled: bool

    def owned_by(self, thread: threading.Thread) -> bool:
        return self.active and self.owner_thread is thread

    @property
    def owned_by_current_thread(self) -> bool:
        return self.owned_by(threading.current_thread())


@dataclass
class _CalibrationCandidate:
    token: StageTaskToken
    objective_name: str
    payload: object
    decision: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False
    publishing: bool = False
    published: bool = False


class StageOperationLease:
    """Reservation that may be released only by its owning thread."""

    def __init__(
        self,
        lifecycle: StageOperationLifecycle,
        *,
        owner_thread: threading.Thread,
        label: str,
        token: StageTaskToken,
    ) -> None:
        self._lifecycle = lifecycle
        self._owner_thread = owner_thread
        self._label = label
        self._token = token
        self._released = False

    @property
    def owner_thread(self) -> threading.Thread:
        return self._owner_thread

    @property
    def label(self) -> str:
        return self._label

    @property
    def token(self) -> StageTaskToken:
        return self._token

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if threading.current_thread() is not self._owner_thread:
            raise RuntimeError("Only the owning thread may release a Stage operation.")
        self._lifecycle._release(self)

    def __enter__(self) -> StageOperationLease:
        if threading.current_thread() is not self._owner_thread:
            raise RuntimeError("Only the owning thread may enter a Stage operation lease.")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        self.release()
        return False


class StageOperationLifecycle:
    """Own Stage task contention, cancellation generations, and candidate handoff."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._transition_gate = threading.Lock()
        self._publish_gate = threading.Lock()
        self._cancel_event = threading.Event()
        self._active_lease: StageOperationLease | None = None
        self._generation = 0
        self._latest_token: StageTaskToken | None = None
        self._calibration_candidates: dict[int, _CalibrationCandidate] = {}

    def start_background(
        self,
        label: str,
        run: Callable[[], None],
        *,
        before_start: Callable[[], None] | None = None,
    ) -> StageOperationLease | None:
        """Reserve Stage and start ``run`` on the lease-owning worker thread."""

        lease: StageOperationLease | None = None

        def run_owned() -> None:
            assert lease is not None
            try:
                run()
            finally:
                lease.release()

        with self._transition_gate:
            with self._publish_gate:
                with self._lock:
                    if self._active_lease is not None:
                        return None
                    self._cancel_event.clear()
                    token = self._rotate_generation_locked(label)
                    worker = threading.Thread(target=run_owned, daemon=True)
                    lease = StageOperationLease(
                        self,
                        owner_thread=worker,
                        label=str(label),
                        token=token,
                    )
                    self._active_lease = lease
        try:
            if before_start is not None:
                before_start()
            worker.start()
        except BaseException:
            self._abandon_unstarted(lease)
            raise
        return lease

    def reserve_external(self, label: str) -> StageOperationLease:
        """Reserve Stage for the current thread and return an exception-safe lease."""

        with self._transition_gate:
            with self._publish_gate:
                with self._lock:
                    if self._active_lease is not None:
                        raise StageOperationBusyError(
                            f"Stage is busy. Cannot start {label}."
                        )
                    self._cancel_event.clear()
                    token = self._rotate_generation_locked(f"external:{label}")
                    lease = StageOperationLease(
                        self,
                        owner_thread=threading.current_thread(),
                        label=str(label),
                        token=token,
                    )
                    self._active_lease = lease
                    return lease

    def try_reserve_idle(self, label: str) -> StageOperationLease | None:
        """Atomically guard an idle-only foreground operation without resetting state."""

        with self._transition_gate:
            with self._publish_gate:
                with self._lock:
                    if self._active_lease is not None:
                        return None
                    token = StageTaskToken(
                        generation=self._generation,
                        source=f"idle:{label}",
                    )
                    lease = StageOperationLease(
                        self,
                        owner_thread=threading.current_thread(),
                        label=str(label),
                        token=token,
                    )
                    self._active_lease = lease
                    return lease

    def cancel(self, source: str) -> StageOperationSnapshot:
        """Invalidate the active generation and wake every candidate waiter."""

        with self._transition_gate:
            self._cancel_event.set()
            with self._publish_gate:
                with self._lock:
                    self._rotate_generation_locked(str(source))
                    return self._snapshot_locked()

    def cancel_if_current(self, token: object, source: str) -> bool:
        """Cancel only when ``token`` still owns the active operation."""

        with self._transition_gate:
            with self._lock:
                lease = self._active_lease
                if (
                    lease is None
                    or lease.token is not token
                    or self._latest_token is not token
                ):
                    return False
            self._cancel_event.set()
            with self._publish_gate:
                with self._lock:
                    self._rotate_generation_locked(str(source))
                    return True

    def snapshot(self) -> StageOperationSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def wait_for_active(self, timeout_s: float) -> bool:
        owner = self.snapshot().owner_thread
        if owner is None or owner is threading.current_thread():
            return owner is None
        if owner.is_alive():
            owner.join(timeout=max(0.0, float(timeout_s)))
        return not owner.is_alive()

    def release_current(self) -> bool:
        with self._lock:
            lease = self._active_lease
            if lease is None or lease.owner_thread is not threading.current_thread():
                return False
        lease.release()
        return True

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def wait_for_cancel(self, timeout_s: float) -> bool:
        return self._cancel_event.wait(timeout_s)

    def clear_cancellation(self) -> None:
        self._cancel_event.clear()

    def restore_cancellation(self) -> None:
        self._cancel_event.set()

    @property
    def cancellation_event(self) -> threading.Event:
        """Compatibility view for legacy tests while lifecycle retains ownership."""

        return self._cancel_event

    def token_for_current_thread(self, source: str) -> StageTaskToken:
        with self._lock:
            lease = self._active_lease
            if lease is not None and lease.owner_thread is threading.current_thread():
                return lease.token
        return self.advance_generation(source)

    def advance_generation(self, source: str) -> StageTaskToken:
        """Invalidate outstanding tokens without cancelling the active operation."""

        with self._publish_gate:
            with self._lock:
                return self._rotate_generation_locked(str(source))

    def token_is_current(self, token: object) -> bool:
        with self._lock:
            return self._token_is_current_locked(token)

    def offer_calibration_candidate(
        self,
        token: object,
        objective_name: str,
        payload: object,
    ) -> bool:
        with self._lock:
            if not self._token_is_current_locked(token):
                return False
            existing = self._calibration_candidates.get(id(token))
            if existing is not None and not existing.decision.is_set():
                return False
            assert isinstance(token, StageTaskToken)
            self._calibration_candidates[id(token)] = _CalibrationCandidate(
                token=token,
                objective_name=str(objective_name),
                payload=payload,
            )
            return True

    def accept_calibration_candidate(self, token: object, payload: object) -> bool:
        with self._lock:
            candidate = self._candidate_locked(token)
            if (
                candidate is None
                or not self._token_is_current_locked(token)
                or candidate.decision.is_set()
                or candidate.accepted
                or candidate.publishing
            ):
                return False
            candidate.payload = payload
            candidate.accepted = True
            return True

    def publish_calibration_candidate(
        self,
        token: object,
        commit: Callable[[str, object], None],
    ) -> bool:
        with self._publish_gate:
            with self._lock:
                candidate = self._candidate_locked(token)
                if (
                    candidate is None
                    or not self._token_is_current_locked(token)
                    or not candidate.accepted
                    or candidate.decision.is_set()
                    or candidate.publishing
                ):
                    return False
                candidate.publishing = True
                objective_name = candidate.objective_name
                payload = candidate.payload
            try:
                commit(objective_name, payload)
            except BaseException:
                with self._lock:
                    candidate.publishing = False
                raise
            with self._lock:
                if not self._token_is_current_locked(token):
                    candidate.publishing = False
                    return False
                candidate.publishing = False
                candidate.published = True
                candidate.decision.set()
                return True

    def reject_calibration_candidate(self, token: object) -> bool:
        with self._lock:
            candidate = self._candidate_locked(token)
            if candidate is None or candidate.publishing:
                return False
            if not candidate.decision.is_set():
                candidate.accepted = False
                candidate.decision.set()
            return True

    def wait_for_calibration_candidate(
        self,
        token: object,
        *,
        timeout_s: float,
    ) -> bool:
        with self._lock:
            candidate = self._candidate_locked(token)
            if candidate is None:
                return False
            decision = candidate.decision
        decided = decision.wait(max(0.0, float(timeout_s)))
        with self._lock:
            candidate = self._calibration_candidates.pop(id(token), None)
            return bool(
                decided
                and candidate is not None
                and candidate.token is token
                and candidate.accepted
                and candidate.published
                and self._token_is_current_locked(token)
            )

    def _release(self, lease: StageOperationLease) -> None:
        with self._lock:
            if lease._released:
                return
            if self._active_lease is not lease:
                raise RuntimeError("Stage operation lease is no longer active.")
            self._active_lease = None
            lease._released = True

    def _abandon_unstarted(self, lease: StageOperationLease) -> None:
        with self._lock:
            if self._active_lease is lease:
                self._active_lease = None
            lease._released = True

    def _rotate_generation_locked(self, source: str) -> StageTaskToken:
        for candidate in self._calibration_candidates.values():
            if not candidate.decision.is_set():
                candidate.accepted = False
                candidate.decision.set()
        self._generation += 1
        token = StageTaskToken(generation=self._generation, source=str(source))
        self._latest_token = token
        return token

    def _token_is_current_locked(self, token: object) -> bool:
        return bool(
            isinstance(token, StageTaskToken)
            and token is self._latest_token
            and not self._cancel_event.is_set()
        )

    def _candidate_locked(self, token: object) -> _CalibrationCandidate | None:
        candidate = self._calibration_candidates.get(id(token))
        if candidate is None or candidate.token is not token:
            return None
        return candidate

    def _snapshot_locked(self) -> StageOperationSnapshot:
        lease = self._active_lease
        return StageOperationSnapshot(
            active=lease is not None,
            label=None if lease is None else lease.label,
            owner_thread=None if lease is None else lease.owner_thread,
            token=None if lease is None else lease.token,
            cancelled=self._cancel_event.is_set(),
        )


__all__ = [
    "StageOperationBusyError",
    "StageOperationLease",
    "StageOperationLifecycle",
    "StageOperationSnapshot",
]
