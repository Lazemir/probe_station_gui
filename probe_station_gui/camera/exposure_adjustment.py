"""Camera adjustment, freshness, and software retry policy."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .auto_exposure import (
    AutoExposureConfig,
    AutoExposureFrame,
    highlight_level,
)

SoftwareOnce = Callable[[AutoExposureConfig | None], Mapping[str, Any]]
SettingsRead = Callable[[list[str]], Mapping[str, Any]]
SettingsWrite = Callable[[list[tuple[str, object]]], Mapping[str, Any]]
FrameRead = Callable[[int, float], AutoExposureFrame]
ErrorFactory = Callable[[str], Exception]
WarningChanged = Callable[[str], None]


class ExposureAdjustmentController:
    """Own camera adjustment execution and software retry state."""

    DRIFT_TOLERANCE_FRACTION = 0.05
    HARDWARE_ONCE_TIMEOUT_S = 5.0
    FRAME_TIMEOUT_S = 2.5
    HARDWARE_POLL_INTERVAL_S = 0.01

    def __init__(
        self,
        *,
        software_once: SoftwareOnce,
        settings_read: SettingsRead,
        settings_write: SettingsWrite,
        frame_read: FrameRead,
        error_factory: ErrorFactory,
        shutdown_requested: Callable[[], bool],
        warning_changed: WarningChanged | None = None,
    ) -> None:
        self._software_once = software_once
        self._settings_read = settings_read
        self._settings_write = settings_write
        self._frame_read = frame_read
        self._error_factory = error_factory
        self._shutdown_requested = shutdown_requested
        self._warning_changed = warning_changed
        self._software_retry_armed = False
        self._software_retry_brightness: float | None = None
        self._software_retry_warning = ""

    def run_once(self, engine: str) -> dict[str, object]:
        if str(engine) == "software":
            return self._software_once_result()
        if str(engine) == "camera":
            return self._hardware_once()
        raise self._error_factory(f"Unsupported exposure engine: {engine!r}.")

    def start_software_auto(self) -> dict[str, object]:
        result = self._normalize_software_once_result(
            self._software_once_attempt(),
            failure_brightness=None,
        )
        if not bool(result.get("converged", False)):
            self.native_off()
        return result

    def monitor_check(self) -> dict[str, object]:
        _nodes, response = self._read_nodes(("ExposureTime",))
        watermark = _response_counter(response, 0)
        frame = self._frame_read(watermark, self.FRAME_TIMEOUT_S)
        if int(frame.counter) <= watermark:
            raise self._error_factory(
                "Software exposure monitor received a stale frame."
            )
        config = AutoExposureConfig(target_tolerance_fraction=0.02)
        measured = highlight_level(frame.rgb, config)
        relative_error = abs(measured - config.target_level) / config.target_level
        result: dict[str, object] = {
            "accepted": True,
            "engine": "software",
            "brightness": measured,
            "adjusted": False,
            "final_frame_counter": int(frame.counter),
        }
        if self._software_retry_suppresses(measured):
            return self._software_retry_result(
                measured=measured,
                frame_counter=int(frame.counter),
            )
        if relative_error > self.DRIFT_TOLERANCE_FRACTION:
            result = self._software_once_result_for_monitor(measured=measured)
        return result

    def camera_snapshot(self) -> dict[str, object]:
        nodes, _response = self._read_nodes(("ExposureAuto", "ExposureTime"))
        return {name: node.get("value") for name, node in nodes.items()}

    def restore_camera_snapshot(self, snapshot: Mapping[str, object]) -> None:
        errors: list[str] = []
        try:
            self.native_off()
        except Exception as exc:
            errors.append(str(exc))
        if "ExposureTime" in snapshot:
            try:
                self._write_settings(
                    [("ExposureTime", snapshot.get("ExposureTime"))]
                )
            except Exception as exc:
                errors.append(str(exc))
        native = snapshot.get("ExposureAuto")
        if native not in (None, "Off") and not self._shutdown_requested():
            try:
                self._write_settings([("ExposureAuto", native)])
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise self._error_factory(
                f"Unable to restore camera exposure: {'; '.join(errors)}"
            )

    def native_off(self) -> None:
        self._write_settings([("ExposureAuto", "Off")])

    def enable_native_continuous_if_allowed(self) -> bool:
        if self._shutdown_requested():
            return False
        self._write_settings([("ExposureAuto", "Continuous")])
        return True

    def read_exposure_time(self) -> float:
        nodes, _response = self._read_nodes(("ExposureTime",))
        try:
            return float(nodes["ExposureTime"].get("value"))
        except (TypeError, ValueError) as exc:
            raise self._error_factory("Camera exposure time is unavailable.") from exc

    def clear_software_retry(self) -> None:
        self._software_retry_armed = False
        self._software_retry_brightness = None
        self._software_retry_warning = ""

    def _software_once_result(self) -> dict[str, object]:
        result = self._software_once_attempt()
        if not _response_accepted(result) or not bool(result.get("converged", True)):
            if self._is_recoverable_nonconvergence(result):
                self._recover_software_nonconvergence(
                    result,
                    brightness=None,
                )
            raise self._error_factory(
                str(result.get("message") or "Software exposure did not converge.")
            )
        self.clear_software_retry()
        return self._successful_software_result(result)

    def _software_once_attempt(self) -> dict[str, object]:
        config = AutoExposureConfig(target_tolerance_fraction=0.02)
        return dict(self._software_once(config))

    @staticmethod
    def _successful_software_result(
        result: Mapping[str, Any],
    ) -> dict[str, object]:
        normalized = dict(result)
        normalized.setdefault("accepted", True)
        normalized["engine"] = "software"
        return normalized

    @staticmethod
    def _is_recoverable_nonconvergence(result: Mapping[str, Any]) -> bool:
        return (
            str(result.get("failure_reason") or "") == "not_converged"
            and bool(result.get("restored", False))
        )

    def _remember_software_retry(
        self,
        *,
        brightness: float | None,
        warning: str,
    ) -> None:
        self._software_retry_armed = True
        self._software_retry_brightness = brightness
        self._software_retry_warning = str(warning)

    def _software_retry_suppresses(self, measured: float) -> bool:
        if not self._software_retry_armed:
            return False
        baseline = self._software_retry_brightness
        if baseline is None:
            self._software_retry_brightness = float(measured)
            return True
        relative_change = abs(float(measured) - baseline) / max(abs(baseline), 1.0)
        if relative_change <= self.DRIFT_TOLERANCE_FRACTION:
            return True
        self.clear_software_retry()
        return False

    def _software_retry_result(
        self,
        *,
        measured: float,
        frame_counter: int,
    ) -> dict[str, object]:
        warning = self._software_retry_warning
        self._publish_warning(warning)
        return {
            "accepted": True,
            "engine": "software",
            "brightness": measured,
            "adjusted": False,
            "retry_suppressed": True,
            "warning": warning,
            "final_frame_counter": frame_counter,
        }

    def _recover_software_nonconvergence(
        self,
        result: Mapping[str, Any],
        *,
        brightness: float | None,
    ) -> dict[str, object]:
        warning = str(result.get("message") or "Software exposure did not converge.")
        self._remember_software_retry(brightness=brightness, warning=warning)
        self._publish_warning(warning)
        recovered = dict(result)
        recovered["accepted"] = True
        recovered["engine"] = "software"
        recovered["warning"] = warning
        return recovered

    def _normalize_software_once_result(
        self,
        result: Mapping[str, Any],
        *,
        failure_brightness: float | None,
    ) -> dict[str, object]:
        if _response_accepted(result) and bool(result.get("converged", True)):
            self.clear_software_retry()
            return self._successful_software_result(result)
        if self._is_recoverable_nonconvergence(result):
            return self._recover_software_nonconvergence(
                result,
                brightness=failure_brightness,
            )
        raise self._error_factory(
            str(result.get("message") or "Software exposure did not converge.")
        )

    def _software_once_result_for_monitor(
        self,
        *,
        measured: float,
    ) -> dict[str, object]:
        result = self._normalize_software_once_result(
            self._software_once_attempt(),
            failure_brightness=measured,
        )
        if bool(result.get("converged", False)):
            result["adjusted"] = True
        else:
            result["adjusted"] = False
            result["retry_suppressed"] = True
        return result

    def _hardware_once(self) -> dict[str, object]:
        response = self._write_settings([("ExposureAuto", "Once")])
        watermark = _response_counter(response, 0)
        deadline = time.monotonic() + self.HARDWARE_ONCE_TIMEOUT_S
        while True:
            nodes, read_response = self._read_nodes(("ExposureAuto",))
            watermark = max(watermark, _response_counter(read_response, watermark))
            if str(nodes["ExposureAuto"].get("value")) == "Off":
                break
            if time.monotonic() >= deadline:
                raise self._error_factory("Camera exposure Once timed out.")
            time.sleep(self.HARDWARE_POLL_INTERVAL_S)
        frame = self._frame_read(watermark, self.FRAME_TIMEOUT_S)
        if int(frame.counter) <= watermark:
            raise self._error_factory(
                "Camera exposure Once did not produce a fresh raw frame."
            )
        final_exposure = self.read_exposure_time()
        return {
            "accepted": True,
            "status_code": 200,
            "message": "Camera exposure Once completed.",
            "converged": True,
            "engine": "camera",
            "final_exposure_us": final_exposure,
            "final_frame_counter": int(frame.counter),
        }

    def _read_nodes(
        self,
        names: Sequence[str],
    ) -> tuple[dict[str, dict[str, Any]], Mapping[str, Any]]:
        response = self._settings_read([str(name) for name in names])
        if not _response_accepted(response):
            raise self._error_factory(
                str(response.get("message") or "Camera settings read failed.")
            )
        candidates: list[object] = list(response.get("nodes") or [])
        for node_map in response.get("maps") or []:
            if isinstance(node_map, Mapping):
                candidates.extend(node_map.get("nodes") or [])
        nodes = {
            str(node.get("name") or ""): dict(node)
            for node in candidates
            if isinstance(node, Mapping)
        }
        missing = [name for name in names if name not in nodes]
        if missing:
            raise self._error_factory(
                f"Camera setting is unavailable: {missing[0]}."
            )
        return nodes, response

    def _write_settings(
        self,
        settings: list[tuple[str, object]],
    ) -> Mapping[str, Any]:
        response = self._settings_write(list(settings))
        if not _response_accepted(response):
            raise self._error_factory(
                str(response.get("message") or "Camera settings write failed.")
            )
        return response

    def _publish_warning(self, warning: str) -> None:
        if self._warning_changed is not None:
            self._warning_changed(str(warning))


def _response_accepted(response: Mapping[str, Any]) -> bool:
    if "accepted" in response:
        return bool(response.get("accepted"))
    return bool(response.get("ok"))


def _response_counter(response: Mapping[str, Any], default: int) -> int:
    try:
        return int(response.get("frame_counter_at_completion", default))
    except (TypeError, ValueError):
        return int(default)


__all__ = ["ExposureAdjustmentController"]
