"""Production bindings for :mod:`optical_calibration_runtime`."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtGui import QImage

from probe_station_gui.camera import microscope_scan
from probe_station_gui.camera.optical_calibration_geometry import (
    LensFitLimits,
    LensPreviewMetrics,
    PixelMatrix,
    validate_lens_previews,
)
from probe_station_gui.settings.objective_config import (
    normalize_objective_name,
    parse_pixels_to_mm_matrix,
)


@dataclass(frozen=True)
class FlatFieldCalibrationRequest:
    run_id: str
    wizard_run_id: int | None
    objective_name: str
    magnification: float | None
    pixels_to_mm: PixelMatrix
    pixel_size_mm: tuple[float, float]
    linear_feedrate: float
    needle_feedrate: float
    full_wizard: bool = False
    parent_session_token: str | None = None
    grid_size: int = 3
    overlap_fraction: float = 0.8
    settle_s: float = 0.12
    camera_timeout_s: float = 2.0


@dataclass(frozen=True)
class LensDistortionCalibrationRequest:
    run_id: str
    wizard_run_id: int | None
    objective_name: str
    magnification: float | None
    pixels_to_mm: PixelMatrix
    pixel_size_mm: tuple[float, float]
    linear_feedrate: float
    needle_feedrate: float
    full_wizard: bool = False
    parent_session_token: str | None = None
    grid_size: int = 3
    fov_fraction: float = 0.35
    settle_s: float = 0.12
    camera_timeout_s: float = 2.0
    fit_limits: LensFitLimits = LensFitLimits()


@dataclass(frozen=True)
class CalibrationStartDecision:
    accepted: bool
    status_code: int
    message: str
    run_id: str


@dataclass(frozen=True)
class LensCalibrationArtifact:
    payload: dict[str, object]
    before_preview: QImage
    after_preview: QImage


@dataclass(frozen=True)
class OpticalCalibrationOutcome:
    run_id: str
    wizard_run_id: int | None
    kind: str
    success: bool
    message: str
    objective_name: str
    full_wizard: bool
    flat_payload: dict[str, object] | None = None
    lens_artifact: LensCalibrationArtifact | None = None
    restore_warning: str = ""


@dataclass(frozen=True)
class OpticalCalibrationProgress:
    run_id: str
    wizard_run_id: int | None
    kind: str
    message: str


@dataclass(frozen=True)
class OpticalCalibrationState:
    active_run_id: str | None
    active_kind: str | None
    parent_session_token: str | None
    cancel_requested: bool
    shutdown_requested: bool


class ObjectiveScalePort(Protocol):
    pixels_to_mm: object
    pixel_size_x_mm: float
    pixel_size_y_mm: float


@dataclass(frozen=True)
class OpticalCalibrationRequestData:
    objective_name: str
    magnification: float | None
    pixels_to_mm: PixelMatrix
    pixel_size_mm: tuple[float, float]


@dataclass(frozen=True)
class OpticalCalibrationRequestAdapter:
    objective_metadata: Callable[[], tuple[str, float | None]]
    objective_scale: Callable[[], ObjectiveScalePort]

    def capture(self) -> OpticalCalibrationRequestData:
        objective_name, magnification = self.objective_metadata()
        objective_name = normalize_objective_name(objective_name)
        if not objective_name:
            raise RuntimeError("No active objective selected.")
        scale = self.objective_scale()
        parsed = parse_pixels_to_mm_matrix(scale.pixels_to_mm)
        matrix: PixelMatrix = (
            tuple(tuple(float(value) for value in row) for row in parsed)
            if parsed
            else ((0.0, 0.0), (0.0, 0.0))
        )
        try:
            pixel_size = (
                float(scale.pixel_size_x_mm),
                float(scale.pixel_size_y_mm),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Optical calibration requires click-to-move calibration."
            ) from exc
        return OpticalCalibrationRequestData(
            objective_name,
            magnification,
            matrix,
            pixel_size,
        )


@dataclass(frozen=True)
class LensCompletionPresentation:
    success: bool
    message: str
    artifact: LensCalibrationArtifact | None
    metrics: LensPreviewMetrics | None

    def wizard_kwargs(self) -> dict[str, object]:
        if not self.success or self.artifact is None or self.metrics is None:
            return {}
        return {
            "before_preview": self.artifact.before_preview,
            "after_preview": self.artifact.after_preview,
            "without_calibration_metrics": self.metrics.without_calibration,
            "with_calibration_metrics": self.metrics.with_calibration,
        }


class StagePort(Protocol):
    def reserve(self, operation: str) -> None: ...
    def start_position(self) -> tuple[float, float]: ...
    def raise_needles(self, feedrate: float) -> None: ...
    def move_xy(self, x_mm: float, y_mm: float, feedrate: float) -> None: ...
    def release(self) -> None: ...


class CameraPort(Protocol):
    def lock(self) -> str: ...
    def restore(self, key: str) -> str: ...
    def latest_raw_counter(self) -> int: ...
    def wait_raw(self, *, after_counter: int | None, timeout_s: float) -> QImage | None: ...


class OpticalSessionLeasePort(Protocol):
    token: str
    def snapshot(self) -> dict[str, object]: ...
    def close(self) -> Mapping[str, object]: ...
    def is_active(self) -> bool: ...


class OpticalSessionPort(Protocol):
    def open(self, operation: str, parent_token: str | None = None) -> OpticalSessionLeasePort: ...


class FlatFieldStorePort(Protocol):
    def install(
        self,
        objective_name: str,
        frames: Sequence[QImage],
        metadata: dict[str, object],
    ) -> object: ...


class OpticalCalibrationEventPort(Protocol):
    def progress(self, event: OpticalCalibrationProgress) -> None: ...
    def complete(self, outcome: OpticalCalibrationOutcome) -> None: ...
    def warning(self, message: str) -> None: ...


def optical_calibration_preflight_message(
    kind: str,
    state: OpticalCalibrationState,
    *,
    stage_ready: bool,
    stage_busy: bool,
) -> str:
    if state.active_run_id is not None:
        return (
            "Flat-field calibration is already running."
            if state.active_kind == "flat"
            else "Lens distortion calibration is already running."
        )
    if not stage_ready:
        return "Connect the stage controller before calibration."
    if stage_busy:
        label = "flat-field" if kind == "flat" else "lens distortion"
        return f"Stage is busy; {label} calibration not started."
    return ""


def optical_calibration_blocks_mutation(
    state: OpticalCalibrationState,
    *,
    outcome_owns_mutation: bool,
) -> bool:
    calibration_owned = (
        state.active_run_id is not None or state.parent_session_token is not None
    )
    return calibration_owned and not outcome_owns_mutation


def prepare_lens_completion(
    outcome: OpticalCalibrationOutcome,
    success: bool,
    message: str,
    artifact: object,
    *,
    limits: LensFitLimits,
    save: Callable[[dict[str, object], str, OpticalCalibrationOutcome], None],
) -> LensCompletionPresentation:
    if not isinstance(artifact, LensCalibrationArtifact):
        return LensCompletionPresentation(bool(success), str(message), None, None)
    if not success and not outcome.restore_warning:
        return LensCompletionPresentation(False, str(message), artifact, None)
    try:
        metrics = validate_lens_previews(
            artifact.payload,
            artifact.before_preview,
            artifact.after_preview,
            limits,
        )
        save(artifact.payload, outcome.objective_name, outcome)
    except Exception as exc:
        return LensCompletionPresentation(
            False,
            f"Lens distortion calibration save failed: {exc}",
            artifact,
            None,
        )
    return LensCompletionPresentation(bool(success), str(message), artifact, metrics)


class SessionLease(Protocol):
    token: str
    def snapshot(self) -> dict[str, object]: ...
    def close(self) -> Mapping[str, object]: ...
    def is_active(self) -> bool: ...


class SessionManager(Protocol):
    def open(self, operation: str, parent_token: str | None = None) -> SessionLease: ...


@dataclass(frozen=True)
class OpticalCalibrationStageAdapter:
    begin_task: Callable[[str], None]
    read_position: Callable[[], Sequence[float]]
    raise_action: Callable[[str, float], None]
    move_xy_callback: Callable[..., None]
    finish_task: Callable[[], None]

    def reserve(self, operation: str) -> None:
        self.begin_task(operation)

    def start_position(self) -> tuple[float, float]:
        position = self.read_position()
        if len(position) < 2:
            raise RuntimeError("Unable to read X/Y stage position.")
        return float(position[0]), float(position[1])

    def raise_needles(self, feedrate: float) -> None:
        self.raise_action("raise", float(feedrate))

    def move_xy(self, x_mm: float, y_mm: float, feedrate: float) -> None:
        self.move_xy_callback(float(x_mm), float(y_mm), feedrate=float(feedrate))

    def release(self) -> None:
        self.finish_task()


@dataclass(frozen=True)
class OpticalCalibrationCameraAdapter:
    apply_lock: Callable[[microscope_scan.CameraLockSettings], str]
    restore_lock: Callable[[str], str]
    raw_counter: Callable[[], int]
    wait_raw_callback: Callable[..., tuple[QImage | None, int]]

    def lock(self) -> str:
        return str(
            self.apply_lock(
                microscope_scan.CameraLockSettings(
                    enabled=True,
                    settings=microscope_scan.DEFAULT_CAMERA_LOCK_SETTINGS,
                )
            )
        )

    def restore(self, key: str) -> str:
        return str(self.restore_lock(str(key)) or "")

    def latest_raw_counter(self) -> int:
        return int(self.raw_counter())

    def wait_raw(self, *, after_counter: int | None, timeout_s: float) -> QImage | None:
        frame, _counter = self.wait_raw_callback(
            after_counter=after_counter,
            timeout_s=float(timeout_s),
        )
        return frame


@dataclass(frozen=True)
class OpticalCalibrationSessionAdapter:
    manager: SessionManager

    def open(self, operation: str, parent_token: str | None = None) -> SessionLease:
        return self.manager.open(operation, parent_token=parent_token)


@dataclass(frozen=True)
class OpticalCalibrationStoreAdapter:
    install_callback: Callable[..., object]

    def install(
        self,
        objective_name: str,
        frames: Sequence[QImage],
        metadata: dict[str, object],
    ) -> object:
        return self.install_callback(
            objective_name,
            frames,
            metadata=metadata,
        )


@dataclass(frozen=True)
class OpticalCalibrationEventAdapter:
    progress_callback: Callable[[OpticalCalibrationProgress], None]
    completion_callback: Callable[[OpticalCalibrationOutcome], None]
    warning_callback: Callable[[str], None]

    def progress(self, event: OpticalCalibrationProgress) -> None:
        self.progress_callback(event)

    def complete(self, outcome: OpticalCalibrationOutcome) -> None:
        self.completion_callback(outcome)

    def warning(self, message: str) -> None:
        self.warning_callback(str(message))


__all__ = [
    "CalibrationStartDecision",
    "CameraPort",
    "FlatFieldCalibrationRequest",
    "FlatFieldStorePort",
    "LensCalibrationArtifact",
    "LensCompletionPresentation",
    "LensDistortionCalibrationRequest",
    "OpticalCalibrationCameraAdapter",
    "OpticalCalibrationEventAdapter",
    "OpticalCalibrationEventPort",
    "OpticalCalibrationOutcome",
    "OpticalCalibrationProgress",
    "OpticalCalibrationRequestAdapter",
    "OpticalCalibrationRequestData",
    "OpticalCalibrationSessionAdapter",
    "OpticalCalibrationState",
    "OpticalCalibrationStageAdapter",
    "OpticalCalibrationStoreAdapter",
    "OpticalSessionLeasePort",
    "OpticalSessionPort",
    "ObjectiveScalePort",
    "StagePort",
    "optical_calibration_blocks_mutation",
    "optical_calibration_preflight_message",
    "prepare_lens_completion",
]
