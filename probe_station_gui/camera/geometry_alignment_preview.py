"""Signed-grid composition of optical-geometry alignment previews."""

from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np
from PySide6.QtGui import QImage

from probe_station_gui.camera.distortion import (
    StageGeometryCorrection,
    apply_distortion_correction,
    correction_from_payload,
)
from probe_station_gui.camera.geometry_segmentation import GeometryMaskFrame


_PREVIEW_CROP_MARGIN_PX = 12
_PREVIEW_OCCUPANCY_DECAY = 0.58
_PREVIEW_GRID_ZERO_TOLERANCE_FRACTION = 0.20
_PREVIEW_ROLE_COUNTS = {"horizontal": 2, "vertical": 2, "diagonal": 4}
_GUI_TO_IMAGE_PIXEL_AXES = np.diag((1.0, -1.0))


class GeometryAlignmentPreviewError(ValueError):
    """A preview could not be safely composed from the fitted geometry."""


def build_geometry_alignment_previews(
    raw_frames: Sequence[object],
    masks: Sequence[object],
    initial_pixels_to_mm: Sequence[Sequence[float]],
    fitted_payload: object,
) -> tuple[QImage, QImage]:
    """Render common-crop RGB alignment previews before and after fitting."""
    if len(raw_frames) != len(masks):
        raise GeometryAlignmentPreviewError(
            "raw_frames and masks must have the same length."
        )
    if not raw_frames:
        raise GeometryAlignmentPreviewError(
            "At least one mask is required for an alignment preview."
        )

    binary_masks, frame_size = _preview_binary_masks(masks)
    initial_image_matrix = _image_matrix_from_persisted_gui(
        initial_pixels_to_mm,
        "initial_pixels_to_mm",
    )
    stage_offsets = tuple(_stage_offset_mm(frame) for frame in raw_frames)
    center_index, roles = _preview_grid_roles(stage_offsets, initial_image_matrix)
    fitted_correction, fitted_image_matrix = _fitted_preview_correction(
        fitted_payload,
        frame_size,
    )

    before_masks = tuple(mask.copy() for mask in binary_masks)
    before_footprints = tuple(np.ones_like(mask, dtype=bool) for mask in binary_masks)
    after_masks = tuple(
        _apply_fitted_geometry_to_preview_mask(mask, fitted_correction)
        for mask in binary_masks
    )
    after_footprints = tuple(
        _apply_fitted_geometry_to_preview_mask(footprint, fitted_correction)
        for footprint in before_footprints
    )
    before_placements = _preview_mask_placements(
        before_masks,
        before_footprints,
        stage_offsets,
        initial_image_matrix,
    )
    after_placements = _preview_mask_placements(
        after_masks,
        after_footprints,
        stage_offsets,
        fitted_image_matrix,
    )
    if not any(np.any(mask) for mask in before_masks):
        raise GeometryAlignmentPreviewError(
            "The alignment preview mask union is empty."
        )
    canvas_origin, canvas_size = _shared_preview_canvas_bounds(
        (*before_placements, *after_placements),
    )
    before_canvas = _accumulate_preview_occupancy(
        before_placements,
        canvas_origin=canvas_origin,
        canvas_size=canvas_size,
    )
    after_canvas = _accumulate_preview_occupancy(
        after_placements,
        canvas_origin=canvas_origin,
        canvas_size=canvas_size,
    )
    before_channels = _accumulate_preview_disagreement(
        before_placements,
        center_index=center_index,
        roles=roles,
        canvas_origin=canvas_origin,
        canvas_size=canvas_size,
    )
    after_channels = _accumulate_preview_disagreement(
        after_placements,
        center_index=center_index,
        roles=roles,
        canvas_origin=canvas_origin,
        canvas_size=canvas_size,
    )
    return (
        _preview_occupancy_qimage(before_canvas, before_channels),
        _preview_occupancy_qimage(after_canvas, after_channels),
    )


def _preview_binary_masks(
    masks: Sequence[object],
) -> tuple[tuple[np.ndarray, ...], tuple[int, int]]:
    normalized: list[np.ndarray] = []
    expected_size: tuple[int, int] | None = None
    for item in masks:
        source = item.mask if isinstance(item, GeometryMaskFrame) else item
        array = np.asarray(source)
        if array.ndim != 2 or not array.shape[0] or not array.shape[1]:
            raise GeometryAlignmentPreviewError(
                "Each preview mask must be a non-empty 2D binary mask."
            )
        if not np.issubdtype(array.dtype, np.bool_) and not np.issubdtype(
            array.dtype,
            np.number,
        ):
            raise GeometryAlignmentPreviewError("Each preview mask must be binary.")
        if np.issubdtype(array.dtype, np.floating) and not np.isfinite(array).all():
            raise GeometryAlignmentPreviewError(
                "Each preview mask must contain finite values."
            )
        mask = np.ascontiguousarray(array != 0)
        frame_size = (int(mask.shape[1]), int(mask.shape[0]))
        declared_size = getattr(item, "frame_size", frame_size)
        if tuple(declared_size) != frame_size:
            raise GeometryAlignmentPreviewError(
                "Each geometry mask must match its declared frame size."
            )
        if expected_size is None:
            expected_size = frame_size
        elif frame_size != expected_size:
            raise GeometryAlignmentPreviewError(
                "All preview masks must have the same size."
            )
        normalized.append(mask)
    if expected_size is None:
        raise GeometryAlignmentPreviewError(
            "At least one mask is required for an alignment preview."
        )
    return (tuple(normalized), expected_size)


def _image_matrix_from_persisted_gui(
    matrix: Sequence[Sequence[float]],
    label: str,
) -> np.ndarray:
    try:
        gui_matrix = _valid_image_pixels_to_mm(matrix)
    except ValueError as exc:
        raise GeometryAlignmentPreviewError(
            f"{label} must be a finite non-singular 2x2 matrix."
        ) from exc
    return gui_matrix @ _GUI_TO_IMAGE_PIXEL_AXES


def _fitted_preview_correction(
    payload: object,
    frame_size: tuple[int, int],
) -> tuple[StageGeometryCorrection, np.ndarray]:
    if not isinstance(payload, dict):
        raise GeometryAlignmentPreviewError(
            "fitted payload must be a stage-geometry object."
        )
    if str(payload.get("model_type", "")).strip().lower() != "stage_geometry":
        raise GeometryAlignmentPreviewError(
            "fitted payload must use the stage_geometry model."
        )
    try:
        image_matrix = _image_matrix_from_persisted_gui(
            payload.get("pixels_to_mm"),
            "fitted payload pixels_to_mm",
        )
    except (TypeError, ValueError) as exc:
        raise GeometryAlignmentPreviewError(
            "fitted payload has an invalid pixels_to_mm matrix."
        ) from exc
    image_payload = dict(payload)
    image_payload["pixels_to_mm"] = image_matrix.tolist()
    if "calibrated_pixels_to_mm" in image_payload:
        try:
            image_payload["calibrated_pixels_to_mm"] = (
                _image_matrix_from_persisted_gui(
                    image_payload["calibrated_pixels_to_mm"],
                    "fitted payload calibrated_pixels_to_mm",
                ).tolist()
            )
        except (TypeError, ValueError) as exc:
            raise GeometryAlignmentPreviewError(
                "fitted payload has an invalid calibrated_pixels_to_mm matrix."
            ) from exc
    try:
        correction = correction_from_payload(image_payload)
    except Exception as exc:
        raise GeometryAlignmentPreviewError(
            "Unable to prepare the fitted geometry correction."
        ) from exc
    if not isinstance(correction, StageGeometryCorrection):
        raise GeometryAlignmentPreviewError(
            "fitted payload did not produce a stage-geometry correction."
        )
    if correction.frame_size != frame_size:
        raise GeometryAlignmentPreviewError(
            "fitted correction frame size must match the preview masks."
        )
    return correction, image_matrix


def _preview_grid_roles(
    stage_offsets: Sequence[tuple[float, float]],
    initial_image_matrix: np.ndarray,
) -> tuple[int, tuple[str | None, ...]]:
    """Classify captured offsets as the positions of a complete 3x3 grid."""
    displacements = _preview_grid_displacements(stage_offsets, initial_image_matrix)
    zero_tolerances = _preview_grid_zero_tolerances(displacements)
    signed_coordinates: list[tuple[int, int]] = []
    roles: list[str | None] = []
    for displacement in displacements:
        signed_coordinate, role = _preview_grid_coordinate_and_role(
            displacement,
            zero_tolerances,
        )
        signed_coordinates.append(signed_coordinate)
        roles.append(role)
    _validate_preview_grid_roles(signed_coordinates, roles)
    return roles.index(None), tuple(roles)


def _preview_grid_displacements(
    stage_offsets: Sequence[tuple[float, float]],
    initial_image_matrix: np.ndarray,
) -> np.ndarray:
    try:
        stage_to_pixel = np.linalg.inv(initial_image_matrix)
        offsets = np.asarray(stage_offsets, dtype=float)
        if offsets.ndim != 2 or offsets.shape[1] != 2 or not np.isfinite(offsets).all():
            raise ValueError
        return offsets @ stage_to_pixel.T
    except (TypeError, ValueError, np.linalg.LinAlgError) as exc:
        raise GeometryAlignmentPreviewError(
            "Preview captures must form a complete 3x3 grid."
        ) from exc


def _preview_grid_zero_tolerances(
    displacements: np.ndarray,
) -> tuple[float, float]:
    nonzero = np.abs(displacements)
    spacings = tuple(
        float(np.median(axis[axis > np.finfo(float).eps]))
        if np.any(axis > np.finfo(float).eps)
        else 0.0
        for axis in nonzero.T
    )
    if any(spacing <= 0.0 or not np.isfinite(spacing) for spacing in spacings):
        raise GeometryAlignmentPreviewError(
            "Preview captures must form a complete 3x3 grid."
        )
    return tuple(
        spacing * _PREVIEW_GRID_ZERO_TOLERANCE_FRACTION for spacing in spacings
    )


def _preview_grid_coordinate_and_role(
    displacement: np.ndarray,
    zero_tolerances: tuple[float, float],
) -> tuple[tuple[int, int], str | None]:
    x_value, y_value = displacement
    x_zero = abs(float(x_value)) <= zero_tolerances[0]
    y_zero = abs(float(y_value)) <= zero_tolerances[1]
    coordinate = (
        0 if x_zero else (1 if x_value > 0.0 else -1),
        0 if y_zero else (1 if y_value > 0.0 else -1),
    )
    if x_zero and y_zero:
        return coordinate, None
    if not x_zero and y_zero:
        return coordinate, "horizontal"
    if x_zero and not y_zero:
        return coordinate, "vertical"
    return coordinate, "diagonal"


def _validate_preview_grid_roles(
    signed_coordinates: Sequence[tuple[int, int]],
    roles: Sequence[str | None],
) -> None:
    expected_coordinates = {
        (x_sign, y_sign)
        for x_sign in (-1, 0, 1)
        for y_sign in (-1, 0, 1)
    }
    if (
        roles.count(None) != 1
        or any(
            roles.count(role) != count
            for role, count in _PREVIEW_ROLE_COUNTS.items()
        )
        or len(signed_coordinates) != len(expected_coordinates)
        or set(signed_coordinates) != expected_coordinates
    ):
        raise GeometryAlignmentPreviewError(
            "Preview captures must form a complete 3x3 grid."
        )


def _apply_fitted_geometry_to_preview_mask(
    mask: np.ndarray,
    correction: StageGeometryCorrection,
) -> np.ndarray:
    if correction.map_x is None or correction.map_y is None:
        if all(
            abs(float(value)) <= 1e-15
            for value in (correction.k1, correction.k2, correction.p1, correction.p2)
        ):
            return mask.copy()
        rgb = np.repeat((mask.astype(np.uint8) * 255)[:, :, None], 3, axis=2)
        height, width = mask.shape
        source = QImage(
            rgb.data,
            width,
            height,
            int(rgb.strides[0]),
            QImage.Format_RGB888,
        ).copy()
        try:
            apply_distortion_correction(source, correction)
        except Exception as exc:
            raise GeometryAlignmentPreviewError(
                "Unable to apply the fitted geometry correction."
            ) from exc
    if correction.map_x is None or correction.map_y is None:
        return mask.copy()
    try:
        corrected = cv2.remap(
            mask.astype(np.uint8),
            correction.map_x,
            correction.map_y,
            interpolation=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    except cv2.error as exc:
        raise GeometryAlignmentPreviewError(
            "Unable to apply the fitted geometry correction map."
        ) from exc
    return corrected.astype(bool)


def _preview_mask_placements(
    masks: Sequence[np.ndarray],
    footprints: Sequence[np.ndarray],
    stage_offsets: Sequence[tuple[float, float]],
    image_matrix: np.ndarray,
) -> tuple[tuple[np.ndarray, np.ndarray, np.ndarray], ...]:
    try:
        stage_to_pixel = np.linalg.inv(image_matrix)
    except np.linalg.LinAlgError as exc:
        raise GeometryAlignmentPreviewError(
            "Preview pixel matrix must be non-singular."
        ) from exc
    return tuple(
        (
            mask,
            footprint,
            -(stage_to_pixel @ np.asarray(stage_offset, dtype=float)),
        )
        for mask, footprint, stage_offset in zip(masks, footprints, stage_offsets)
    )


def _shared_preview_canvas_bounds(
    placements: Sequence[tuple[np.ndarray, np.ndarray, np.ndarray]],
) -> tuple[np.ndarray, tuple[int, int]]:
    bounds: list[tuple[float, float, float, float]] = []
    for _mask, footprint, translation in placements:
        ys, xs = np.nonzero(footprint)
        if not xs.size:
            continue
        bounds.append(
            (
                float(xs.min()) + float(translation[0]),
                float(ys.min()) + float(translation[1]),
                float(xs.max()) + float(translation[0]),
                float(ys.max()) + float(translation[1]),
            )
        )
    if not bounds:
        raise GeometryAlignmentPreviewError(
            "The alignment preview mask union is empty."
        )
    left = int(np.floor(min(bound[0] for bound in bounds))) - _PREVIEW_CROP_MARGIN_PX
    top = int(np.floor(min(bound[1] for bound in bounds))) - _PREVIEW_CROP_MARGIN_PX
    right = int(np.ceil(max(bound[2] for bound in bounds))) + _PREVIEW_CROP_MARGIN_PX
    bottom = int(np.ceil(max(bound[3] for bound in bounds))) + _PREVIEW_CROP_MARGIN_PX
    return np.asarray((left, top), dtype=float), (right - left + 1, bottom - top + 1)


def _accumulate_preview_occupancy(
    placements: Sequence[tuple[np.ndarray, np.ndarray, np.ndarray]],
    *,
    canvas_origin: np.ndarray,
    canvas_size: tuple[int, int],
) -> np.ndarray:
    width, height = canvas_size
    occupancy = np.zeros((height, width), dtype=np.float32)
    for mask, _footprint, translation in placements:
        transform = np.asarray(
            (
                (1.0, 0.0, float(translation[0] - canvas_origin[0])),
                (0.0, 1.0, float(translation[1] - canvas_origin[1])),
            ),
            dtype=np.float32,
        )
        occupancy += cv2.warpAffine(
            mask.astype(np.float32),
            transform,
            canvas_size,
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    return occupancy


def _warp_preview_array(
    values: np.ndarray,
    translation: np.ndarray,
    *,
    canvas_origin: np.ndarray,
    canvas_size: tuple[int, int],
) -> np.ndarray:
    transform = np.asarray(
        (
            (1.0, 0.0, float(translation[0] - canvas_origin[0])),
            (0.0, 1.0, float(translation[1] - canvas_origin[1])),
        ),
        dtype=np.float32,
    )
    return cv2.warpAffine(
        values.astype(np.uint8),
        transform,
        canvas_size,
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    ).astype(bool)


def _accumulate_preview_disagreement(
    placements: Sequence[tuple[np.ndarray, np.ndarray, np.ndarray]],
    *,
    center_index: int,
    roles: Sequence[str | None],
    canvas_origin: np.ndarray,
    canvas_size: tuple[int, int],
) -> np.ndarray:
    center_mask, center_footprint, center_translation = placements[center_index]
    warped_center_mask = _warp_preview_array(
        center_mask,
        center_translation,
        canvas_origin=canvas_origin,
        canvas_size=canvas_size,
    )
    warped_center_footprint = _warp_preview_array(
        center_footprint,
        center_translation,
        canvas_origin=canvas_origin,
        canvas_size=canvas_size,
    )
    differences = np.zeros((*warped_center_mask.shape, 3), dtype=np.float32)
    comparisons = np.zeros_like(differences)
    channel_indexes = {"horizontal": 0, "vertical": 1, "diagonal": 2}
    for index, role in enumerate(roles):
        if role is None:
            continue
        neighbor_mask, neighbor_footprint, neighbor_translation = placements[index]
        warped_neighbor_mask = _warp_preview_array(
            neighbor_mask,
            neighbor_translation,
            canvas_origin=canvas_origin,
            canvas_size=canvas_size,
        )
        warped_neighbor_footprint = _warp_preview_array(
            neighbor_footprint,
            neighbor_translation,
            canvas_origin=canvas_origin,
            canvas_size=canvas_size,
        )
        valid = warped_center_footprint & warped_neighbor_footprint
        channel = channel_indexes[role]
        differences[:, :, channel] += (
            (warped_center_mask != warped_neighbor_mask) & valid
        )
        comparisons[:, :, channel] += valid
    return np.divide(
        differences,
        comparisons,
        out=np.zeros_like(differences),
        where=comparisons > 0,
    )


def _preview_occupancy_qimage(occupancy: np.ndarray, channels: np.ndarray) -> QImage:
    values = np.zeros(occupancy.shape, dtype=np.uint8)
    occupied = occupancy > 0.0
    values[occupied] = np.rint(
        255.0 * (1.0 - np.power(_PREVIEW_OCCUPANCY_DECAY, occupancy[occupied]))
    ).astype(np.uint8)
    alpha = channels.max(axis=2, keepdims=True)
    rgb = (
        np.rint(
            values[:, :, None].astype(np.float32) * (1.0 - alpha)
            + channels * 255.0
        )
        .clip(0.0, 255.0)
        .astype(np.uint8)
    )
    height, width = values.shape
    return QImage(
        rgb.data,
        width,
        height,
        int(rgb.strides[0]),
        QImage.Format_RGB888,
    ).copy()


def _valid_image_pixels_to_mm(matrix: Sequence[Sequence[float]]) -> np.ndarray:
    values = np.asarray(matrix, dtype=float)
    if values.shape != (2, 2) or not np.isfinite(values).all():
        raise ValueError("image_pixels_to_mm must be a finite 2x2 matrix.")
    if abs(float(np.linalg.det(values))) < 1e-18:
        raise ValueError("image_pixels_to_mm must be non-singular.")
    return values


def _stage_offset_mm(frame: object) -> tuple[float, float]:
    try:
        raw_offset = getattr(frame, "stage_offset_mm")
        stage = np.asarray(raw_offset, dtype=float)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Each frame must provide a finite stage_offset_mm pair.") from exc
    if stage.shape != (2,) or not np.isfinite(stage).all():
        raise ValueError("Each frame must provide a finite stage_offset_mm pair.")
    return (float(stage[0]), float(stage[1]))
