"""Overlap registration and robust microscope scan scale refinement."""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Any

from PySide6.QtGui import QImage

from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    MicroscopeScanTile,
    _overlap_patches,
    _registration_gray,
    _scan_tile_pixel_placements,
    _scan_tiles_are_axis_neighbors,
)


STITCH_MAX_REGISTRATION_SHIFT_PX = 32.0
STITCH_SCALE_REFINEMENT_MAX_SHIFT_PX = 96.0
STITCH_SCALE_REFINEMENT_INLIER_PX = 12.0
STITCH_SCALE_REFINEMENT_MIN_INLIER_FRACTION = 0.5
STITCH_MAX_REGISTRATION_OVERLAP_FRACTION = 0.25

logger = logging.getLogger(__name__)


def refine_scan_scale_from_tile_overlaps(
    tile_images: Sequence[tuple[MicroscopeScanTile, QImage]],
    scale: MicroscopeScaleCalibration,
) -> MicroscopeScaleCalibration:
    import cv2  # noqa: F401 - preserve the lazy OpenCV dependency check
    import numpy as np

    if len(tile_images) < 3:
        return scale
    try:
        placements = _scan_tile_pixel_placements(tile_images, scale)
    except (TypeError, ValueError):
        return scale
    stage_vectors: list[tuple[float, float]] = []
    pixel_vectors: list[tuple[float, float]] = []
    for first_index, first in enumerate(placements):
        for second in placements[first_index + 1 :]:
            if not _scan_tiles_are_axis_neighbors(first[0], second[0]):
                continue
            overlap = _overlap_patches(first, second)
            if overlap is None:
                continue
            existing_patch, current_patch = overlap
            shift = _phase_overlap_shift(existing_patch, current_patch)
            if shift is None:
                continue
            shift_x, shift_y, response = shift
            if response < 0.01 or not _registration_shift_is_plausible(
                shift_x,
                shift_y,
                STITCH_SCALE_REFINEMENT_MAX_SHIFT_PX,
            ):
                continue
            first_tile, first_left, first_top, _first_raw = first
            second_tile, second_left, second_top, _second_raw = second
            measured_left_delta = float(second_left - first_left) - float(shift_x)
            measured_top_delta = float(second_top - first_top) - float(shift_y)
            stage_vectors.append(
                (
                    float(second_tile.stage_xy[0]) - float(first_tile.stage_xy[0]),
                    float(second_tile.stage_xy[1]) - float(first_tile.stage_xy[1]),
                )
            )
            pixel_vectors.append((-measured_left_delta, measured_top_delta))
    if len(stage_vectors) < 2:
        return scale
    stage = np.asarray(stage_vectors, dtype=float)
    pixels = np.asarray(pixel_vectors, dtype=float)
    if np.linalg.matrix_rank(stage) < 2:
        return scale
    fit = _robust_scan_overlap_stage_to_pixel_fit(stage, pixels)
    if fit is None:
        logger.info(
            "Microscope scan overlap scale refinement skipped: pairs=%d "
            "reason=no-consensus",
            len(stage_vectors),
        )
        return scale
    coefficients, inlier_mask, errors = fit
    stage_to_pixel = coefficients.T
    determinant = float(np.linalg.det(stage_to_pixel))
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        return scale
    pixels_to_mm = np.linalg.inv(stage_to_pixel)
    if not np.isfinite(pixels_to_mm).all():
        return scale
    column_x = pixels_to_mm[:, 0]
    column_y = pixels_to_mm[:, 1]
    refined = MicroscopeScaleCalibration(
        pixel_size_x_um=float(np.linalg.norm(column_x)) * 1000.0,
        pixel_size_y_um=float(np.linalg.norm(column_y)) * 1000.0,
        source=f"{scale.source}+scan-overlap" if scale.source else "scan-overlap",
        pixels_to_mm=(
            (float(pixels_to_mm[0, 0]), float(pixels_to_mm[0, 1])),
            (float(pixels_to_mm[1, 0]), float(pixels_to_mm[1, 1])),
        ),
    )
    inlier_errors = errors[inlier_mask]
    logger.info(
        "Microscope scan overlap scale refinement: pairs=%d inliers=%d "
        "matrix=%s residual_mean_px=%.3f residual_max_px=%.3f "
        "rejected_max_px=%.3f",
        len(stage_vectors),
        int(np.count_nonzero(inlier_mask)),
        refined.pixels_to_mm,
        float(np.mean(inlier_errors)),
        float(np.max(inlier_errors)),
        float(np.max(errors)),
    )
    return refined


def _robust_scan_overlap_stage_to_pixel_fit(
    stage: Any,
    pixels: Any,
) -> tuple[Any, Any, Any] | None:
    import numpy as np

    pair_count = int(stage.shape[0])
    if pair_count < 2 or np.linalg.matrix_rank(stage) < 2:
        return None
    if pair_count == 2:
        keep = np.ones(pair_count, dtype=bool)
        coefficients = _least_squares_stage_to_pixel(stage, pixels, keep)
        errors = np.linalg.norm(stage @ coefficients - pixels, axis=1)
        return coefficients, keep, errors

    best_keep: Any | None = None
    best_score: tuple[int, float, float] | None = None
    for first_index in range(pair_count):
        for second_index in range(first_index + 1, pair_count):
            sample_indices = np.asarray((first_index, second_index), dtype=int)
            sample_stage = stage[sample_indices]
            if np.linalg.matrix_rank(sample_stage) < 2:
                continue
            similarity = _similarity_stage_to_pixel_fit(
                sample_stage,
                pixels[sample_indices],
            )
            similarity_errors = _similarity_stage_to_pixel_errors(
                stage,
                pixels,
                similarity,
            )
            keep = similarity_errors <= STITCH_SCALE_REFINEMENT_INLIER_PX
            if np.count_nonzero(keep) < 2 or np.linalg.matrix_rank(stage[keep]) < 2:
                continue
            inlier_errors = similarity_errors[keep]
            score = (
                int(np.count_nonzero(keep)),
                -float(np.median(inlier_errors)),
                -float(np.max(inlier_errors)),
            )
            if best_score is None or score > best_score:
                best_score = score
                best_keep = keep

    if best_keep is None:
        return None

    min_inliers = max(
        2,
        int(math.ceil(pair_count * STITCH_SCALE_REFINEMENT_MIN_INLIER_FRACTION)),
    )
    if int(np.count_nonzero(best_keep)) < min_inliers:
        return None

    coefficients = _least_squares_stage_to_pixel(stage, pixels, best_keep)
    errors = np.linalg.norm(stage @ coefficients - pixels, axis=1)
    return coefficients, best_keep, errors


def _least_squares_stage_to_pixel(stage: Any, pixels: Any, keep: Any) -> Any:
    import numpy as np

    coefficients, _residuals, _rank, _singular = np.linalg.lstsq(
        stage[keep],
        pixels[keep],
        rcond=None,
    )
    return coefficients


def _similarity_stage_to_pixel_fit(stage: Any, pixels: Any) -> tuple[float, float]:
    import numpy as np

    rows: list[tuple[float, float]] = []
    values: list[float] = []
    for stage_delta, pixel_delta in zip(stage, pixels, strict=False):
        dx_mm = float(stage_delta[0])
        dy_mm = float(stage_delta[1])
        dx_px = float(pixel_delta[0])
        dy_px = float(pixel_delta[1])
        rows.append((dx_mm, -dy_mm))
        values.append(dx_px)
        rows.append((dy_mm, dx_mm))
        values.append(dy_px)
    coefficients, _residuals, _rank, _singular = np.linalg.lstsq(
        np.asarray(rows, dtype=float),
        np.asarray(values, dtype=float),
        rcond=None,
    )
    return float(coefficients[0]), float(coefficients[1])


def _similarity_stage_to_pixel_errors(
    stage: Any,
    pixels: Any,
    similarity: tuple[float, float],
) -> Any:
    import numpy as np

    scale_cos, scale_sin = similarity
    predicted = np.column_stack(
        (
            scale_cos * stage[:, 0] - scale_sin * stage[:, 1],
            scale_sin * stage[:, 0] + scale_cos * stage[:, 1],
        )
    )
    return np.linalg.norm(predicted - pixels, axis=1)


def _refine_scan_tile_pixel_placements(
    placements: list[tuple[MicroscopeScanTile, float, float, Any]],
) -> list[tuple[MicroscopeScanTile, float, float, Any]]:
    refined: list[tuple[MicroscopeScanTile, float, float, Any]] = []
    for placement in placements:
        tile, left_px, top_px, raw = placement
        corrections: list[tuple[float, float, float]] = []
        for existing in refined:
            if not _scan_tiles_are_axis_neighbors(existing[0], tile):
                continue
            estimate = _estimate_overlap_registration(existing, placement)
            if estimate is None:
                continue
            shift_x, shift_y, weight = estimate
            if not _registration_shift_is_plausible(shift_x, shift_y):
                continue
            corrections.append((-shift_x, -shift_y, weight))
        if corrections:
            total_weight = sum(item[2] for item in corrections)
            if total_weight > 0.0:
                left_px += sum(item[0] * item[2] for item in corrections) / total_weight
                top_px += sum(item[1] * item[2] for item in corrections) / total_weight
        refined.append((tile, left_px, top_px, raw))
    return refined


def _estimate_overlap_registration(
    existing: tuple[MicroscopeScanTile, float, float, Any],
    current: tuple[MicroscopeScanTile, float, float, Any],
) -> tuple[float, float, float] | None:
    overlap = _overlap_patches(existing, current)
    if overlap is None:
        return None
    existing_patch, current_patch = overlap
    overlap_h, overlap_w = existing_patch.shape[:2]
    shift = _phase_overlap_shift(existing_patch, current_patch)
    if shift is None:
        return None
    shift_x, shift_y, response = shift
    max_shift = _overlap_registration_shift_limit(overlap_w, overlap_h)
    if not _registration_shift_is_plausible(shift_x, shift_y, max_shift):
        return None
    weight = max(float(response), 1e-4) * float(overlap_w * overlap_h)
    return float(shift_x), float(shift_y), weight


def _overlap_registration_shift_limit(overlap_w: int, overlap_h: int) -> float:
    narrow_overlap = float(max(0, min(int(overlap_w), int(overlap_h))))
    overlap_limit = max(3.0, narrow_overlap * STITCH_MAX_REGISTRATION_OVERLAP_FRACTION)
    return min(STITCH_MAX_REGISTRATION_SHIFT_PX, overlap_limit)


def _registration_shift_is_plausible(
    shift_x: float,
    shift_y: float,
    max_shift_px: float = STITCH_MAX_REGISTRATION_SHIFT_PX,
) -> bool:
    return abs(float(shift_x)) <= max_shift_px and abs(float(shift_y)) <= max_shift_px


def _phase_overlap_shift(
    existing_patch: Any,
    current_patch: Any,
) -> tuple[float, float, float] | None:
    import cv2
    import numpy as np

    existing_gray = _registration_gray(existing_patch)
    current_gray = _registration_gray(current_patch)
    if existing_gray.shape != current_gray.shape:
        return None
    if float(existing_gray.std()) < 1e-3 or float(current_gray.std()) < 1e-3:
        return None
    window = cv2.createHanningWindow(
        (existing_gray.shape[1], existing_gray.shape[0]),
        cv2.CV_32F,
    )
    (shift_x, shift_y), response = cv2.phaseCorrelate(
        existing_gray.astype(np.float32, copy=False),
        current_gray.astype(np.float32, copy=False),
        window,
    )
    if not all(math.isfinite(value) for value in (shift_x, shift_y, response)):
        return None
    if response < 0.001:
        return None
    return float(shift_x), float(shift_y), float(response)


def _refine_integer_overlap_shift(
    existing_gray: Any,
    current_gray: Any,
    shift_x: float,
    shift_y: float,
) -> tuple[int, int]:
    import numpy as np  # noqa: F401 - preserve the lazy NumPy dependency check

    base_x = int(round(float(shift_x)))
    base_y = int(round(float(shift_y)))
    best_score = -math.inf
    best_shift = (base_x, base_y)
    for candidate_y in range(base_y - 3, base_y + 4):
        for candidate_x in range(base_x - 3, base_x + 4):
            score = _integer_shift_score(
                existing_gray,
                current_gray,
                candidate_x,
                candidate_y,
            )
            if score > best_score:
                best_score = score
                best_shift = (candidate_x, candidate_y)
    return best_shift


def _integer_shift_score(
    existing_gray: Any,
    current_gray: Any,
    shift_x: int,
    shift_y: int,
) -> float:
    import numpy as np

    height, width = existing_gray.shape[:2]
    existing_x0 = max(0, -int(shift_x))
    current_x0 = max(0, int(shift_x))
    existing_y0 = max(0, -int(shift_y))
    current_y0 = max(0, int(shift_y))
    overlap_w = min(width - existing_x0, width - current_x0)
    overlap_h = min(height - existing_y0, height - current_y0)
    if overlap_w < 3 or overlap_h < 3:
        return -math.inf
    a = existing_gray[
        existing_y0 : existing_y0 + overlap_h,
        existing_x0 : existing_x0 + overlap_w,
    ]
    b = current_gray[
        current_y0 : current_y0 + overlap_h,
        current_x0 : current_x0 + overlap_w,
    ]
    a_flat = a.reshape(-1).astype(np.float32, copy=False)
    b_flat = b.reshape(-1).astype(np.float32, copy=False)
    a_std = float(a_flat.std())
    b_std = float(b_flat.std())
    if a_std < 1e-6 or b_std < 1e-6:
        return -math.inf
    return float(
        np.mean(
            ((a_flat - float(a_flat.mean())) / a_std)
            * ((b_flat - float(b_flat.mean())) / b_std)
        )
    )


__all__ = ["refine_scan_scale_from_tile_overlaps"]
