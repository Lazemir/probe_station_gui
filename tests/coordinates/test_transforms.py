import math

import pytest

from probe_station_gui.coordinates.model import (
    STAGE_AXES,
    VISIBLE_STAGE_AXES,
    PhysicalMachinePose,
)
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.coordinates.model import normalize_axis_values
from probe_station_gui.coordinates.transforms import rotate_xy


def test_c_is_retained_in_backend_but_hidden_from_ordinary_gui() -> None:
    assert STAGE_AXES == ("X", "Y", "Z", "A", "B", "C")
    assert VISIBLE_STAGE_AXES == ("X", "Y", "Z", "A", "B")


def test_physical_pose_rejects_raw_non_finite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        PhysicalMachinePose.from_mapping({"X": math.nan})


def test_physical_pose_normalizes_axes_and_requires_known_values() -> None:
    pose = PhysicalMachinePose.from_mapping({" x ": 1, "c": 2.5})

    assert pose.to_dict() == {"X": 1.0, "C": 2.5}
    assert pose.require(" X ") == 1.0
    with pytest.raises(ValueError, match="Physical Machine Y"):
        pose.require("Y")


def test_normalize_axis_values_returns_immutable_canonical_mapping() -> None:
    values = normalize_axis_values({"z": 3})

    assert dict(values) == {"Z": 3.0}
    with pytest.raises(TypeError):
        values["X"] = 1.0  # type: ignore[index]


def test_normalize_axis_values_rejects_unsupported_axis() -> None:
    with pytest.raises(ValueError, match="Unsupported stage axis"):
        normalize_axis_values({"Q": 1.0})


def test_rotate_xy_uses_counter_clockwise_rotation() -> None:
    assert rotate_xy((1.0, 0.0), 90.0) == pytest.approx((0.0, 1.0))


def test_b_attached_transform_round_trips_after_rotation() -> None:
    transform = BFrameTransform(
        origin_xy_at_reference_b=(10.0, 0.0),
        reference_b_deg=0.0,
        xy_angle_at_reference_b_deg=90.0,
        b_zero_machine_deg=-90.0,
        z_zero_machine_mm=5.0,
        a_zero_machine_mm=7.0,
    )
    machine_xy = transform.frame_xy_to_machine(
        (2.0, 3.0),
        machine_b_deg=90.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    assert machine_xy == pytest.approx((-2.0, 7.0))
    assert transform.machine_xy_to_frame(
        machine_xy,
        machine_b_deg=90.0,
        pivot_machine_xy=(0.0, 0.0),
    ) == pytest.approx((2.0, 3.0))
    assert transform.machine_b_to_frame(0.0) == pytest.approx(90.0)
    assert transform.machine_z_to_frame(6.5) == pytest.approx(1.5)
    assert transform.machine_a_to_frame(8.25) == pytest.approx(1.25)


def test_missing_z_or_a_origin_is_not_silently_machine_zero() -> None:
    transform = BFrameTransform(
        origin_xy_at_reference_b=(0.0, 0.0),
        reference_b_deg=0.0,
        xy_angle_at_reference_b_deg=0.0,
        b_zero_machine_deg=0.0,
    )
    with pytest.raises(ValueError, match="Z origin"):
        transform.machine_z_to_frame(1.0)
    with pytest.raises(ValueError, match="A origin"):
        transform.machine_a_to_frame(1.0)


def test_b_attached_transform_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        BFrameTransform(
            origin_xy_at_reference_b=(math.nan, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
        )
