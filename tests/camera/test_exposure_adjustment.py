from __future__ import annotations

import pytest

from probe_station_gui.camera.exposure_adjustment import (
    ExposureAdjustmentController,
)
from probe_station_gui.camera.exposure_policy import ExposurePolicyError
from tests.camera.exposure_policy_test_support import FakePolicyCamera


def _adjustment(
    camera: FakePolicyCamera,
    *,
    software_results: list[dict[str, object]] | None = None,
) -> tuple[ExposureAdjustmentController, list[object]]:
    pending = list(software_results or [])
    software_configs: list[object] = []

    def software_once(config=None) -> dict[str, object]:
        software_configs.append(config)
        if pending:
            return pending.pop(0)
        return {
            "accepted": True,
            "converged": True,
            "final_exposure_us": camera.state["ExposureTime"],
            "final_frame_counter": camera.counter,
        }

    return (
        ExposureAdjustmentController(
            software_once=software_once,
            settings_read=camera.settings,
            settings_write=camera.write,
            frame_read=camera.frame,
            error_factory=ExposurePolicyError,
            shutdown_requested=lambda: False,
        ),
        software_configs,
    )


def test_recoverable_software_retry_waits_for_a_scene_change() -> None:
    camera = FakePolicyCamera(brightness=248)
    adjustment, software_configs = _adjustment(
        camera,
        software_results=[
            {
                "accepted": False,
                "converged": False,
                "message": "no convergence",
                "restored": True,
                "failure_reason": "not_converged",
            },
            {"accepted": True, "converged": True},
        ],
    )

    first = adjustment.start_software_auto()
    unchanged = adjustment.monitor_check()
    unchanged_counter = unchanged["final_frame_counter"]
    camera.brightness = 180
    changed = adjustment.monitor_check()

    assert first["warning"] == "no convergence"
    assert unchanged["retry_suppressed"] is True
    assert unchanged_counter > first.get("final_frame_counter", 0)
    assert changed["adjusted"] is True
    assert len(software_configs) == 2
    assert software_configs[-1].target_tolerance_fraction == pytest.approx(0.02)


def test_native_once_waits_for_off_then_reads_a_fresh_frame() -> None:
    camera = FakePolicyCamera(brightness=235)
    camera.hardware_once_reads_before_off = 2
    adjustment, _software_configs = _adjustment(camera)

    result = adjustment.run_once("camera")

    assert camera.write_calls[0] == [("ExposureAuto", "Once")]
    assert camera.exposure_auto_reads == 3
    assert camera.frame_watermarks[-1] == camera.once_completion_counter
    assert result["final_frame_counter"] > camera.once_completion_counter
    assert result["final_exposure_us"] == pytest.approx(1500.0)


def test_camera_restore_aggregates_each_failed_step() -> None:
    camera = FakePolicyCamera(brightness=235)
    adjustment, _software_configs = _adjustment(camera)

    def fail_restore(settings):
        value = settings[0][1]
        if value == "Off":
            return {"accepted": False, "message": "off failed"}
        if settings[0][0] == "ExposureTime":
            return {"accepted": False, "message": "time failed"}
        return {"accepted": False, "message": "native failed"}

    adjustment._settings_write = fail_restore

    with pytest.raises(ExposurePolicyError) as error:
        adjustment.restore_camera_snapshot(
            {"ExposureAuto": "Continuous", "ExposureTime": 1500.0}
        )

    assert "off failed" in str(error.value)
    assert "time failed" in str(error.value)
    assert "native failed" in str(error.value)


def test_camera_restore_preserves_empty_error_text() -> None:
    camera = FakePolicyCamera(brightness=235)
    adjustment, _software_configs = _adjustment(camera)

    def fail_with_empty_error(_settings):
        raise RuntimeError()

    adjustment._settings_write = fail_with_empty_error

    with pytest.raises(ExposurePolicyError) as error:
        adjustment.restore_camera_snapshot({"ExposureAuto": "Off"})

    assert str(error.value) == "Unable to restore camera exposure: "
