from __future__ import annotations

import gc

import cv2
import numpy as np
import pytest
from PySide6.QtGui import QImage

import probe_station_gui.camera.geometry_alignment_preview as alignment_preview
from probe_station_gui.camera.distortion import GridCalibrationFrame
from probe_station_gui.camera.geometry_alignment_preview import (
    GeometryAlignmentPreviewError,
    build_geometry_alignment_previews,
)


def preview_mask_pattern(frame_size: tuple[int, int]) -> np.ndarray:
    width, height = frame_size
    mask = np.zeros((height, width), dtype=np.uint8)
    for x in (width // 2 - 16, width // 2, width // 2 + 16):
        cv2.line(mask, (x, height // 2 - 20), (x, height // 2 + 20), 1, 3)
    for y in (height // 2 - 20, height // 2, height // 2 + 20):
        cv2.line(mask, (width // 2 - 16, y), (width // 2 + 16, y), 1, 3)
    for spine_x, tooth_end_x in ((12, 34), (width - 13, width - 35)):
        cv2.line(mask, (spine_x, 18), (spine_x, height - 19), 1, 3)
        for y in (28, 44, 60, height - 61, height - 45, height - 29):
            cv2.line(mask, (spine_x, y), (tooth_end_x, y), 1, 3)
    return mask.astype(bool)


def inverse_preview_geometry_point(
    corrected_px: np.ndarray,
    *,
    frame_size: tuple[int, int],
    center_px: tuple[float, float],
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> np.ndarray:
    from probe_station_gui.camera.distortion import _stage_geometry_correct_point

    estimate = corrected_px.astype(float, copy=True)
    for _ in range(30):
        mapped = np.asarray(
            _stage_geometry_correct_point(
                (float(estimate[0]), float(estimate[1])),
                frame_size=frame_size,
                center_px=center_px,
                k1=k1,
                k2=k2,
                p1=p1,
                p2=p2,
            ),
            dtype=float,
        )
        estimate += corrected_px - mapped
    return estimate


def distorted_preview_mask(
    corrected_mask: np.ndarray,
    *,
    frame_size: tuple[int, int],
    center_px: tuple[float, float],
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> np.ndarray:
    observed = np.zeros_like(corrected_mask, dtype=bool)
    ys, xs = np.nonzero(corrected_mask)
    for x, y in zip(xs, ys):
        raw = inverse_preview_geometry_point(
            np.asarray((x, y), dtype=float),
            frame_size=frame_size,
            center_px=center_px,
            k1=k1,
            k2=k2,
            p1=p1,
            p2=p2,
        )
        raw_x, raw_y = np.rint(raw).astype(int)
        if 0 <= raw_x < frame_size[0] and 0 <= raw_y < frame_size[1]:
            observed[raw_y, raw_x] = True
    return observed


def translated_preview_mask(mask: np.ndarray, shift_px: np.ndarray) -> np.ndarray:
    height, width = mask.shape
    return cv2.warpAffine(
        mask.astype(np.uint8),
        np.asarray(((1.0, 0.0, shift_px[0]), (0.0, 1.0, shift_px[1]))),
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    ).astype(bool)


def preview_rgb_array(image: QImage) -> np.ndarray:
    assert image.format() == QImage.Format_RGB888
    rows = np.frombuffer(
        image.constBits(),
        dtype=np.uint8,
        count=image.sizeInBytes(),
    ).reshape((image.height(), image.bytesPerLine()))
    return rows[:, : image.width() * 3].reshape(
        (image.height(), image.width(), 3)
    ).copy()


def nine_preview_frames(step_px: int = 4) -> tuple[GridCalibrationFrame, ...]:
    offsets = [(0.0, 0.0)]
    offsets.extend(
        (float(x), float(y))
        for y in (-step_px, 0, step_px)
        for x in (-step_px, 0, step_px)
        if (x, y) != (0, 0)
    )
    return tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in offsets
    )


def preview_landmark_masks(
    frames: tuple[GridCalibrationFrame, ...],
    *,
    frame_size: tuple[int, int] = (64, 48),
    adjustments: dict[int, tuple[float, float]] | None = None,
    image_matrix: np.ndarray | None = None,
) -> tuple[np.ndarray, ...]:
    width, height = frame_size
    adjustments = adjustments or {}
    masks: list[np.ndarray] = []
    for index, frame in enumerate(frames):
        point = np.asarray((30.0, 22.0))
        if image_matrix is None:
            point += np.asarray(frame.stage_offset_mm)
        else:
            point += np.linalg.inv(image_matrix) @ np.asarray(frame.stage_offset_mm)
        point += np.asarray(adjustments.get(index, (0.0, 0.0)))
        x, y = np.rint(point).astype(int)
        mask = np.zeros((height, width), dtype=bool)
        mask[y, x] = True
        masks.append(mask)
    return tuple(masks)


def identity_preview_args(
    frames: tuple[GridCalibrationFrame, ...],
    masks: tuple[np.ndarray, ...],
    *,
    matrix: np.ndarray | None = None,
) -> tuple[
    tuple[GridCalibrationFrame, ...],
    tuple[np.ndarray, ...],
    np.ndarray,
    dict[str, object],
]:
    frame_size = (masks[0].shape[1], masks[0].shape[0])
    matrix = matrix if matrix is not None else np.array(((1.0, 0.0), (0.0, -1.0)))
    return (
        frames,
        masks,
        matrix,
        preview_payload(
            frame_size=frame_size,
            pixels_to_mm=matrix,
            center_px=(frame_size[0] / 2.0, frame_size[1] / 2.0),
        ),
    )


def render_preview_rgb(
    frames: tuple[GridCalibrationFrame, ...],
    masks: tuple[np.ndarray, ...],
    *,
    matrix: np.ndarray | None = None,
) -> np.ndarray:
    before, _after = build_geometry_alignment_previews(
        *identity_preview_args(frames, masks, matrix=matrix)
    )
    return preview_rgb_array(before)


def preview_spread_metric(image: QImage) -> float:
    values = preview_rgb_array(image).max(axis=2)
    return float(np.count_nonzero(values)) / float(values.sum())


def assert_preview_occupancy_levels(image: QImage) -> np.ndarray:
    rgb = preview_rgb_array(image)
    values = rgb[:, :, 0]
    neutral = np.all(rgb == rgb[:, :, :1], axis=2)
    nonzero_levels = np.unique(values[neutral & (values > 0)])

    assert np.any(values == 0)
    assert nonzero_levels.size >= 1
    return values


def preview_payload(
    *,
    frame_size: tuple[int, int],
    pixels_to_mm: np.ndarray,
    center_px: tuple[float, float],
    k1: float = 0.0,
    k2: float = 0.0,
    p1: float = 0.0,
    p2: float = 0.0,
) -> dict[str, object]:
    return {
        "model_version": 1,
        "model_type": "stage_geometry",
        "frame_size": list(frame_size),
        "pixels_to_mm": pixels_to_mm.tolist(),
        "calibrated_pixels_to_mm": pixels_to_mm.tolist(),
        "center_px": list(center_px),
        "k1": k1,
        "k2": k2,
        "p1": p1,
        "p2": p2,
    }


def test_alignment_previews_use_the_same_exact_crop_and_owned_rgb_images() -> None:
    frame_size = (120, 84)
    matrix = np.array(((1.0, 0.0), (0.0, -1.0)))
    mask = preview_mask_pattern(frame_size)
    frames = nine_preview_frames(step_px=6)
    masks = tuple(
        translated_preview_mask(mask, np.asarray(frame.stage_offset_mm))
        for frame in frames
    )
    payload = preview_payload(
        frame_size=frame_size,
        pixels_to_mm=matrix,
        center_px=(60.0, 42.0),
    )

    before, after = build_geometry_alignment_previews(frames, masks, matrix, payload)
    expected_before = preview_rgb_array(before)
    masks[0].fill(False)
    gc.collect()
    _discarded = np.full(expected_before.shape, 255, dtype=np.uint8)

    assert not before.isNull()
    assert not after.isNull()
    assert before.size() == after.size()
    assert np.array_equal(expected_before, preview_rgb_array(before))
    assert np.array_equal(preview_rgb_array(before), preview_rgb_array(after))


def test_alignment_previews_keep_combs_and_reduce_mask_spread() -> None:
    frame_size = (160, 112)
    center_px = (81.0, 53.0)
    initial_gui_matrix = np.array(((-0.00122, 0.0), (0.0, -0.00093)))
    fitted_gui_matrix = np.array(((-0.001, 0.00004), (0.00002, -0.001)))
    fitted_image_matrix = fitted_gui_matrix @ np.diag((1.0, -1.0))
    distortion = {"k1": 0.042, "k2": -0.009, "p1": 0.002, "p2": -0.0015}
    pixel_offsets = [
        (0.0, 0.0),
        (-12.0, -8.0),
        (0.0, -8.0),
        (12.0, -8.0),
        (-12.0, 0.0),
        (12.0, 0.0),
        (-12.0, 8.0),
        (0.0, 8.0),
        (12.0, 8.0),
    ]
    stage_offsets = tuple(
        tuple(fitted_image_matrix @ np.asarray(offset)) for offset in pixel_offsets
    )
    base_mask = preview_mask_pattern(frame_size)
    masks = tuple(
        distorted_preview_mask(
            translated_preview_mask(base_mask, np.asarray(offset)),
            frame_size=frame_size,
            center_px=center_px,
            **distortion,
        )
        for offset in pixel_offsets
    )
    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in stage_offsets
    )

    before, after = build_geometry_alignment_previews(
        frames,
        masks,
        initial_gui_matrix,
        preview_payload(
            frame_size=frame_size,
            pixels_to_mm=fitted_gui_matrix,
            center_px=center_px,
            **distortion,
        ),
    )

    assert not before.isNull()
    assert not after.isNull()
    assert before.size() == after.size()
    assert before.format() == QImage.Format_RGB888
    assert after.format() == QImage.Format_RGB888
    assert_preview_occupancy_levels(before)
    after_values = assert_preview_occupancy_levels(after)
    assert np.count_nonzero(after_values) > 2500

    component_count, _labels, component_stats, _centroids = (
        cv2.connectedComponentsWithStats(
            (after_values > 0).astype(np.uint8),
            connectivity=8,
        )
    )
    components = [tuple(int(value) for value in row) for row in component_stats[1:]]
    assert component_count - 1 >= 2
    assert sum(area > 500 for _x, _y, _width, _height, area in components) >= 2
    after_rgb = preview_rgb_array(after)
    neutral = np.all(after_rgb == after_rgb[:, :, :1], axis=2)
    assert all(
        np.count_nonzero(after_rgb[:, :, channel][neutral]) > 500
        for channel in range(3)
    )
    assert preview_spread_metric(after) < preview_spread_metric(before)
    before_rgb = preview_rgb_array(before)
    assert int((after_rgb.max(axis=2) - after_rgb.min(axis=2)).sum()) < int(
        (before_rgb.max(axis=2) - before_rgb.min(axis=2)).sum()
    )


def test_alignment_previews_align_asymmetric_landmark_with_signed_axes() -> None:
    frame_size = (96, 72)
    gui_matrix = np.array(((-0.5, 0.0), (0.0, -0.25)))
    image_matrix = gui_matrix @ np.diag((1.0, -1.0))
    landmark_px = np.asarray((40.0, 30.0))
    pixel_offsets = (
        (0.0, 0.0),
        (-8.0, -6.0),
        (0.0, -6.0),
        (8.0, -6.0),
        (-8.0, 0.0),
        (8.0, 0.0),
        (-8.0, 6.0),
        (0.0, 6.0),
        (8.0, 6.0),
    )
    stage_offsets = tuple(
        tuple(image_matrix @ np.asarray(offset)) for offset in pixel_offsets
    )
    assert stage_offsets[1][0] > 0.0 and stage_offsets[1][1] < 0.0
    assert stage_offsets[3][0] < 0.0 and stage_offsets[3][1] < 0.0
    masks: list[np.ndarray] = []
    for pixel_offset in pixel_offsets:
        source_px = landmark_px + np.asarray(pixel_offset)
        mask = np.zeros((frame_size[1], frame_size[0]), dtype=bool)
        source_x, source_y = np.rint(source_px).astype(int)
        mask[source_y, source_x] = True
        masks.append(mask)
    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in stage_offsets
    )
    payload = preview_payload(
        frame_size=frame_size,
        pixels_to_mm=gui_matrix,
        center_px=(48.0, 36.0),
    )

    _before, after = build_geometry_alignment_previews(
        frames,
        tuple(masks),
        gui_matrix,
        payload,
    )

    after_values = preview_rgb_array(after)
    occupied = np.any(after_values > 0, axis=2)
    assert np.count_nonzero(occupied) == 1
    assert np.array_equal(after_values[occupied][0], np.array((253, 253, 253)))


def test_alignment_previews_build_fitted_maps_once(monkeypatch) -> None:
    frame_size = (80, 60)
    matrix = np.array(((1.0, 0.0), (0.0, -1.0)))
    frames = nine_preview_frames()
    masks = tuple(np.ones((60, 80), dtype=bool) for _ in frames)
    payload = preview_payload(
        frame_size=frame_size,
        pixels_to_mm=matrix,
        center_px=(40.0, 30.0),
        k1=0.01,
    )
    apply_calls: list[object] = []
    apply_correction = alignment_preview.apply_distortion_correction

    def count_apply(frame, correction):
        apply_calls.append(correction)
        return apply_correction(frame, correction)

    monkeypatch.setattr(
        alignment_preview,
        "apply_distortion_correction",
        count_apply,
    )

    build_geometry_alignment_previews(frames, masks, matrix, payload)

    assert len(apply_calls) == 1


@pytest.mark.parametrize(
    ("frames", "masks", "matrix", "payload", "message"),
    [
        (
            (GridCalibrationFrame(frame=None, stage_offset_mm=(0.0, 0.0)),),
            (),
            ((-0.001, 0.0), (0.0, -0.001)),
            preview_payload(
                frame_size=(80, 60),
                pixels_to_mm=np.array(((-0.001, 0.0), (0.0, -0.001))),
                center_px=(40.0, 30.0),
            ),
            "same length",
        ),
        (
            nine_preview_frames(),
            tuple(np.zeros((60, 80), dtype=bool) for _ in range(9)),
            np.array(((1.0, 0.0), (0.0, -1.0))),
            preview_payload(
                frame_size=(80, 60),
                pixels_to_mm=np.array(((1.0, 0.0), (0.0, -1.0))),
                center_px=(40.0, 30.0),
            ),
            "empty",
        ),
        (
            nine_preview_frames(),
            tuple(np.ones((60, 80), dtype=bool) for _ in range(9)),
            np.array(((1.0, 0.0), (0.0, -1.0))),
            {
                "model_version": 1,
                "model_type": "stage_geometry",
                "frame_size": [80, 60],
                "pixels_to_mm": [[1.0, 2.0], [2.0, 4.0]],
            },
            "fitted",
        ),
    ],
)
def test_alignment_previews_reject_invalid_inputs(
    frames: tuple[GridCalibrationFrame, ...],
    masks: tuple[np.ndarray, ...],
    matrix: object,
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_geometry_alignment_previews(frames, masks, matrix, payload)


def test_alignment_previews_reject_size_mismatch_and_correction_errors(
    monkeypatch,
) -> None:
    frame_size = (80, 60)
    matrix = np.array(((1.0, 0.0), (0.0, -1.0)))
    frames = nine_preview_frames()
    payload = preview_payload(
        frame_size=frame_size,
        pixels_to_mm=matrix,
        center_px=(40.0, 30.0),
        k1=0.01,
    )

    with pytest.raises(ValueError, match="same size"):
        build_geometry_alignment_previews(
            frames,
            tuple(
                np.ones((59, 80), dtype=bool)
                if index == 1
                else np.ones((60, 80), dtype=bool)
                for index in range(9)
            ),
            matrix,
            payload,
        )

    monkeypatch.setattr(
        alignment_preview,
        "apply_distortion_correction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("map unavailable")
        ),
        raising=False,
    )
    with pytest.raises(ValueError, match="correction"):
        build_geometry_alignment_previews(
            frames,
            tuple(np.ones((60, 80), dtype=bool) for _ in frames),
            matrix,
            payload,
        )


def test_alignment_previews_render_one_horizontal_disagreement_at_half_red() -> None:
    frames = nine_preview_frames()
    masks = preview_landmark_masks(frames, adjustments={4: (1.0, 0.0)})

    values = render_preview_rgb(frames, masks)
    red_chroma = values[:, :, 0].astype(int) - values[:, :, 1].astype(int)
    blue_chroma = values[:, :, 2].astype(int) - values[:, :, 1].astype(int)

    assert red_chroma.max() == 128
    assert blue_chroma.max() <= 0


def test_alignment_previews_render_vertical_and_corner_channels() -> None:
    frames = nine_preview_frames()

    vertical_values = render_preview_rgb(
        frames,
        preview_landmark_masks(frames, adjustments={2: (0.0, 1.0)}),
    )
    vertical_chroma = (
        vertical_values[:, :, 1].astype(int)
        - vertical_values[:, :, 0].astype(int)
    )
    assert vertical_chroma.max() == 128

    corner_values = render_preview_rgb(
        frames,
        preview_landmark_masks(frames, adjustments={1: (1.0, 0.0)}),
    )
    blue_chroma = (
        corner_values[:, :, 2].astype(int) - corner_values[:, :, 1].astype(int)
    )
    assert blue_chroma.max() == 64


def test_alignment_previews_render_full_red_for_two_horizontal_disagreements() -> None:
    frames = nine_preview_frames()
    masks = preview_landmark_masks(
        frames,
        adjustments={4: (1.0, 0.0), 5: (-1.0, 0.0)},
    )

    values = render_preview_rgb(frames, masks)
    red_chroma = values[:, :, 0].astype(int) - values[:, :, 1].astype(int)

    assert red_chroma.max() == 255


def test_preview_grid_roles_are_stable_for_shuffled_affine_grid() -> None:
    persisted_gui_matrix = np.array(((1.0, 0.2), (0.1, -1.0)))
    image_matrix = persisted_gui_matrix @ np.diag((1.0, -1.0))
    pixel_offsets = [
        (0.0, 0.0),
        (-4.0, -4.0),
        (0.0, -4.0),
        (4.0, -4.0),
        (-4.0, 0.0),
        (4.0, 0.0),
        (-4.0, 4.0),
        (0.0, 4.0),
        (4.0, 4.0),
    ]
    stage_offsets = tuple(
        tuple(image_matrix @ np.asarray(offset)) for offset in pixel_offsets
    )
    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in stage_offsets
    )
    masks = preview_landmark_masks(frames, image_matrix=image_matrix)
    order = (8, 2, 5, 0, 7, 1, 4, 6, 3)

    original = render_preview_rgb(frames, masks, matrix=persisted_gui_matrix)
    shuffled = render_preview_rgb(
        tuple(frames[index] for index in order),
        tuple(masks[index] for index in order),
        matrix=persisted_gui_matrix,
    )

    assert np.array_equal(original, shuffled)


def test_alignment_previews_require_a_complete_3x3_grid() -> None:
    frames = nine_preview_frames()[:-1]
    masks = preview_landmark_masks(frames)

    with pytest.raises(GeometryAlignmentPreviewError, match="3x3"):
        build_geometry_alignment_previews(*identity_preview_args(frames, masks))


def test_alignment_previews_reject_role_counts_without_signed_grid() -> None:
    malformed_offsets = (
        (0.0, 0.0),
        (4.0, 0.0),
        (8.0, 0.0),
        (0.0, 4.0),
        (0.0, 8.0),
        (4.0, 4.0),
        (4.0, 8.0),
        (8.0, 4.0),
        (8.0, 8.0),
    )
    frames = tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in malformed_offsets
    )
    masks = preview_landmark_masks(frames)

    with pytest.raises(GeometryAlignmentPreviewError, match="complete 3x3"):
        build_geometry_alignment_previews(*identity_preview_args(frames, masks))


def test_alignment_previews_ignore_disagreement_outside_shared_footprint() -> None:
    frames = nine_preview_frames()
    masks = list(preview_landmark_masks(frames, frame_size=(40, 32)))
    masks[5][16, 0] = True
    matrix = np.array(((1.0, 0.0), (0.0, -1.0)))

    before, after = build_geometry_alignment_previews(
        frames,
        tuple(masks),
        matrix,
        preview_payload(
            frame_size=(40, 32),
            pixels_to_mm=matrix,
            center_px=(20.0, 16.0),
            k1=-0.05,
        ),
    )
    values = preview_rgb_array(before)
    row = int(np.rint(16.0 + 16.0))
    column = 12

    assert int(values[row, column].max() - values[row, column].min()) == 0
    fitted_values = preview_rgb_array(after)
    transformed_column = 13
    assert fitted_values[row, transformed_column].max() > 0
    assert (
        int(
            fitted_values[row, transformed_column].max()
            - fitted_values[row, transformed_column].min()
        )
        == 0
    )
