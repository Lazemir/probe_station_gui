from __future__ import annotations

import math

import numpy as np
import pytest

from probe_station_gui.design.model import DesignModelError
from probe_station_gui.design.rigid_registration import (
    DesignRegistration,
    ResidualSummary,
)


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
        tuple(
            float(value) for value in scale * rotation @ np.asarray(point) + translation
        )
        for point in points
    ]


def test_registration_types_have_one_canonical_owner() -> None:
    import probe_station_gui

    from probe_station_gui.design import model, rigid_registration

    assert DesignRegistration.__module__ == (
        "probe_station_gui.design.rigid_registration"
    )
    assert ResidualSummary.__module__ == ("probe_station_gui.design.rigid_registration")
    assert set(model.__all__) == {
        "DesignDocument",
        "DesignModelError",
        "LayerKey",
        "MeasurementTarget",
        "Point2D",
        "SnapResult",
    }
    assert not hasattr(model, "DesignRegistration")
    assert not hasattr(model, "ResidualSummary")
    assert "DesignRegistration" not in probe_station_gui.__all__
    assert not hasattr(probe_station_gui, "DesignRegistration")
    assert set(rigid_registration.__all__) == {
        "DesignRegistration",
        "ResidualMetrics",
        "ResidualSummary",
        "RigidRegistrationFit",
        "fit_rigid_registration",
    }


@pytest.mark.parametrize(
    "source",
    [
        [(0.0, 0.0), (10.0, 0.0)],
        [(0.0, 0.0), (10.0, 0.0), (2.0, 7.0)],
    ],
)
def test_rigid_fit_keeps_scale_as_a_diagnostic_only(source) -> None:
    stage = _transform(
        source,
        scale=2.5,
        rotation_deg=37.0,
        offset=(12.0, -4.0),
    )

    registration = DesignRegistration.from_marks(source, stage)

    assert registration.valid
    assert registration.distance_scale_ratio == pytest.approx(2.5)
    assert registration.scale == pytest.approx(2.5)
    assert registration.rotation_deg == pytest.approx(37.0)
    assert np.linalg.det(registration.matrix) == pytest.approx(1.0)
    assert registration.design_to_stage(source[0]) != pytest.approx(stage[0])
    assert registration.source_residual_summary.count == len(source)
    assert registration.source_residual_summary.rms > 0.0
    assert registration.source_residual_summary.max_error >= (
        registration.source_residual_summary.rms
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
    assert registration.distance_scale_ratio == pytest.approx(1.8, abs=0.05)


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
def test_rigid_fit_rejects_invalid_pair_geometry(source, stage, message) -> None:
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
