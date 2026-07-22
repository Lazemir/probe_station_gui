from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
)
from probe_station_gui.coordinates.registry import (
    CoordinateFrameRegistry,
    FrameVersionConflict,
    invalidate_axes,
)
from probe_station_gui.coordinates.transforms import BFrameTransform
import pytest


def _ready_design() -> CoordinateFrameRecord:
    return CoordinateFrameRecord.create_design(
        frame_id="8f54e770-d348-4c4f-8da8-d7d46678aa26",
        name="chip-a",
        transform=BFrameTransform.identity(),
        readiness={
            axis: AxisReadiness(ReadinessStatus.READY)
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata={},
    )


def test_z_invalidation_always_invalidates_a() -> None:
    changed = invalidate_axes(_ready_design(), {"Z"}, "Focus reference changed.")
    assert changed.readiness["Z"].status is ReadinessStatus.STALE
    assert changed.readiness["A"].status is ReadinessStatus.STALE
    assert changed.readiness["X"].available


def test_xy_or_b_invalidation_cascades_through_z_and_a() -> None:
    changed = invalidate_axes(_ready_design(), {"B"}, "B calibration changed.")
    assert {axis for axis, state in changed.readiness.items() if not state.available} == {
        "X", "Y", "Z", "A", "B"
    }


def test_registry_rejects_stale_writer_version() -> None:
    registry = CoordinateFrameRegistry()
    added = registry.add(_ready_design())
    registry.replace(added.with_name("first"), expected_version=added.version)
    try:
        registry.replace(added.with_name("second"), expected_version=added.version)
    except FrameVersionConflict as exc:
        assert exc.frame_id == added.frame_id
    else:
        raise AssertionError("stale replacement was accepted")


def test_temporary_authority_block_does_not_erase_reference_values() -> None:
    original = _ready_design()
    blocked = original.with_authority_block({"B"}, "Controller B unavailable.")
    assert blocked.transform == original.transform
    assert blocked.readiness["B"] == AxisReadiness(
        ReadinessStatus.BLOCKED,
        "Controller B unavailable.",
    )


def test_design_constructor_requires_valid_identity_name_and_all_axes() -> None:
    ready = {
        axis: AxisReadiness(ReadinessStatus.READY)
        for axis in ("X", "Y", "Z", "A", "B")
    }

    with pytest.raises(ValueError, match="UUID"):
        CoordinateFrameRecord.create_design(
            frame_id="not-a-uuid",
            name="chip-a",
            transform=BFrameTransform.identity(),
            readiness=ready,
            metadata={},
        )
    with pytest.raises(ValueError, match="name"):
        CoordinateFrameRecord.create_design(
            frame_id="8f54e770-d348-4c4f-8da8-d7d46678aa26",
            name=" ",
            transform=BFrameTransform.identity(),
            readiness=ready,
            metadata={},
        )
    with pytest.raises(ValueError, match="readiness"):
        CoordinateFrameRecord.create_design(
            frame_id="8f54e770-d348-4c4f-8da8-d7d46678aa26",
            name="chip-a",
            transform=BFrameTransform.identity(),
            readiness={axis: state for axis, state in ready.items() if axis != "A"},
            metadata={},
        )


def test_snapshot_is_versioned_and_orders_machine_design_then_custom() -> None:
    readiness = {
        axis: AxisReadiness(ReadinessStatus.READY)
        for axis in ("X", "Y", "Z", "A", "B")
    }
    registry = CoordinateFrameRegistry()
    registry.add(
        CoordinateFrameRecord(
            "11111111-1111-4111-8111-111111111111",
            FrameKind.CUSTOM,
            "alpha-custom",
            0,
            None,
            readiness,
            {},
        )
    )
    registry.add(
        CoordinateFrameRecord(
            "22222222-2222-4222-8222-222222222222",
            FrameKind.DESIGN,
            "zeta-design",
            0,
            BFrameTransform.identity(),
            readiness,
            {},
        )
    )
    registry.add(
        CoordinateFrameRecord(
            "33333333-3333-4333-8333-333333333333",
            FrameKind.MACHINE,
            "machine",
            0,
            None,
            readiness,
            {},
        )
    )

    snapshot = registry.snapshot()

    assert snapshot.generation == 3
    assert [record.kind for record in snapshot.records] == [
        FrameKind.MACHINE,
        FrameKind.DESIGN,
        FrameKind.CUSTOM,
    ]
