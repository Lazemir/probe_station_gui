"""Immutable snapshots and atomic restoration for a design session."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable, Protocol

from probe_station_gui.design.model import (
    DesignDocument,
    MeasurementTarget,
    Point2D,
)
from probe_station_gui.design.rigid_registration import DesignRegistration
from probe_station_gui.route.model import MeasurementRoute


@dataclass(frozen=True)
class DesignSessionState:
    """Detached snapshot of every mutable field owned by a design session."""

    document: DesignDocument | None = None
    registration: DesignRegistration | None = None
    source_design_marks: tuple[Point2D, ...] = ()
    source_stage_marks: tuple[Point2D, ...] = ()
    check_design_marks: tuple[Point2D, ...] = ()
    check_stage_marks: tuple[Point2D, ...] = ()
    targets: tuple[MeasurementTarget, ...] = ()
    selected_target_index: int = -1
    route: MeasurementRoute | None = None
    selected_route_point_index: int = -1
    registration_status: str = "No design registration."
    active_frame_id: str | None = None
    _runtime_blocked_persisted_state: dict[str, object] | None = None
    _legacy_stage_coordinate_provenance: object = None
    _legacy_stage_coordinate_provenance_present: bool = False


class _DesignSessionOwner(Protocol):
    document: DesignDocument | None
    registration: DesignRegistration | None
    source_design_marks: tuple[Point2D, ...]
    source_stage_marks: tuple[Point2D, ...]
    check_design_marks: list[Point2D]
    check_stage_marks: list[Point2D]
    targets: list[MeasurementTarget]
    selected_target_index: int
    route: MeasurementRoute | None
    selected_route_point_index: int
    registration_status: str
    active_frame_id: str | None
    _runtime_blocked_persisted_state: dict[str, object] | None
    _legacy_stage_coordinate_provenance: object
    _legacy_stage_coordinate_provenance_present: bool
    __dict__: dict[str, object]


def snapshot_session_state(session: _DesignSessionOwner) -> DesignSessionState:
    """Copy every owned value into a detached session snapshot."""

    return DesignSessionState(
        document=session.document,
        registration=session.registration,
        source_design_marks=_copy_points(session.source_design_marks),
        source_stage_marks=_copy_points(session.source_stage_marks),
        check_design_marks=_copy_points(session.check_design_marks),
        check_stage_marks=_copy_points(session.check_stage_marks),
        targets=tuple(deepcopy(session.targets)),
        selected_target_index=session.selected_target_index,
        route=deepcopy(session.route),
        selected_route_point_index=session.selected_route_point_index,
        registration_status=session.registration_status,
        active_frame_id=session.active_frame_id,
        _runtime_blocked_persisted_state=deepcopy(
            session._runtime_blocked_persisted_state
        ),
        _legacy_stage_coordinate_provenance=deepcopy(
            session._legacy_stage_coordinate_provenance
        ),
        _legacy_stage_coordinate_provenance_present=(
            session._legacy_stage_coordinate_provenance_present
        ),
    )


def restore_session_state(
    session: _DesignSessionOwner,
    state: DesignSessionState,
) -> None:
    """Atomically restore a detached snapshot into an existing session."""

    if not isinstance(state, DesignSessionState):
        raise TypeError("state must be a DesignSessionState")
    restored = {
        "document": state.document,
        "registration": state.registration,
        "source_design_marks": _copy_points(state.source_design_marks),
        "source_stage_marks": _copy_points(state.source_stage_marks),
        "check_design_marks": list(_copy_points(state.check_design_marks)),
        "check_stage_marks": list(_copy_points(state.check_stage_marks)),
        "targets": list(deepcopy(state.targets)),
        "selected_target_index": state.selected_target_index,
        "route": deepcopy(state.route),
        "selected_route_point_index": state.selected_route_point_index,
        "registration_status": state.registration_status,
        "active_frame_id": state.active_frame_id,
        "_runtime_blocked_persisted_state": deepcopy(
            state._runtime_blocked_persisted_state
        ),
        "_legacy_stage_coordinate_provenance": deepcopy(
            state._legacy_stage_coordinate_provenance
        ),
        "_legacy_stage_coordinate_provenance_present": (
            state._legacy_stage_coordinate_provenance_present
        ),
    }
    session.__dict__ = restored


def export_persisted_session_state(
    state: DesignSessionState,
    *,
    document_size: int | None = None,
    document_mtime_ns: int | None = None,
    route_path: str | None = None,
) -> dict[str, object] | None:
    """Serialize one detached state using only pre-observed file metadata."""

    if not isinstance(state, DesignSessionState):
        raise TypeError("state must be a DesignSessionState")
    document = state.document
    if document is None:
        return None
    if (
        state.active_frame_id is None
        and state._runtime_blocked_persisted_state is not None
    ):
        return deepcopy(state._runtime_blocked_persisted_state)
    result: dict[str, object] = {
        "version": 3 if state.active_frame_id is not None else 2,
        "document_path": str(document.path),
        "top_cell_name": document.top_cell_name,
        "rotation_quarter_turns": int(document.rotation_quarter_turns),
        "visible_layers": [
            [int(layer), int(datatype)]
            for layer, datatype in sorted(document.visible_layers)
        ],
    }
    if state.active_frame_id is not None:
        result["active_frame_id"] = state.active_frame_id
    else:
        registration = state.registration
        result.update(
            {
                "source_design_marks": _serialize_points(state.source_design_marks),
                "source_stage_marks": _serialize_points(state.source_stage_marks),
                "check_design_marks": _serialize_points(state.check_design_marks),
                "check_stage_marks": _serialize_points(state.check_stage_marks),
                "registration_valid": bool(
                    registration is not None and registration.valid
                ),
                "registration_status": state.registration_status,
                "registration_stale_reason": (
                    registration.stale_reason if registration is not None else ""
                ),
            }
        )
    if document_mtime_ns is not None:
        result["document_mtime_ns"] = int(document_mtime_ns)
    if document_size is not None:
        result["document_size"] = int(document_size)
    if state.active_frame_id is None and (
        state._legacy_stage_coordinate_provenance_present
        or state._legacy_stage_coordinate_provenance is not None
    ):
        result["stage_coordinate_provenance"] = deepcopy(
            state._legacy_stage_coordinate_provenance
        )
    if state.route is not None and route_path:
        result["route"] = {
            "path": str(route_path),
            "selected_route_point_index": int(state.selected_route_point_index),
        }
    return result


def _copy_points(points: Iterable[Point2D]) -> tuple[Point2D, ...]:
    return tuple((float(point[0]), float(point[1])) for point in points)


def _serialize_points(points: Iterable[Point2D]) -> list[list[float]]:
    return [[float(point[0]), float(point[1])] for point in points if point is not None]


__all__ = ["DesignSessionState", "export_persisted_session_state"]
