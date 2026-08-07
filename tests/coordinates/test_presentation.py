from __future__ import annotations

from dataclasses import replace

import pytest

from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    PhysicalMachinePose,
    ReadinessStatus,
)
from probe_station_gui.coordinates.presentation import (
    MACHINE_FRAME_ID,
    build_coordinate_display_plan,
    decide_pending_frame_restore,
)
from probe_station_gui.coordinates.registry import RegistrySnapshot, invalidate_axes
from probe_station_gui.coordinates.transforms import BFrameTransform


DESIGN_ID = "11111111-1111-4111-8111-111111111111"
CUSTOM_ID = "22222222-2222-4222-8222-222222222222"
DRAFT_ID = "33333333-3333-4333-8333-333333333333"


def _readiness(*ready_axes: str) -> dict[str, AxisReadiness]:
    ready = set(ready_axes)
    return {
        axis: AxisReadiness(
            ReadinessStatus.READY if axis in ready else ReadinessStatus.MISSING,
            "" if axis in ready else f"Register {axis}.",
        )
        for axis in ("X", "Y", "Z", "A", "B")
    }


def _record(
    frame_id: str,
    kind: FrameKind,
    name: str,
    *,
    ready_axes: tuple[str, ...] = ("X", "Y", "Z", "A", "B"),
    transform: BFrameTransform | None = None,
) -> CoordinateFrameRecord:
    if transform is None:
        transform = BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=0.0 if "Z" in ready_axes else None,
            a_zero_machine_mm=0.0 if "A" in ready_axes else None,
        )
    return CoordinateFrameRecord(
        frame_id=frame_id,
        kind=kind,
        name=name,
        version=0,
        transform=transform,
        readiness=_readiness(*ready_axes),
        metadata={},
    )


def _snapshot(*records: CoordinateFrameRecord, generation: int = 1) -> RegistrySnapshot:
    return RegistrySnapshot(generation=generation, records=tuple(records))


def _pose(**overrides: float) -> PhysicalMachinePose:
    values = {"X": 10.0, "Y": 0.0, "Z": 5.0, "A": 9.0, "B": 0.0}
    values.update(overrides)
    return PhysicalMachinePose(values)


def _plan(
    snapshot: RegistrySnapshot,
    *,
    selected_frame_id: str = MACHINE_FRAME_ID,
    pose: PhysicalMachinePose | None = None,
    homed_axes: set[str] | None = None,
    authority_axes: set[str] | None = None,
    preview: bool = False,
):
    return build_coordinate_display_plan(
        snapshot,
        selected_frame_id=selected_frame_id,
        physical_pose=pose or _pose(),
        pivot_machine_xy=(0.0, 0.0),
        homed_axes={"X", "Y", "Z", "A"} if homed_axes is None else homed_axes,
        authority_axes={"X", "Y", "Z", "A", "B"}
        if authority_axes is None
        else authority_axes,
        allow_unavailable_selection_for_preview=preview,
    )


def test_selector_groups_machine_designs_and_customs_with_stable_ids() -> None:
    snapshot = _snapshot(
        _record(DESIGN_ID, FrameKind.DESIGN, "chip-a"),
        _record(CUSTOM_ID, FrameKind.CUSTOM, "fixture"),
    )

    plan = _plan(snapshot)

    assert [(entry.group, entry.name) for entry in plan.selector_entries] == [
        ("Machine", "Machine"),
        ("Designs", "chip-a"),
        ("Custom", "fixture"),
    ]
    assert [entry.frame_id for entry in plan.selector_entries] == [
        MACHINE_FRAME_ID,
        DESIGN_ID,
        CUSTOM_ID,
    ]
    assert all(entry.name != "Chip" for entry in plan.selector_entries)


def test_design_draft_starts_all_visible_axes_yellow_with_exact_reasons() -> None:
    draft = _record(
        DRAFT_ID,
        FrameKind.DESIGN,
        "chip-draft",
        ready_axes=(),
    )

    plan = _plan(
        _snapshot(draft),
        selected_frame_id=DRAFT_ID,
        preview=True,
    )

    assert plan.selected_frame_id == DRAFT_ID
    assert [update.axis for update in plan.axis_updates] == ["X", "Y", "Z", "A", "B"]
    assert all(update.color_role == "unavailable" for update in plan.axis_updates)
    assert {update.axis: update.tooltip for update in plan.axis_updates} == {
        axis: f"Register {axis}." for axis in ("X", "Y", "Z", "A", "B")
    }


def test_b_attached_frame_recomputes_xy_about_global_pivot() -> None:
    transform = BFrameTransform(
        origin_xy_at_reference_b=(10.0, 0.0),
        reference_b_deg=0.0,
        xy_angle_at_reference_b_deg=0.0,
        b_zero_machine_deg=5.0,
        z_zero_machine_mm=3.0,
        a_zero_machine_mm=7.0,
    )
    frame = _record(DESIGN_ID, FrameKind.DESIGN, "chip-a", transform=transform)
    snapshot = _snapshot(frame)

    at_reference = _plan(snapshot, selected_frame_id=DESIGN_ID, pose=_pose(X=10.0, Y=0.0, B=0.0))
    after_rotation = _plan(snapshot, selected_frame_id=DESIGN_ID, pose=_pose(X=0.0, Y=10.0, B=90.0))

    reference_values = {item.axis: item.value for item in at_reference.axis_updates}
    rotated_values = {item.axis: item.value for item in after_rotation.axis_updates}
    assert reference_values["X"] == pytest.approx(0.0)
    assert reference_values["Y"] == pytest.approx(0.0)
    assert reference_values["B"] == pytest.approx(-5.0)
    assert reference_values["Z"] == pytest.approx(2.0)
    assert reference_values["A"] == pytest.approx(2.0)
    assert rotated_values["X"] == pytest.approx(0.0)
    assert rotated_values["Y"] == pytest.approx(0.0)
    assert rotated_values["B"] == pytest.approx(85.0)
    assert all(item.color_role == "available" for item in after_rotation.axis_updates)


def test_missing_z_always_makes_dependent_a_unavailable() -> None:
    frame = _record(DESIGN_ID, FrameKind.DESIGN, "chip-a")
    invalidated = invalidate_axes(frame, {"Z"}, "Focus reference changed.")

    plan = _plan(_snapshot(invalidated), selected_frame_id=DESIGN_ID, preview=True)

    axes = {item.axis: item for item in plan.axis_updates}
    assert axes["Z"].color_role == "unavailable"
    assert axes["Z"].tooltip == "Focus reference changed."
    assert axes["A"].color_role == "unavailable"
    assert axes["A"].tooltip == "Focus reference changed."


def test_existing_selection_is_unavailable_until_xy_homing_and_b_authority() -> None:
    frame = _record(DESIGN_ID, FrameKind.DESIGN, "chip-a")
    snapshot = _snapshot(frame)

    unhomed = _plan(snapshot, selected_frame_id=DESIGN_ID, homed_axes=set())
    missing_b = _plan(
        snapshot,
        selected_frame_id=DESIGN_ID,
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A"},
    )

    assert unhomed.selected_frame_id == DESIGN_ID
    assert unhomed.selection_available is False
    assert not next(item for item in unhomed.selector_entries if item.frame_id == DESIGN_ID).enabled
    assert "Home X and Y" in next(
        item for item in unhomed.selector_entries if item.frame_id == DESIGN_ID
    ).reason
    assert missing_b.selected_frame_id == DESIGN_ID
    assert missing_b.selection_available is False
    assert "B coordinate is unavailable" in next(
        item for item in missing_b.selector_entries if item.frame_id == DESIGN_ID
    ).reason


def test_missing_selected_frame_falls_back_to_machine() -> None:
    plan = _plan(_snapshot(), selected_frame_id=DESIGN_ID)

    assert plan.selected_frame_id == MACHINE_FRAME_ID


def test_existing_selection_survives_temporary_b_authority_loss() -> None:
    plan = _plan(
        _snapshot(_record(DESIGN_ID, FrameKind.DESIGN, "chip-a")),
        selected_frame_id=DESIGN_ID,
        authority_axes={"X", "Y", "Z", "A"},
    )

    assert plan.selected_frame_id == DESIGN_ID
    assert plan.selection_available is False
    assert plan.selection_reason
    assert all(update.value is None for update in plan.axis_updates)
    assert all(update.color_role == "unavailable" for update in plan.axis_updates)


def test_missing_selected_record_is_permanent_machine_fallback() -> None:
    plan = _plan(_snapshot(), selected_frame_id=DESIGN_ID)

    assert plan.selected_frame_id == MACHINE_FRAME_ID
    assert plan.selection_available is True


def test_restore_waits_for_authority_then_allows_yellow_a() -> None:
    frame = _record(
        DESIGN_ID,
        FrameKind.DESIGN,
        "chip-a",
        ready_axes=("X", "Y", "Z", "B"),
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=1.0,
            a_zero_machine_mm=None,
        ),
    )
    snapshot = _snapshot(frame)

    assert decide_pending_frame_restore(
        snapshot,
        frame_id=DESIGN_ID,
        homed_axes=set(),
        authority_axes={"X", "Y", "Z", "A", "B"},
    ) is None
    assert decide_pending_frame_restore(
        snapshot,
        frame_id=DESIGN_ID,
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A"},
    ) is None
    assert decide_pending_frame_restore(
        snapshot,
        frame_id=DESIGN_ID,
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A", "B"},
    ) == DESIGN_ID


def test_restore_rejects_missing_z_or_deleted_frame_after_authority_is_known() -> None:
    missing_z = _record(
        DESIGN_ID,
        FrameKind.DESIGN,
        "chip-a",
        ready_axes=("X", "Y", "B"),
    )

    assert decide_pending_frame_restore(
        _snapshot(missing_z),
        frame_id=DESIGN_ID,
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A", "B"},
    ) == MACHINE_FRAME_ID
    assert decide_pending_frame_restore(
        _snapshot(),
        frame_id=DESIGN_ID,
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A", "B"},
    ) == MACHINE_FRAME_ID


def test_semantically_invalid_ready_origins_remain_displayed_but_cannot_restore() -> None:
    corrupt = _record(
        DESIGN_ID,
        FrameKind.DESIGN,
        "corrupt",
        transform=BFrameTransform.identity(),
    )
    snapshot = _snapshot(corrupt)

    plan = _plan(snapshot, selected_frame_id=DESIGN_ID)

    entry = next(item for item in plan.selector_entries if item.frame_id == DESIGN_ID)
    assert entry.enabled is False
    assert "READY Z requires" in entry.reason
    assert plan.selected_frame_id == DESIGN_ID
    assert plan.selection_available is False
    assert decide_pending_frame_restore(
        snapshot,
        frame_id=DESIGN_ID,
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A", "B"},
    ) == MACHINE_FRAME_ID


@pytest.mark.parametrize("status", ["pending", "blocked"])
def test_unverified_design_provenance_cannot_select_or_restore(status: str) -> None:
    record = _record(DESIGN_ID, FrameKind.DESIGN, "chip-a")
    record = replace(
        record,
        metadata={
            "_runtime_provenance_status": status,
            "_runtime_provenance_reason": "Design source changed.",
        },
    )
    snapshot = _snapshot(record)

    plan = _plan(snapshot, selected_frame_id=DESIGN_ID)

    entry = next(item for item in plan.selector_entries if item.frame_id == DESIGN_ID)
    assert entry.enabled is False
    assert entry.reason == "Design source changed."
    assert decide_pending_frame_restore(
        snapshot,
        frame_id=DESIGN_ID,
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A", "B"},
    ) == MACHINE_FRAME_ID


def test_record_version_change_preserves_selection_by_frame_id() -> None:
    original = _record(DESIGN_ID, FrameKind.DESIGN, "chip-a")
    updated = replace(original, version=7, name="chip-renamed")

    plan = _plan(
        _snapshot(updated, generation=9),
        selected_frame_id=DESIGN_ID,
    )

    assert plan.selected_frame_id == DESIGN_ID
    assert next(item for item in plan.selector_entries if item.frame_id == DESIGN_ID).name == "chip-renamed"


def test_missing_physical_axis_yellows_only_dependent_values_without_stale_value() -> None:
    frame = _record(
        DESIGN_ID,
        FrameKind.DESIGN,
        "chip-a",
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=0.0,
            a_zero_machine_mm=0.0,
        ),
    )

    plan = _plan(
        _snapshot(frame),
        selected_frame_id=DESIGN_ID,
        pose=PhysicalMachinePose({"Y": 0.0, "Z": 5.0, "A": 9.0, "B": 0.0}),
        authority_axes={"Y", "Z", "A", "B"},
    )

    axes = {item.axis: item for item in plan.axis_updates}
    assert axes["X"].value is None
    assert axes["Y"].value is None
    assert axes["X"].color_role == "unavailable"
    assert axes["Y"].color_role == "unavailable"
    assert axes["X"].tooltip == "Physical Machine X coordinate authority is unavailable."
    assert axes["Y"].tooltip == "Physical Machine X coordinate authority is unavailable."
    assert all(axes[axis].color_role == "available" for axis in ("Z", "A", "B"))
