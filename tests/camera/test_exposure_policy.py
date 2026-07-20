from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from probe_station_gui.camera.auto_exposure import AutoExposureFrame
from probe_station_gui.camera.exposure_policy import (
    ExposureEngine,
    ExposurePolicy,
    ExposurePolicyBusyError,
    ExposurePolicyController,
    ExposurePolicyError,
    OpticalSessionManager,
)


def test_policy_model_normalizes_engine_for_persistence() -> None:
    policy = ExposurePolicy(auto_enabled=False, engine="camera")

    assert policy.engine is ExposureEngine.CAMERA
    assert policy.to_dict() == {"auto_enabled": False, "engine": "camera"}


@pytest.mark.parametrize(
    ("auto_enabled", "engine", "expected_once", "expected_native"),
    [
        (False, "software", "software", "Off"),
        (False, "camera", "camera", "Off"),
        (True, "software", "software", "Off"),
        (True, "camera", "camera", "Continuous"),
    ],
)
def test_once_uses_selected_engine_without_changing_policy(
    auto_enabled: bool,
    engine: str,
    expected_once: str,
    expected_native: str,
) -> None:
    rig = PolicyRig(auto_enabled=auto_enabled, engine=engine)
    rig.controller.start()
    rig.clear_activity()

    result = rig.controller.run_once()

    assert result["accepted"] is True
    assert result["engine"] == expected_once
    assert rig.software_once_calls == (1 if expected_once == "software" else 0)
    assert rig.camera_once_calls == (1 if expected_once == "camera" else 0)
    assert rig.camera.state["ExposureAuto"] == expected_native
    assert rig.controller.snapshot()["auto_enabled"] is auto_enabled
    assert rig.controller.snapshot()["engine"] == engine
    rig.controller.shutdown()


def test_policy_transitions_cover_manual_and_automatic_modes() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    rig.controller.start()
    rig.clear_activity()

    manual_camera = rig.controller.set_policy(auto_enabled=False, engine="camera")
    assert manual_camera["engine"] == "camera"
    assert rig.software_once_calls == 0
    assert rig.camera_once_calls == 0

    auto_camera = rig.controller.set_policy(auto_enabled=True, engine="camera")
    assert auto_camera["auto_enabled"] is True
    assert rig.camera_once_calls == 1
    assert rig.camera.state["ExposureAuto"] == "Continuous"

    writes_before_software = len(rig.camera.write_calls)
    auto_software = rig.controller.set_policy(auto_enabled=True, engine="software")
    assert auto_software["engine"] == "software"
    assert rig.software_once_calls == 1
    assert rig.software_exposure_auto_states == ["Continuous"]
    assert rig.camera.state["ExposureAuto"] == "Off"
    assert rig.camera.write_calls[writes_before_software:] == []

    manual_software = rig.controller.set_policy(
        auto_enabled=False,
        engine="software",
    )
    assert manual_software["auto_enabled"] is False
    assert rig.camera.state["ExposureAuto"] == "Off"
    assert [item.to_dict() for item in rig.persisted] == [
        {"auto_enabled": False, "engine": "camera"},
        {"auto_enabled": True, "engine": "camera"},
        {"auto_enabled": True, "engine": "software"},
        {"auto_enabled": False, "engine": "software"},
    ]
    rig.controller.shutdown()


def test_manual_exposure_write_rejects_auto_and_optical_session() -> None:
    auto_rig = PolicyRig(auto_enabled=True, engine="software")
    command_calls: list[object] = []

    with pytest.raises(ExposurePolicyError, match="automatic exposure"):
        auto_rig.controller.run_manual_exposure_write(
            lambda: command_calls.append("auto") or {"accepted": True}
        )

    manual_rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(manual_rig.controller)
    with sessions.open("scan"):
        with pytest.raises(ExposurePolicyBusyError, match="session"):
            manual_rig.controller.run_manual_exposure_write(
                lambda: command_calls.append("session") or {"accepted": True}
            )

    assert command_calls == []


def test_callback_can_unsubscribe_itself_without_deadlocking() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    callbacks: list[dict[str, object]] = []

    def callback(state: dict[str, object]) -> None:
        callbacks.append(state)
        rig.controller.unsubscribe(callback)

    rig.controller.subscribe(callback)
    rig.controller.set_policy(auto_enabled=False, engine="camera")
    rig.controller.set_policy(auto_enabled=False, engine="software")

    assert [state["engine"] for state in callbacks] == ["camera"]


def test_software_monitor_checks_brightness_before_adjusting() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software", brightness=230)
    rig.controller.start()
    rig.clear_activity()

    rig.advance_monitor_interval()

    assert rig.camera.frame_reads == 1
    assert rig.software_once_calls == 0
    rig.controller.shutdown()


@pytest.mark.parametrize("brightness", [222, 248])
def test_software_monitor_adjusts_only_beyond_five_percent_drift(
    brightness: int,
) -> None:
    rig = PolicyRig(auto_enabled=True, engine="software", brightness=brightness)
    rig.controller.start()
    rig.clear_activity()

    rig.advance_monitor_interval()

    assert rig.camera.frame_reads >= 1
    assert rig.software_once_calls == 1
    assert rig.software_configs[-1].target_tolerance_fraction == pytest.approx(0.02)
    rig.controller.shutdown()


def test_software_monitor_retries_failed_fit_only_after_scene_changes() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software", brightness=248)
    rig.controller.start()
    rig.clear_activity()
    rig.software_results.append(
        {
            "accepted": False,
            "converged": False,
            "message": "no convergence",
            "restored": True,
            "failure_reason": "not_converged",
        }
    )

    rig.advance_monitor_interval()

    assert rig.software_once_calls == 1
    rig.clear_activity()

    rig.advance_monitor_interval()

    assert rig.camera.frame_reads == 1
    assert rig.software_once_calls == 0
    assert rig.controller.snapshot()["warning"] == "no convergence"
    rig.clear_activity()
    rig.camera.brightness = 180

    rig.advance_monitor_interval()

    assert rig.software_once_calls == 1
    rig.controller.shutdown()


def test_failed_manual_once_does_not_trigger_an_unchanged_monitor_retry() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software", brightness=248)
    rig.controller.start()
    rig.clear_activity()
    rig.software_results.append(
        {
            "accepted": False,
            "converged": False,
            "message": "no convergence",
            "restored": True,
            "failure_reason": "not_converged",
        }
    )

    with pytest.raises(ExposurePolicyError, match="no convergence"):
        rig.controller.run_once()
    rig.clear_activity()

    rig.advance_monitor_interval()

    assert rig.camera.frame_reads == 1
    assert rig.software_once_calls == 0
    assert rig.controller.snapshot()["warning"] == "no convergence"
    rig.controller.shutdown()


def test_hardware_once_waits_for_off_and_a_newer_raw_frame() -> None:
    rig = PolicyRig(auto_enabled=False, engine="camera")
    rig.camera.hardware_once_reads_before_off = 2
    rig.controller.start()
    rig.clear_activity()

    result = rig.controller.run_once()

    assert rig.camera.exposure_auto_reads >= 3
    assert rig.camera.frame_watermarks[-1] == rig.camera.once_completion_counter
    assert result["final_frame_counter"] > rig.camera.once_completion_counter
    assert result["final_exposure_us"] == pytest.approx(1500.0)
    rig.controller.shutdown()


def test_failed_transition_rolls_back_camera_and_persisted_policy() -> None:
    rig = PolicyRig(auto_enabled=True, engine="camera")
    rig.controller.start()
    rig.clear_activity()
    rig.software_results.append(
        {"accepted": False, "converged": False, "message": "no convergence"}
    )

    with pytest.raises(ExposurePolicyError, match="no convergence"):
        rig.controller.set_policy(auto_enabled=True, engine="software")

    assert rig.controller.snapshot()["engine"] == "camera"
    assert rig.controller.snapshot()["auto_enabled"] is True
    assert rig.camera.state["ExposureAuto"] == "Continuous"
    assert rig.persisted == []
    rig.controller.shutdown()


def test_nonconverged_software_auto_transition_keeps_selected_policy() -> None:
    rig = PolicyRig(auto_enabled=True, engine="camera", brightness=248)
    rig.controller.start()
    rig.clear_activity()
    rig.software_results.append(
        {
            "accepted": False,
            "converged": False,
            "message": "no convergence",
            "restored": True,
            "failure_reason": "not_converged",
        }
    )

    result = rig.controller.set_policy(auto_enabled=True, engine="software")

    assert result["engine"] == "software"
    assert result["auto_enabled"] is True
    assert rig.controller.snapshot()["monitoring"] is True
    assert rig.controller.snapshot()["warning"] == "no convergence"
    assert rig.camera.state["ExposureAuto"] == "Off"
    assert rig.camera.write_calls == [[("ExposureAuto", "Off")]]
    assert [policy.to_dict() for policy in rig.persisted] == [
        {"auto_enabled": True, "engine": "software"}
    ]
    rig.clear_activity()

    rig.advance_monitor_interval()

    assert rig.camera.frame_reads == 1
    assert rig.software_once_calls == 0
    assert rig.controller.snapshot()["warning"] == "no convergence"
    rig.controller.shutdown()


def test_manual_policy_persistence_mutation_is_compensated() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    saved = ExposurePolicy(False, "software")
    calls: list[ExposurePolicy] = []

    def persist_then_fail(policy: ExposurePolicy) -> None:
        nonlocal saved
        saved = policy.clone()
        calls.append(policy.clone())
        if len(calls) == 1:
            raise RuntimeError("persist new policy failed")

    rig.controller._persist = persist_then_fail

    with pytest.raises(RuntimeError, match="persist new policy failed"):
        rig.controller.set_policy(auto_enabled=False, engine="camera")

    assert [policy.to_dict() for policy in calls] == [
        {"auto_enabled": False, "engine": "camera"},
        {"auto_enabled": False, "engine": "software"},
    ]
    assert saved.to_dict() == {"auto_enabled": False, "engine": "software"}
    assert rig.controller.snapshot()["engine"] == "software"
    assert rig.camera.state["ExposureAuto"] == "Off"


def test_auto_transition_persistence_mutation_restores_old_policy_and_camera() -> None:
    rig = PolicyRig(auto_enabled=True, engine="camera")
    rig.controller.start()
    rig.clear_activity()
    saved = ExposurePolicy(True, "camera")
    calls: list[ExposurePolicy] = []

    def persist_then_fail(policy: ExposurePolicy) -> None:
        nonlocal saved
        saved = policy.clone()
        calls.append(policy.clone())
        if len(calls) == 1:
            raise RuntimeError("persist new policy failed")

    rig.controller._persist = persist_then_fail

    with pytest.raises(RuntimeError, match="persist new policy failed"):
        rig.controller.set_policy(auto_enabled=True, engine="software")

    assert [policy.to_dict() for policy in calls] == [
        {"auto_enabled": True, "engine": "software"},
        {"auto_enabled": True, "engine": "camera"},
    ]
    assert saved.to_dict() == {"auto_enabled": True, "engine": "camera"}
    assert rig.controller.snapshot()["engine"] == "camera"
    assert rig.camera.state["ExposureAuto"] == "Continuous"
    rig.controller.shutdown()


def test_persistence_compensation_failure_surfaces_both_errors() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    calls: list[ExposurePolicy] = []

    def fail_both(policy: ExposurePolicy) -> None:
        calls.append(policy.clone())
        if len(calls) == 1:
            raise RuntimeError("persist new policy failed")
        raise RuntimeError("persist old policy failed")

    rig.controller._persist = fail_both

    with pytest.raises(ExposurePolicyError) as error:
        rig.controller.set_policy(auto_enabled=False, engine="camera")

    assert "persist new policy failed" in str(error.value)
    assert "persist old policy failed" in str(error.value)
    assert rig.controller.snapshot()["engine"] == "software"
    assert rig.camera.state["ExposureAuto"] == "Off"


def test_busy_command_is_rejected_without_queueing() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    assert rig.controller._command_lock.acquire(blocking=False)
    try:
        started = time.monotonic()
        with pytest.raises(ExposurePolicyBusyError, match="busy"):
            rig.controller.run_once()
        elapsed = time.monotonic() - started
    finally:
        rig.controller._command_lock.release()

    assert elapsed < 0.1
    assert rig.software_once_calls == 0


def test_invalid_engine_is_a_policy_error() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")

    with pytest.raises(ExposurePolicyError, match="Unsupported exposure engine"):
        rig.controller.set_policy(auto_enabled=True, engine="invalid")


def test_software_once_type_error_is_not_retried() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    calls = 0

    def fail_once(config=None):
        nonlocal calls
        calls += 1
        raise TypeError("software callback failed")

    rig.controller._software_once = fail_once

    with pytest.raises(TypeError, match="software callback failed"):
        rig.controller.run_once()

    assert calls == 1


def test_state_callback_runs_after_controller_locks_are_released() -> None:
    callbacks: list[dict[str, object]] = []
    lock_checks: list[tuple[bool, bool]] = []
    rig = PolicyRig(
        auto_enabled=False, engine="software", state_changed=callbacks.append
    )

    def check_locks(state: dict[str, object]) -> None:
        del state
        command_free = rig.controller._command_lock.acquire(blocking=False)
        if command_free:
            rig.controller._command_lock.release()
        state_free = rig.controller._state_lock.acquire(blocking=False)
        if state_free:
            rig.controller._state_lock.release()
        lock_checks.append((command_free, state_free))

    rig.controller.subscribe(check_locks)
    rig.controller.set_policy(auto_enabled=False, engine="camera")

    assert callbacks[-1]["engine"] == "camera"
    assert lock_checks[-1] == (True, True)


def test_shutdown_timeout_keeps_requested_state_and_can_be_retried() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software", brightness=230)
    rig.controller.start()
    rig.clear_activity()
    entered = threading.Event()
    release = threading.Event()
    original_frame_read = rig.controller._frame_read

    def blocking_frame_read(after_counter: int, timeout_s: float):
        entered.set()
        assert release.wait(1.0)
        return original_frame_read(after_counter, timeout_s)

    rig.controller._frame_read = blocking_frame_read
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
    original_frame_read = rig.controller._frame_read
    result: dict[str, object] = {}

    def blocking_frame_read(after_counter: int, timeout_s: float):
        entered.set()
        assert release.wait(1.0)
        return original_frame_read(after_counter, timeout_s)

    rig.controller._frame_read = blocking_frame_read
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
    original_write = rig.controller._settings_write
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

    rig.controller._settings_write = block_continuous

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


class PolicyRig:
    def __init__(
        self,
        *,
        auto_enabled: bool,
        engine: str,
        brightness: int = 235,
        state_changed=None,
    ) -> None:
        self.camera = FakePolicyCamera(brightness=brightness)
        self.software_once_calls = 0
        self.software_configs = []
        self.software_exposure_auto_states: list[object] = []
        self.software_results: list[dict[str, object]] = []
        self.persisted: list[ExposurePolicy] = []
        self.controller = ExposurePolicyController(
            initial_policy=ExposurePolicy(auto_enabled, engine),
            software_once=self.software_once,
            settings_read=self.camera.settings,
            settings_write=self.camera.write,
            frame_read=self.camera.frame,
            persist=self.persisted.append,
            state_changed=state_changed,
        )

    @property
    def camera_once_calls(self) -> int:
        return sum(
            1 for call in self.camera.write_calls if ("ExposureAuto", "Once") in call
        )

    def software_once(self, config=None) -> dict[str, object]:
        self.software_once_calls += 1
        self.software_configs.append(config)
        self.software_exposure_auto_states.append(self.camera.state["ExposureAuto"])
        if self.software_results:
            result = self.software_results.pop(0)
        else:
            result = {
                "accepted": True,
                "converged": True,
                "final_exposure_us": float(self.camera.state["ExposureTime"]),
                "final_frame_counter": self.camera.counter,
            }
        if bool(result.get("accepted")) and bool(result.get("converged", True)):
            self.camera.state["ExposureAuto"] = "Off"
        return result

    def clear_activity(self) -> None:
        self.software_once_calls = 0
        self.software_configs.clear()
        self.software_exposure_auto_states.clear()
        self.camera.write_calls.clear()
        self.camera.frame_reads = 0
        self.camera.frame_watermarks.clear()
        self.camera.exposure_auto_reads = 0
        self.persisted.clear()

    def advance_monitor_interval(self) -> None:
        original = self.controller.MONITOR_INTERVAL_S
        self.controller.MONITOR_INTERVAL_S = 0.01
        self.controller.wake_monitor()
        deadline = time.monotonic() + 1.0
        while self.camera.frame_reads == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert self.controller._command_lock.acquire(timeout=1.0)
        self.controller._command_lock.release()
        self.controller.MONITOR_INTERVAL_S = original


class FakePolicyCamera:
    def __init__(self, *, brightness: int) -> None:
        self.state: dict[str, object] = {
            "ExposureAuto": "Off",
            "ExposureTime": 1500.0,
        }
        self.counter = 20
        self.brightness = brightness
        self.write_calls: list[list[tuple[str, object]]] = []
        self.frame_reads = 0
        self.frame_watermarks: list[int] = []
        self.exposure_auto_reads = 0
        self.hardware_once_reads_before_off = 0
        self.once_completion_counter = 0
        self.fail_write_value: object | None = None
        self._once_reads_remaining = 0
        self._lock = threading.Lock()

    def settings(self, names: list[str]) -> dict[str, object]:
        nodes = []
        for name in names:
            if name == "ExposureAuto":
                self.exposure_auto_reads += 1
                if self._once_reads_remaining > 0:
                    value = "Once"
                    self._once_reads_remaining -= 1
                else:
                    value = self.state[name]
            else:
                value = self.state[name]
            nodes.append({"name": name, "value": value})
        return {
            "accepted": True,
            "nodes": nodes,
            "frame_counter_at_completion": self.counter,
        }

    def write(self, settings: list[tuple[str, object]]) -> dict[str, object]:
        ordered = list(settings)
        if any(value == self.fail_write_value for _name, value in ordered):
            return {"accepted": False, "message": "write failed"}
        self.write_calls.append(ordered)
        for name, value in ordered:
            if name == "ExposureAuto" and value == "Once":
                self._once_reads_remaining = self.hardware_once_reads_before_off
                self.state[name] = "Off"
            else:
                self.state[name] = value
        self.counter += 1
        if ("ExposureAuto", "Once") in ordered:
            self.once_completion_counter = self.counter
        return {
            "accepted": True,
            "nodes": [
                {"name": name, "value": self.state[name]} for name, _value in ordered
            ],
            "frame_counter_at_completion": self.counter,
        }

    def frame(self, after_counter: int, timeout_s: float) -> AutoExposureFrame:
        del timeout_s
        with self._lock:
            self.frame_reads += 1
            self.frame_watermarks.append(int(after_counter))
            self.counter = max(self.counter + 1, int(after_counter) + 1)
            rgb = np.full((8, 8, 3), self.brightness, dtype=np.uint8)
            return AutoExposureFrame(rgb=rgb, counter=self.counter)
