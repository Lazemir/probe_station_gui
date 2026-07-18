"""Application-controlled one-shot camera exposure."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class AutoExposureConfig:
    target_percentile: float = 99.5
    target_level: float = 235.0
    saturation_level: int = 254
    min_step_ratio: float = 0.5
    max_step_ratio: float = 2.0
    clipping_reduction_ratio: float = 0.5
    settling_frames: int = 2
    convergence_window: int = 3
    target_tolerance_fraction: float = 0.02
    max_iterations: int = 12
    frame_timeout_ms: int = 2500
    fixed_gain_db: float = 0.0


@dataclass(frozen=True)
class AutoExposureFrame:
    rgb: np.ndarray
    counter: int


class AutoExposureBusyError(RuntimeError):
    """Raised when a one-shot exposure operation is already active."""


SettingsRead = Callable[[list[str]], Mapping[str, Any]]
SettingsWrite = Callable[[list[tuple[str, object]]], Mapping[str, Any]]
FrameRead = Callable[[int, float], AutoExposureFrame]


class CameraAutoExposureController:
    """Adjust exposure from fresh raw frames without changing camera transport."""

    SNAPSHOT_NODES = (
        "ExposureAuto",
        "ExposureTime",
        "GainAuto",
        "Gain",
        "BalanceWhiteAuto",
    )

    def __init__(
        self,
        *,
        settings_read: SettingsRead,
        settings_write: SettingsWrite,
        frame_read: FrameRead,
    ) -> None:
        self._settings_read = settings_read
        self._settings_write = settings_write
        self._frame_read = frame_read
        self._operation_lock = threading.Lock()

    def run(self, config: AutoExposureConfig | None = None) -> dict[str, Any]:
        cfg = config or AutoExposureConfig()
        validate_auto_exposure_config(cfg)
        if not self._operation_lock.acquire(blocking=False):
            raise AutoExposureBusyError("Camera auto exposure is already running.")
        try:
            return self._run_locked(cfg)
        finally:
            self._operation_lock.release()

    def _run_locked(self, config: AutoExposureConfig) -> dict[str, Any]:
        started = time.monotonic()
        snapshot: dict[str, dict[str, Any]] | None = None
        trace: list[float] = []
        counter = 0
        try:
            snapshot = _read_nodes(self._settings_read, self.SNAPSHOT_NODES)
            exposure_node = snapshot["ExposureTime"]
            exposure_limits = (
                _finite_float(exposure_node.get("minimum"), 1.0),
                _finite_float(exposure_node.get("maximum"), 30_000_000.0),
            )
            exposure = min(
                exposure_limits[1],
                max(
                    exposure_limits[0],
                    _finite_float(exposure_node.get("value"), exposure_limits[0]),
                ),
            )
            response = _write_settings(
                self._settings_write,
                [
                    ("ExposureAuto", "Off"),
                    ("GainAuto", "Off"),
                    ("BalanceWhiteAuto", "Off"),
                ],
            )
            counter = _response_counter(response, counter)
            response = _write_settings(
                self._settings_write,
                [
                    ("Gain", float(config.fixed_gain_db)),
                    ("ExposureTime", exposure),
                ],
            )
            counter = _response_counter(response, counter)

            converged_count = 0
            for _iteration in range(int(config.max_iterations)):
                for _discarded in range(int(config.settling_frames)):
                    frame = self._frame_read(
                        counter,
                        float(config.frame_timeout_ms) / 1000.0,
                    )
                    counter = int(frame.counter)
                frame = self._frame_read(
                    counter,
                    float(config.frame_timeout_ms) / 1000.0,
                )
                counter = int(frame.counter)
                measured = highlight_level(frame.rgb, config)
                trace.append(measured)
                relative_error = abs(measured - config.target_level) / config.target_level
                if relative_error <= float(config.target_tolerance_fraction):
                    converged_count += 1
                    if converged_count >= int(config.convergence_window):
                        final_nodes = _read_nodes(
                            self._settings_read,
                            ("ExposureTime", "Gain"),
                        )
                        return {
                            "accepted": True,
                            "status_code": 200,
                            "message": "Camera auto exposure converged.",
                            "converged": True,
                            "restored": False,
                            "final_exposure_us": _node_float(
                                final_nodes, "ExposureTime"
                            ),
                            "final_gain_db": _node_float(final_nodes, "Gain"),
                            "final_frame_counter": counter,
                            "iterations": len(trace),
                            "brightness_trace": trace,
                            "convergence_s": time.monotonic() - started,
                            "config": asdict(config),
                        }
                else:
                    converged_count = 0
                exposure = next_exposure_us(
                    exposure,
                    measured,
                    exposure_limits,
                    config,
                )
                response = _write_settings(
                    self._settings_write,
                    [("ExposureTime", exposure)],
                )
                counter = _response_counter(response, counter)
            message = "Camera auto exposure did not converge."
        except Exception as exc:
            message = str(exc) or type(exc).__name__

        restored, restore_error = _restore_snapshot(self._settings_write, snapshot)
        result: dict[str, Any] = {
            "accepted": False,
            "status_code": 409 if restored else 500,
            "message": message,
            "converged": False,
            "restored": restored,
            "final_frame_counter": counter,
            "iterations": len(trace),
            "brightness_trace": trace,
            "convergence_s": time.monotonic() - started,
            "config": asdict(config),
        }
        if restore_error:
            result["restore_error"] = restore_error
        return result


def validate_auto_exposure_config(config: AutoExposureConfig) -> None:
    finite_fields = {
        "target_percentile": config.target_percentile,
        "target_level": config.target_level,
        "min_step_ratio": config.min_step_ratio,
        "max_step_ratio": config.max_step_ratio,
        "clipping_reduction_ratio": config.clipping_reduction_ratio,
        "target_tolerance_fraction": config.target_tolerance_fraction,
        "fixed_gain_db": config.fixed_gain_db,
    }
    for name, value in finite_fields.items():
        if not math.isfinite(float(value)):
            raise ValueError(f"auto exposure {name} must be finite.")
    if not 0.0 < float(config.target_percentile) <= 100.0:
        raise ValueError("auto exposure target_percentile must be in (0, 100].")
    if not 0.0 < float(config.target_level) < 255.0:
        raise ValueError("auto exposure target_level must be in (0, 255).")
    if not 1 <= int(config.saturation_level) <= 255:
        raise ValueError("auto exposure saturation_level must be in [1, 255].")
    if not 0.0 < float(config.min_step_ratio) <= float(config.max_step_ratio):
        raise ValueError("auto exposure step ratios are invalid.")
    if not 0.0 < float(config.clipping_reduction_ratio) <= 1.0:
        raise ValueError("auto exposure clipping_reduction_ratio must be in (0, 1].")
    if int(config.settling_frames) < 0:
        raise ValueError("auto exposure settling_frames must be non-negative.")
    if int(config.convergence_window) < 1:
        raise ValueError("auto exposure convergence_window must be positive.")
    if float(config.target_tolerance_fraction) <= 0.0:
        raise ValueError("auto exposure target_tolerance_fraction must be positive.")
    if int(config.max_iterations) < int(config.convergence_window):
        raise ValueError("auto exposure max_iterations is too small.")
    if int(config.frame_timeout_ms) < 1:
        raise ValueError("auto exposure frame_timeout_ms must be positive.")
    if float(config.fixed_gain_db) < 0.0:
        raise ValueError("auto exposure fixed_gain_db must be non-negative.")


def next_exposure_us(
    current_us: float,
    measured_level: float,
    limits: tuple[float, float],
    config: object,
) -> float:
    current = float(current_us)
    measured = float(measured_level)
    minimum, maximum = (float(limits[0]), float(limits[1]))
    values = (current, measured, minimum, maximum)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Exposure update values must be finite.")
    if measured <= 0.0 or minimum <= 0.0 or maximum < minimum:
        raise ValueError("Exposure update values are outside valid bounds.")
    ratio = float(getattr(config, "target_level")) / measured
    if measured >= float(getattr(config, "saturation_level")):
        ratio = min(ratio, float(getattr(config, "clipping_reduction_ratio")))
    ratio = min(
        float(getattr(config, "max_step_ratio")),
        max(float(getattr(config, "min_step_ratio")), ratio),
    )
    return min(maximum, max(minimum, current * ratio))


def highlight_level(rgb: np.ndarray, config: object) -> float:
    array = np.asarray(rgb)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("Auto exposure frame must be an RGB array.")
    if array.size == 0:
        raise ValueError("Auto exposure frame is empty.")
    return float(
        np.percentile(
            np.max(array, axis=2),
            float(getattr(config, "target_percentile")),
        )
    )


def _read_nodes(
    settings_read: SettingsRead,
    names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    response = settings_read([str(name) for name in names])
    if not _response_accepted(response):
        raise RuntimeError(str(response.get("message") or "Camera settings read failed."))
    nodes = {
        str(node.get("name") or ""): dict(node)
        for node in response.get("nodes", ())
        if isinstance(node, Mapping)
    }
    missing = [name for name in names if name not in nodes]
    if missing:
        raise RuntimeError(f"Camera setting is unavailable: {missing[0]}.")
    return nodes


def _write_settings(
    settings_write: SettingsWrite,
    settings: list[tuple[str, object]],
) -> Mapping[str, Any]:
    response = settings_write(list(settings))
    if not _response_accepted(response):
        raise RuntimeError(str(response.get("message") or "Camera settings write failed."))
    return response


def _restore_snapshot(
    settings_write: SettingsWrite,
    snapshot: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[bool, str]:
    if snapshot is None:
        return False, "Camera state was not available for restoration."
    try:
        _write_settings(
            settings_write,
            [
                ("ExposureAuto", "Off"),
                ("GainAuto", "Off"),
                ("BalanceWhiteAuto", "Off"),
            ],
        )
        _write_settings(
            settings_write,
            [
                ("Gain", snapshot["Gain"].get("value")),
                ("ExposureTime", snapshot["ExposureTime"].get("value")),
            ],
        )
        _write_settings(
            settings_write,
            [
                ("GainAuto", snapshot["GainAuto"].get("value")),
                ("ExposureAuto", snapshot["ExposureAuto"].get("value")),
                (
                    "BalanceWhiteAuto",
                    snapshot["BalanceWhiteAuto"].get("value"),
                ),
            ],
        )
    except Exception as exc:
        return False, str(exc) or type(exc).__name__
    return True, ""


def _response_accepted(response: Mapping[str, Any]) -> bool:
    if "accepted" in response:
        return bool(response.get("accepted"))
    return bool(response.get("ok"))


def _response_counter(response: Mapping[str, Any], default: int) -> int:
    try:
        return int(response.get("frame_counter_at_completion", default))
    except (TypeError, ValueError):
        return int(default)


def _node_float(nodes: Mapping[str, Mapping[str, Any]], name: str) -> float:
    return _finite_float(nodes[name].get("value"), math.nan)


def _finite_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


__all__ = [
    "AutoExposureBusyError",
    "AutoExposureConfig",
    "AutoExposureFrame",
    "CameraAutoExposureController",
    "highlight_level",
    "next_exposure_us",
    "validate_auto_exposure_config",
]
