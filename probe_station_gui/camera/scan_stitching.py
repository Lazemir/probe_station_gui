"""Stage-coordinate microscope scan photometry and blending."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from PySide6.QtGui import QImage

from probe_station_gui.camera.flat_field_processing import rgb_array_to_qimage
from probe_station_gui.camera.imaging import (
    MicroscopeScaleCalibration,
    MicroscopeScanPlan,
    MicroscopeScanTile,
    _overlap_patches,
    _registration_gray,
    _scan_tile_pixel_placements,
    _scan_tiles_are_axis_neighbors,
    _sobel_magnitude,
)


STITCH_PHOTOMETRY_MIN_SAMPLES = 16
STITCH_PHOTOMETRY_GAIN_MIN = 0.65
STITCH_PHOTOMETRY_GAIN_MAX = 1.55
STITCH_PHOTOMETRY_OFFSET_LIMIT = 28.0
STITCH_PHOTOMETRY_PLANE_OFFSET_LIMIT = 24.0
STITCH_PHOTOMETRY_PLANE_MAX_SAMPLES = 3000
STITCH_BRIGHT_FEATURE_LUMINANCE_MIN = 160.0
STITCH_BRIGHT_FEATURE_LOCAL_CONTRAST_MIN = 12.0
STITCH_BRIGHT_FEATURE_GRADIENT_MIN = 20.0


def stitch_scan_tiles(
    *,
    plan: MicroscopeScanPlan,
    tile_images: Sequence[tuple[MicroscopeScanTile, QImage]],
    scale: MicroscopeScaleCalibration,
) -> QImage:
    """Place scan tiles into a single stage-coordinate mosaic."""

    import cv2
    import numpy as np

    if not tile_images:
        raise ValueError("No scan tiles were captured.")
    placements = _normalize_scan_tile_photometry(
        _scan_tile_pixel_placements(
            tile_images,
            scale,
        )
    )
    min_left = min(placement[1] for placement in placements)
    min_top = min(placement[2] for placement in placements)
    max_right = max(placement[1] + placement[3].shape[1] for placement in placements)
    max_bottom = max(placement[2] + placement[3].shape[0] for placement in placements)
    width_px = max(1, int(math.ceil(max_right - min_left)))
    height_px = max(1, int(math.ceil(max_bottom - min_top)))
    hard_output = np.zeros((height_px, width_px, 3), dtype=np.uint8)
    weighted_sum = np.zeros((height_px, width_px, 3), dtype=np.float32)
    weight_sum = np.zeros((height_px, width_px), dtype=np.float32)
    coverage_count = np.zeros((height_px, width_px), dtype=np.uint16)
    scores = np.zeros((height_px, width_px), dtype=np.float32)
    hard_feature_mask = np.zeros((height_px, width_px), dtype=bool)
    best_feature_luma = np.full((height_px, width_px), -1.0, dtype=np.float32)
    best_feature_rgb = np.zeros((height_px, width_px, 3), dtype=np.uint8)
    for _tile, left_px, top_px, raw in placements:
        x_offset = float(left_px - min_left)
        y_offset = float(top_px - min_top)
        raw_h, raw_w, _channels = raw.shape
        dst_x0 = max(0, int(math.floor(x_offset)) - 1)
        dst_y0 = max(0, int(math.floor(y_offset)) - 1)
        dst_x1 = min(width_px, int(math.ceil(x_offset + raw_w)) + 1)
        dst_y1 = min(height_px, int(math.ceil(y_offset + raw_h)) + 1)
        if dst_x0 >= dst_x1 or dst_y0 >= dst_y1:
            continue
        blend_weights = _tile_blend_weights(
            raw_w,
            raw_h,
            plan.overlap_fraction,
        )
        source_view, blend_weights, feature_mask = _warp_tile_to_mosaic_roi(
            raw,
            blend_weights,
            _bright_scan_feature_mask(raw).astype(np.float32, copy=False),
            x_offset=x_offset - float(dst_x0),
            y_offset=y_offset - float(dst_y0),
            width_px=dst_x1 - dst_x0,
            height_px=dst_y1 - dst_y0,
        )
        if not np.any(blend_weights > 1e-6):
            continue
        source_feature = feature_mask >= 0.25
        weight_view = blend_weights[:, :, np.newaxis]
        weighted_sum[dst_y0:dst_y1, dst_x0:dst_x1, :] += (
            source_view.astype(np.float32, copy=False) * weight_view
        )
        weight_sum[dst_y0:dst_y1, dst_x0:dst_x1] += blend_weights
        coverage_count[dst_y0:dst_y1, dst_x0:dst_x1] += (blend_weights > 1e-6).astype(
            np.uint16,
            copy=False,
        )
        score_view = scores[dst_y0:dst_y1, dst_x0:dst_x1]
        replace = (blend_weights > 1e-6) & (blend_weights >= score_view)
        hard_view = hard_output[dst_y0:dst_y1, dst_x0:dst_x1, :]
        hard_view[replace] = source_view[replace]
        feature_view = hard_feature_mask[dst_y0:dst_y1, dst_x0:dst_x1]
        feature_view[replace] = source_feature[replace]
        score_view[replace] = blend_weights[replace]
        if np.any(source_feature):
            luma = (
                source_view[:, :, 0].astype(np.float32) * 0.299
                + source_view[:, :, 1].astype(np.float32) * 0.587
                + source_view[:, :, 2].astype(np.float32) * 0.114
            )
            feature_score_view = best_feature_luma[dst_y0:dst_y1, dst_x0:dst_x1]
            feature_replace = source_feature & (luma > feature_score_view)
            feature_score_view[feature_replace] = luma[feature_replace]
            feature_rgb_view = best_feature_rgb[dst_y0:dst_y1, dst_x0:dst_x1, :]
            feature_rgb_view[feature_replace] = source_view[feature_replace]
    averaged = np.divide(
        weighted_sum,
        np.maximum(weight_sum, 1e-6)[:, :, np.newaxis],
        out=np.zeros_like(weighted_sum),
        where=weight_sum[:, :, np.newaxis] > 0.0,
    )
    output = np.where(
        hard_feature_mask[:, :, np.newaxis],
        hard_output.astype(np.float32, copy=False),
        averaged,
    )
    candidate_feature = (coverage_count <= 2) & (best_feature_luma >= 0.0)
    if np.any(candidate_feature):
        nearby_current_feature = cv2.dilate(
            hard_feature_mask.astype(np.uint8, copy=False),
            np.ones((5, 5), dtype=np.uint8),
            iterations=1,
        ).astype(bool)
        two_tile_feature = candidate_feature & ~nearby_current_feature
    else:
        two_tile_feature = candidate_feature
    output = np.where(
        two_tile_feature[:, :, np.newaxis],
        best_feature_rgb.astype(np.float32, copy=False),
        output,
    )
    output = np.clip(output, 0.0, 255.0).astype(np.uint8)
    return rgb_array_to_qimage(output)


def _bright_scan_feature_mask(rgb: Any) -> Any:
    import cv2
    import numpy as np

    luminance = (
        rgb[:, :, 0].astype(np.float32) * 0.299
        + rgb[:, :, 1].astype(np.float32) * 0.587
        + rgb[:, :, 2].astype(np.float32) * 0.114
    )
    bright = luminance >= STITCH_BRIGHT_FEATURE_LUMINANCE_MIN
    if min(luminance.shape[:2]) < 5 or not bool(np.any(bright)):
        return bright

    low_frequency = cv2.GaussianBlur(
        luminance,
        (0, 0),
        sigmaX=2.0,
        sigmaY=2.0,
    )
    local_contrast = luminance - low_frequency
    gradient_x = cv2.Sobel(luminance, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(luminance, cv2.CV_32F, 0, 1, ksize=3)
    gradient = cv2.magnitude(gradient_x, gradient_y)
    feature = bright & (
        (local_contrast >= STITCH_BRIGHT_FEATURE_LOCAL_CONTRAST_MIN)
        | (gradient >= STITCH_BRIGHT_FEATURE_GRADIENT_MIN)
    )
    if not bool(np.any(feature)):
        return feature
    kernel = np.ones((3, 3), dtype=np.uint8)
    dilated = cv2.dilate(feature.astype(np.uint8), kernel, iterations=1).astype(bool)
    return dilated & bright


def _warp_tile_to_mosaic_roi(
    raw: Any,
    weights: Any,
    feature_mask: Any,
    *,
    x_offset: float,
    y_offset: float,
    width_px: int,
    height_px: int,
) -> tuple[Any, Any, Any]:
    import cv2
    import numpy as np

    transform = np.asarray(
        [[1.0, 0.0, float(x_offset)], [0.0, 1.0, float(y_offset)]],
        dtype=np.float32,
    )
    size = (int(width_px), int(height_px))
    warped_raw = cv2.warpAffine(
        raw,
        transform,
        size,
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    warped_weights = cv2.warpAffine(
        weights.astype(np.float32, copy=False),
        transform,
        size,
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    warped_feature_mask = cv2.warpAffine(
        feature_mask.astype(np.float32, copy=False),
        transform,
        size,
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return (
        warped_raw,
        warped_weights.astype(np.float32, copy=False),
        warped_feature_mask.astype(np.float32, copy=False),
    )


def _normalize_scan_tile_photometry(
    placements: list[tuple[MicroscopeScanTile, float, float, Any]],
) -> list[tuple[MicroscopeScanTile, float, float, Any]]:
    import numpy as np

    normalized: list[tuple[MicroscopeScanTile, float, float, Any]] = []
    for placement in placements:
        tile, left_px, top_px, raw = placement
        adjustments: list[tuple[Any, Any, float]] = []
        for existing in normalized:
            if not _scan_tiles_are_axis_neighbors(existing[0], tile):
                continue
            adjustment = _estimate_overlap_photometry_adjustment(existing, placement)
            if adjustment is not None:
                gain, offset, weight = adjustment
                adjustments.append((gain, offset, weight))
        if adjustments:
            total_weight = sum(item[2] for item in adjustments)
            if total_weight > 0.0:
                gain = sum(item[0] * item[2] for item in adjustments) / total_weight
                offset = sum(item[1] * item[2] for item in adjustments) / total_weight
                raw = np.clip(
                    raw.astype(np.float32, copy=False) * gain.reshape((1, 1, 3))
                    + offset.reshape((1, 1, 3)),
                    0.0,
                    255.0,
                ).astype(np.uint8)
        plane = _estimate_overlap_photometry_plane(
            normalized,
            (tile, left_px, top_px, raw),
        )
        if plane is not None:
            raw = _apply_photometry_plane(raw, plane)
        normalized.append((tile, left_px, top_px, raw))
    return normalized


def _estimate_overlap_photometry_adjustment(
    existing: tuple[MicroscopeScanTile, float, float, Any],
    current: tuple[MicroscopeScanTile, float, float, Any],
) -> tuple[Any, Any, float] | None:
    import numpy as np

    overlap = _overlap_patches(existing, current)
    if overlap is None:
        return None
    existing_patch, current_patch = overlap
    mask = _photometry_overlap_mask(existing_patch, current_patch)
    sample_count = int(mask.sum())
    if sample_count < STITCH_PHOTOMETRY_MIN_SAMPLES:
        return None
    existing_values = existing_patch[mask].astype(np.float32, copy=False)
    current_values = current_patch[mask].astype(np.float32, copy=False)
    existing_median = np.median(existing_values, axis=0).astype(np.float32)
    current_median = np.median(current_values, axis=0).astype(np.float32)
    gain = existing_median / np.maximum(current_median, 1.0)
    gain = np.clip(gain, STITCH_PHOTOMETRY_GAIN_MIN, STITCH_PHOTOMETRY_GAIN_MAX)
    residual = existing_values - current_values * gain.reshape((1, 3))
    offset = np.median(residual, axis=0).astype(np.float32)
    offset = np.clip(
        offset,
        -STITCH_PHOTOMETRY_OFFSET_LIMIT,
        STITCH_PHOTOMETRY_OFFSET_LIMIT,
    )
    return gain, offset, float(sample_count)


def _estimate_overlap_photometry_plane(
    normalized: list[tuple[MicroscopeScanTile, float, float, Any]],
    current: tuple[MicroscopeScanTile, float, float, Any],
) -> Any | None:
    import numpy as np

    tile, _left_px, _top_px, raw = current
    rows: list[Any] = []
    targets: list[list[Any]] = [[], [], []]
    for existing in normalized:
        if not _scan_tiles_are_axis_neighbors(existing[0], tile):
            continue
        overlap = _overlap_patches_with_current_origin(existing, current)
        if overlap is None:
            continue
        existing_patch, current_patch, current_x0, current_y0 = overlap
        mask = _photometry_overlap_mask(existing_patch, current_patch)
        if int(mask.sum()) < STITCH_PHOTOMETRY_MIN_SAMPLES:
            continue
        existing_gray = _registration_gray(existing_patch)
        current_gray = _registration_gray(current_patch)
        existing_gradient = _sobel_magnitude(existing_gray)
        current_gradient = _sobel_magnitude(current_gray)
        mask = mask & (existing_gradient < 12.0) & (current_gradient < 12.0)
        ys, xs = np.where(mask)
        sample_count = int(len(xs))
        if sample_count < STITCH_PHOTOMETRY_MIN_SAMPLES:
            continue
        step = max(1, sample_count // STITCH_PHOTOMETRY_PLANE_MAX_SAMPLES)
        xs = xs[::step]
        ys = ys[::step]
        height, width = raw.shape[:2]
        x_norm = (
            (xs.astype(np.float32) + float(current_x0)) / max(1.0, width - 1.0)
        ) * 2.0 - 1.0
        y_norm = (
            (ys.astype(np.float32) + float(current_y0)) / max(1.0, height - 1.0)
        ) * 2.0 - 1.0
        rows.append(
            np.column_stack(
                [
                    np.ones_like(x_norm, dtype=np.float32),
                    x_norm,
                    y_norm,
                ]
            )
        )
        residual = existing_patch[ys, xs].astype(
            np.float32, copy=False
        ) - current_patch[ys, xs].astype(np.float32, copy=False)
        for channel in range(3):
            targets[channel].append(residual[:, channel])
    if not rows:
        return None
    design = np.vstack(rows).astype(np.float32, copy=False)
    if design.shape[0] < STITCH_PHOTOMETRY_MIN_SAMPLES:
        return None
    coefficients = []
    for channel in range(3):
        target = np.concatenate(targets[channel]).astype(np.float32, copy=False)
        keep = _robust_plane_samples(target)
        if int(keep.sum()) < STITCH_PHOTOMETRY_MIN_SAMPLES:
            keep = np.ones(target.shape, dtype=bool)
        coefficient, *_unused = np.linalg.lstsq(design[keep], target[keep], rcond=None)
        coefficients.append(coefficient.astype(np.float32, copy=False))
    return np.stack(coefficients, axis=0)


def _overlap_patches_with_current_origin(
    existing: tuple[MicroscopeScanTile, float, float, Any],
    current: tuple[MicroscopeScanTile, float, float, Any],
) -> tuple[Any, Any, int, int] | None:
    _existing_tile, existing_left, existing_top, existing_raw = existing
    _current_tile, current_left, current_top, current_raw = current
    existing_h, existing_w = existing_raw.shape[:2]
    current_h, current_w = current_raw.shape[:2]
    left = max(existing_left, current_left)
    top = max(existing_top, current_top)
    right = min(existing_left + existing_w, current_left + current_w)
    bottom = min(existing_top + existing_h, current_top + current_h)
    overlap_w = int(round(right - left))
    overlap_h = int(round(bottom - top))
    if overlap_w < 4 or overlap_h < 4:
        return None
    x0 = int(round(left))
    y0 = int(round(top))
    existing_x = x0 - int(round(existing_left))
    existing_y = y0 - int(round(existing_top))
    current_x = x0 - int(round(current_left))
    current_y = y0 - int(round(current_top))
    existing_patch = existing_raw[
        existing_y : existing_y + overlap_h,
        existing_x : existing_x + overlap_w,
    ]
    current_patch = current_raw[
        current_y : current_y + overlap_h,
        current_x : current_x + overlap_w,
    ]
    if existing_patch.shape[:2] != current_patch.shape[:2]:
        return None
    return existing_patch, current_patch, current_x, current_y


def _robust_plane_samples(values: Any) -> Any:
    import numpy as np

    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    limit = max(5.0, 2.5 * 1.4826 * mad)
    return np.abs(values - median) <= limit


def _apply_photometry_plane(raw: Any, plane: Any) -> Any:
    import numpy as np

    height, width = raw.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width]
    x_norm = (xx.astype(np.float32) / max(1.0, width - 1.0)) * 2.0 - 1.0
    y_norm = (yy.astype(np.float32) / max(1.0, height - 1.0)) * 2.0 - 1.0
    design = np.stack(
        [
            np.ones_like(x_norm, dtype=np.float32),
            x_norm,
            y_norm,
        ],
        axis=2,
    )
    corrected = raw.astype(np.float32, copy=False).copy()
    for channel in range(3):
        correction = np.sum(design * plane[channel].reshape((1, 1, 3)), axis=2)
        corrected[:, :, channel] += np.clip(
            correction,
            -STITCH_PHOTOMETRY_PLANE_OFFSET_LIMIT,
            STITCH_PHOTOMETRY_PLANE_OFFSET_LIMIT,
        )
    return np.clip(corrected, 0.0, 255.0).astype(np.uint8)


def _photometry_overlap_mask(existing_patch: Any, current_patch: Any) -> Any:
    import numpy as np

    existing_max = existing_patch.max(axis=2)
    current_max = current_patch.max(axis=2)
    existing_min = existing_patch.min(axis=2)
    current_min = current_patch.min(axis=2)
    mask = (
        (existing_max < 220)
        & (current_max < 220)
        & (existing_min > 5)
        & (current_min > 5)
    )
    if int(mask.sum()) < STITCH_PHOTOMETRY_MIN_SAMPLES:
        return mask
    existing_gray = existing_patch.mean(axis=2)
    current_gray = current_patch.mean(axis=2)
    combined = (existing_gray + current_gray) * 0.5
    values = combined[mask]
    low, high = np.percentile(values, (8.0, 85.0))
    filtered = mask & (combined >= low) & (combined <= high)
    if int(filtered.sum()) >= STITCH_PHOTOMETRY_MIN_SAMPLES:
        return filtered
    return mask


def _tile_blend_weights(width: int, height: int, overlap_fraction: float) -> Any:
    import numpy as np

    overlap = min(max(float(overlap_fraction), 0.0), 0.95)
    if overlap <= 0.0:
        return np.ones((int(height), int(width)), dtype=np.float32)
    feather_x = min(int(width) // 2, max(1, int(round(float(width) * overlap * 0.5))))
    feather_y = min(int(height) // 2, max(1, int(round(float(height) * overlap * 0.5))))
    wx = np.ones(int(width), dtype=np.float32)
    wy = np.ones(int(height), dtype=np.float32)
    if feather_x > 0:
        ramp = np.linspace(0.05, 1.0, feather_x, dtype=np.float32)
        wx[:feather_x] = np.minimum(wx[:feather_x], ramp)
        wx[-feather_x:] = np.minimum(wx[-feather_x:], ramp[::-1])
    if feather_y > 0:
        ramp = np.linspace(0.05, 1.0, feather_y, dtype=np.float32)
        wy[:feather_y] = np.minimum(wy[:feather_y], ramp)
        wy[-feather_y:] = np.minimum(wy[-feather_y:], ramp[::-1])
    return wy[:, None] * wx[None, :]


__all__ = ["stitch_scan_tiles"]
