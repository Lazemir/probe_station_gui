import numpy as np
import pytest

from probe_station_gui.design.rigid_registration import (
    ResidualMetrics,
    RigidRegistrationFit,
    fit_rigid_registration,
)


def test_rigid_fit_does_not_apply_measured_scale_difference() -> None:
    fit = fit_rigid_registration(
        design_points=((0.0, 0.0), (10.0, 0.0)),
        machine_points=((1.0, 2.0), (21.0, 2.0)),
        design_unit_mm=1.0,
    )

    assert fit.rotation_deg == pytest.approx(0.0)
    assert fit.distance_scale_ratio == pytest.approx(2.0)
    assert fit.design_mm_to_machine_xy((5.0, 5.0)) == pytest.approx((11.0, 7.0))


def test_rigid_fit_recovers_rotation_translation_and_check_residuals() -> None:
    fit = fit_rigid_registration(
        design_points=((0.0, 0.0), (2.0, 0.0), (0.0, 2.0)),
        machine_points=((5.0, 7.0), (5.0, 9.0), (3.0, 7.0)),
        design_unit_mm=1.0,
        check_design_points=((1.0, 1.0),),
        check_machine_points=((4.0, 8.1),),
    )

    assert fit.rotation_deg == pytest.approx(90.0)
    assert fit.source_residual.rms_mm == pytest.approx(0.0, abs=1e-10)
    assert fit.check_residual.max_mm == pytest.approx(0.1)
    assert fit.machine_xy_to_design_mm((4.0, 8.0)) == pytest.approx((1.0, 1.0))


def test_design_unit_conversion_happens_before_fit() -> None:
    fit = fit_rigid_registration(
        design_points=((0.0, 0.0), (1000.0, 0.0)),
        machine_points=((0.0, 0.0), (1.0, 0.0)),
        design_unit_mm=0.001,
    )

    assert fit.distance_scale_ratio == pytest.approx(1.0)


def test_reflected_unit_square_keeps_zero_scale_ratio_as_a_diagnostic() -> None:
    fit = fit_rigid_registration(
        design_points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        machine_points=((0.0, 0.0), (-1.0, 0.0), (-1.0, 1.0), (0.0, 1.0)),
        design_unit_mm=1.0,
    )

    assert fit.distance_scale_ratio == pytest.approx(0.0, abs=1e-12)
    assert fit.source_residual.rms_mm > 0.0


def test_fit_arrays_are_immutable_and_detached_from_constructor_inputs() -> None:
    rotation = np.eye(2, dtype=float)
    offset = np.asarray((1.0, 2.0), dtype=float)
    fit = RigidRegistrationFit(
        rotation=rotation,
        offset_machine_mm=offset,
        rotation_deg=0.0,
        distance_scale_ratio=1.0,
        source_residual=ResidualMetrics(),
        check_residual=ResidualMetrics(),
    )

    rotation[0, 0] = 99.0
    offset[0] = 99.0

    assert fit.rotation[0, 0] == pytest.approx(1.0)
    assert fit.offset_machine_mm[0] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        fit.rotation[0, 0] = 2.0
    with pytest.raises(ValueError):
        fit.offset_machine_mm[0] = 2.0
