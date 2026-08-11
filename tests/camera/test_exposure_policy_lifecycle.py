from __future__ import annotations

import threading
import time

import pytest

from probe_station_gui.camera.exposure_policy import ExposurePolicyError
from tests.camera.exposure_policy_test_support import PolicyRig


def test_shutdown_timeout_keeps_requested_state_and_can_be_retried() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software", brightness=230)
    rig.controller.start()
    rig.clear_activity()
    entered = threading.Event()
    release = threading.Event()
    original_frame_read = rig.controller._adjustment._frame_read

    def blocking_frame_read(after_counter: int, timeout_s: float):
        entered.set()
        assert release.wait(1.0)
        return original_frame_read(after_counter, timeout_s)

    rig.controller._adjustment._frame_read = blocking_frame_read
    rig.controller.MONITOR_INTERVAL_S = 0.01
    rig.controller.wake_monitor()
    assert entered.wait(1.0)

    shutdown_error = None
    try:
        rig.controller.shutdown(timeout_s=0.01)
    except ExposurePolicyError as exc:
        shutdown_error = exc
    if shutdown_error is None:
        release.set()
        rig.controller.shutdown(timeout_s=1.0)
    assert shutdown_error is not None
    assert "did not stop" in str(shutdown_error)

    timed_out = rig.controller.snapshot()
    assert timed_out["shutdown_requested"] is True
    assert timed_out["shutdown_complete"] is False
    assert timed_out["monitoring"] is False
    with pytest.raises(ExposurePolicyError, match="shutting down"):
        rig.controller.run_once()

    release.set()
    rig.controller.shutdown(timeout_s=1.0)

    complete = rig.controller.snapshot()
    assert complete["shutdown_requested"] is True
    assert complete["shutdown_complete"] is True
    assert rig.controller._monitor_thread is None


def test_shutdown_prevents_inflight_camera_once_from_reenabling_native_auto() -> None:
    rig = PolicyRig(auto_enabled=True, engine="camera")
    rig.controller.start()
    rig.clear_activity()
    entered = threading.Event()
    release = threading.Event()
    original_frame_read = rig.controller._adjustment._frame_read
    result: dict[str, object] = {}

    def blocking_frame_read(after_counter: int, timeout_s: float):
        entered.set()
        assert release.wait(1.0)
        return original_frame_read(after_counter, timeout_s)

    rig.controller._adjustment._frame_read = blocking_frame_read
    worker = threading.Thread(
        target=lambda: result.update(rig.controller.run_once()),
        daemon=True,
    )
    worker.start()
    assert entered.wait(1.0)

    shutdown_error = None
    try:
        rig.controller.shutdown(timeout_s=0.01)
    except ExposurePolicyError as exc:
        shutdown_error = exc
    if shutdown_error is None:
        release.set()
        worker.join(1.0)
        rig.controller.shutdown(timeout_s=1.0)
    assert shutdown_error is not None
    assert "did not stop" in str(shutdown_error)

    release.set()
    worker.join(1.0)
    assert not worker.is_alive()
    assert result["accepted"] is True
    assert rig.camera.state["ExposureAuto"] == "Off"
    rig.controller.shutdown(timeout_s=1.0)


def test_shutdown_state_callback_runs_after_controller_locks_are_released() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    lock_checks: list[tuple[bool, bool, bool]] = []

    def check_locks(state: dict[str, object]) -> None:
        command_free = rig.controller._command_lock.acquire(blocking=False)
        if command_free:
            rig.controller._command_lock.release()
        state_free = rig.controller._state_lock.acquire(blocking=False)
        if state_free:
            rig.controller._state_lock.release()
        lock_checks.append((command_free, state_free, bool(state["shutdown_complete"])))

    rig.controller.subscribe(check_locks)
    rig.controller.start()
    lock_checks.clear()

    rig.controller.shutdown(timeout_s=1.0)

    assert lock_checks[-1] == (True, True, True)


def test_shutdown_cannot_complete_in_start_monitor_publication_gap() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    emit_entered = threading.Event()
    release_emit = threading.Event()
    original_emit = rig.controller._emit_state
    start_errors: list[BaseException] = []

    def block_starter_emit() -> None:
        if threading.current_thread().name == "policy-starter":
            emit_entered.set()
            assert release_emit.wait(1.0)
        original_emit()

    rig.controller._emit_state = block_starter_emit

    def start_policy() -> None:
        try:
            rig.controller.start()
        except BaseException as exc:
            start_errors.append(exc)

    starter = threading.Thread(target=start_policy, name="policy-starter", daemon=True)
    starter.start()
    assert emit_entered.wait(1.0)

    rig.controller.shutdown(timeout_s=1.0)
    release_emit.set()
    starter.join(1.0)

    assert not starter.is_alive()
    assert start_errors == []
    assert rig.controller.snapshot()["shutdown_complete"] is True
    assert rig.controller._monitor_thread is None


def test_shutdown_final_off_wins_check_then_continuous_race() -> None:
    rig = PolicyRig(auto_enabled=True, engine="camera")
    rig.controller.start()
    rig.clear_activity()
    continuous_entered = threading.Event()
    release_continuous = threading.Event()
    original_write = rig.controller._adjustment._settings_write
    command_errors: list[BaseException] = []
    shutdown_errors: list[BaseException] = []

    def block_continuous(settings):
        if (
            settings == [("ExposureAuto", "Continuous")]
            and threading.current_thread().name == "policy-once"
        ):
            continuous_entered.set()
            assert release_continuous.wait(1.0)
        return original_write(settings)

    rig.controller._adjustment._settings_write = block_continuous

    def run_once() -> None:
        try:
            rig.controller.run_once()
        except BaseException as exc:
            command_errors.append(exc)

    command = threading.Thread(target=run_once, name="policy-once", daemon=True)
    command.start()
    assert continuous_entered.wait(1.0)

    def shutdown_policy() -> None:
        try:
            rig.controller.shutdown(timeout_s=1.0)
        except BaseException as exc:
            shutdown_errors.append(exc)

    shutdown = threading.Thread(
        target=shutdown_policy,
        name="policy-shutdown",
        daemon=True,
    )
    shutdown.start()
    deadline = time.monotonic() + 1.0
    while (
        not rig.controller.snapshot()["shutdown_requested"]
        and time.monotonic() < deadline
    ):
        time.sleep(0.001)
    assert rig.controller.snapshot()["shutdown_requested"] is True

    release_continuous.set()
    command.join(1.0)
    shutdown.join(1.0)

    assert not command.is_alive()
    assert not shutdown.is_alive()
    assert command_errors == []
    assert shutdown_errors == []
    assert rig.controller.snapshot()["shutdown_complete"] is True
    assert rig.camera.state["ExposureAuto"] == "Off"
