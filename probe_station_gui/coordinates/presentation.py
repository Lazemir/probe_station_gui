"""Pure presentation plans for software coordinate systems."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .model import (
    CoordinateFrameRecord,
    FrameKind,
    PhysicalMachinePose,
    ReadinessStatus,
    VISIBLE_STAGE_AXES,
)
from .registry import RegistrySnapshot


MACHINE_FRAME_ID = "machine"


@dataclass(frozen=True)
class CoordinateSelectorEntry:
    frame_id: str
    name: str
    group: str
    enabled: bool
    reason: str = ""


@dataclass(frozen=True)
class CoordinateAxisDisplay:
    axis: str
    value: float | None
    color_role: str
    tooltip: str


@dataclass(frozen=True)
class CoordinateDisplayPlan:
    selected_frame_id: str
    selector_entries: tuple[CoordinateSelectorEntry, ...]
    axis_updates: tuple[CoordinateAxisDisplay, ...]


def _normalized_axes(axes: Iterable[str]) -> frozenset[str]:
    return frozenset(str(axis).strip().upper() for axis in axes)


def _frame_group(kind: FrameKind) -> str:
    return {
        FrameKind.MACHINE: "Machine",
        FrameKind.DESIGN: "Designs",
        FrameKind.CUSTOM: "Custom",
    }[kind]


def _frame_selection_reason(
    record: CoordinateFrameRecord,
    *,
    homed_axes: frozenset[str],
    authority_axes: frozenset[str],
) -> str:
    if not {"X", "Y"}.issubset(homed_axes):
        return "Home X and Y to use this coordinate system."
    if "B" not in authority_axes:
        return "B coordinate is unavailable."
    if record.transform is None:
        return "Coordinate transform is unavailable."
    return ""


def _selector_entries(
    snapshot: RegistrySnapshot,
    *,
    homed_axes: frozenset[str],
    authority_axes: frozenset[str],
) -> tuple[CoordinateSelectorEntry, ...]:
    entries = [
        CoordinateSelectorEntry(
            frame_id=MACHINE_FRAME_ID,
            name="Machine",
            group="Machine",
            enabled=True,
        )
    ]
    records = sorted(
        (record for record in snapshot.records if record.kind is not FrameKind.MACHINE),
        key=lambda record: (
            0 if record.kind is FrameKind.DESIGN else 1,
            record.name.casefold(),
            record.frame_id,
        ),
    )
    for record in records:
        reason = _frame_selection_reason(
            record,
            homed_axes=homed_axes,
            authority_axes=authority_axes,
        )
        entries.append(
            CoordinateSelectorEntry(
                frame_id=record.frame_id,
                name=record.name,
                group=_frame_group(record.kind),
                enabled=not reason,
                reason=reason,
            )
        )
    return tuple(entries)


def _machine_axis_display(
    axis: str,
    *,
    physical_pose: PhysicalMachinePose,
    homed_axes: frozenset[str],
    authority_axes: frozenset[str],
) -> CoordinateAxisDisplay:
    value = physical_pose.values.get(axis)
    if value is None or axis not in authority_axes:
        return CoordinateAxisDisplay(
            axis,
            None,
            "unavailable",
            f"Physical Machine {axis} coordinate is unavailable.",
        )
    if axis != "B" and axis not in homed_axes:
        return CoordinateAxisDisplay(
            axis,
            float(value),
            "unavailable",
            f"Home {axis} to establish Machine {axis} authority.",
        )
    return CoordinateAxisDisplay(
        axis,
        float(value),
        "available",
        f"Physical Machine {axis} coordinate.",
    )


def _readiness_reason(record: CoordinateFrameRecord, axis: str) -> str | None:
    dependencies = {
        "X": ("X", "Y", "B"),
        "Y": ("Y", "X", "B"),
        "Z": ("Z",),
        "A": ("A", "Z"),
        "B": ("B",),
    }[axis]
    for dependency in dependencies:
        state = record.readiness[dependency]
        if state.status is not ReadinessStatus.READY:
            return state.reason or f"{dependency} reference is unavailable."
    return None


def _authority_reason(axis: str, authority_axes: frozenset[str]) -> str | None:
    dependencies = {
        "X": ("X", "Y", "B"),
        "Y": ("X", "Y", "B"),
        "Z": ("Z",),
        "A": ("A",),
        "B": ("B",),
    }[axis]
    missing = [dependency for dependency in dependencies if dependency not in authority_axes]
    if not missing:
        return None
    joined = "/".join(missing)
    return f"Physical Machine {joined} coordinate authority is unavailable."


def _frame_axis_values(
    record: CoordinateFrameRecord,
    physical_pose: PhysicalMachinePose,
    pivot_machine_xy: tuple[float, float],
) -> dict[str, float]:
    transform = record.transform
    if transform is None:
        return {}
    values: dict[str, float] = {}
    machine_b = physical_pose.values.get("B")
    machine_x = physical_pose.values.get("X")
    machine_y = physical_pose.values.get("Y")
    if machine_x is not None and machine_y is not None and machine_b is not None:
        values["X"], values["Y"] = transform.machine_xy_to_frame(
            (machine_x, machine_y),
            machine_b_deg=machine_b,
            pivot_machine_xy=pivot_machine_xy,
        )
    if machine_b is not None:
        values["B"] = transform.machine_b_to_frame(machine_b)
    machine_z = physical_pose.values.get("Z")
    if machine_z is not None and transform.z_zero_machine_mm is not None:
        values["Z"] = transform.machine_z_to_frame(machine_z)
    machine_a = physical_pose.values.get("A")
    if machine_a is not None and transform.a_zero_machine_mm is not None:
        values["A"] = transform.machine_a_to_frame(machine_a)
    return values


def _frame_axis_display(
    record: CoordinateFrameRecord,
    axis: str,
    *,
    values: dict[str, float],
    authority_axes: frozenset[str],
) -> CoordinateAxisDisplay:
    reason = _readiness_reason(record, axis)
    if reason is None:
        reason = _authority_reason(axis, authority_axes)
    if reason is None and axis not in values:
        reason = f"{record.name} {axis} coordinate is unavailable."
    if reason is not None:
        return CoordinateAxisDisplay(axis, None, "unavailable", reason)
    return CoordinateAxisDisplay(
        axis,
        values[axis],
        "available",
        f"{axis} coordinate in {record.name}.",
    )


def build_coordinate_display_plan(
    snapshot: RegistrySnapshot,
    *,
    selected_frame_id: str,
    physical_pose: PhysicalMachinePose,
    pivot_machine_xy: tuple[float, float],
    homed_axes: Iterable[str],
    authority_axes: Iterable[str],
    allow_unavailable_selection_for_preview: bool = False,
) -> CoordinateDisplayPlan:
    """Build one immutable selector and axis-display snapshot without I/O."""

    homed = _normalized_axes(homed_axes)
    authority = _normalized_axes(authority_axes)
    entries = _selector_entries(
        snapshot,
        homed_axes=homed,
        authority_axes=authority,
    )
    records = {record.frame_id: record for record in snapshot.records}
    requested = str(selected_frame_id or MACHINE_FRAME_ID)
    record = records.get(requested)
    if requested == MACHINE_FRAME_ID:
        selected = MACHINE_FRAME_ID
        record = None
    elif record is None:
        selected = MACHINE_FRAME_ID
    else:
        selectable = next(
            (entry.enabled for entry in entries if entry.frame_id == requested),
            False,
        )
        selected = (
            requested
            if selectable or allow_unavailable_selection_for_preview
            else MACHINE_FRAME_ID
        )
        if selected == MACHINE_FRAME_ID:
            record = None

    if record is None:
        axis_updates = tuple(
            _machine_axis_display(
                axis,
                physical_pose=physical_pose,
                homed_axes=homed,
                authority_axes=authority,
            )
            for axis in VISIBLE_STAGE_AXES
        )
    else:
        values = _frame_axis_values(record, physical_pose, pivot_machine_xy)
        axis_updates = tuple(
            _frame_axis_display(
                record,
                axis,
                values=values,
                authority_axes=authority,
            )
            for axis in VISIBLE_STAGE_AXES
        )
    return CoordinateDisplayPlan(selected, entries, axis_updates)


def decide_pending_frame_restore(
    snapshot: RegistrySnapshot,
    *,
    frame_id: str,
    homed_axes: Iterable[str],
    authority_axes: Iterable[str],
) -> str | None:
    """Return a selected ID, Machine fallback, or ``None`` while authority is pending."""

    requested = str(frame_id or MACHINE_FRAME_ID)
    if requested == MACHINE_FRAME_ID:
        return MACHINE_FRAME_ID
    homed = _normalized_axes(homed_axes)
    authority = _normalized_axes(authority_axes)
    if not {"X", "Y"}.issubset(homed) or not {"X", "Y", "B"}.issubset(authority):
        return None
    record = next(
        (record for record in snapshot.records if record.frame_id == requested),
        None,
    )
    if record is None or record.transform is None:
        return MACHINE_FRAME_ID
    if not all(
        record.readiness[axis].status is ReadinessStatus.READY
        for axis in ("X", "Y", "B", "Z")
    ):
        return MACHINE_FRAME_ID
    return requested


__all__ = [
    "MACHINE_FRAME_ID",
    "CoordinateAxisDisplay",
    "CoordinateDisplayPlan",
    "CoordinateSelectorEntry",
    "build_coordinate_display_plan",
    "decide_pending_frame_restore",
]
