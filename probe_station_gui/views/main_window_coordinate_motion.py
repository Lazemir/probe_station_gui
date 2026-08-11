"""Immutable Coordinate System motion projection for Main-window adapters."""

from __future__ import annotations

from typing import Any

from probe_station_gui.coordinates.coordinator_model import (
    CoordinateMotionLease,
    CoordinateMotionProjection,
    CoordinateMotionRequest,
)


def project_gui_coordinate_motion(
    owner: Any,
    axis_values: tuple[tuple[str, float], ...],
    *,
    mode: str,
    lease: CoordinateMotionLease | None = None,
    allow_pose_rebase: bool = False,
) -> CoordinateMotionProjection | None:
    """Project against the coordinator snapshot rendered by the GUI."""

    coordinator = getattr(owner, "_coordinate_system_coordinator", None)
    if coordinator is None:
        return None
    active_lease = lease or coordinator.snapshot().motion_lease
    if active_lease is None:
        return None
    return coordinator.project_motion(
        CoordinateMotionRequest(
            lease=active_lease,
            mode=mode,
            axis_values=axis_values,
            allow_pose_rebase=allow_pose_rebase,
        )
    )


def project_gui_relative_motion(
    owner: Any,
    requested_distances: tuple[tuple[str, float], ...],
    lease: object | None,
) -> CoordinateMotionProjection:
    """Project one continuous jog, rebasing pose on the same frozen basis."""

    typed_lease = lease if isinstance(lease, CoordinateMotionLease) else None
    projection = project_gui_coordinate_motion(
        owner,
        requested_distances,
        mode="G91",
        lease=typed_lease,
        allow_pose_rebase=typed_lease is not None,
    )
    if projection is None:
        raise ValueError("Current Machine coordinates are unavailable.")
    return projection


def gui_motion_lease(owner: Any) -> CoordinateMotionLease | object | None:
    """Return the exact Machine, Design, or Custom lease shown by the GUI."""

    coordinator = getattr(owner, "_coordinate_system_coordinator", None)
    if coordinator is None:
        return None
    return coordinator.snapshot().motion_lease


def motion_basis(lease: CoordinateMotionLease | object | None) -> object | None:
    """Return the stable selected-system identity across safe pose rebases."""

    if lease is None:
        return None
    fingerprint = getattr(lease, "basis_fingerprint", None)
    if isinstance(fingerprint, tuple):
        return fingerprint
    return lease


def projection_limit_error(
    owner: Any,
    projection: CoordinateMotionProjection,
) -> str | None:
    """Check projected endpoints in physical Machine coordinates."""

    for axis, machine_target in projection.machine_targets:
        reason = owner._machine_axis_target_limit_error(axis, machine_target)
        if reason is not None:
            return reason
    return None


def projection_targets(
    projection: CoordinateMotionProjection,
) -> dict[str, tuple[float, float]]:
    """Pair configured controller targets with selected-system display values."""

    raw_targets = dict(projection.raw_targets)
    return {
        axis: (float(raw_targets[axis]), float(display_target))
        for axis, display_target in projection.display_targets
        if axis in raw_targets
    }


__all__ = [
    "gui_motion_lease",
    "motion_basis",
    "project_gui_coordinate_motion",
    "project_gui_relative_motion",
    "projection_limit_error",
    "projection_targets",
]
