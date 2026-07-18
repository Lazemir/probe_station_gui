from __future__ import annotations

import pytest

from probe_station_gui.camera.exposure_policy import (
    ExposurePolicyBusyError,
    ExposurePolicyError,
    OpticalSessionManager,
)
from tests.camera.test_exposure_policy import PolicyRig


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
    original_write = rig.controller._settings_write

    def fail_after_partial_once(settings):
        if settings == [("ExposureAuto", "Once")]:
            rig.camera.state["ExposureAuto"] = "Once"
            rig.camera.state["ExposureTime"] = 2800.0
            return {"accepted": False, "message": "native Once partially failed"}
        return original_write(settings)

    rig.controller._settings_write = fail_after_partial_once

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
    original_write = rig.controller._settings_write
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

    rig.controller._software_once = final_once
    rig.controller._settings_write = fail_one_off

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
    original_write = rig.controller._settings_write

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

    rig.controller._software_once = final_once
    rig.controller._settings_write = fail_fixed_restore

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
    original_read = rig.controller._settings_read

    def fail_exposure_snapshot(names):
        if names == ["ExposureTime"]:
            return {"accepted": False, "message": "snapshot read failed"}
        return original_read(names)

    rig.controller._settings_read = fail_exposure_snapshot

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
    original_read = rig.controller._settings_read

    def none_exposure_snapshot(names):
        if names == ["ExposureTime"]:
            return {
                "accepted": True,
                "nodes": [{"name": "ExposureTime", "value": None}],
            }
        return original_read(names)

    rig.controller._settings_read = none_exposure_snapshot

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
