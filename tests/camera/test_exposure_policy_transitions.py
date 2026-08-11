from __future__ import annotations

import time

import pytest

from probe_station_gui.camera.exposure_policy import (
    ExposureEngine,
    ExposurePolicy,
    ExposurePolicyBusyError,
    ExposurePolicyError,
)
from probe_station_gui.camera.optical_session import OpticalSessionManager
from tests.camera.exposure_policy_test_support import PolicyRig


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

    rig.controller._adjustment._software_once = fail_once

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
