from __future__ import annotations

import math

import numpy as np
import pytest

from probe_station_gui.design.model import DesignModelError, DesignRegistration


def _transform(
    points: list[tuple[float, float]],
    *,
    scale: float,
    rotation_deg: float,
    offset: tuple[float, float],
) -> list[tuple[float, float]]:
    angle = math.radians(rotation_deg)
    rotation = np.asarray(
        [[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]],
        dtype=float,
    )
    translation = np.asarray(offset, dtype=float)
    return [
        tuple(float(value) for value in scale * rotation @ np.asarray(point) + translation)
        for point in points
    ]


@pytest.mark.parametrize(
    "source",
    [
        [(0.0, 0.0), (10.0, 0.0)],
        [(0.0, 0.0), (10.0, 0.0), (2.0, 7.0)],
    ],
)
def test_similarity_fit_uses_all_pairs_and_recovers_exact_transform(source) -> None:
    stage = _transform(
        source,
        scale=2.5,
        rotation_deg=37.0,
        offset=(12.0, -4.0),
    )

    registration = DesignRegistration.from_marks(source, stage)

    assert registration.valid
    assert registration.scale == pytest.approx(2.5)
    assert registration.rotation_deg == pytest.approx(37.0)
    assert registration.offset == pytest.approx(np.asarray([12.0, -4.0]))
    assert registration.source_residual_summary.count == len(source)
    assert registration.source_residual_summary.rms == pytest.approx(0.0, abs=1e-10)
    assert registration.source_residual_summary.max_error == pytest.approx(
        0.0, abs=1e-10
    )


def test_noisy_all_point_fit_reports_visible_rms_and_max_without_rejecting() -> None:
    source = [(0.0, 0.0), (10.0, 0.0), (1.0, 8.0), (9.0, 6.0)]
    exact = np.asarray(
        _transform(source, scale=1.8, rotation_deg=-23.0, offset=(3.0, 7.0))
    )
    noise = np.asarray([[0.4, -0.2], [-0.3, 0.1], [0.2, 0.35], [-0.1, -0.25]])

    registration = DesignRegistration.from_marks(source, exact + noise)

    assert registration.valid
    assert registration.source_residual_summary.count == 4
    assert registration.source_residual_summary.rms > 0.0
    assert registration.source_residual_summary.max_error >= (
        registration.source_residual_summary.rms
    )
    assert registration.source_residual_summary.max_error == pytest.approx(
        max(registration.source_residuals)
    )
    assert registration.rotation_deg == pytest.approx(-23.0, abs=1.0)
    assert registration.scale == pytest.approx(1.8, abs=0.05)


def test_reflection_input_returns_best_proper_rotation_with_residuals() -> None:
    source = [(0.0, 0.0), (8.0, 0.0), (1.0, 3.0), (4.0, 1.0)]
    reflected = [(x + 2.0, -y + 5.0) for x, y in source]

    registration = DesignRegistration.from_marks(source, reflected)

    assert registration.valid
    assert np.linalg.det(registration.matrix) > 0.0
    assert registration.source_residual_summary.rms > 0.0
    assert registration.source_residual_summary.max_error > 0.0


@pytest.mark.parametrize(
    ("source", "stage", "message"),
    [
        ([(0.0, 0.0)], [(1.0, 1.0)], "At least two"),
        ([(0.0, 0.0), (1.0, 0.0)], [(1.0, 1.0)], "counts must match"),
        (
            [(0.0, 0.0), (math.nan, 1.0)],
            [(1.0, 1.0), (2.0, 2.0)],
            "finite",
        ),
        (
            [(1.0, 1.0), (1.0, 1.0), (1.0, 1.0)],
            [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)],
            "degenerate",
        ),
    ],
)
def test_similarity_fit_rejects_invalid_pair_geometry(source, stage, message) -> None:
    with pytest.raises(DesignModelError, match=message):
        DesignRegistration.from_marks(source, stage)


def test_non_finite_check_marks_are_rejected() -> None:
    with pytest.raises(DesignModelError, match="finite"):
        DesignRegistration.from_marks(
            [(0.0, 0.0), (1.0, 0.0)],
            [(2.0, 3.0), (3.0, 3.0)],
            check_design_marks=[(math.inf, 0.0)],
            check_stage_marks=[(2.0, 3.0)],
        )
