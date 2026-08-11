from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from probe_station_gui.camera.exposure_policy import ExposurePolicyBusyError
from probe_station_gui.camera.optical_calibration_lifecycle import (
    OpticalCalibrationLifecycle,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    LensCalibrationArtifact,
    OpticalCalibrationOutcome,
    OpticalCalibrationRuntime,
)
from tests.camera.optical_calibration_runtime_test_support import (
    _Camera,
    _DeferredThread,
    _Events,
    _InlineThread,
    _Lease,
    _Sessions,
    _Stage,
    _Store,
    _flat_request,
    _frame,
    _lens_request,
    _runtime,
    _valid_lens_payload,
)


@pytest.fixture(autouse=True)
def _surface_inline_worker_errors():
    _InlineThread.errors.clear()
    yield
    assert _InlineThread.errors == []


def test_real_worker_thread_start_is_nonblocking_and_cancel_wins_initial_capture() -> None:
    events: list[object] = []
    entered_capture = threading.Event()
    release_capture = threading.Event()
    completed = threading.Event()

    class _BlockingCamera(_Camera):
        def wait_raw(self, *, after_counter: int | None, timeout_s: float):
            entered_capture.set()
            assert release_capture.wait(1.0)
            return super().wait_raw(after_counter=after_counter, timeout_s=timeout_s)

    class _ThreadEvents(_Events):
        def complete(self, outcome) -> None:
            super().complete(outcome)
            completed.set()

    emitted = _ThreadEvents()
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_BlockingCamera(events, [_frame()]),
        sessions=_Sessions(events),
        store=_Store(events),
        events=emitted,
    )

    decision = runtime.start_flat(_flat_request())

    assert decision.accepted is True
    assert entered_capture.wait(0.5)
    assert runtime.state().active_run_id == "flat-1"
    runtime.cancel("flat-1")
    release_capture.set()
    assert completed.wait(1.0)
    assert emitted.finished[0].success is False
    assert "stopped by user" in emitted.finished[0].message
    assert [event for event in events if event[0] == "move"] == []
    assert runtime.consume(emitted.finished[0]) is False


@pytest.mark.parametrize("blocked_action", ("shutdown", "start"))
def test_real_worker_remains_tracked_until_completion_callback_returns(
    blocked_action,
) -> None:
    events: list[object] = []
    completion_entered = threading.Event()
    release_completion = threading.Event()

    class _BlockingCompletionEvents(_Events):
        def complete(self, outcome) -> None:
            completion_entered.set()
            assert release_completion.wait(2.0)
            super().complete(outcome)

    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, [_frame() for _ in range(10)]),
        sessions=_Sessions(events),
        store=_Store(events),
        events=_BlockingCompletionEvents(),
    )
    assert runtime.start_flat(_flat_request()).accepted is True
    assert completion_entered.wait(1.0)

    try:
        if blocked_action == "shutdown":
            assert runtime.shutdown(0.0) is False
        else:
            decision = runtime.start_flat(_flat_request(run_id="flat-2"))
            assert decision.accepted is False
    finally:
        release_completion.set()

    assert runtime.shutdown(1.0) is True


def test_cancel_after_finish_defers_parent_close_until_completion_returns() -> None:
    events: list[object] = []
    completion_entered = threading.Event()
    release_completion = threading.Event()
    parent_closed = threading.Event()

    class _ParentLease(_Lease):
        def close(self) -> dict[str, object]:
            result = super().close()
            if self.operation == "optical calibration":
                parent_closed.set()
            return result

    class _ParentSessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            token = f"lease-{len(self.leases) + 1}"
            lease = _ParentLease(self.events, operation, token)
            self.leases.append(lease)
            return lease

    class _BlockingCompletionEvents(_Events):
        def complete(self, outcome) -> None:
            completion_entered.set()
            assert release_completion.wait(2.0)
            super().complete(outcome)

    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, [_frame() for _ in range(10)]),
        sessions=_ParentSessions(events),
        store=_Store(events),
        events=_BlockingCompletionEvents(),
    )
    assert runtime.start_flat(_flat_request(full_wizard=True)).accepted is True
    assert completion_entered.wait(1.0)

    runtime.cancel("flat-1")
    assert parent_closed.wait(0.05) is False
    release_completion.set()

    assert parent_closed.wait(1.0) is True
    assert runtime.shutdown(1.0) is True


def test_full_wizard_reuses_parent_session_across_flat_then_lens(monkeypatch) -> None:
    events: list[object] = []
    sessions = _Sessions(events)
    runtime, emitted = _runtime(
        events,
        frames=[_frame() for _ in range(20)],
        sessions=sessions,
    )
    artifact = LensCalibrationArtifact(_valid_lens_payload(), _frame(), _frame())
    monkeypatch.setattr(
        "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
        lambda *_args, **_kwargs: artifact,
    )

    runtime.start_flat(_flat_request(full_wizard=True, wizard_run_id=17))
    parent_token = runtime.state().parent_session_token
    assert parent_token == "lease-1"
    runtime.start_lens(
        _lens_request(
            full_wizard=True, wizard_run_id=17, parent_session_token=parent_token
        )
    )

    opens = [event for event in events if event[0] == "session_open"]
    assert opens == [
        ("session_open", "optical calibration", None, "lease-1"),
        ("session_open", "flat-field calibration", "lease-1", "lease-2"),
        ("session_open", "lens distortion calibration", "lease-1", "lease-3"),
    ]
    assert events[-1] == ("session_close", "optical calibration")
    assert [item.success for item in emitted.finished] == [True, True]


def test_retained_parent_rejects_standalone_and_missing_token_continuations() -> None:
    events: list[object] = []
    runtime, _emitted = _runtime(events, frames=[_frame() for _ in range(10)])
    runtime.start_flat(_flat_request(full_wizard=True, wizard_run_id=17))
    parent_token = runtime.state().parent_session_token

    standalone = runtime.start_flat(_flat_request(run_id="standalone"))
    missing_token = runtime.start_lens(
        _lens_request(run_id="missing", full_wizard=True, wizard_run_id=17)
    )

    assert parent_token is not None
    assert standalone.accepted is False
    assert missing_token.accepted is False
    assert runtime.state().parent_session_token == parent_token


def test_cancel_between_full_wizard_phases_closes_parent_session() -> None:
    events: list[object] = []
    runtime, _emitted = _runtime(events, frames=[_frame() for _ in range(10)])
    runtime.start_flat(_flat_request(full_wizard=True, wizard_run_id=17))

    runtime.cancel("flat-1")

    assert events[-1] == ("session_close", "optical calibration")
    assert runtime.state().parent_session_token is None


def test_thread_start_failure_preserves_retained_parent() -> None:
    class _StartFailure(_InlineThread):
        def start(self) -> None:
            raise RuntimeError("thread start failed")

    events: list[object] = []
    emitted = _Events()
    sessions = _Sessions(events)
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, []),
        sessions=sessions,
        store=_Store(events),
        events=emitted,
        thread_factory=_StartFailure,
    )

    child = runtime._lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    runtime._lifecycle.close_child(child)
    parent_token = runtime.state().parent_session_token

    decision = runtime.start_lens(
        _lens_request(
            full_wizard=True,
            wizard_run_id=17,
            parent_session_token=parent_token,
        )
    )

    assert decision.accepted is False
    assert decision.status_code == 500
    assert "thread start failed" in decision.message
    assert runtime.state().active_run_id is None
    assert runtime.state().parent_session_token == parent_token


def test_stale_finish_does_not_clear_or_publish_current_run() -> None:
    events: list[object] = []
    lifecycle = OpticalCalibrationLifecycle(
        sessions=_Sessions(events),
        events=_Events(),
        thread_factory=_DeferredThread,
    )
    current = _flat_request(run_id="current")
    stale = _flat_request(run_id="stale")

    assert lifecycle.start(current, "flat", lambda _request: None).accepted is True
    assert lifecycle.finish(stale) is False
    assert lifecycle.state().active_run_id == "current"


def test_queued_outcome_is_single_use_and_new_run_or_cancel_invalidates_it() -> None:
    events: list[object] = []
    emitted = _Events()
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, []),
        sessions=_Sessions(events),
        store=_Store(events),
        events=emitted,
        thread_factory=_DeferredThread,
    )
    first = _flat_request(run_id="first")
    first_outcome = OpticalCalibrationOutcome(
        "first", None, "flat", True, "saved", "X20", False, flat_payload={}
    )
    runtime.start_flat(first)
    runtime._finish_run(first, first_outcome)

    assert runtime.consume(first_outcome) is True
    assert runtime.consume(first_outcome) is False

    second = _flat_request(run_id="second")
    second_outcome = replace(first_outcome, run_id="second")
    runtime.start_flat(second)
    runtime._finish_run(second, second_outcome)
    runtime.start_flat(_flat_request(run_id="third"))
    assert runtime.consume(second_outcome) is False

    third_outcome = replace(first_outcome, run_id="third")
    runtime._finish_run(_flat_request(run_id="third"), third_outcome)
    runtime.cancel()
    assert runtime.consume(third_outcome) is False


def test_parent_session_close_retries_exposure_policy_contention() -> None:
    class _RetryLease(_Lease):
        def __init__(self, events, operation, token) -> None:
            super().__init__(events, operation, token)
            self.close_attempts = 0

        def close(self) -> dict[str, object]:
            self.close_attempts += 1
            if self.close_attempts < 3:
                raise ExposurePolicyBusyError("busy")
            return super().close()

    class _RetrySessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            token = f"lease-{len(self.leases) + 1}"
            self.events.append(("session_open", operation, parent_token, token))
            lease = (
                _RetryLease(self.events, operation, token)
                if operation == "optical calibration"
                else _Lease(self.events, operation, token)
            )
            self.leases.append(lease)
            return lease

    events: list[object] = []
    sessions = _RetrySessions(events)
    sleeps: list[float] = []
    lifecycle = OpticalCalibrationLifecycle(
        sessions=sessions,
        events=_Events(),
        thread_factory=_InlineThread,
        sleep=sleeps.append,
    )
    request = _flat_request(full_wizard=True)
    child = lifecycle.open_child_session("flat-field calibration", request)

    assert lifecycle.close_child(child) == []
    assert lifecycle.close_parent_session() == ""
    assert sessions.leases[0].close_attempts == 3
    assert sleeps == [0.05, 0.05]


def test_parent_close_thread_start_failure_retains_session_and_warns() -> None:
    class _StartFailure:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread start failed")

        def is_alive(self) -> bool:
            return False

    events: list[object] = []
    emitted = _Events()
    lifecycle = OpticalCalibrationLifecycle(
        sessions=_Sessions(events),
        events=emitted,
        thread_factory=_StartFailure,
    )
    child = lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    lifecycle.close_child(child)
    parent_token = lifecycle.state().parent_session_token

    lifecycle.cancel()

    assert lifecycle.state().parent_session_token == parent_token
    assert emitted.finished == [
        ("warning", "Exposure policy restore could not start: thread start failed")
    ]


def test_duplicate_parent_race_closes_loser_without_holding_lifecycle_lock() -> None:
    barrier = threading.Barrier(2)
    lifecycle_holder = []

    class _RaceLease(_Lease):
        closed_without_lock = False

        def close(self) -> dict[str, object]:
            probe_finished = threading.Event()

            def probe_state() -> None:
                lifecycle_holder[0].state()
                probe_finished.set()

            probe = threading.Thread(target=probe_state, daemon=True)
            probe.start()
            self.closed_without_lock = probe_finished.wait(0.2)
            probe.join(timeout=1.0)
            if not self.closed_without_lock:
                raise AssertionError("outer lease closed while lifecycle lock was held")
            return super().close()

    class _RaceSessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            lease = _RaceLease(
                self.events,
                operation,
                f"lease-{len(self.leases) + 1}",
            )
            self.leases.append(lease)
            barrier.wait(timeout=1.0)
            return lease

    events: list[object] = []
    sessions = _RaceSessions(events)
    lifecycle = OpticalCalibrationLifecycle(
        sessions=sessions,
        events=_Events(),
        thread_factory=threading.Thread,
    )
    lifecycle_holder.append(lifecycle)
    errors = []

    def open_parent() -> None:
        try:
            lifecycle._open_parent_session()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_parent) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    loser = next(lease for lease in sessions.leases if not lease.active)
    assert loser.closed_without_lock is True


def test_shutdown_timeout_keeps_cancelled_state_and_can_be_retried() -> None:
    class _ControlledThread:
        alive = True

        def __init__(self, *, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return self.alive

        def join(self, _timeout=None) -> None:
            return None

    events: list[object] = []
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, []),
        sessions=_Sessions(events),
        store=_Store(events),
        events=_Events(),
        thread_factory=_ControlledThread,
    )
    runtime.start_flat(_flat_request())

    assert runtime.shutdown(0.0) is False
    assert runtime.state().shutdown_requested is True
    runtime._lifecycle._worker.alive = False
    assert runtime.shutdown(0.1) is True


def test_shutdown_timeout_bounds_parent_session_contention_and_can_retry() -> None:
    class _BusyLease(_Lease):
        busy = True

        def close(self) -> dict[str, object]:
            if self.busy:
                raise ExposurePolicyBusyError("busy")
            return super().close()

    class _BusySessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            token = f"lease-{len(self.leases) + 1}"
            lease = (
                _BusyLease(self.events, operation, token)
                if operation == "optical calibration"
                else _Lease(self.events, operation, token)
            )
            self.leases.append(lease)
            return lease

    class _DeferredThread:
        def __init__(self, *, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, _timeout=None) -> None:
            return None

    events: list[object] = []
    sessions = _BusySessions(events)
    lifecycle = OpticalCalibrationLifecycle(
        sessions=sessions,
        events=_Events(),
        thread_factory=threading.Thread,
    )
    child = lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    lifecycle.close_child(child)

    assert lifecycle.shutdown(0.0) is False
    sessions.leases[0].busy = False
    assert lifecycle.shutdown(0.1) is True


def test_parent_session_shutdown_close_runs_off_calling_thread() -> None:
    caller_thread_id = threading.get_ident()
    close_thread_ids: list[int] = []

    class _RecordingLease(_Lease):
        def close(self) -> dict[str, object]:
            if self.operation == "optical calibration":
                close_thread_ids.append(threading.get_ident())
            return super().close()

    class _RecordingSessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            token = f"lease-{len(self.leases) + 1}"
            lease = _RecordingLease(self.events, operation, token)
            self.leases.append(lease)
            return lease

    events: list[object] = []
    lifecycle = OpticalCalibrationLifecycle(
        sessions=_RecordingSessions(events),
        events=_Events(),
        thread_factory=threading.Thread,
    )
    child = lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    lifecycle.close_child(child)

    assert lifecycle.shutdown(1.0) is True
    assert close_thread_ids
    assert all(thread_id != caller_thread_id for thread_id in close_thread_ids)
