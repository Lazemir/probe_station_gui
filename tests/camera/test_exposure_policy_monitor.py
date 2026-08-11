from __future__ import annotations

import pytest

from probe_station_gui.camera.exposure_policy import ExposurePolicyError
from tests.camera.exposure_policy_test_support import PolicyRig


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
