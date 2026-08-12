"""Immutable snapshots and atomic restoration for a design session."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Iterable, Protocol

from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    MeasurementTarget,
    Point2D,
)
from probe_station_gui.design.rigid_registration import DesignRegistration
from probe_station_gui.design import session_registration
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


@dataclass(frozen=True)
class PreparedDesignSessionRestore:
    """Detached persisted state prepared entirely before GUI publication."""

    state: DesignSessionState


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


def persisted_design_source_is_current(state: dict[str, object]) -> bool:
    """Check saved source metadata on the caller-owned loading thread."""

    path_text = str(state.get("document_path") or "").strip()
    if not path_text:
        return False
    try:
        source_stat = Path(path_text).expanduser().stat()
    except OSError:
        return False
    saved_size = state.get("document_size")
    if saved_size is not None:
        try:
            if int(saved_size) != int(source_stat.st_size):
                return False
        except (TypeError, ValueError):
            return False
    saved_mtime = state.get("document_mtime_ns")
    if saved_mtime is not None:
        try:
            if int(saved_mtime) != int(source_stat.st_mtime_ns):
                return False
        except (TypeError, ValueError):
            return False
    return True


def parse_persisted_visible_layers(value: object) -> set[tuple[int, int]]:
    """Coerce persisted layer pairs while ignoring malformed entries."""

    layers: set[tuple[int, int]] = set()
    if not isinstance(value, list):
        return layers
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            layers.add((int(item[0]), int(item[1])))
        except (TypeError, ValueError):
            continue
    return layers


def document_with_persisted_session_view(
    document: DesignDocument,
    persisted: dict[str, object],
) -> DesignDocument:
    """Apply persisted cell, rotation, and layer selection to a loaded document."""

    top_cell_name = str(persisted.get("top_cell_name") or "").strip()
    if top_cell_name and top_cell_name != document.top_cell_name:
        document = document.with_top_cell(top_cell_name)
    try:
        rotation_quarter_turns = int(persisted.get("rotation_quarter_turns", 0))
    except (TypeError, ValueError):
        rotation_quarter_turns = 0
    if rotation_quarter_turns:
        document = document.with_rotation_delta(rotation_quarter_turns)
    visible_layers = parse_persisted_visible_layers(persisted.get("visible_layers"))
    if visible_layers:
        document = document.with_visible_layers(visible_layers)
    return document


def prepare_persisted_session_restore(
    document: DesignDocument,
    persisted: dict[str, object],
) -> PreparedDesignSessionRestore:
    """Read and validate persisted route data into one detached publication."""

    try:
        version = int(persisted.get("version", 1))
    except (TypeError, ValueError):
        version = 1
    route, route_index = _prepare_route(document, persisted.get("route"))
    provenance_present = version < 3 and "stage_coordinate_provenance" in persisted
    provenance = (
        deepcopy(persisted.get("stage_coordinate_provenance"))
        if provenance_present
        else None
    )
    if version >= 3:
        frame_id = str(persisted.get("active_frame_id") or "").strip()
        return PreparedDesignSessionRestore(
            DesignSessionState(
                document=document,
                route=route,
                selected_route_point_index=route_index,
                registration_status="Design frame is loading.",
                active_frame_id=frame_id or None,
            )
        )
    if version <= 1:
        design_marks = tuple(
            point
            for point in _coerce_optional_points(
                persisted.get("source_design_marks"), expected_count=2
            )
            if point is not None
        )
        stage_marks = tuple(
            point
            for point in _coerce_optional_points(
                persisted.get("source_stage_marks"), expected_count=2
            )
            if point is not None
        )
    else:
        design_marks = _coerce_points(persisted.get("source_design_marks"))
        stage_marks = _coerce_points(persisted.get("source_stage_marks"))
    check_design_marks = _coerce_points(persisted.get("check_design_marks"))
    check_stage_marks = _coerce_points(persisted.get("check_stage_marks"))
    try:
        registration, status = session_registration.build_registration(
            document,
            design_marks,
            stage_marks,
            check_design_marks,
            check_stage_marks,
        )
    except DesignModelError as exc:
        registration, status = None, str(exc)
    if registration is not None and not bool(persisted.get("registration_valid", True)):
        reason = str(
            persisted.get("registration_stale_reason")
            or persisted.get("registration_status")
            or "Design registration is stale."
        )
        registration = registration.mark_stale(reason)
        status = reason
    return PreparedDesignSessionRestore(
        DesignSessionState(
            document=document,
            registration=registration,
            source_design_marks=design_marks,
            source_stage_marks=stage_marks,
            check_design_marks=check_design_marks,
            check_stage_marks=check_stage_marks,
            route=route,
            selected_route_point_index=route_index,
            registration_status=status,
            _legacy_stage_coordinate_provenance=provenance,
            _legacy_stage_coordinate_provenance_present=provenance_present,
        )
    )


def apply_prepared_session_restore(
    session: _DesignSessionOwner,
    prepared: PreparedDesignSessionRestore,
) -> None:
    """Atomically publish a background-prepared restore without filesystem I/O."""

    if not isinstance(prepared, PreparedDesignSessionRestore):
        raise TypeError("prepared must be a PreparedDesignSessionRestore")
    restore_session_state(session, prepared.state)


def _prepare_route(
    document: DesignDocument,
    value: object,
) -> tuple[MeasurementRoute | None, int]:
    if not isinstance(value, dict):
        return None, -1
    path_text = str(value.get("path") or "").strip()
    if not path_text:
        return None, -1
    route_path = Path(path_text).expanduser()
    if not route_path.exists():
        return None, -1
    try:
        route = MeasurementRoute.load(route_path)
        route.validate_for_document(document)
    except DesignModelError:
        return None, -1
    try:
        selected_index = int(value.get("selected_route_point_index", 0))
    except (TypeError, ValueError):
        selected_index = 0
    if not route.points:
        return route, -1
    return route, min(max(0, selected_index), len(route.points) - 1)


def _coerce_optional_points(
    value: object,
    *,
    expected_count: int,
) -> tuple[Point2D | None, ...]:
    points: list[Point2D | None] = []
    if isinstance(value, list):
        for item in value[:expected_count]:
            points.append(_coerce_optional_point(item))
    while len(points) < expected_count:
        points.append(None)
    return tuple(points)


def _coerce_points(value: object) -> tuple[Point2D, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        point for item in value if (point := _coerce_optional_point(item)) is not None
    )


def _coerce_optional_point(value: object) -> Point2D | None:
    if value is None or not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        x_value = float(value[0])
        y_value = float(value[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x_value) or not math.isfinite(y_value):
        return None
    return (x_value, y_value)


def _copy_points(points: Iterable[Point2D]) -> tuple[Point2D, ...]:
    return tuple((float(point[0]), float(point[1])) for point in points)


def _serialize_points(points: Iterable[Point2D]) -> list[list[float]]:
    return [[float(point[0]), float(point[1])] for point in points if point is not None]


__all__ = [
    "DesignSessionState",
    "PreparedDesignSessionRestore",
    "apply_prepared_session_restore",
    "document_with_persisted_session_view",
    "export_persisted_session_state",
    "parse_persisted_visible_layers",
    "persisted_design_source_is_current",
    "prepare_persisted_session_restore",
]
