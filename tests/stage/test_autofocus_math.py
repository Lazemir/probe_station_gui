import numpy as np
import pytest

from probe_station_gui.stage.autofocus_math import (
    autofocus_sweep_feedrate_mm_min,
    estimate_shift_with_response,
    focus_metric,
    frame_rate_from_timestamps,
    parabolic_focus_peak,
    static_focus_candidates,
)


def test_frame_rate_from_timestamps_requires_increasing_samples() -> None:
    assert frame_rate_from_timestamps([1.0]) is None
    assert frame_rate_from_timestamps([1.0, 1.0]) is None
    assert frame_rate_from_timestamps([0.0, 0.05, 0.10, 0.15]) == pytest.approx(20.0)


def test_autofocus_sweep_feedrate_uses_step_frame_rate_and_minimum() -> None:
    assert autofocus_sweep_feedrate_mm_min(
        0.002,
        20.0,
        min_feedrate_mm_min=1.0,
    ) == pytest.approx(2.4)
    assert autofocus_sweep_feedrate_mm_min(
        0.0001,
        20.0,
        min_feedrate_mm_min=1.0,
    ) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="enough frames"):
        autofocus_sweep_feedrate_mm_min(
            0.002,
            None,
            min_feedrate_mm_min=1.0,
        )
    with pytest.raises(ValueError, match="speed estimate"):
        autofocus_sweep_feedrate_mm_min(
            0.0,
            20.0,
            min_feedrate_mm_min=1.0,
        )


def test_static_focus_candidates_are_centered_odd_and_clamped() -> None:
    assert static_focus_candidates(
        10.0,
        min_z=0.0,
        max_z=20.0,
        step_mm=0.01,
        max_points=4,
    ) == pytest.approx([9.98, 9.99, 10.0, 10.01, 10.02])

    assert static_focus_candidates(
        0.005,
        min_z=0.0,
        max_z=0.02,
        step_mm=0.01,
        max_points=5,
    ) == pytest.approx([0.0, 0.005, 0.015, 0.02])


def test_parabolic_focus_peak_fits_interior_maximum_only() -> None:
    scored = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]

    assert parabolic_focus_peak(scored, 1) == pytest.approx(1.0)
    assert parabolic_focus_peak(scored, 0) is None


def test_focus_metric_distinguishes_edges_from_flat_image() -> None:
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "medianBlur"):
        pytest.skip("cv2 test stub does not provide image filters")
    flat = np.full((32, 32), 128, dtype=np.uint8)
    edge = np.zeros((32, 32), dtype=np.uint8)
    edge[:, 16:] = 255

    assert focus_metric(edge) > focus_metric(flat)


def test_estimate_shift_with_response_returns_zero_for_identical_frames() -> None:
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "createHanningWindow"):
        pytest.skip("cv2 test stub does not provide phase correlation")
    frame = np.zeros((32, 32), dtype=np.uint8)
    frame[8:24, 8:24] = 255

    shift_x, shift_y, response = estimate_shift_with_response(frame, frame)

    assert shift_x == pytest.approx(0.0, abs=1e-5)
    assert shift_y == pytest.approx(0.0, abs=1e-5)
    assert response > 0.0
