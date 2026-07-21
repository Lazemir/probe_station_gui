"""Production adapters and planning for microscope scan runtime ports."""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from probe_station_gui.camera import microscope_scan
from probe_station_gui.camera.imaging import (
    MicroscopeCaptureResult,
    MicroscopeScanPlan,
    MicroscopeScanTile,
    apply_flat_field_correction,
    apply_self_flat_field_correction,
    build_median_flat_field_profile,
    refine_scan_scale_from_tile_overlaps,
    save_microscope_image,
    stitch_scan_tiles,
    utc_timestamp,
)
from probe_station_gui.camera.microscope_scan import (
    CameraLockSettings,
    FlatFieldScanOptions,
)
from probe_station_gui.camera.microscope_scan_runtime import (
    CapturedFrame,
    MicroscopeScanRunRequest,
)


logger = logging.getLogger(__name__)


class ScanLaunchPort(Protocol):
    objective_name: str
    magnification: float | None
    scan_name: str
    scale: object

    def metadata(self) -> dict[str, object]: ...

    def design_to_raw_stage(
        self,
        design_xy: tuple[float, float],
    ) -> tuple[float, float]: ...

    def raw_stage_to_design(
        self,
        raw_stage_xy: tuple[float, float],
    ) -> tuple[float, float] | None: ...


class OpticalSessionLeasePort(Protocol):
    def snapshot(self) -> Mapping[str, object]: ...

    def close(self) -> Mapping[str, object]: ...


class OpticalSessionManagerPort(Protocol):
    def open(
        self,
        operation: str,
        parent_token: object | None = None,
    ) -> OpticalSessionLeasePort: ...


class CameraSettingsSignalPort(Protocol):
    def connect(
        self,
        callback: Callable[[object], None],
        connection_type: object,
    ) -> None: ...

    def disconnect(self, callback: Callable[[object], None]) -> None: ...


class GrabberSettingsPort(Protocol):
    camera_settings_override_changed: CameraSettingsSignalPort

    def request_temporary_camera_settings(
        self,
        settings: Sequence[tuple[str, object]],
        *,
        restore_key: str,
    ) -> None: ...

    def request_restore_camera_settings(self, *, restore_key: str) -> None: ...


@dataclass(frozen=True)
class MicroscopeDesignScanRequest:
    bounds: tuple[float, float, float, float]
    overlap_fraction: float


@dataclass(frozen=True)
class MicroscopeAreaScanRequest:
    row_count: int
    column_count: int
    overlap_fraction: float
    scan_pattern: str
    structure_size_mm: float | None = None
    placement_fraction: float | None = None


def build_microscope_scan_plan(
    planning_request: object,
    frame_size_px: tuple[int, int],
    *,
    launch: ScanLaunchPort,
    center_stage_xy: tuple[float, float] | None = None,
) -> MicroscopeScanPlan:
    width_px, height_px = (int(frame_size_px[0]), int(frame_size_px[1]))
    if width_px <= 0 or height_px <= 0:
        raise RuntimeError("Camera frame size is unavailable.")
    if isinstance(planning_request, MicroscopeDesignScanRequest):
        decision = microscope_scan.scan_plan_decision(
            document=planning_request,
            scale=launch.scale,
            frame_size_px=(width_px, height_px),
            overlap_fraction=planning_request.overlap_fraction,
            design_to_stage_xy=launch.design_to_raw_stage,
        )
        if not decision.accepted or decision.plan is None:
            message = (
                decision.status.message
                if decision.status is not None
                else "Microscope scan plan is unavailable."
            )
            raise RuntimeError(message)
        return decision.plan
    if isinstance(planning_request, MicroscopeAreaScanRequest):
        if center_stage_xy is None or len(center_stage_xy) < 2:
            raise RuntimeError("Unable to read X/Y stage position.")
        center_xy = (float(center_stage_xy[0]), float(center_stage_xy[1]))
        if not all(math.isfinite(value) for value in center_xy):
            raise RuntimeError("Unable to read X/Y stage position.")
        pixels_to_mm = getattr(launch.scale, "pixels_to_mm", None)
        if planning_request.scan_pattern == "stitch_debug":
            return microscope_scan.stitch_debug_scan_plan_from_pixel_matrix(
                center_stage_xy=center_xy,
                frame_size_px=(width_px, height_px),
                pixels_to_mm=pixels_to_mm,
                structure_size_mm=float(planning_request.structure_size_mm),
                placement_fraction=float(planning_request.placement_fraction),
                overlap_fraction=planning_request.overlap_fraction,
            )
        if pixels_to_mm is not None:
            return microscope_scan.centered_area_scan_plan_from_pixel_matrix(
                center_stage_xy=center_xy,
                frame_size_px=(width_px, height_px),
                pixels_to_mm=pixels_to_mm,
                row_count=planning_request.row_count,
                column_count=planning_request.column_count,
                overlap_fraction=planning_request.overlap_fraction,
            )
        fov_size_mm = (
            float(width_px) * float(getattr(launch.scale, "pixel_size_x_mm")),
            float(height_px) * float(getattr(launch.scale, "pixel_size_y_mm")),
        )
        return microscope_scan.centered_area_scan_plan(
            center_stage_xy=center_xy,
            fov_size_mm=fov_size_mm,
            row_count=planning_request.row_count,
            column_count=planning_request.column_count,
            overlap_fraction=planning_request.overlap_fraction,
        )
    if isinstance(planning_request, MicroscopeScanPlan):
        return planning_request
    raise RuntimeError("Microscope scan planning request is invalid.")


@dataclass
class MicroscopeScanStageAdapter:
    begin_task: Callable[[str], None]
    read_reserved_position: Callable[[], Sequence[float]]
    raise_action: Callable[[str, float], None]
    needle_feedrate: Callable[[], float]
    move_xy: Callable[[float, float], None]
    latest_position: Callable[[], Sequence[float] | None]
    finish_task: Callable[[], None]

    def reserve(self) -> tuple[float, float]:
        self.begin_task("microscope design scan")
        try:
            position = self.read_reserved_position()
            if len(position) < 2:
                raise RuntimeError("Unable to read X/Y stage position.")
            start_xy = (float(position[0]), float(position[1]))
            if not all(math.isfinite(value) for value in start_xy):
                raise RuntimeError("Unable to read X/Y stage position.")
            return start_xy
        except BaseException:
            self.finish_task()
            raise

    def raise_needles(self) -> None:
        self.raise_action("raise", self.needle_feedrate())

    def move_to(self, tile: MicroscopeScanTile, *, approach_mm: float) -> None:
        target_x = float(tile.stage_xy[0])
        target_y = float(tile.stage_xy[1])
        approach = max(0.0, float(approach_mm))
        if approach > 1e-9:
            self.move_xy(target_x - approach, target_y - approach)
        self.move_xy(target_x, target_y)

    def actual_position(self) -> tuple[float, ...] | None:
        position = self.latest_position()
        if position is None or len(position) < 2:
            return None
        try:
            values = tuple(float(value) for value in position)
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in values[:2]):
            return None
        return values

    def return_to(self, start_xy: tuple[float, float]) -> None:
        self.move_xy(start_xy[0], start_xy[1])

    def release(self) -> None:
        self.finish_task()


@dataclass
class MicroscopeScanCameraAdapter:
    grabber: GrabberSettingsPort
    stop_event: threading.Event
    latest_frame_counter: Callable[[], int]
    wait_for_frame: Callable[..., tuple[QImage | None, int]]
    latest_raw_frame_counter: Callable[[], int]
    wait_for_raw_frame: Callable[..., tuple[QImage | None, int]]
    correct_lens: Callable[[QImage], QImage]
    settings_timeout_s: float

    def planning_frame_size(self) -> tuple[int, int]:
        before_counter = self.latest_frame_counter()
        frame, _counter = self.wait_for_frame(
            after_counter=before_counter,
            timeout_s=2.0,
        )
        if frame is None:
            raise RuntimeError("Camera frame is unavailable; cannot scan.")
        return int(frame.width()), int(frame.height())

    def apply_lock(self, settings: CameraLockSettings) -> object | None:
        if not settings.enabled or not settings.settings:
            return None
        restore_key = f"microscope_scan_{uuid.uuid4().hex}"
        result = self._wait_for_settings_override(
            restore_key=restore_key,
            request=lambda: self.grabber.request_temporary_camera_settings(
                settings.settings,
                restore_key=restore_key,
            ),
        )
        if not bool(result.get("ok")):
            raise RuntimeError(
                str(result.get("message") or "Camera settings lock failed.")
            )
        return restore_key

    def settle(self, settle_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(settle_s))
        while True:
            if self.stop_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            time.sleep(min(remaining, 0.05))

    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def capture(self, *, raw: bool) -> QImage:
        if raw:
            before_counter = self.latest_raw_frame_counter()
            wait_for_frame = self.wait_for_raw_frame
        else:
            before_counter = self.latest_frame_counter()
            wait_for_frame = self.wait_for_frame
        frame, _counter = wait_for_frame(
            after_counter=before_counter,
            timeout_s=2.0,
        )
        if frame is None:
            raise RuntimeError("Camera frame is unavailable.")
        return frame

    def flat_field_profile(
        self,
        captured_frames: Sequence[CapturedFrame],
        options: FlatFieldScanOptions,
    ) -> object | None:
        if not options.enabled:
            return None
        mode = options.mode.lower()
        if mode == "self":
            return None
        if mode == "scan":
            frames = [captured.frame for captured in captured_frames]
            source = "scan_median"
        elif mode == "reference":
            if not options.reference_images:
                raise RuntimeError("Flat-field reference images are not configured.")
            frames = []
            for reference_image in options.reference_images:
                image_path = Path(reference_image).expanduser()
                image = QImage(str(image_path))
                if image.isNull():
                    raise RuntimeError(
                        f"Flat-field reference image is unreadable: {image_path}"
                    )
                frames.append(image.convertToFormat(QImage.Format_RGB32))
            source = "reference"
        else:
            raise RuntimeError(f"Unsupported flat-field mode: {mode}")
        return build_median_flat_field_profile(
            frames,
            blur_radius_px=options.blur_radius_px,
            max_gain=options.max_gain,
            source=source,
        )

    def correct(
        self,
        frame: object,
        options: FlatFieldScanOptions,
        *,
        flat_field_profile: object | None,
    ) -> QImage:
        if not isinstance(frame, QImage):
            raise TypeError("Microscope scan frame must be a QImage.")
        mode = options.mode.lower()
        if mode in {"scan", "reference"}:
            if flat_field_profile is None:
                raise RuntimeError("Flat-field profile is unavailable.")
            flat_corrected = apply_flat_field_correction(frame, flat_field_profile)
        elif mode == "self":
            flat_corrected = apply_self_flat_field_correction(
                frame,
                blur_radius_px=options.blur_radius_px,
                max_gain=options.max_gain,
            )
        else:
            raise RuntimeError(f"Unsupported flat-field mode: {mode}")
        return self.correct_lens(flat_corrected)

    def restore_lock(self, restore_key: object) -> None:
        key = str(restore_key)
        result = self._wait_for_settings_override(
            restore_key=key,
            request=lambda: self.grabber.request_restore_camera_settings(
                restore_key=key,
            ),
        )
        if not bool(result.get("ok")):
            raise RuntimeError(
                str(result.get("message") or "Camera settings restore failed.")
            )

    def _wait_for_settings_override(
        self,
        *,
        restore_key: str,
        request: Callable[[], None],
    ) -> dict[str, object]:
        event = threading.Event()
        result_holder: dict[str, object] = {}

        def on_changed(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            if str(payload.get("restore_key") or "") != restore_key:
                return
            result_holder.update(payload)
            event.set()

        self.grabber.camera_settings_override_changed.connect(
            on_changed,
            Qt.ConnectionType.DirectConnection,
        )
        try:
            request()
            if not event.wait(self.settings_timeout_s):
                raise RuntimeError("Camera settings response timeout.")
            return dict(result_holder)
        finally:
            try:
                self.grabber.camera_settings_override_changed.disconnect(on_changed)
            except (TypeError, RuntimeError):
                pass


@dataclass
class MicroscopeScanArtifactAdapter:
    launch: ScanLaunchPort
    stage_position_for_metadata: Callable[..., tuple[float, ...] | None]

    def save_tile(
        self,
        captured: CapturedFrame,
        plan: MicroscopeScanPlan,
        request: MicroscopeScanRunRequest,
        corrections: Mapping[str, object],
    ) -> MicroscopeCaptureResult:
        extra: dict[str, object] = {"scan_launch": self.launch.metadata()}
        if corrections:
            extra["corrections"] = dict(corrections)
        if captured.actual_stage_position is not None:
            extra["actual_stage_position"] = [
                float(value) for value in captured.actual_stage_position
            ]
        save_plan = microscope_scan.tile_image_save_plan(
            output_dir=request.output_dir,
            scan_name=self.launch.scan_name,
            tile=captured.tile,
            plan=plan,
            captured_at=captured.captured_at,
            objective_name=self.launch.objective_name,
            magnification=self.launch.magnification,
            design_xy=self.launch.raw_stage_to_design(captured.tile.stage_xy),
            stage_position=self.stage_position_for_metadata(
                stage_xy=captured.tile.stage_xy
            ),
            extra=extra,
        )
        return save_microscope_image(
            frame=captured.frame,
            output_dir=save_plan.output_dir,
            filename_stem=save_plan.filename_stem,
            metadata=save_plan.metadata,
            scale=request.scale,
            save_raw=True,
        )

    def refine_scale(
        self,
        captured_tiles: Sequence[tuple[MicroscopeScanTile, object]],
        scale: object,
    ) -> object:
        return refine_scan_scale_from_tile_overlaps(captured_tiles, scale)

    def stitch(
        self,
        plan: MicroscopeScanPlan,
        tile_images: Sequence[tuple[MicroscopeScanTile, object]],
        scale: object,
    ) -> object:
        return stitch_scan_tiles(plan=plan, tile_images=tile_images, scale=scale)

    def save_mosaic(
        self,
        mosaic: object,
        plan: MicroscopeScanPlan,
        request: MicroscopeScanRunRequest,
        scale: object,
        corrections: Mapping[str, object],
        *,
        filename_suffix: str = "",
        extra: Mapping[str, object] | None = None,
    ) -> MicroscopeCaptureResult:
        metadata_extra: dict[str, object] = {}
        if corrections:
            metadata_extra["corrections"] = dict(corrections)
        if extra:
            metadata_extra.update(extra)
        metadata_extra["scan_launch"] = self.launch.metadata()
        save_plan = microscope_scan.mosaic_image_save_plan(
            output_dir=request.output_dir,
            scan_name=self.launch.scan_name,
            plan=plan,
            captured_at=utc_timestamp(),
            objective_name=self.launch.objective_name,
            magnification=self.launch.magnification,
            filename_suffix=filename_suffix,
            extra=metadata_extra,
        )
        return save_microscope_image(
            frame=mosaic,
            output_dir=save_plan.output_dir,
            filename_stem=save_plan.filename_stem,
            metadata=save_plan.metadata,
            scale=scale,
        )

    def write_manifest(
        self,
        request: MicroscopeScanRunRequest,
        plan: MicroscopeScanPlan,
        tile_results: Sequence[MicroscopeCaptureResult],
        mosaic_result: MicroscopeCaptureResult,
        corrections: Mapping[str, object],
        diagnostic_mosaics: Mapping[str, MicroscopeCaptureResult],
    ) -> Path:
        return microscope_scan.write_manifest(
            output_dir=request.output_dir,
            plan=plan,
            tile_results=tile_results,
            mosaic_result=mosaic_result,
            corrections=corrections,
            diagnostic_mosaics=diagnostic_mosaics or None,
        )


@dataclass
class MicroscopeScanSessionAdapter:
    manager: OpticalSessionManagerPort
    _lease: OpticalSessionLeasePort | None = None

    def acquire(self) -> Mapping[str, object]:
        self._lease = self.manager.open("microscope scan")
        try:
            return self._lease.snapshot()
        except BaseException:
            try:
                self._lease.close()
            except Exception:
                logger.exception(
                    "Microscope scan session cleanup after snapshot failure failed"
                )
            finally:
                self._lease = None
            raise

    def release(self) -> None:
        if self._lease is None:
            return
        result = self._lease.close()
        self._lease = None
        warning = str(result.get("warning") or "")
        if warning:
            raise RuntimeError(warning)


@dataclass(frozen=True)
class MicroscopeScanEventAdapter:
    status_callback: Callable[[str], None]
    finished_callback: Callable[[bool, str], None]

    def status(self, message: str) -> None:
        self.status_callback(message)

    def finished(self, success: bool, message: str) -> None:
        self.finished_callback(success, message)


__all__ = [
    "MicroscopeAreaScanRequest",
    "MicroscopeDesignScanRequest",
    "MicroscopeScanArtifactAdapter",
    "MicroscopeScanCameraAdapter",
    "MicroscopeScanEventAdapter",
    "MicroscopeScanSessionAdapter",
    "MicroscopeScanStageAdapter",
    "ScanLaunchPort",
    "build_microscope_scan_plan",
]
