from __future__ import annotations

import pytest

from probe_station_gui.coordinates import presentation as presentation_module
from probe_station_gui.coordinates.lifecycle import MACHINE_FRAME_ID
from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    PhysicalMachinePose,
    ReadinessStatus,
)
from probe_station_gui.coordinates.presentation import (
    CoordinateSelectorEntry,
    build_coordinate_display_plan,
)
from probe_station_gui.coordinates.registry import RegistrySnapshot, invalidate_axes
from probe_station_gui.coordinates.transforms import BFrameTransform


DESIGN_ID = "11111111-1111-4111-8111-111111111111"


def _record(
    *,
    ready_axes: tuple[str, ...] = ("X", "Y", "Z", "A", "B"),
    transform: BFrameTransform | None = None,
) -> CoordinateFrameRecord:
    ready = set(ready_axes)
    return CoordinateFrameRecord(
        frame_id=DESIGN_ID,
        kind=FrameKind.DESIGN,
        name="chip",
        version=0,
        transform=transform
        or BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=0.0 if "Z" in ready else None,
            a_zero_machine_mm=0.0 if "A" in ready else None,
        ),
        readiness={
            axis: AxisReadiness(
                ReadinessStatus.READY if axis in ready else ReadinessStatus.MISSING,
                "" if axis in ready else f"Register {axis}.",
            )
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata={},
    )


def _plan(
    record: CoordinateFrameRecord,
    *,
    pose: PhysicalMachinePose | None = None,
    homed_axes: set[str] | None = None,
    authority_axes: set[str] | None = None,
    selection_available: bool = True,
    selection_reason: str | None = None,
):
    snapshot = RegistrySnapshot(1, (record,))
    entries = (
        CoordinateSelectorEntry(MACHINE_FRAME_ID, "Machine", "Machine", True),
        CoordinateSelectorEntry(
            record.frame_id,
            record.name,
            "Designs",
            selection_available,
            selection_reason or "",
        ),
    )
    return build_coordinate_display_plan(
        snapshot,
        selected_frame_id=record.frame_id,
        physical_pose=pose
        or PhysicalMachinePose({"X": 10.0, "Y": 0.0, "Z": 5.0, "A": 9.0, "B": 0.0}),
        pivot_machine_xy=(0.0, 0.0),
        homed_axes=(
            {"X", "Y", "Z", "A"}
            if homed_axes is None
            else homed_axes
        ),
        authority_axes={"X", "Y", "Z", "A", "B"}
        if authority_axes is None
        else authority_axes,
        selector_entries=entries,
        selection_available=selection_available,
        selection_reason=selection_reason,
    )


def test_presentation_consumes_finished_selection_without_lifecycle_policy() -> None:
    assert "CoordinateFrameLifecycle" not in vars(presentation_module)
    assert "FrameSelectionContext" not in vars(presentation_module)
    plan = _plan(
        _record(),
        selection_available=False,
        selection_reason="B coordinate is unavailable.",
    )
    assert plan.selected_frame_id == DESIGN_ID
    assert plan.selection_available is False
    assert plan.selection_reason == "B coordinate is unavailable."
    assert all(update.value is None for update in plan.axis_updates)


def test_b_attached_frame_recomputes_xy_about_global_pivot() -> None:
    frame = _record(
        transform=BFrameTransform(
            origin_xy_at_reference_b=(10.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=5.0,
            z_zero_machine_mm=3.0,
            a_zero_machine_mm=7.0,
        )
    )
    reference = _plan(frame)
    rotated = _plan(
        frame,
        pose=PhysicalMachinePose(
            {"X": 0.0, "Y": 10.0, "Z": 5.0, "A": 9.0, "B": 90.0}
        ),
    )
    reference_values = {item.axis: item.value for item in reference.axis_updates}
    rotated_values = {item.axis: item.value for item in rotated.axis_updates}
    assert reference_values == pytest.approx(
        {"X": 0.0, "Y": 0.0, "Z": 2.0, "A": 2.0, "B": -5.0}
    )
    assert rotated_values["X"] == pytest.approx(0.0)
    assert rotated_values["Y"] == pytest.approx(0.0)
    assert rotated_values["B"] == pytest.approx(85.0)


def test_missing_z_always_makes_dependent_a_unavailable() -> None:
    invalidated = invalidate_axes(_record(), {"Z"}, "Focus reference changed.")
    axes = {item.axis: item for item in _plan(invalidated).axis_updates}
    assert axes["Z"].color_role == "unavailable"
    assert axes["Z"].tooltip == "Focus reference changed."
    assert axes["A"].color_role == "unavailable"
    assert axes["A"].tooltip == "Focus reference changed."


def test_lost_physical_z_authority_yellows_z_and_dependent_a() -> None:
    axes = {
        item.axis: item
        for item in _plan(
            _record(),
            homed_axes={"X", "Y", "A"},
        ).axis_updates
    }

    assert axes["Z"].color_role == "unavailable"
    assert axes["A"].color_role == "unavailable"
    assert axes["A"].tooltip == (
        "Physical Machine Z coordinate authority is unavailable."
    )


def test_missing_physical_axis_yellows_only_dependent_values() -> None:
    plan = _plan(
        _record(),
        pose=PhysicalMachinePose({"Y": 0.0, "Z": 5.0, "A": 9.0, "B": 0.0}),
        authority_axes={"Y", "Z", "A", "B"},
    )
    axes = {item.axis: item for item in plan.axis_updates}
    assert axes["X"].value is None
    assert axes["Y"].value is None
    assert axes["X"].tooltip == "Physical Machine X coordinate authority is unavailable."
    assert axes["Y"].tooltip == "Physical Machine X coordinate authority is unavailable."
    assert all(axes[axis].color_role == "available" for axis in ("Z", "A", "B"))


def test_c_axis_never_appears_in_coordinate_display_plan() -> None:
    plan = _plan(_record())
    assert [update.axis for update in plan.axis_updates] == ["X", "Y", "Z", "A", "B"]
