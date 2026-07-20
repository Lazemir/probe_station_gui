"""Microscope design-scan planning and artifact payloads."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from probe_station_gui.camera.imaging import (
    MicroscopeCaptureResult,
    MicroscopeImageMetadata,
    MicroscopeScanPlan,
    MicroscopeScanTile,
    build_design_scan_plan,
    safe_filename_component,
    scan_tile_filename,
    stage_bounds_from_design_bounds,
    utc_timestamp,
)
from probe_station_gui.design.model import DesignModelError


Point2D = tuple[float, float]
DEFAULT_FLAT_FIELD_BLUR_RADIUS_PX = 401
DEFAULT_FLAT_FIELD_MAX_GAIN = 4.0
DEFAULT_CAMERA_LOCK_SETTINGS: tuple[tuple[str, object], ...] = (
    ("GainAuto", "Off"),
    ("BalanceWhiteAuto", "Off"),
)


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


@dataclass(frozen=True)
class FlatFieldScanOptions:
    enabled: bool
    mode: str = "scan"
    blur_radius_px: int = DEFAULT_FLAT_FIELD_BLUR_RADIUS_PX
    max_gain: float = DEFAULT_FLAT_FIELD_MAX_GAIN
    reference_images: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": bool(self.enabled),
            "mode": self.mode,
            "blur_radius_px": int(self.blur_radius_px),
            "max_gain": float(self.max_gain),
        }
        if self.reference_images:
            payload["reference_images"] = [str(path) for path in self.reference_images]
        return payload


@dataclass(frozen=True)
class CameraLockSettings:
    enabled: bool
    settings: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(
            (str(node_name).strip(), value)
            for node_name, value in self.settings
        )
        for node_name, _value in normalized:
            if node_name.casefold() in {"exposureauto", "exposuretime"}:
                raise ValueError(
                    f"Camera lock setting {node_name} is owned by the optical session."
                )
        object.__setattr__(self, "settings", normalized)

    def to_metadata(self) -> dict[str, object]:
        return {
            "enabled": bool(self.enabled),
            "settings": [
                {"node_name": node_name, "value": value}
                for node_name, value in self.settings
            ],
        }


@dataclass(frozen=True)
class StitchDebugMosaicGroup:
    name: str
    tiles: tuple[MicroscopeScanTile, ...]


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


def centered_area_scan_plan(
    *,
    center_stage_xy: Point2D,
    fov_size_mm: Point2D,
    row_count: int,
    column_count: int,
    overlap_fraction: float,
) -> MicroscopeScanPlan:
    """Build a serpentine tile plan centered on the current stage position."""

    rows = _positive_int(row_count, "row_count")
    columns = _positive_int(column_count, "column_count")
    fov_w = _positive_float(fov_size_mm[0], "FOV width")
    fov_h = _positive_float(fov_size_mm[1], "FOV height")
    overlap = min(max(float(overlap_fraction), 0.0), 0.95)
    step_x = fov_w * (1.0 - overlap)
    step_y = fov_h * (1.0 - overlap)
    center_x = float(center_stage_xy[0])
    center_y = float(center_stage_xy[1])
    x_start = center_x + step_x * float(columns - 1) * 0.5
    y_top = center_y + step_y * float(rows - 1) * 0.5

    tiles: list[MicroscopeScanTile] = []
    for row in range(rows):
        columns_for_row = list(range(columns))
        if row % 2 == 1:
            columns_for_row.reverse()
        y_value = y_top - step_y * float(row)
        for column in columns_for_row:
            x_value = x_start - step_x * float(column)
            tiles.append(
                MicroscopeScanTile(
                    index=len(tiles) + 1,
                    row=row,
                    column=column,
                    stage_xy=(float(x_value), float(y_value)),
                )
            )
    left = min(tile.stage_xy[0] for tile in tiles) - fov_w * 0.5
    right = max(tile.stage_xy[0] for tile in tiles) + fov_w * 0.5
    bottom = min(tile.stage_xy[1] for tile in tiles) - fov_h * 0.5
    top = max(tile.stage_xy[1] for tile in tiles) + fov_h * 0.5
    return MicroscopeScanPlan(
        tiles=tuple(tiles),
        stage_bounds=(float(left), float(bottom), float(right), float(top)),
        covered_stage_bounds=(float(left), float(bottom), float(right), float(top)),
        fov_size_mm=(fov_w, fov_h),
        overlap_fraction=overlap,
        row_count=rows,
        column_count=columns,
    )


def centered_area_scan_plan_from_pixel_matrix(
    *,
    center_stage_xy: Point2D,
    frame_size_px: tuple[int, int],
    pixels_to_mm: Sequence[Sequence[float]],
    row_count: int,
    column_count: int,
    overlap_fraction: float,
) -> MicroscopeScanPlan:
    """Build a centered area plan using the full pixel-to-stage calibration matrix."""

    rows = _positive_int(row_count, "row_count")
    columns = _positive_int(column_count, "column_count")
    width_px = _positive_int(frame_size_px[0], "frame width")
    height_px = _positive_int(frame_size_px[1], "frame height")
    matrix = _coerce_pixel_matrix(pixels_to_mm)
    overlap = min(max(float(overlap_fraction), 0.0), 0.95)
    step_x_px = float(width_px) * (1.0 - overlap)
    step_y_px = float(height_px) * (1.0 - overlap)
    mid_col = float(columns - 1) * 0.5
    mid_row = float(rows - 1) * 0.5
    center_x = float(center_stage_xy[0])
    center_y = float(center_stage_xy[1])

    tiles: list[MicroscopeScanTile] = []
    for row in range(rows):
        columns_for_row = list(range(columns))
        if row % 2 == 1:
            columns_for_row.reverse()
        for column in columns_for_row:
            offset_x_px = (float(column) - mid_col) * step_x_px
            offset_y_px = (float(row) - mid_row) * step_y_px
            dx_mm, dy_mm = _pixel_delta_to_stage(matrix, -offset_x_px, offset_y_px)
            tiles.append(
                MicroscopeScanTile(
                    index=len(tiles) + 1,
                    row=row,
                    column=column,
                    stage_xy=(float(center_x + dx_mm), float(center_y + dy_mm)),
                )
            )
    bounds = _stage_bounds_for_matrix_tiles(
        [tile.stage_xy for tile in tiles],
        frame_size_px=(width_px, height_px),
        matrix=matrix,
    )
    x_vector = _pixel_delta_to_stage(matrix, float(width_px), 0.0)
    y_vector = _pixel_delta_to_stage(matrix, 0.0, float(height_px))
    return MicroscopeScanPlan(
        tiles=tuple(tiles),
        stage_bounds=bounds,
        covered_stage_bounds=bounds,
        fov_size_mm=(math.hypot(*x_vector), math.hypot(*y_vector)),
        overlap_fraction=overlap,
        row_count=rows,
        column_count=columns,
    )


def stitch_debug_scan_plan_from_pixel_matrix(
    *,
    center_stage_xy: Point2D,
    frame_size_px: tuple[int, int],
    pixels_to_mm: Sequence[Sequence[float]],
    structure_size_mm: float,
    placement_fraction: float = 1.0,
    overlap_fraction: float = 0.0,
) -> MicroscopeScanPlan:
    """Build a seam-focused stitch-debug plan around a centered structure."""

    width_px = _positive_int(frame_size_px[0], "frame width")
    height_px = _positive_int(frame_size_px[1], "frame height")
    matrix = _coerce_pixel_matrix(pixels_to_mm)
    structure_size = _positive_float(structure_size_mm, "structure_size_mm")
    try:
        placement = float(placement_fraction)
    except (TypeError, ValueError) as exc:
        raise ValueError("placement_fraction must be between 0 and 1.") from exc
    if not math.isfinite(placement) or placement < 0.0 or placement > 1.0:
        raise ValueError("placement_fraction must be between 0 and 1.")
    try:
        overlap = float(overlap_fraction)
    except (TypeError, ValueError) as exc:
        raise ValueError("overlap_fraction must be between 0 and 0.95.") from exc
    if not math.isfinite(overlap) or overlap < 0.0 or overlap > 0.95:
        raise ValueError("overlap_fraction must be between 0 and 0.95.")

    x_vector = _pixel_delta_to_stage(matrix, float(width_px), 0.0)
    y_vector = _pixel_delta_to_stage(matrix, 0.0, float(height_px))
    fov_x_mm = math.hypot(*x_vector)
    fov_y_mm = math.hypot(*y_vector)
    if structure_size >= fov_x_mm or structure_size >= fov_y_mm:
        raise ValueError("structure_size_mm must be smaller than the camera FOV.")
    seam_offset_fraction = (1.0 - overlap) * placement
    edge_x_px = float(width_px) * 0.5 * seam_offset_fraction
    edge_y_px = float(height_px) * 0.5 * seam_offset_fraction
    center_x = float(center_stage_xy[0])
    center_y = float(center_stage_xy[1])
    positions = (
        (1, 1, 0.0, 0.0, "control"),
        (1, 0, edge_x_px, 0.0, "vertical_left"),
        (1, 2, -edge_x_px, 0.0, "vertical_right"),
        (0, 1, 0.0, edge_y_px, "horizontal_top"),
        (2, 1, 0.0, -edge_y_px, "horizontal_bottom"),
        (0, 0, edge_x_px, edge_y_px, "corner_top_left"),
        (0, 2, -edge_x_px, edge_y_px, "corner_top_right"),
        (2, 0, edge_x_px, -edge_y_px, "corner_bottom_left"),
        (2, 2, -edge_x_px, -edge_y_px, "corner_bottom_right"),
    )
    tiles: list[MicroscopeScanTile] = []
    for row, column, image_dx_px, image_dy_px, label in positions:
        dx_mm, dy_mm = _pixel_delta_to_stage(matrix, -image_dx_px, image_dy_px)
        tiles.append(
            MicroscopeScanTile(
                index=len(tiles) + 1,
                row=row,
                column=column,
                stage_xy=(float(center_x + dx_mm), float(center_y + dy_mm)),
                label=label,
            )
        )
    bounds = _stage_bounds_for_matrix_tiles(
        [tile.stage_xy for tile in tiles],
        frame_size_px=(width_px, height_px),
        matrix=matrix,
    )
    return MicroscopeScanPlan(
        tiles=tuple(tiles),
        stage_bounds=bounds,
        covered_stage_bounds=bounds,
        fov_size_mm=(float(fov_x_mm), float(fov_y_mm)),
        overlap_fraction=float(overlap),
        row_count=3,
        column_count=3,
    )


def stitch_debug_mosaic_groups(
    plan: MicroscopeScanPlan,
) -> tuple[StitchDebugMosaicGroup, ...]:
    by_label = {
        str(getattr(tile, "label", "") or ""): tile
        for tile in plan.tiles
    }

    def tiles(*labels: str) -> tuple[MicroscopeScanTile, ...]:
        try:
            return tuple(by_label[label] for label in labels)
        except KeyError as exc:
            raise ValueError("Plan is not a stitch-debug seam test plan.") from exc

    return (
        StitchDebugMosaicGroup(
            name="vertical_seam",
            tiles=tiles("vertical_left", "vertical_right"),
        ),
        StitchDebugMosaicGroup(
            name="horizontal_seam",
            tiles=tiles("horizontal_top", "horizontal_bottom"),
        ),
        StitchDebugMosaicGroup(
            name="corner_seam",
            tiles=tiles(
                "corner_top_left",
                "corner_top_right",
                "corner_bottom_left",
                "corner_bottom_right",
            ),
        ),
    )


def stitch_debug_mosaic_group_plan(
    parent_plan: MicroscopeScanPlan,
    group: StitchDebugMosaicGroup,
) -> MicroscopeScanPlan:
    if not group.tiles:
        raise ValueError("Stitch-debug mosaic group has no tiles.")
    rows = {int(tile.row) for tile in group.tiles}
    columns = {int(tile.column) for tile in group.tiles}
    row_map = {row: index for index, row in enumerate(sorted(rows))}
    column_map = {column: index for index, column in enumerate(sorted(columns))}
    tiles = tuple(
        replace(
            tile,
            row=row_map[int(tile.row)],
            column=column_map[int(tile.column)],
        )
        for tile in group.tiles
    )
    half_w = float(parent_plan.fov_size_mm[0]) * 0.5
    half_h = float(parent_plan.fov_size_mm[1]) * 0.5
    left = min(float(tile.stage_xy[0]) for tile in tiles) - half_w
    right = max(float(tile.stage_xy[0]) for tile in tiles) + half_w
    bottom = min(float(tile.stage_xy[1]) for tile in tiles) - half_h
    top = max(float(tile.stage_xy[1]) for tile in tiles) + half_h
    bounds = (float(left), float(bottom), float(right), float(top))
    return MicroscopeScanPlan(
        tiles=tiles,
        stage_bounds=bounds,
        covered_stage_bounds=bounds,
        fov_size_mm=parent_plan.fov_size_mm,
        overlap_fraction=parent_plan.overlap_fraction,
        row_count=len(row_map),
        column_count=len(column_map),
    )


def flat_field_options_from_payload(
    payload: Mapping[str, object],
    *,
    default_enabled: bool = False,
) -> FlatFieldScanOptions:
    value = payload.get("flat_field", default_enabled)
    if isinstance(value, Mapping):
        enabled = _bool_from_payload(value.get("enabled", default_enabled))
        mode = str(value.get("mode") or "scan").strip().lower()
        if mode not in {"scan", "self", "reference"}:
            raise ValueError("flat_field.mode must be 'scan', 'self', or 'reference'.")
        reference_images = _flat_field_reference_images_from_payload(value)
        if enabled and mode == "reference" and not reference_images:
            raise ValueError(
                "flat_field.reference_images is required when mode is 'reference'."
            )
        blur_radius = _positive_int(
            value.get("blur_radius_px", DEFAULT_FLAT_FIELD_BLUR_RADIUS_PX),
            "flat_field.blur_radius_px",
        )
        if blur_radius % 2 == 0:
            blur_radius += 1
        max_gain = _positive_float(
            value.get("max_gain", DEFAULT_FLAT_FIELD_MAX_GAIN),
            "flat_field.max_gain",
        )
        return FlatFieldScanOptions(
            enabled=enabled,
            mode=mode,
            blur_radius_px=blur_radius,
            max_gain=max_gain,
            reference_images=reference_images,
        )
    return FlatFieldScanOptions(enabled=_bool_from_payload(value))


def _flat_field_reference_images_from_payload(
    payload: Mapping[str, object],
) -> tuple[str, ...]:
    values: list[str] = []
    singular = payload.get("reference_image", payload.get("reference_image_path"))
    if singular is not None:
        values.append(_flat_field_reference_image_path(singular))
    plural = payload.get("reference_images", payload.get("reference_image_paths"))
    if plural is not None:
        if isinstance(plural, (str, bytes)) or not isinstance(plural, Sequence):
            raise ValueError("flat_field.reference_images must be a list of paths.")
        values.extend(_flat_field_reference_image_path(item) for item in plural)
    return tuple(values)


def _flat_field_reference_image_path(value: object) -> str:
    path = str(value or "").strip()
    if not path:
        raise ValueError("flat_field.reference_images must contain file paths.")
    return path


def camera_lock_settings_from_payload(
    payload: Mapping[str, object],
    *,
    default_enabled: bool = False,
) -> CameraLockSettings:
    value = payload.get("camera_lock", default_enabled)
    if isinstance(value, Mapping):
        enabled = _bool_from_payload(value.get("enabled", default_enabled))
        configured_settings = value.get("settings")
    else:
        enabled = _bool_from_payload(value)
        configured_settings = None
    if configured_settings is None:
        settings = DEFAULT_CAMERA_LOCK_SETTINGS if enabled else ()
    else:
        settings = _camera_lock_settings_from_payload(configured_settings)
    return CameraLockSettings(enabled=enabled, settings=settings)


def _camera_lock_settings_from_payload(
    value: object,
) -> tuple[tuple[str, object], ...]:
    if isinstance(value, Mapping):
        return tuple((str(name), setting) for name, setting in value.items())
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("camera_lock.settings must be a list of camera settings.")
    settings: list[tuple[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            node_name = str(item.get("node_name") or "").strip()
            if not node_name or "value" not in item:
                raise ValueError(
                    "camera_lock.settings entries require node_name and value."
                )
            settings.append((node_name, item.get("value")))
            continue
        if (
            isinstance(item, Sequence)
            and not isinstance(item, (str, bytes))
            and len(item) == 2
        ):
            settings.append((str(item[0]), item[1]))
            continue
        raise ValueError(
            "camera_lock.settings entries require node_name and value."
        )
    return tuple(settings)


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
    extra: Mapping[str, object] | None = None,
) -> MicroscopeScanImageSavePlan:
    metadata_extra: dict[str, object] = {
        "overlap_fraction": plan.overlap_fraction,
        "row_count": plan.row_count,
        "column_count": plan.column_count,
    }
    if extra:
        metadata_extra.update(dict(extra))
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
        extra=metadata_extra,
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
    filename_suffix: str = "",
    extra: Mapping[str, object] | None = None,
) -> MicroscopeScanImageSavePlan:
    metadata_extra: dict[str, object] = {
        "overlap_fraction": plan.overlap_fraction,
        "row_count": plan.row_count,
        "column_count": plan.column_count,
        "stage_bounds": list(plan.stage_bounds),
        "covered_stage_bounds": list(plan.covered_stage_bounds),
        "fov_size_mm": list(plan.fov_size_mm),
    }
    if extra:
        metadata_extra.update(dict(extra))
    metadata = MicroscopeImageMetadata(
        title="Probe Station Design Scan Mosaic",
        mode="design scan mosaic",
        captured_at=captured_at,
        objective_name=objective_name,
        magnification=magnification,
        scan_tile_total=len(plan.tiles),
        notes=("stage-coordinate tile mosaic",),
        extra=metadata_extra,
    )
    return MicroscopeScanImageSavePlan(
        output_dir=output_dir,
        filename_stem=(
            f"{scan_name}_mosaic"
            f"{'_' + safe_filename_component(filename_suffix) if filename_suffix else ''}"
            f"_{captured_at}"
        ),
        metadata=metadata,
    )


def manifest_payload(
    *,
    plan: MicroscopeScanPlan,
    tile_results: Sequence[MicroscopeCaptureResult],
    mosaic_result: MicroscopeCaptureResult,
    created_at: str,
    corrections: Mapping[str, object] | None = None,
    diagnostic_mosaics: Mapping[str, MicroscopeCaptureResult] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
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
            _manifest_tile_payload(tile, result)
            for tile, result in zip(plan.tiles, tile_results)
        ],
    }
    if diagnostic_mosaics:
        payload["diagnostic_mosaics"] = {
            str(name): {
                "image": str(result.image_path),
                "metadata": str(result.metadata_path),
            }
            for name, result in diagnostic_mosaics.items()
        }
    if corrections:
        payload["corrections"] = dict(corrections)
    return payload


def _manifest_tile_payload(
    tile: MicroscopeScanTile,
    result: MicroscopeCaptureResult,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "index": tile.index,
        "row": tile.row,
        "column": tile.column,
        "stage_xy": list(tile.stage_xy),
        "image": str(result.image_path),
        "metadata": str(result.metadata_path),
    }
    label = str(getattr(tile, "label", "") or "")
    if label:
        payload["label"] = label
    raw_image_path = getattr(result, "raw_image_path", None)
    if raw_image_path is not None:
        payload["raw_image"] = str(raw_image_path)
    return payload


def write_manifest(
    *,
    output_dir: Path,
    plan: MicroscopeScanPlan,
    tile_results: Sequence[MicroscopeCaptureResult],
    mosaic_result: MicroscopeCaptureResult,
    created_at: str | None = None,
    corrections: Mapping[str, object] | None = None,
    diagnostic_mosaics: Mapping[str, MicroscopeCaptureResult] | None = None,
) -> Path:
    manifest_path = output_dir / "microscope-scan-manifest.json"
    data = manifest_payload(
        plan=plan,
        tile_results=tile_results,
        mosaic_result=mosaic_result,
        created_at=created_at or utc_timestamp(),
        corrections=corrections,
        diagnostic_mosaics=diagnostic_mosaics,
    )
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    return manifest_path


def _rejected(message: str, timeout_ms: int) -> MicroscopeScanStartDecision:
    return MicroscopeScanStartDecision(
        accepted=False,
        status=MicroscopeScanStatus(message, timeout_ms),
    )


def _positive_int(value: object, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer.") from exc
    if number <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return number


def _positive_float(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive.") from exc
    if number <= 0.0:
        raise ValueError(f"{label} must be positive.")
    return number


def _coerce_pixel_matrix(
    value: Sequence[Sequence[float]],
) -> tuple[tuple[float, float], tuple[float, float]]:
    try:
        matrix = (
            (float(value[0][0]), float(value[0][1])),
            (float(value[1][0]), float(value[1][1])),
        )
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("pixels_to_mm must be a 2x2 matrix.") from exc
    values = (matrix[0][0], matrix[0][1], matrix[1][0], matrix[1][1])
    if not all(math.isfinite(item) for item in values):
        raise ValueError("pixels_to_mm contains non-finite values.")
    determinant = matrix[0][0] * matrix[1][1] - matrix[0][1] * matrix[1][0]
    if abs(determinant) < 1e-18:
        raise ValueError("pixels_to_mm matrix is singular.")
    return matrix


def _pixel_delta_to_stage(
    matrix: tuple[tuple[float, float], tuple[float, float]],
    dx_px: float,
    dy_px: float,
) -> Point2D:
    return (
        matrix[0][0] * float(dx_px) + matrix[0][1] * float(dy_px),
        matrix[1][0] * float(dx_px) + matrix[1][1] * float(dy_px),
    )


def _stage_bounds_for_matrix_tiles(
    centers: Sequence[Point2D],
    *,
    frame_size_px: tuple[int, int],
    matrix: tuple[tuple[float, float], tuple[float, float]],
) -> tuple[float, float, float, float]:
    half_w = float(frame_size_px[0]) * 0.5
    half_h = float(frame_size_px[1]) * 0.5
    points: list[Point2D] = []
    for center_x, center_y in centers:
        for dx_px, dy_px in (
            (-half_w, -half_h),
            (half_w, -half_h),
            (-half_w, half_h),
            (half_w, half_h),
        ):
            dx_mm, dy_mm = _pixel_delta_to_stage(matrix, -dx_px, dy_px)
            points.append((float(center_x + dx_mm), float(center_y + dy_mm)))
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _bool_from_payload(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", ""}:
        return False
    return bool(value)


__all__ = [
    "CameraLockSettings",
    "FlatFieldScanOptions",
    "MicroscopeScanStartDecision",
    "MicroscopeScanStatus",
    "StitchDebugMosaicGroup",
    "camera_lock_settings_from_payload",
    "centered_area_scan_plan",
    "centered_area_scan_plan_from_pixel_matrix",
    "completion_message",
    "default_output_dir",
    "flat_field_options_from_payload",
    "output_dir_from_configuration",
    "scan_plan_decision",
    "scan_name_from_document",
    "start_design_decision",
    "start_environment_decision",
    "start_scale_decision",
    "starting_status",
    "stitch_debug_scan_plan_from_pixel_matrix",
    "stitch_debug_mosaic_group_plan",
    "stitch_debug_mosaic_groups",
    "mosaic_image_save_plan",
    "tile_image_save_plan",
    "tile_status",
    "write_manifest",
]
