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
