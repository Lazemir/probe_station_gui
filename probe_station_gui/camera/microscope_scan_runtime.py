"""Core runtime transaction for microscope tile scans."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from probe_station_gui.camera.imaging import (
    MicroscopeScanPlan,
    MicroscopeScanTile,
    utc_timestamp,
)
from probe_station_gui.camera.microscope_artifacts import MicroscopeCaptureResult
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


class StagePort(Protocol):
    def reserve(self) -> tuple[float, float]: ...

    def raise_needles(self) -> None: ...

    def move_to(self, tile: MicroscopeScanTile) -> None: ...

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
    ) -> MicroscopeCaptureResult: ...

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
    ) -> MicroscopeCaptureResult: ...

    def write_manifest(
        self,
        request: "MicroscopeScanRunRequest",
        plan: MicroscopeScanPlan,
        tile_results: Sequence[MicroscopeCaptureResult],
        mosaic_result: MicroscopeCaptureResult,
        corrections: Mapping[str, object],
        diagnostic_mosaics: Mapping[str, MicroscopeCaptureResult],
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
            captured_frames, stopped = self._capture_tiles(request, plan)
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
    ) -> tuple[list[CapturedFrame], bool]:
        captured_frames: list[CapturedFrame] = []
        for tile in plan.tiles:
            if self._camera.stop_requested():
                return captured_frames, True
            self._events.status(tile_status(tile, len(plan.tiles)))
            self._stage.move_to(tile)
            if not self._camera.settle(request.settle_s):
                return captured_frames, True
            captured_frames.append(
                CapturedFrame(
                    tile=tile,
                    frame=self._camera.capture(raw=request.flat_field_options.enabled),
                    captured_at=utc_timestamp(),
                    actual_stage_position=self._stage.actual_position(),
                )
            )
        return captured_frames, False

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
        tile_results: list[MicroscopeCaptureResult] = []
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
            result = self._artifacts.save_tile(captured, plan, request, corrections)
            tile_results.append(result)
            captured_tiles.append((captured.tile, result.raw_image))
        stitch_scale = request.scale
        if request.refine_scale_from_overlaps:
            stitch_scale = self._artifacts.refine_scale(captured_tiles, request.scale)
        diagnostic_mosaics: dict[str, MicroscopeCaptureResult] = {}
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
        results: dict[str, MicroscopeCaptureResult],
    ) -> MicroscopeCaptureResult:
        raw_images_by_label = {tile.label: image for tile, image in captured_tiles}
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


__all__ = [
    "ArtifactPort",
    "CapturedFrame",
    "MicroscopeScanRunRequest",
    "MicroscopeScanRuntime",
]
