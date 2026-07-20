from __future__ import annotations

import threading

import numpy as np
import pytest

from probe_station_gui.camera.auto_exposure import (
    AutoExposureBusyError,
    AutoExposureConfig,
    AutoExposureFrame,
    CameraAutoExposureController,
)


def test_controller_converges_with_fresh_frames_and_leaves_manual_result() -> None:
    camera = _SyntheticAutoExposureCamera()
    controller = _controller(camera)

    result = controller.run(
        AutoExposureConfig(settling_frames=0, convergence_window=2)
    )

    assert result["accepted"] is True
    assert result["converged"] is True
    assert camera.state["ExposureAuto"] == "Off"
    assert camera.state["GainAuto"] == "Off"
    assert camera.state["BalanceWhiteAuto"] == "Off"
    assert float(camera.state["Gain"]) == 0.0
    assert float(camera.state["ExposureTime"]) == pytest.approx(2000.0, rel=0.01)
    assert result["brightness_trace"][-2:] == pytest.approx([235.0, 235.0])
    assert camera.calls[0] == [
        ("ExposureAuto", "Off"),
        ("GainAuto", "Off"),
        ("BalanceWhiteAuto", "Off"),
    ]
    assert camera.calls[1][0] == ("Gain", 0.0)
    assert all(
        after_counter >= watermark
        for after_counter, watermark in camera.frame_watermarks
    )


def test_controller_drains_native_auto_frames_before_software_adjustment() -> None:
    camera = _SyntheticAutoExposureCamera()
    controller = _controller(camera)

    result = controller.run(
        AutoExposureConfig(
            native_auto_release_frames=4,
            settling_frames=0,
            convergence_window=2,
        )
    )

    assert result["accepted"] is True
    first_manual_write = camera.events.index("manual-write")
    assert camera.events[1:first_manual_write] == ["frame"] * 4


def test_controller_restores_original_state_when_frame_capture_fails() -> None:
    camera = _SyntheticAutoExposureCamera(fail_frames=True)
    original = dict(camera.state)
    controller = _controller(camera)

    result = controller.run(AutoExposureConfig(settling_frames=0))

    assert result["accepted"] is False
    assert result["converged"] is False
    assert "frame unavailable" in result["message"]
    assert camera.state == original
    assert result["restored"] is True
    assert result["failure_reason"] == "error"


def test_controller_labels_exhausted_adjustment_as_nonconvergence() -> None:
    camera = _SyntheticAutoExposureCamera()
    controller = _controller(camera)

    result = controller.run(
        AutoExposureConfig(
            settling_frames=0,
            convergence_window=3,
            max_iterations=3,
        )
    )

    assert result["accepted"] is False
    assert result["converged"] is False
    assert result["failure_reason"] == "not_converged"


def test_controller_rejects_concurrent_operation_without_waiting() -> None:
    camera = _SyntheticAutoExposureCamera()
    controller = _controller(camera)
    assert controller._operation_lock.acquire(blocking=False)
    try:
        with pytest.raises(AutoExposureBusyError, match="already running"):
            controller.run()
    finally:
        controller._operation_lock.release()


def test_config_rejects_invalid_target_percentile() -> None:
    camera = _SyntheticAutoExposureCamera()
    controller = _controller(camera)

    with pytest.raises(ValueError, match="target_percentile"):
        controller.run(AutoExposureConfig(target_percentile=0.0))


def _controller(camera: "_SyntheticAutoExposureCamera") -> CameraAutoExposureController:
    return CameraAutoExposureController(
        settings_read=camera.settings,
        settings_write=camera.update_settings,
        frame_read=camera.frame,
    )


class _SyntheticAutoExposureCamera:
    def __init__(self, *, fail_frames: bool = False) -> None:
        self.state: dict[str, str] = {
            "ExposureAuto": "Continuous",
            "ExposureTime": "3076.14",
            "GainAuto": "Continuous",
            "Gain": "1.5",
            "BalanceWhiteAuto": "Continuous",
        }
        self.counter = 10
        self.calls: list[list[tuple[str, object]]] = []
        self.frame_watermarks: list[tuple[int, int]] = []
        self.events: list[str] = []
        self.fail_frames = fail_frames
        self._last_write_counter = self.counter
        self._lock = threading.Lock()

    def settings(self, names: list[str]) -> dict[str, object]:
        nodes = []
        for name in names:
            node: dict[str, object] = {
                "name": name,
                "value": self.state[name],
                "available": True,
                "readable": True,
                "writable": True,
            }
            if name == "ExposureTime":
                node["minimum"] = 100.0
                node["maximum"] = 10000.0
            nodes.append(node)
        return {
            "accepted": True,
            "nodes": nodes,
            "frame_counter_at_completion": self.counter,
        }

    def update_settings(
        self,
        settings: list[tuple[str, object]],
    ) -> dict[str, object]:
        ordered = list(settings)
        names = {name for name, _value in ordered}
        if self.state["GainAuto"] != "Off" and "Gain" in names:
            raise RuntimeError("Gain is read-only while GainAuto is enabled")
        if self.state["ExposureAuto"] != "Off" and "ExposureTime" in names:
            raise RuntimeError("ExposureTime is read-only while ExposureAuto is enabled")
        self.calls.append(ordered)
        self.events.append(
            "manual-write"
            if any(name in {"Gain", "ExposureTime"} for name, _value in ordered)
            else "mode-write"
        )
        for name, value in ordered:
            self.state[name] = str(value)
        self.counter += 1
        self._last_write_counter = self.counter
        return {
            "accepted": True,
            "nodes": [
                {"name": name, "value": self.state[name]}
                for name, _value in ordered
            ],
            "frame_counter_at_completion": self.counter,
        }

    def frame(self, after_counter: int, timeout_s: float) -> AutoExposureFrame:
        del timeout_s
        if self.fail_frames:
            raise RuntimeError("frame unavailable")
        with self._lock:
            self.events.append("frame")
            self.frame_watermarks.append((int(after_counter), self._last_write_counter))
            self.counter = max(self.counter + 1, int(after_counter) + 1)
            exposure = float(self.state["ExposureTime"])
            brightness = int(np.clip(exposure / 2000.0 * 235.0, 1.0, 255.0))
            rgb = np.full((24, 32, 3), 60, dtype=np.uint8)
            rgb[:6, :, :] = brightness
            return AutoExposureFrame(rgb=rgb, counter=self.counter)
