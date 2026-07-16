"""Background flat-field and lens correction for the live camera stream."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from probe_station_gui.camera.distortion import (
    apply_distortion_correction,
    correction_from_payload,
)
from probe_station_gui.camera.imaging import (
    CompiledFlatFieldCorrection,
    apply_compiled_flat_field_correction,
    build_flat_field_profile,
    compile_flat_field_correction,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveCameraCorrectionRequest:
    sequence: int
    frame: QImage
    objective_name: str
    distortion_configured: bool
    distortion_payload: object


@dataclass(frozen=True)
class LiveCameraCorrectionResult:
    sequence: int
    frame: QImage


class LiveCameraCorrectionPipeline:
    """Cache and apply the active objective's live image corrections."""

    def __init__(
        self,
        config_dir: str | Path,
        *,
        profile_refresh_s: float = 1.0,
        flat_field_apply: Callable[[QImage, object], QImage] | None = None,
        distortion_compile: Callable[[object], object] | None = None,
        distortion_apply: Callable[[QImage, object], QImage] | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._profile_refresh_s = max(0.0, float(profile_refresh_s))
        self._flat_field_apply = (
            flat_field_apply or apply_compiled_flat_field_correction
        )
        self._distortion_compile = distortion_compile or correction_from_payload
        self._distortion_apply = distortion_apply or apply_distortion_correction
        self._flat_objective_name = ""
        self._flat_signature: tuple[object, ...] | None = None
        self._flat_correction: CompiledFlatFieldCorrection | None = None
        self._flat_next_refresh_at = 0.0
        self._flat_warning_signature: tuple[object, ...] | None = None
        self._distortion_signature: tuple[str, str] | None = None
        self._distortion_model: object | None = None
        self._distortion_warning_signature: tuple[str, str] | None = None

    def process(
        self,
        request: LiveCameraCorrectionRequest,
    ) -> LiveCameraCorrectionResult:
        frame = request.frame
        flat_correction = self._compiled_flat_field_for_objective(
            request.objective_name
        )
        if flat_correction is not None:
            try:
                frame = self._flat_field_apply(frame, flat_correction)
            except ValueError as exc:
                self._warn_flat_field_once(
                    (self._flat_signature, frame.width(), frame.height()),
                    exc,
                )

        if request.distortion_configured:
            model = self._distortion_for_objective(
                request.objective_name,
                request.distortion_payload,
            )
            if model is not None:
                try:
                    frame = self._distortion_apply(frame, model)
                except ValueError as exc:
                    self._warn_distortion_once(self._distortion_signature, exc)
        else:
            self._clear_distortion_cache()

        return LiveCameraCorrectionResult(
            sequence=int(request.sequence),
            frame=frame,
        )

    def _compiled_flat_field_for_objective(
        self,
        objective_name: str,
    ) -> CompiledFlatFieldCorrection | None:
        objective = str(objective_name or "").strip()
        now = time.monotonic()
        if (
            objective == self._flat_objective_name
            and now < self._flat_next_refresh_at
        ):
            return self._flat_correction
        self._flat_objective_name = objective
        self._flat_next_refresh_at = now + self._profile_refresh_s

        manifest_path = self._flat_field_manifest_path(objective)
        if manifest_path is None or not manifest_path.is_file():
            self._flat_signature = (objective, "missing")
            self._flat_correction = None
            self._flat_warning_signature = None
            return None

        manifest_file_signature = _file_signature(manifest_path)
        error_signature: tuple[object, ...] = (
            objective,
            str(manifest_path),
            *manifest_file_signature,
        )
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, Mapping):
                raise ValueError("flat-field current.json must contain an object")
            declared_objective = str(payload.get("objective") or objective).strip()
            if declared_objective.casefold() != objective.casefold():
                raise ValueError(
                    "flat-field objective does not match the active objective"
                )
            reference_path = _reference_path(payload, manifest_path.parent)
            reference_signature = _file_signature(reference_path)
            blur_radius_px = int(payload.get("blur_radius_px", 401))
            max_gain = float(payload.get("max_gain", 4.0))
            signature = (
                *error_signature,
                str(reference_path),
                *reference_signature,
                blur_radius_px,
                max_gain,
            )
            if signature == self._flat_signature:
                return self._flat_correction

            reference = QImage(str(reference_path))
            if reference.isNull():
                raise ValueError(f"unable to load flat-field reference {reference_path}")
            declared_size = payload.get("frame_size_px")
            if declared_size is not None:
                size = tuple(int(value) for value in declared_size)
                if size != (reference.width(), reference.height()):
                    raise ValueError(
                        "flat-field reference size does not match current.json"
                    )
            profile = build_flat_field_profile(
                reference,
                blur_radius_px=blur_radius_px,
                max_gain=max_gain,
                source=f"{objective} live flat-field",
            )
            self._flat_correction = compile_flat_field_correction(profile)
            self._flat_signature = signature
            self._flat_warning_signature = None
            return self._flat_correction
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._flat_correction = None
            self._warn_flat_field_once(error_signature, exc)
            return None

    def _flat_field_manifest_path(self, objective_name: str) -> Path | None:
        if not objective_name:
            return None
        name_path = Path(objective_name)
        if name_path.name != objective_name or name_path.is_absolute():
            return None
        return (
            self._config_dir
            / "calibrations"
            / "flat-field"
            / objective_name
            / "current.json"
        )

    def _distortion_for_objective(
        self,
        objective_name: str,
        payload: object,
    ) -> object | None:
        signature = (
            str(objective_name or ""),
            _payload_signature(payload),
        )
        if signature == self._distortion_signature:
            return self._distortion_model
        try:
            model = self._distortion_compile(payload)
        except (TypeError, ValueError) as exc:
            self._distortion_signature = signature
            self._distortion_model = None
            self._warn_distortion_once(signature, exc)
            return None
        self._distortion_signature = signature
        self._distortion_model = model
        self._distortion_warning_signature = None
        return model

    def _clear_distortion_cache(self) -> None:
        self._distortion_signature = None
        self._distortion_model = None
        self._distortion_warning_signature = None

    def _warn_flat_field_once(
        self,
        signature: tuple[object, ...],
        exc: Exception,
    ) -> None:
        if signature == self._flat_warning_signature:
            return
        self._flat_warning_signature = signature
        logger.warning("Unable to apply live flat-field correction: %s", exc)

    def _warn_distortion_once(
        self,
        signature: tuple[str, str] | None,
        exc: Exception,
    ) -> None:
        if signature == self._distortion_warning_signature:
            return
        self._distortion_warning_signature = signature
        logger.warning("Unable to apply live lens distortion correction: %s", exc)


class LatestFrameProcessor(QObject):
    """Run one frame at a time while retaining only the latest pending frame."""

    frame_ready = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        process: Callable[[object], object],
        *,
        thread_name: str = "camera-live-correction",
    ) -> None:
        super().__init__()
        self._process = process
        self._condition = threading.Condition()
        self._pending: object | None = None
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run,
            name=str(thread_name),
            daemon=True,
        )
        self._thread.start()

    def submit(self, request: object) -> bool:
        with self._condition:
            if self._stopping:
                return False
            self._pending = request
            self._condition.notify()
            return True

    def shutdown(self, *, timeout_s: float = 2.0) -> None:
        with self._condition:
            self._stopping = True
            self._pending = None
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, float(timeout_s)))

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                request = self._pending
                self._pending = None
            try:
                result = self._process(request)
            except Exception as exc:
                self.error.emit(str(exc) or type(exc).__name__)
            else:
                self.frame_ready.emit(result)


def _reference_path(payload: Mapping[str, Any], base_dir: Path) -> Path:
    value = str(payload.get("reference_image") or "").strip()
    if not value:
        raise ValueError("flat-field reference_image is missing")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_mtime_ns), int(stat.st_size)


def _payload_signature(payload: object) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        return repr(payload)


__all__ = [
    "LatestFrameProcessor",
    "LiveCameraCorrectionPipeline",
    "LiveCameraCorrectionRequest",
    "LiveCameraCorrectionResult",
]
