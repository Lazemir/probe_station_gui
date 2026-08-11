from __future__ import annotations

import threading
import time

import numpy as np

from probe_station_gui.camera.auto_exposure import AutoExposureFrame
from probe_station_gui.camera.exposure_policy import (
    ExposurePolicy,
    ExposurePolicyController,
)


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
