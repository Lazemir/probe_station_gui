"""Runtime transaction for microscope tile scans."""

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
from probe_station_gui.camera import microscope_scan
from probe_station_gui.camera.microscope_scan import (
    CameraLockSettings,
    FlatFieldScanOptions,
    completion_message,
    starting_status,
    stitch_debug_mosaic_group_plan,
    stitch_debug_mosaic_groups,
    tile_status,
)


logger = logging.getLogger(__name__)


class ScanLaunchPort(Protocol):
    objective_name: str
    magnification: float | None
    scan_name: str
    scale: object

    def metadata(self) -> dict[str, object]: ...

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
    def connect(self, callback: Callable[[object], None], connection_type: object) -> None: ...

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
            raise RuntimeError(str(result.get("message") or "Camera settings lock failed."))
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
        if options.mode in {"scan", "reference"}:
            if flat_field_profile is None:
                raise RuntimeError("Flat-field profile is unavailable.")
            flat_corrected = apply_flat_field_correction(frame, flat_field_profile)
        elif options.mode == "self":
            flat_corrected = apply_self_flat_field_correction(
                frame,
                blur_radius_px=options.blur_radius_px,
                max_gain=options.max_gain,
            )
        else:
            raise RuntimeError(f"Unsupported flat-field mode: {options.mode}")
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
        tile_results: Sequence[object],
        mosaic_result: object,
        corrections: Mapping[str, object],
        diagnostic_mosaics: Mapping[str, object],
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


class StagePort(Protocol):
    def reserve(self) -> tuple[float, float]: ...

    def raise_needles(self) -> None: ...

    def move_to(self, tile: MicroscopeScanTile, *, approach_mm: float) -> None: ...

    def actual_position(self) -> tuple[float, ...] | None: ...

    def return_to(self, start_xy: tuple[float, float]) -> None: ...

    def release(self) -> None: ...


class CameraPort(Protocol):
    def planning_frame_size(self) -> tuple[int, int]: ...

    def apply_lock(self, settings: CameraLockSettings) -> object | None: ...

    def settle(self, settle_s: float) -> bool: ...

    def stop_requested(self) -> bool: ...

    def capture(self, *, raw: bool) -> object: ...

    def flat_field_profile(
        self,
        captured_frames: Sequence["CapturedFrame"],
        options: FlatFieldScanOptions,
    ) -> object | None: ...

    def correct(
        self,
        frame: object,
        options: FlatFieldScanOptions,
        *,
        flat_field_profile: object | None,
    ) -> object: ...

    def restore_lock(self, restore_key: object) -> None: ...


class ArtifactPort(Protocol):
    def save_tile(
        self,
        captured: "CapturedFrame",
        plan: MicroscopeScanPlan,
        request: "MicroscopeScanRunRequest",
        corrections: Mapping[str, object],
    ) -> object: ...

    def refine_scale(
        self,
        captured_tiles: Sequence[tuple[MicroscopeScanTile, object]],
        scale: object,
    ) -> object: ...

    def stitch(
        self,
        plan: MicroscopeScanPlan,
        tile_images: Sequence[tuple[MicroscopeScanTile, object]],
        scale: object,
    ) -> object: ...

    def save_mosaic(
        self,
        mosaic: object,
        plan: MicroscopeScanPlan,
        request: "MicroscopeScanRunRequest",
        scale: object,
        corrections: Mapping[str, object],
        *,
        filename_suffix: str = "",
        extra: Mapping[str, object] | None = None,
    ) -> object: ...

    def write_manifest(
        self,
        request: "MicroscopeScanRunRequest",
        plan: MicroscopeScanPlan,
        tile_results: Sequence[object],
        mosaic_result: object,
        corrections: Mapping[str, object],
        diagnostic_mosaics: Mapping[str, object],
    ) -> Path: ...


class EventSink(Protocol):
    def status(self, message: str) -> None: ...

    def finished(self, success: bool, message: str) -> None: ...


class SessionPort(Protocol):
    def acquire(self) -> Mapping[str, object]: ...

    def release(self) -> None: ...


@dataclass(frozen=True)
class CapturedFrame:
    tile: MicroscopeScanTile
    frame: object
    captured_at: str
    actual_stage_position: tuple[float, ...] | None


@dataclass(frozen=True)
class MicroscopeScanRunRequest:
    output_dir: Path
    scale: object | None
    plan: MicroscopeScanPlan | None = None
    plan_factory: (
        Callable[[tuple[int, int], tuple[float, float]], MicroscopeScanPlan] | None
    ) = None
    flat_field_options: FlatFieldScanOptions = FlatFieldScanOptions(enabled=False)
    camera_lock_settings: CameraLockSettings = CameraLockSettings(enabled=False)
    settle_s: float = 0.0
    tile_approach_mm: float = 0.0
    scan_pattern: str = "grid"
    refine_scale_from_overlaps: bool = True


@dataclass
class _RunResources:
    session_acquired: bool = False
    stage_reserved: bool = False
    return_required: bool = False
    start_stage_xy: tuple[float, float] | None = None
    camera_restore_key: object | None = None


class MicroscopeScanRuntime:
    def __init__(
        self,
        *,
        stage: StagePort,
        camera: CameraPort,
        artifacts: ArtifactPort,
        event_sink: EventSink,
        session: SessionPort,
    ) -> None:
        self._stage = stage
        self._camera = camera
        self._artifacts = artifacts
        self._events = event_sink
        self._session = session

    def run(self, request: MicroscopeScanRunRequest) -> None:
        success = False
        message = "Microscope scan stopped."
        resources = _RunResources()
        try:
            plan, session_snapshot = self._acquire(request, resources)
            captured_frames, stopped, _stage_position_changed = self._capture_tiles(
                request,
                plan,
            )
            if stopped:
                message = "Microscope scan stopped by user."
            else:
                corrections = _corrections_metadata(request, session_snapshot)
                message = self._finalize_artifacts(
                    request,
                    plan,
                    captured_frames,
                    corrections,
                )
                success = True
        except Exception as exc:
            logger.exception("Microscope scan failed")
            message = f"Microscope scan failed: {exc}"
        finally:
            success, message = self._cleanup(resources, success, message)
            self._events.finished(success, message)

    def _acquire(
        self,
        request: MicroscopeScanRunRequest,
        resources: _RunResources,
    ) -> tuple[MicroscopeScanPlan, Mapping[str, object]]:
        self._events.status("Microscope scan: fixing exposure.")
        session_snapshot = self._session.acquire()
        resources.session_acquired = True
        if request.scale is None:
            raise RuntimeError("Active objective has no calibrated microscope scale.")
        resources.start_stage_xy = self._stage.reserve()
        resources.stage_reserved = True
        frame_size = self._camera.planning_frame_size()
        plan = self._plan(request, frame_size, resources.start_stage_xy)
        resources.return_required = True
        self._events.status(starting_status(plan))
        request.output_dir.mkdir(parents=True, exist_ok=True)
        if request.camera_lock_settings.enabled and request.camera_lock_settings.settings:
            self._events.status("Microscope scan: locking camera settings.")
        resources.camera_restore_key = self._camera.apply_lock(
            request.camera_lock_settings
        )
        self._events.status("Microscope scan: raising needles.")
        self._stage.raise_needles()
        return plan, session_snapshot

    def _cleanup(
        self,
        resources: _RunResources,
        success: bool,
        message: str,
    ) -> tuple[bool, str]:
        if (
            resources.stage_reserved
            and resources.return_required
            and resources.start_stage_xy is not None
        ):
            try:
                self._events.status("Microscope scan: returning to start.")
                self._stage.return_to(resources.start_stage_xy)
            except Exception as exc:
                logger.exception("Microscope scan return to start failed")
                success, message = _cleanup_failure(
                    success, message, "return to start", exc
                )
        if resources.camera_restore_key is not None:
            try:
                self._events.status("Microscope scan: restoring camera settings.")
                self._camera.restore_lock(resources.camera_restore_key)
            except Exception as exc:
                logger.exception("Microscope scan camera restore failed")
                success, message = _cleanup_failure(
                    success, message, "camera settings restore", exc
                )
        if resources.stage_reserved:
            try:
                self._stage.release()
            except Exception as exc:
                logger.exception("Microscope scan stage release failed")
                success, message = _cleanup_failure(
                    success, message, "stage release", exc
                )
        if resources.session_acquired:
            try:
                self._session.release()
            except Exception as exc:
                logger.exception("Microscope scan exposure policy restore failed")
                success, message = _cleanup_failure(
                    success, message, "exposure policy restore", exc
                )
        return success, message

    def _capture_tiles(
        self,
        request: MicroscopeScanRunRequest,
        plan: MicroscopeScanPlan,
    ) -> tuple[list[CapturedFrame], bool, bool]:
        captured_frames: list[CapturedFrame] = []
        stage_position_changed = False
        for tile in plan.tiles:
            if self._camera.stop_requested():
                return captured_frames, True, stage_position_changed
            self._events.status(tile_status(tile, len(plan.tiles)))
            stage_position_changed = True
            self._stage.move_to(tile, approach_mm=request.tile_approach_mm)
            if not self._camera.settle(request.settle_s):
                return captured_frames, True, stage_position_changed
            captured_frames.append(
                CapturedFrame(
                    tile=tile,
                    frame=self._camera.capture(raw=request.flat_field_options.enabled),
                    captured_at=utc_timestamp(),
                    actual_stage_position=self._stage.actual_position(),
                )
            )
        return captured_frames, False, stage_position_changed

    def _finalize_artifacts(
        self,
        request: MicroscopeScanRunRequest,
        plan: MicroscopeScanPlan,
        captured_frames: Sequence[CapturedFrame],
        corrections: Mapping[str, object],
    ) -> str:
        flat_field_profile = self._camera.flat_field_profile(
            captured_frames,
            request.flat_field_options,
        )
        captured_tiles: list[tuple[MicroscopeScanTile, object]] = []
        tile_results: list[object] = []
        for captured in captured_frames:
            frame = captured.frame
            if request.flat_field_options.enabled:
                frame = self._camera.correct(
                    frame,
                    request.flat_field_options,
                    flat_field_profile=flat_field_profile,
                )
                captured = CapturedFrame(
                    tile=captured.tile,
                    frame=frame,
                    captured_at=captured.captured_at,
                    actual_stage_position=captured.actual_stage_position,
                )
            result = self._artifacts.save_tile(
                captured,
                plan,
                request,
                corrections,
            )
            tile_results.append(result)
            captured_tiles.append((captured.tile, result.raw_image))
        stitch_scale = request.scale
        if request.refine_scale_from_overlaps:
            stitch_scale = self._artifacts.refine_scale(captured_tiles, request.scale)
        diagnostic_mosaics: dict[str, object] = {}
        if request.scan_pattern == "stitch_debug":
            mosaic_result = self._save_diagnostic_mosaics(
                request,
                plan,
                captured_tiles,
                stitch_scale,
                corrections,
                diagnostic_mosaics,
            )
        else:
            mosaic = self._artifacts.stitch(plan, captured_tiles, stitch_scale)
            mosaic_result = self._artifacts.save_mosaic(
                mosaic,
                plan,
                request,
                stitch_scale,
                corrections,
            )
        manifest_path = self._artifacts.write_manifest(
            request,
            plan,
            tile_results,
            mosaic_result,
            corrections,
            diagnostic_mosaics,
        )
        if diagnostic_mosaics:
            paths = ", ".join(
                f"{name} {result.image_path}"
                for name, result in diagnostic_mosaics.items()
            )
            return (
                f"Microscope seam debug complete: {len(tile_results)} tiles, "
                f"{paths}, manifest {manifest_path}."
            )
        return completion_message(
            tile_count=len(tile_results),
            mosaic_result=mosaic_result,
            manifest_path=manifest_path,
        )

    def _save_diagnostic_mosaics(
        self,
        request: MicroscopeScanRunRequest,
        plan: MicroscopeScanPlan,
        captured_tiles: Sequence[tuple[MicroscopeScanTile, object]],
        scale: object,
        corrections: Mapping[str, object],
        results: dict[str, object],
    ) -> object:
        raw_images_by_label = {
            tile.label: image for tile, image in captured_tiles
        }
        for group in stitch_debug_mosaic_groups(plan):
            group_plan = stitch_debug_mosaic_group_plan(plan, group)
            group_tile_images = [
                (tile, raw_images_by_label[tile.label]) for tile in group_plan.tiles
            ]
            group_mosaic = self._artifacts.stitch(
                group_plan,
                group_tile_images,
                scale,
            )
            results[group.name] = self._artifacts.save_mosaic(
                group_mosaic,
                group_plan,
                request,
                scale,
                corrections,
                filename_suffix=group.name,
                extra={
                    "stitch_debug_group": group.name,
                    "stitch_debug_tiles": [tile.label for tile in group_plan.tiles],
                },
            )
        return next(iter(results.values()))

    @staticmethod
    def _plan(
        request: MicroscopeScanRunRequest,
        frame_size: tuple[int, int],
        start_stage_xy: tuple[float, float],
    ) -> MicroscopeScanPlan:
        if request.plan_factory is not None:
            return request.plan_factory(frame_size, start_stage_xy)
        if request.plan is not None:
            return request.plan
        raise RuntimeError("Microscope scan plan is unavailable.")


def _cleanup_failure(
    success: bool,
    message: str,
    operation: str,
    error: object,
) -> tuple[bool, str]:
    detail = str(error) or type(error).__name__
    if success:
        return False, f"Microscope scan complete, but {operation} failed: {detail}"
    return False, f"{message} {operation.capitalize()} failed: {detail}"


def _corrections_metadata(
    request: MicroscopeScanRunRequest,
    optical_session: Mapping[str, object],
) -> dict[str, object]:
    corrections: dict[str, object] = {"optical_session": dict(optical_session)}
    flat_metadata = request.flat_field_options.to_metadata()
    if flat_metadata["enabled"]:
        corrections["flat_field"] = flat_metadata
    lock_metadata = request.camera_lock_settings.to_metadata()
    if lock_metadata["enabled"]:
        corrections["camera_lock"] = lock_metadata
    if request.scan_pattern != "grid" or not request.refine_scale_from_overlaps:
        corrections["scan"] = {
            "pattern": request.scan_pattern,
            "refine_scale_from_overlaps": request.refine_scale_from_overlaps,
        }
    return corrections


__all__ = ["MicroscopeScanRunRequest", "MicroscopeScanRuntime"]
