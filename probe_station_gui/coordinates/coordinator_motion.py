"""Pure selected-system motion leases and Machine-vector projection."""

from __future__ import annotations

import math

from .coordinator_model import (
    CoordinateAuthorityObservation,
    CoordinateMotionLease,
    CoordinateMotionProjection,
    CoordinateMotionRequest,
)
from .lifecycle import MACHINE_FRAME_ID
from .model import STAGE_AXES, CoordinateFrameRecord
from .presentation import CoordinateDisplayPlan
from .registry import RegistrySnapshot


def build_coordinate_motion_lease(
    registry: RegistrySnapshot,
    *,
    selected_frame_id: str,
    selection_available: bool,
    selection_reason: str | None,
    authority: CoordinateAuthorityObservation,
    display_plan: CoordinateDisplayPlan,
) -> CoordinateMotionLease:
    """Freeze exactly the transform and authority used by a rendered plan."""

    selected = str(selected_frame_id)
    record = next(
        (candidate for candidate in registry.records if candidate.frame_id == selected),
        None,
    )
    display_values = tuple(
        (update.axis, float(update.value))
        for update in display_plan.axis_updates
        if update.value is not None and update.color_role == "available"
    )
    basis_fingerprint = _basis_fingerprint(
        registry,
        selected=selected,
        selection_available=selection_available,
        selection_reason=selection_reason,
        record=record,
        authority=authority,
        display_values=display_values,
    )
    fingerprint = (
        basis_fingerprint,
        authority.physical_pose,
        authority.machine_snapshot,
        display_values,
    )
    return CoordinateMotionLease(
        selected_frame_id=selected,
        available=bool(selection_available),
        reason=selection_reason or None,
        record=record,
        authority=authority,
        display_values=display_values,
        basis_fingerprint=basis_fingerprint,
        fingerprint=fingerprint,
    )


def project_coordinate_motion(
    current: CoordinateMotionLease,
    request: CoordinateMotionRequest,
) -> CoordinateMotionProjection:
    """Project one current GUI request into configured Machine coordinates."""

    if request.lease.fingerprint != current.fingerprint:
        if (
            not request.allow_pose_rebase
            or request.lease.basis_fingerprint != current.basis_fingerprint
        ):
            return _rejected(
                current,
                "Coordinate System authority changed before movement.",
            )
    if not current.available:
        return _rejected(
            current,
            current.reason or "Coordinate System is unavailable.",
        )
    machine_snapshot = current.authority.machine_snapshot
    if machine_snapshot is None:
        return _rejected(current, "Current Machine coordinates are unavailable.")

    displayed = dict(current.display_values)
    requested = dict(request.axis_values)
    missing = tuple(axis for axis in requested if axis not in displayed)
    if missing:
        return _rejected(
            current,
            f"{missing[0]} coordinate is unavailable in the selected system.",
        )
    desired = dict(displayed)
    for axis, value in request.axis_values:
        desired[axis] = value if request.mode == "G90" else displayed[axis] + value

    try:
        machine_targets, affected_display = _physical_machine_targets(
            current,
            requested_axes=frozenset(requested),
            desired=desired,
        )
        raw_targets, raw_distances = _configured_targets(
            machine_snapshot,
            machine_targets,
        )
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        return _rejected(current, str(exc) or "Coordinate movement is unavailable.")

    return CoordinateMotionProjection(
        lease=current,
        accepted=True,
        raw_targets=tuple(raw_targets.items()),
        raw_distances=tuple(
            (axis, distance)
            for axis, distance in raw_distances.items()
            if not math.isclose(distance, 0.0, rel_tol=0.0, abs_tol=1e-12)
        ),
        display_targets=tuple(
            (axis, desired[axis]) for axis in affected_display
        ),
        machine_targets=tuple(machine_targets.items()),
    )


def _physical_machine_targets(
    lease: CoordinateMotionLease,
    *,
    requested_axes: frozenset[str],
    desired: dict[str, float],
) -> tuple[dict[str, float], tuple[str, ...]]:
    authority = lease.authority
    physical = authority.physical_pose
    record = lease.record
    if lease.selected_frame_id == MACHINE_FRAME_ID:
        ordered_axes = tuple(axis for axis in STAGE_AXES if axis in requested_axes)
        return (
            {axis: float(desired[axis]) for axis in ordered_axes},
            tuple(
                axis
                for axis, _value in lease.display_values
                if axis in requested_axes
            ),
        )
    if record is None or record.transform is None:
        raise ValueError("Selected coordinate transform is unavailable.")
    if "B" in requested_axes:
        raise ValueError(
            "B movement in this Coordinate System requires a planned pivot path."
        )

    transform = record.transform
    targets: dict[str, float] = {}
    affected: list[str] = []
    rotates_xy = bool(requested_axes.intersection({"X", "Y"}))
    if rotates_xy:
        for axis in ("X", "Y", "B"):
            if axis not in desired:
                raise ValueError(f"{axis} coordinate is unavailable in the selected system.")
        pivot = authority.pivot_machine_xy
        if pivot is None or not all(math.isfinite(float(value)) for value in pivot):
            raise ValueError("Rotation pivot is unavailable.")
        target_b = float(desired["B"]) + transform.b_zero_machine_deg
        target_xy = transform.frame_xy_to_machine(
            (float(desired["X"]), float(desired["Y"])),
            machine_b_deg=target_b,
            pivot_machine_xy=(float(pivot[0]), float(pivot[1])),
        )
        targets.update(X=target_xy[0], Y=target_xy[1])
        affected.extend(("X", "Y"))

    if "Z" in requested_axes:
        if transform.z_zero_machine_mm is None:
            raise ValueError("Z reference is unavailable in the selected system.")
        targets["Z"] = float(desired["Z"]) + transform.z_zero_machine_mm
        affected.append("Z")
    if "A" in requested_axes:
        if transform.a_zero_machine_mm is None:
            raise ValueError("A reference is unavailable in the selected system.")
        targets["A"] = float(desired["A"]) + transform.a_zero_machine_mm
        affected.append("A")
    unsupported = requested_axes - {"X", "Y", "Z", "A", "B"}
    if unsupported:
        raise ValueError(
            f"{sorted(unsupported)[0]} is unavailable in the selected system."
        )

    for axis in targets:
        observed = physical.values.get(axis)
        snapshot_value = authority.machine_snapshot.physical_machine_pose.values.get(axis)
        if observed is None or snapshot_value is None or not math.isclose(
            float(observed),
            float(snapshot_value),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("Machine coordinate authority is inconsistent.")
    return targets, tuple(affected)


def _configured_targets(
    snapshot: object,
    machine_targets: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    raw_targets: dict[str, float] = {}
    raw_distances: dict[str, float] = {}
    for axis, target in machine_targets.items():
        index = snapshot.axis_index.get(axis)
        if index is None or int(index) < 0 or int(index) >= len(snapshot.configured_position):
            raise ValueError(f"Machine {axis} coordinate is unavailable.")
        raw_target = float(
            snapshot.physical_machine_to_configured_controller(axis, float(target))
        )
        current_raw = float(snapshot.configured_position[int(index)])
        raw_targets[axis] = raw_target
        raw_distances[axis] = raw_target - current_raw
    return raw_targets, raw_distances


def _rejected(
    lease: CoordinateMotionLease,
    reason: str,
) -> CoordinateMotionProjection:
    return CoordinateMotionProjection(
        lease=lease,
        accepted=False,
        reason=str(reason or "Coordinate movement is unavailable."),
    )


def _basis_fingerprint(
    registry: RegistrySnapshot,
    *,
    selected: str,
    selection_available: bool,
    selection_reason: str | None,
    record: CoordinateFrameRecord | None,
    authority: CoordinateAuthorityObservation,
    display_values: tuple[tuple[str, float], ...],
) -> tuple[object, ...]:
    snapshot = authority.machine_snapshot
    machine_basis = None
    if snapshot is not None:
        machine_basis = (
            snapshot.mapper,
            tuple(sorted(snapshot.axis_index.items())),
            snapshot.position_reporting_mode,
            snapshot.coordinate_system,
            snapshot.work_offset,
        )
    return (
        registry.generation,
        selected,
        bool(selection_available),
        selection_reason or None,
        record,
        authority.homed_axes,
        authority.pivot_machine_xy,
        authority.objective_xy_offset,
        authority.pivot_error,
        authority.pivot_error_permanent,
        machine_basis,
        tuple(axis for axis, _value in display_values),
    )


__all__ = ["build_coordinate_motion_lease", "project_coordinate_motion"]
