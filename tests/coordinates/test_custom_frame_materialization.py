from __future__ import annotations

from uuid import uuid4

import pytest

from probe_station_gui.coordinates import (
    AxisReadiness,
    BFrameTransform,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
)
from probe_station_gui.coordinates.software_frames import materialize_custom_frames
from probe_station_gui.settings.software_coordinates import (
    CustomFrameSettings,
    SoftwareCoordinateSettings,
)


def _custom(frame_id: str, *, name: str = "fixture", z: float | None = None) -> CustomFrameSettings:
    return CustomFrameSettings(
        frame_id=frame_id,
        name=name,
        origin_x_mm=1.0,
        origin_y_mm=2.0,
        reference_b_deg=3.0,
        xy_angle_deg=4.0,
        b_zero_deg=5.0,
        z_zero_mm=z,
    )


def _design(frame_id: str) -> CoordinateFrameRecord:
    return CoordinateFrameRecord(
        frame_id=frame_id,
        kind=FrameKind.DESIGN,
        name="design",
        version=4,
        transform=BFrameTransform.identity(),
        readiness={axis: AxisReadiness(ReadinessStatus.READY) for axis in "XYZAB"},
        metadata={"registration_marks": ["keep"]},
    )


def test_materialization_adds_and_updates_custom_records_without_touching_design() -> None:
    design = _design(str(uuid4()))
    frame_id = str(uuid4())
    added = materialize_custom_frames(
        (design,),
        SoftwareCoordinateSettings(custom_frames=(_custom(frame_id),)),
    )

    custom = next(record for record in added if record.kind is FrameKind.CUSTOM)
    assert custom.version == 0
    assert custom.transform is not None
    assert custom.transform.origin_xy_at_reference_b == (1.0, 2.0)
    assert custom.readiness["Z"].status is ReadinessStatus.MISSING
    assert next(record for record in added if record.kind is FrameKind.DESIGN) == design

    updated = materialize_custom_frames(
        added,
        SoftwareCoordinateSettings(custom_frames=(_custom(frame_id, name="renamed", z=7.0),)),
    )
    revised = next(record for record in updated if record.frame_id == frame_id)
    assert revised.version == 1
    assert revised.name == "renamed"
    assert revised.readiness["Z"].status is ReadinessStatus.READY


def test_materialization_deletes_removed_custom_frame_in_one_prepared_result() -> None:
    first_id, second_id = str(uuid4()), str(uuid4())
    existing = materialize_custom_frames(
        (),
        SoftwareCoordinateSettings(custom_frames=(_custom(first_id), _custom(second_id))),
    )

    reconciled = materialize_custom_frames(
        existing,
        SoftwareCoordinateSettings(custom_frames=(_custom(second_id),)),
    )

    assert [record.frame_id for record in reconciled] == [second_id]


def test_materialization_rejects_id_collision_before_returning_partial_records() -> None:
    collision_id = str(uuid4())
    design = _design(collision_id)

    with pytest.raises(ValueError, match="collides"):
        materialize_custom_frames(
            (design,),
            SoftwareCoordinateSettings(custom_frames=(_custom(collision_id),)),
        )

    assert design.kind is FrameKind.DESIGN
