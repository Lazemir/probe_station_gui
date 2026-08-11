from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from probe_station_gui.camera.exposure_policy import (
    ExposurePolicyBusyError,
    ExposurePolicyError,
)
from probe_station_gui.camera.optical_session import OpticalSessionManager
from probe_station_gui.camera.optical_calibration_lifecycle import (
    OpticalCalibrationLifecycle,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    FlatFieldCalibrationRequest,
)
from tests.camera.exposure_policy_test_support import PolicyRig


def _close_lease_while_policy_lock_is_contended(rig, lease) -> dict[str, object]:
    entered = threading.Event()
    finished = threading.Event()
    result: dict[str, object] = {}
    errors: list[Exception] = []

    def close_lease() -> None:
        entered.set()
        try:
            result.update(lease.close())
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    rig.controller._command_lock.acquire()
    thread = threading.Thread(target=close_lease, daemon=True)
    thread.start()
    assert entered.wait(1.0)
    try:
        assert not finished.wait(0.05)
    finally:
        rig.controller._command_lock.release()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert errors == []
    return result


@pytest.mark.parametrize(
    "operation",
    (
        "flat-field calibration",
        "lens distortion calibration",
        "microscope scan",
    ),
)
def test_standalone_lease_close_waits_for_policy_contention(operation: str) -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    lease = sessions.open(operation)

    result = _close_lease_while_policy_lock_is_contended(rig, lease)

    assert result["accepted"] is True
    assert rig.controller.snapshot()["session_active"] is False
    assert sessions._records == {}


def test_open_waits_for_policy_monitor_contention() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    entered = threading.Event()
    finished = threading.Event()
    leases = []
    errors: list[Exception] = []

    def open_session() -> None:
        entered.set()
        try:
            leases.append(sessions.open("route photography"))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    rig.controller._command_lock.acquire()
    thread = threading.Thread(target=open_session, daemon=True)
    thread.start()
    assert entered.wait(1.0)
    try:
        assert not finished.wait(0.05)
    finally:
        rig.controller._command_lock.release()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert errors == []
    assert len(leases) == 1
    leases[0].close()


def test_full_wizard_nested_and_outer_close_wait_for_policy_contention() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    outer = sessions.open("optical calibration")
    nested = sessions.open(
        "flat-field calibration",
        parent_token=outer.token,
    )

    nested_result = _close_lease_while_policy_lock_is_contended(rig, nested)

    assert nested_result == {"accepted": True, "nested": True}
    assert rig.controller.snapshot()["session_active"] is True
    outer_result = _close_lease_while_policy_lock_is_contended(rig, outer)
    assert outer_result["accepted"] is True
    assert rig.controller.snapshot()["session_active"] is False
    assert sessions._records == {}


def test_runtime_shutdown_is_bounded_while_real_session_lock_is_held() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    lifecycle = OpticalCalibrationLifecycle(
        sessions=sessions,
        events=SimpleNamespace(warning=lambda _message: None),
        thread_factory=threading.Thread,
    )
    request = FlatFieldCalibrationRequest(
        run_id="flat",
        wizard_run_id=17,
        objective_name="X20",
        magnification=20.0,
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
        pixel_size_mm=(0.001, 0.001),
        linear_feedrate=120.0,
        needle_feedrate=70.0,
        full_wizard=True,
    )
    child = lifecycle.open_child_session("flat-field calibration", request)
    lifecycle.close_child(child)
    release = threading.Event()
    locked = threading.Event()

    def hold_command_lock() -> None:
        rig.controller._command_lock.acquire()
        locked.set()
        release.wait(0.5)
        rig.controller._command_lock.release()

    holder = threading.Thread(target=hold_command_lock, daemon=True)
    holder.start()
    assert locked.wait(1.0)
    started = time.monotonic()
    try:
        completed = lifecycle.shutdown(0.02)
        elapsed = time.monotonic() - started
        assert lifecycle.state().parent_session_token is not None
    finally:
        release.set()
        holder.join(timeout=1.0)

    assert completed is False
    assert elapsed < 0.15
    assert lifecycle.shutdown(1.0) is True


def test_outer_session_adjusts_before_ready_and_nested_session_does_not() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    rig.controller.start()
    rig.clear_activity()

    with sessions.open("scan") as outer:
        assert rig.software_once_calls == 1
        assert rig.camera.write_calls[-1] == [("ExposureAuto", "Off")]
        assert rig.controller.snapshot()["session_active"] is True
        with sessions.open("autofocus", parent_token=outer.token):
            assert rig.software_once_calls == 1
        assert rig.controller.snapshot()["session_active"] is True

    assert rig.controller.snapshot()["session_active"] is False
    assert rig.controller.snapshot()["monitoring"] is True
    rig.controller.shutdown()


def test_session_snapshot_reports_policy_and_fixed_exposure_without_tokens() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)

    with sessions.open("optical calibration") as outer:
        outer_snapshot = outer.snapshot()
        with sessions.open("flat-field calibration", parent_token=outer.token) as nested:
            nested_snapshot = nested.snapshot()

    assert outer_snapshot == {
        "operation": "optical calibration",
        "outer_operation": "optical calibration",
        "policy": {"auto_enabled": True, "engine": "software"},
        "fixed_exposure_us": pytest.approx(1500.0),
        "adjustment": {
            "accepted": True,
            "converged": True,
            "engine": "software",
            "final_exposure_us": pytest.approx(1500.0),
            "final_frame_counter": 21,
        },
    }
    assert nested_snapshot == {
        **outer_snapshot,
        "operation": "flat-field calibration",
    }
    assert "token" not in repr(outer_snapshot).lower()


def test_unrelated_and_invalid_nested_sessions_are_rejected() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)

    with sessions.open("scan") as outer:
        with pytest.raises(ExposurePolicyBusyError, match="session"):
            sessions.open("unrelated")
        with pytest.raises(ExposurePolicyError, match="parent token"):
            sessions.open("autofocus", parent_token="wrong-token")
        nested = sessions.open("autofocus", parent_token=outer.token)
        nested.close()


def test_policy_commands_are_not_queued_during_session() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)

    with sessions.open("scan"):
        with pytest.raises(ExposurePolicyBusyError):
            rig.controller.run_once()
        with pytest.raises(ExposurePolicyBusyError):
            rig.controller.set_policy(auto_enabled=True, engine="camera")

    assert rig.software_once_calls == 2
    assert rig.camera_once_calls == 0
    assert rig.controller.snapshot()["auto_enabled"] is False


def test_failed_pre_adjustment_restores_policy_and_releases_session() -> None:
    rig = PolicyRig(auto_enabled=True, engine="camera")
    sessions = OpticalSessionManager(rig.controller)
    rig.controller.start()
    rig.clear_activity()
    rig.camera.fail_write_value = "Once"

    with pytest.raises(ExposurePolicyError, match="write failed"):
        sessions.open("scan")

    snapshot = rig.controller.snapshot()
    assert snapshot["session_active"] is False
    assert snapshot["busy"] is False
    assert rig.camera.state["ExposureAuto"] == "Continuous"
    rig.camera.fail_write_value = None
    with sessions.open("scan"):
        pass
    rig.controller.shutdown()


def test_failed_pre_adjustment_resumes_monitor_when_camera_rollback_fails() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    rig.controller.start()
    rig.clear_activity()
    rig.software_results.append(
        {"accepted": False, "converged": False, "message": "no convergence"}
    )
    rig.camera.fail_write_value = 1500.0

    with pytest.raises(ExposurePolicyError, match="restore camera exposure"):
        sessions.open("scan")

    assert rig.controller.snapshot()["monitoring"] is True
    assert rig.controller.snapshot()["session_active"] is False
    rig.controller.shutdown()


def test_manual_final_once_failure_returns_warning_without_raising() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    rig.software_results.extend(
        [
            {"accepted": True, "converged": True},
            {
                "accepted": False,
                "converged": False,
                "message": "final adjustment failed",
            },
        ]
    )

    lease = sessions.open("scan")
    result = lease.close()

    assert result["accepted"] is True
    assert "final adjustment failed" in result["warning"]
    assert rig.controller.snapshot()["warning"] == result["warning"]
    assert rig.camera.state["ExposureAuto"] == "Off"


def test_manual_hardware_final_once_restores_fixed_exposure_after_partial_mutation() -> (
    None
):
    rig = PolicyRig(auto_enabled=False, engine="camera")
    sessions = OpticalSessionManager(rig.controller)
    lease = sessions.open("scan")
    fixed_exposure = rig.camera.state["ExposureTime"]
    original_write = rig.controller._adjustment._settings_write

    def fail_after_partial_once(settings):
        if settings == [("ExposureAuto", "Once")]:
            rig.camera.state["ExposureAuto"] = "Once"
            rig.camera.state["ExposureTime"] = 2800.0
            return {"accepted": False, "message": "native Once partially failed"}
        return original_write(settings)

    rig.controller._adjustment._settings_write = fail_after_partial_once

    result = lease.close()

    assert result["accepted"] is True
    assert "native Once partially failed" in result["warning"]
    assert rig.camera.state == {
        "ExposureAuto": "Off",
        "ExposureTime": fixed_exposure,
    }


def test_manual_final_off_failure_restores_fixed_exposure_and_returns_warning() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    lease = sessions.open("scan")
    fixed_exposure = rig.camera.state["ExposureTime"]
    original_write = rig.controller._adjustment._settings_write
    fail_next_off = True

    def final_once(config=None):
        del config
        rig.camera.state["ExposureTime"] = 2600.0
        return {"accepted": True, "converged": True}

    def fail_one_off(settings):
        nonlocal fail_next_off
        if settings == [("ExposureAuto", "Off")] and fail_next_off:
            fail_next_off = False
            return {"accepted": False, "message": "final Off failed"}
        return original_write(settings)

    rig.controller._adjustment._software_once = final_once
    rig.controller._adjustment._settings_write = fail_one_off

    result = lease.close()

    assert result["accepted"] is True
    assert "final Off failed" in result["warning"]
    assert rig.camera.state == {
        "ExposureAuto": "Off",
        "ExposureTime": fixed_exposure,
    }


def test_manual_final_once_warning_includes_fixed_exposure_restore_failure() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    lease = sessions.open("scan")
    fixed_exposure = rig.camera.state["ExposureTime"]
    original_write = rig.controller._adjustment._settings_write

    def final_once(config=None):
        del config
        rig.camera.state["ExposureTime"] = 2600.0
        return {
            "accepted": False,
            "converged": False,
            "message": "final adjustment failed",
        }

    def fail_fixed_restore(settings):
        if settings == [("ExposureTime", fixed_exposure)]:
            return {"accepted": False, "message": "fixed exposure restore failed"}
        return original_write(settings)

    rig.controller._adjustment._software_once = final_once
    rig.controller._adjustment._settings_write = fail_fixed_restore

    result = lease.close()

    assert result["accepted"] is True
    assert "final adjustment failed" in result["warning"]
    assert "fixed exposure restore failed" in result["warning"]
    assert rig.camera.state["ExposureAuto"] == "Off"


def test_manual_final_once_runs_when_fixed_exposure_snapshot_read_fails() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    lease = sessions.open("scan")
    before_close = rig.software_once_calls
    original_read = rig.controller._adjustment._settings_read

    def fail_exposure_snapshot(names):
        if names == ["ExposureTime"]:
            return {"accepted": False, "message": "snapshot read failed"}
        return original_read(names)

    rig.controller._adjustment._settings_read = fail_exposure_snapshot

    result = lease.close()

    assert result["accepted"] is True
    assert rig.software_once_calls == before_close + 1
    assert "snapshot read failed" in result["warning"]
    assert rig.camera.state["ExposureAuto"] == "Off"


def test_manual_final_once_runs_when_fixed_exposure_value_is_none() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    lease = sessions.open("scan")
    before_close = rig.software_once_calls
    original_read = rig.controller._adjustment._settings_read

    def none_exposure_snapshot(names):
        if names == ["ExposureTime"]:
            return {
                "accepted": True,
                "nodes": [{"name": "ExposureTime", "value": None}],
            }
        return original_read(names)

    rig.controller._adjustment._settings_read = none_exposure_snapshot

    result = lease.close()

    assert result["accepted"] is True
    assert rig.software_once_calls == before_close + 1
    assert "unavailable" in result["warning"].lower()
    assert rig.camera.state["ExposureAuto"] == "Off"


def test_outer_lease_cannot_close_before_nested_lease() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)

    outer = sessions.open("scan")
    nested = sessions.open("autofocus", parent_token=outer.token)

    with pytest.raises(ExposurePolicyError, match="nested"):
        outer.close()

    nested.close()
    outer.close()


def test_lease_close_is_idempotent() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)

    lease = sessions.open("scan")
    first = lease.close()
    second = lease.close()

    assert first["accepted"] is True
    assert second == first
    assert rig.software_once_calls == 1


def test_closed_token_is_stale_for_direct_close_and_new_parent() -> None:
    rig = PolicyRig(auto_enabled=False, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    outer = sessions.open("scan")
    nested = sessions.open("autofocus", parent_token=outer.token)
    stale_token = nested.token

    nested.close()

    with pytest.raises(ExposurePolicyError, match="not active"):
        sessions.close(stale_token)
    with pytest.raises(ExposurePolicyError, match="parent token"):
        sessions.open("focus retry", parent_token=stale_token)

    outer.close()


def test_shutdown_rejects_active_session_then_completes_after_close() -> None:
    rig = PolicyRig(auto_enabled=True, engine="software")
    sessions = OpticalSessionManager(rig.controller)
    rig.controller.start()
    lease = sessions.open("scan")

    with pytest.raises(ExposurePolicyError, match="optical session"):
        rig.controller.shutdown(timeout_s=1.0)

    requested = rig.controller.snapshot()
    assert requested["shutdown_requested"] is True
    assert requested["shutdown_complete"] is False
    assert requested["session_active"] is True

    result = lease.close()
    rig.controller.shutdown(timeout_s=1.0)

    assert result == {"accepted": True, "nested": False}
    assert rig.controller.snapshot()["shutdown_complete"] is True
    assert rig.camera.state["ExposureAuto"] == "Off"
