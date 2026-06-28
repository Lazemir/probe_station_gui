"""Microscope design-scan planning and artifact payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from probe_station_gui.camera.imaging import (
    MicroscopeCaptureResult,
    MicroscopeImageMetadata,
    MicroscopeScanPlan,
    MicroscopeScanTile,
    build_design_scan_plan,
    scan_tile_filename,
    stage_bounds_from_design_bounds,
    utc_timestamp,
)
from probe_station_gui.design.model import DesignModelError


Point2D = tuple[float, float]


@dataclass(frozen=True)
class MicroscopeScanStatus:
    message: str
    timeout_ms: int


@dataclass(frozen=True)
class MicroscopeScanStartDecision:
    accepted: bool
    status: MicroscopeScanStatus | None = None
    plan: MicroscopeScanPlan | None = None


@dataclass(frozen=True)
class MicroscopeScanImageSavePlan:
    output_dir: Path
    filename_stem: str
    metadata: MicroscopeImageMetadata


def default_output_dir(document: object | None, *, cwd: Path | None = None) -> str:
    if document is not None:
        path = getattr(document, "path")
        return str(path.with_name(f"{path.stem}-microscope-scan"))
    return str((cwd or Path.cwd()) / "microscope-scan")


def output_dir_from_configuration(configuration: object) -> Path:
    return Path(getattr(configuration, "output_dir")).expanduser().resolve()


def start_environment_decision(
    *,
    scan_running: bool,
    serial_connected: bool,
) -> MicroscopeScanStartDecision:
    if scan_running:
        return _rejected("Microscope scan is already running.", 4000)
    if not serial_connected:
        return _rejected("Connect the stage controller before scanning.", 5000)
    return MicroscopeScanStartDecision(accepted=True)


def start_design_decision(
    *,
    document: object | None,
    registration_valid: bool,
) -> MicroscopeScanStartDecision:
    if document is None:
        return _rejected("Load a design before scanning.", 5000)
    if not registration_valid:
        return _rejected("Design registration is required before scanning.", 6000)
    return MicroscopeScanStartDecision(accepted=True)


def start_scale_decision(*, scale: object | None) -> MicroscopeScanStartDecision:
    if scale is None:
        return _rejected(
            "Calibrate click-to-move for the active objective before scanning.",
            8000,
        )
    return MicroscopeScanStartDecision(accepted=True)


def scan_plan_decision(
    *,
    document: object | None,
    scale: object | None,
    frame_size_px: tuple[int, int] | None,
    overlap_fraction: float,
    design_to_stage_xy: Callable[[Point2D], Point2D | None],
) -> MicroscopeScanStartDecision:
    if document is None:
        return _rejected("Load a design before scanning.", 5000)
    if scale is None:
        return _rejected(
            "Calibrate click-to-move for the active objective before scanning.",
            8000,
        )
    if frame_size_px is None:
        return _rejected("Camera frame is unavailable; cannot scan.", 8000)
    width_px, height_px = frame_size_px
    fov_size_mm = (
        float(width_px) * float(getattr(scale, "pixel_size_x_mm")),
        float(height_px) * float(getattr(scale, "pixel_size_y_mm")),
    )
    try:
        stage_bounds = stage_bounds_from_design_bounds(
            getattr(document, "bounds"),
            design_to_stage_xy,
        )
        plan = build_design_scan_plan(
            stage_bounds=stage_bounds,
            fov_size_mm=fov_size_mm,
            overlap_fraction=overlap_fraction,
        )
    except (ValueError, DesignModelError) as exc:
        return _rejected(str(exc), 8000)
    if not plan.tiles:
        return _rejected("Microscope scan plan has no tiles.", 5000)
    return MicroscopeScanStartDecision(accepted=True, plan=plan)


def starting_status(plan: MicroscopeScanPlan) -> str:
    return f"Microscope scan starting: {len(plan.tiles)} tiles."


def tile_status(tile: MicroscopeScanTile, total_tiles: int) -> str:
    return f"Microscope scan: tile {tile.index}/{int(total_tiles)}."


def completion_message(
    *,
    tile_count: int,
    mosaic_result: MicroscopeCaptureResult,
    manifest_path: Path,
) -> str:
    return (
        f"Microscope scan complete: {int(tile_count)} tiles, "
        f"mosaic {mosaic_result.image_path}, manifest {manifest_path}."
    )


def scan_name_from_document(document: object | None) -> str:
    if document is None:
        return "design_scan"
    return str(getattr(document, "path").stem)


def tile_image_save_plan(
    *,
    output_dir: Path,
    scan_name: str,
    tile: MicroscopeScanTile,
    plan: MicroscopeScanPlan,
    captured_at: str,
    objective_name: str,
    magnification: float | None,
    design_xy: Point2D | None,
    stage_position: tuple[float, ...] | None,
) -> MicroscopeScanImageSavePlan:
    metadata = MicroscopeImageMetadata(
        title="Probe Station Design Scan",
        mode="design scan tile",
        captured_at=captured_at,
        objective_name=objective_name,
        magnification=magnification,
        scan_tile_index=tile.index,
        scan_tile_total=len(plan.tiles),
        scan_row=tile.row,
        scan_column=tile.column,
        design_xy=design_xy,
        stage_position=stage_position,
        stage_xy=tile.stage_xy,
        notes=("needles raised before scan",),
        extra={
            "overlap_fraction": plan.overlap_fraction,
            "row_count": plan.row_count,
            "column_count": plan.column_count,
        },
    )
    return MicroscopeScanImageSavePlan(
        output_dir=output_dir / "tiles",
        filename_stem=scan_tile_filename(
            scan_name=scan_name,
            tile=tile,
            captured_at=captured_at,
        ),
        metadata=metadata,
    )


def mosaic_image_save_plan(
    *,
    output_dir: Path,
    scan_name: str,
    plan: MicroscopeScanPlan,
    captured_at: str,
    objective_name: str,
    magnification: float | None,
) -> MicroscopeScanImageSavePlan:
    metadata = MicroscopeImageMetadata(
        title="Probe Station Design Scan Mosaic",
        mode="design scan mosaic",
        captured_at=captured_at,
        objective_name=objective_name,
        magnification=magnification,
        scan_tile_total=len(plan.tiles),
        notes=("stage-coordinate tile mosaic",),
        extra={
            "overlap_fraction": plan.overlap_fraction,
            "row_count": plan.row_count,
            "column_count": plan.column_count,
            "stage_bounds": list(plan.stage_bounds),
            "covered_stage_bounds": list(plan.covered_stage_bounds),
            "fov_size_mm": list(plan.fov_size_mm),
        },
    )
    return MicroscopeScanImageSavePlan(
        output_dir=output_dir,
        filename_stem=f"{scan_name}_mosaic_{captured_at}",
        metadata=metadata,
    )


def manifest_payload(
    *,
    plan: MicroscopeScanPlan,
    tile_results: Sequence[MicroscopeCaptureResult],
    mosaic_result: MicroscopeCaptureResult,
    created_at: str,
) -> dict[str, object]:
    return {
        "version": 1,
        "created_at": created_at,
        "tile_count": len(tile_results),
        "row_count": plan.row_count,
        "column_count": plan.column_count,
        "overlap_fraction": plan.overlap_fraction,
        "stage_bounds": list(plan.stage_bounds),
        "covered_stage_bounds": list(plan.covered_stage_bounds),
        "fov_size_mm": list(plan.fov_size_mm),
        "mosaic": {
            "image": str(mosaic_result.image_path),
            "metadata": str(mosaic_result.metadata_path),
        },
        "tiles": [
            {
                "index": tile.index,
                "row": tile.row,
                "column": tile.column,
                "stage_xy": list(tile.stage_xy),
                "image": str(result.image_path),
                "metadata": str(result.metadata_path),
            }
            for tile, result in zip(plan.tiles, tile_results)
        ],
    }


def write_manifest(
    *,
    output_dir: Path,
    plan: MicroscopeScanPlan,
    tile_results: Sequence[MicroscopeCaptureResult],
    mosaic_result: MicroscopeCaptureResult,
    created_at: str | None = None,
) -> Path:
    manifest_path = output_dir / "microscope-scan-manifest.json"
    data = manifest_payload(
        plan=plan,
        tile_results=tile_results,
        mosaic_result=mosaic_result,
        created_at=created_at or utc_timestamp(),
    )
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    return manifest_path


def _rejected(message: str, timeout_ms: int) -> MicroscopeScanStartDecision:
    return MicroscopeScanStartDecision(
        accepted=False,
        status=MicroscopeScanStatus(message, timeout_ms),
    )


__all__ = [
    "MicroscopeScanStartDecision",
    "MicroscopeScanStatus",
    "completion_message",
    "default_output_dir",
    "output_dir_from_configuration",
    "scan_plan_decision",
    "scan_name_from_document",
    "start_design_decision",
    "start_environment_decision",
    "start_scale_decision",
    "starting_status",
    "mosaic_image_save_plan",
    "tile_image_save_plan",
    "tile_status",
    "write_manifest",
]
