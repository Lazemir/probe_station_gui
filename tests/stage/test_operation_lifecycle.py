from __future__ import annotations

import threading

import pytest

from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.operation_lifecycle import StageOperationLifecycle
from probe_station_gui.stage.operation_lifecycle import StageOperationBusyError


class _BlockingSetEvent:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.set_entered = threading.Event()
        self.allow_set = threading.Event()

    def set(self) -> None:
        self.set_entered.set()
        assert self.allow_set.wait(2.0)
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def test_cancelled_accepted_calibration_candidate_wakes_waiter_and_cannot_publish() -> None:
    lifecycle = StageOperationLifecycle()
    lease = lifecycle.reserve_external("calibration")
    token = lease.token
    assert lifecycle.offer_calibration_candidate(token, "X20", "computed")
    assert lifecycle.accept_calibration_candidate(token, "approved")
    waiter_finished = threading.Event()
    waiter_results: list[bool] = []

    def wait_for_decision() -> None:
        waiter_results.append(
            lifecycle.wait_for_calibration_candidate(token, timeout_s=5.0)
        )
        waiter_finished.set()

    waiter = threading.Thread(target=wait_for_decision)
    waiter.start()
    lifecycle.cancel("cancel accepted calibration")

    assert waiter_finished.wait(1.0)
    assert waiter_results == [False]
    assert lifecycle.publish_calibration_candidate(token, lambda *_args: None) is False
    lease.release()
    waiter.join(timeout=1.0)


def test_reservation_started_during_cancel_waits_for_cancel_to_finish() -> None:
    lifecycle = StageOperationLifecycle()
    blocking_event = _BlockingSetEvent()
    lifecycle._cancel_event = blocking_event
    sequence: list[str] = []
    cancel_snapshots = []
    reservation_finished = threading.Event()

    def cancel() -> None:
        cancel_snapshots.append(lifecycle.cancel("race cancellation"))
        sequence.append("cancel returned")

    def reserve() -> None:
        with lifecycle.reserve_external("next operation"):
            sequence.append("reserved")
        reservation_finished.set()

    canceller = threading.Thread(target=cancel)
    reserver = threading.Thread(target=reserve)
    canceller.start()
    assert blocking_event.set_entered.wait(1.0)
    reserver.start()
    try:
        assert reservation_finished.wait(0.1) is False
    finally:
        blocking_event.allow_set.set()
        canceller.join(timeout=1.0)
        reserver.join(timeout=1.0)

    assert sequence == ["cancel returned", "reserved"]
    assert cancel_snapshots[0].cancelled is True
    assert lifecycle.snapshot().active is False
    assert lifecycle.snapshot().cancelled is False


def test_external_lease_releases_after_body_exception() -> None:
    lifecycle = StageOperationLifecycle()

    with pytest.raises(ValueError, match="body failed"):
        with lifecycle.reserve_external("route"):
            raise ValueError("body failed")

    assert lifecycle.snapshot().active is False


def test_idle_guard_preserves_cancellation_and_current_generation() -> None:
    lifecycle = StageOperationLifecycle()
    token = lifecycle.advance_generation("existing generation")
    lifecycle.restore_cancellation()

    with lifecycle.try_reserve_idle("read only") as lease:
        assert lease is not None
        assert lifecycle.snapshot().cancelled is True

    assert lifecycle.snapshot().cancelled is True
    lifecycle.clear_cancellation()
    assert lifecycle.token_is_current(token) is True


def test_busy_stage_rejects_external_and_background_contention() -> None:
    lifecycle = StageOperationLifecycle()
    with lifecycle.reserve_external("first"):
        with pytest.raises(StageOperationBusyError, match="Stage is busy"):
            lifecycle.reserve_external("second")
        assert lifecycle.start_background("worker", lambda: None) is None


def test_wrong_thread_cannot_release_an_external_lease() -> None:
    lifecycle = StageOperationLifecycle()
    lease = lifecycle.reserve_external("owned")
    errors: list[BaseException] = []

    contender = threading.Thread(
        target=lambda: _capture_release_error(lease.release, errors)
    )
    contender.start()
    contender.join(timeout=1.0)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert lifecycle.snapshot().active is True
    lease.release()


def _capture_release_error(
    release: object,
    errors: list[BaseException],
) -> None:
    try:
        assert callable(release)
        release()
    except BaseException as exc:
        errors.append(exc)


def test_background_worker_owns_and_releases_its_lease() -> None:
    lifecycle = StageOperationLifecycle()
    observed: list[bool] = []
    finished = threading.Event()

    def run() -> None:
        observed.append(lifecycle.snapshot().owned_by(threading.current_thread()))
        finished.set()

    lease = lifecycle.start_background("worker", run)

    assert lease is not None
    assert finished.wait(1.0)
    assert lifecycle.wait_for_active(1.0)
    assert observed == [True]
    assert lease.released is True


def test_thread_start_failure_rolls_back_without_running_target(monkeypatch) -> None:
    lifecycle = StageOperationLifecycle()
    calls: list[str] = []

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="thread start failed"):
        lifecycle.start_background("worker", lambda: calls.append("run"))

    assert calls == []
    assert lifecycle.snapshot().active is False


def test_cancelled_token_stays_stale_after_next_reservation() -> None:
    lifecycle = StageOperationLifecycle()
    first = lifecycle.reserve_external("first")
    stale = first.token
    lifecycle.cancel("cancel first")
    first.release()

    with lifecycle.reserve_external("second") as current:
        assert lifecycle.token_is_current(stale) is False
        assert lifecycle.token_is_current(current.token) is True


def test_generation_advance_invalidates_candidate_without_cancelling_owner() -> None:
    lifecycle = StageOperationLifecycle()
    lease = lifecycle.reserve_external("calibration")
    token = lease.token
    assert lifecycle.offer_calibration_candidate(token, "X20", "computed")

    current = lifecycle.advance_generation("objective changed")

    assert current is not token
    assert lifecycle.token_is_current(token) is False
    assert lifecycle.snapshot().cancelled is False
    assert lifecycle.wait_for_calibration_candidate(token, timeout_s=0.01) is False
    lease.release()


def test_conditional_cancel_rejects_stale_token_without_cancelling_new_owner() -> None:
    lifecycle = StageOperationLifecycle()
    first = lifecycle.reserve_external("first")
    stale = first.token
    first.release()

    with lifecycle.reserve_external("second") as current:
        assert lifecycle.cancel_if_current(stale, "stale request") is False
        assert lifecycle.snapshot().cancelled is False
        assert lifecycle.token_is_current(current.token) is True


def test_conditional_cancel_rejects_token_invalidated_by_prior_cancel() -> None:
    lifecycle = StageOperationLifecycle()
    lease = lifecycle.reserve_external("first")
    token = lease.token
    lifecycle.cancel("first cancel")

    assert lifecycle.cancel_if_current(token, "stale second cancel") is False

    lease.release()


def test_cancel_waits_for_inflight_publish_so_commit_never_runs_after_return() -> None:
    lifecycle = StageOperationLifecycle()
    lease = lifecycle.reserve_external("calibration")
    token = lease.token
    assert lifecycle.offer_calibration_candidate(token, "X20", "computed")
    assert lifecycle.accept_calibration_candidate(token, "approved")
    commit_started = threading.Event()
    release_commit = threading.Event()
    cancel_returned = threading.Event()
    sequence: list[str] = []
    publish_results: list[bool] = []

    def commit(_objective_name: str, _payload: object) -> None:
        commit_started.set()
        assert release_commit.wait(2.0)
        sequence.append("commit")

    publisher = threading.Thread(
        target=lambda: publish_results.append(
            lifecycle.publish_calibration_candidate(token, commit)
        )
    )
    canceller = threading.Thread(
        target=lambda: (
            lifecycle.cancel("cancel during publish"),
            sequence.append("cancel returned"),
            cancel_returned.set(),
        )
    )
    publisher.start()
    assert commit_started.wait(1.0)
    canceller.start()
    assert cancel_returned.wait(0.1) is False
    release_commit.set()
    publisher.join(timeout=1.0)
    canceller.join(timeout=1.0)

    assert sequence == ["commit", "cancel returned"]
    assert publish_results == [False]
    assert lifecycle.wait_for_calibration_candidate(token, timeout_s=0.1) is False
    lease.release()


def test_controller_external_lease_blocks_terminal_read_without_serial_access() -> None:
    controller = StageController()
    read_calls: list[int | None] = []
    controller._serial = type("Serial", (), {"is_open": True})()
    controller._fluidnc_session_for = lambda _serial: type(
        "Session",
        (),
        {
            "read_pending_output": lambda _self, max_bytes=None: (
                read_calls.append(max_bytes) or b"pending"
            )
        },
    )()

    with controller.reserve_external_task("test terminal gate"):
        assert controller.read_pending_serial_output() == b""

    assert read_calls == []
    controller.shutdown()


def test_terminal_idle_guard_blocks_automation_until_read_finishes() -> None:
    controller = StageController()
    read_started = threading.Event()
    release_read = threading.Event()
    read_finished = threading.Event()
    controller._serial = type("Serial", (), {"is_open": True})()
    controller.status_message = type("Signal", (), {"emit": lambda *_args: None})()

    class Session:
        def read_pending_output(self, *, max_bytes=None) -> bytes:
            assert max_bytes is None
            read_started.set()
            assert release_read.wait(2.0)
            return b"pending"

    controller._fluidnc_session_for = lambda _serial: Session()

    reader = threading.Thread(
        target=lambda: (
            controller.read_pending_serial_output(),
            read_finished.set(),
        )
    )
    reader.start()
    assert read_started.wait(1.0)
    try:
        assert controller._start_background_task(
            target=lambda: None,
            busy_message="busy",
        ) is False
    finally:
        release_read.set()
        reader.join(timeout=1.0)
        controller.shutdown()

    assert read_finished.is_set()


def test_terminal_idle_guard_releases_when_serial_lock_is_unavailable() -> None:
    controller = StageController()
    controller._serial = type("Serial", (), {"is_open": True})()
    read_results: list[bytes] = []
    controller._serial_session_lock.acquire()
    reader = threading.Thread(
        target=lambda: read_results.append(controller.read_pending_serial_output())
    )
    try:
        reader.start()
        reader.join(timeout=1.0)
    finally:
        controller._serial_session_lock.release()

    try:
        assert read_results == [b""]
        with controller.reserve_external_task("after failed terminal read"):
            assert controller.is_busy()
    finally:
        controller.shutdown()
