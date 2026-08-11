"""Detached immutable inputs for Design tool previews."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import TypeAlias

from probe_station_gui.design.model import Point2D
from probe_station_gui.design.selection_model import (
    EntityOwner,
    MixedArrayPlan,
    MixedArrayRequest,
    SelectableDesignEntity,
    plan_mixed_array,
)
from probe_station_gui.route.model import (
    MeasurementRoute,
    NeedleOffset,
    RouteDesignBinding,
    RoutePoint,
)


DocumentToken: TypeAlias = str | int | tuple[object, ...] | None


@dataclass(frozen=True, slots=True)
class _RoutePointSnapshot:
    """Hashable route-point value with recursively detached JSON metadata."""

    id: str
    label: str
    camera_center: Point2D
    enabled: bool
    metadata_json: str

    @classmethod
    def capture(cls, point: RoutePoint) -> _RoutePointSnapshot:
        return cls(
            id=str(point.id),
            label=str(point.label),
            camera_center=_point(point.camera_center),
            enabled=bool(point.enabled),
            metadata_json=json.dumps(
                point.metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def materialize(self) -> RoutePoint:
        metadata = json.loads(self.metadata_json)
        return RoutePoint(
            id=self.id,
            label=self.label,
            camera_center=self.camera_center,
            enabled=self.enabled,
            metadata=metadata if isinstance(metadata, dict) else {},
        )


@dataclass(frozen=True, slots=True)
class _RouteSnapshot:
    """Private indexed route input captured only when the route changes."""

    name: str
    design: RouteDesignBinding
    needle_offsets: tuple[NeedleOffset, ...]
    point_ids: tuple[str, ...]
    points: tuple[_RoutePointSnapshot, ...]
    created_at_utc: str
    updated_at_utc: str
    path: Path | None

    @classmethod
    def capture(cls, route: MeasurementRoute) -> _RouteSnapshot:
        points = tuple(
            sorted(
                (_RoutePointSnapshot.capture(point) for point in route.points),
                key=lambda point: point.id,
            )
        )
        return cls(
            name=str(route.name),
            design=route.design,
            needle_offsets=tuple(route.needle_offsets),
            point_ids=tuple(point.id for point in points),
            points=points,
            created_at_utc=str(route.created_at_utc),
            updated_at_utc=str(route.updated_at_utc),
            path=route.path,
        )

    def materialize_selected(
        self,
        source_ids: frozenset[str],
    ) -> MeasurementRoute:
        selected: list[RoutePoint] = []
        for source_id in sorted(source_ids):
            index = bisect_left(self.point_ids, source_id)
            if index < len(self.points) and self.points[index].id == source_id:
                selected.append(self.points[index].materialize())
        return MeasurementRoute(
            name=self.name,
            design=self.design,
            needle_offsets=list(self.needle_offsets),
            points=selected,
            created_at_utc=self.created_at_utc,
            updated_at_utc=self.updated_at_utc,
            path=self.path,
        )


@dataclass(frozen=True, slots=True)
class DesignToolContext:
    """Detached canonical inputs needed to validate previews and intents."""

    document_token: DocumentToken = None
    selection_ids: frozenset[str] = frozenset()
    selectable_entities: tuple[SelectableDesignEntity, ...] = ()
    _route: _RouteSnapshot | None = None
    edit_safe: bool = True

    @classmethod
    def capture(
        cls,
        *,
        document_token: DocumentToken,
        selection_ids: object = (),
        selectable_entities: object = (),
        route: MeasurementRoute | None = None,
        edit_safe: bool = True,
    ) -> DesignToolContext:
        ids = _selection_ids(selection_ids)
        return cls(
            document_token=document_token,
            selection_ids=ids,
            selectable_entities=_selected_entities(selectable_entities, ids),
            _route=_RouteSnapshot.capture(route) if route is not None else None,
            edit_safe=bool(edit_safe),
        )

    @property
    def has_document(self) -> bool:
        return self.document_token is not None

    def replace_inputs(
        self,
        *,
        document_token: DocumentToken,
        selection_ids: object,
        selectable_entities: object,
        edit_safe: bool,
    ) -> DesignToolContext:
        ids = _selection_ids(selection_ids)
        return replace(
            self,
            document_token=document_token,
            selection_ids=ids,
            selectable_entities=_selected_entities(selectable_entities, ids),
            edit_safe=bool(edit_safe),
        )

    def replace_route(
        self,
        route: MeasurementRoute | None,
    ) -> DesignToolContext:
        return replace(
            self,
            _route=_RouteSnapshot.capture(route) if route is not None else None,
        )

    def plan_array(self, request: MixedArrayRequest) -> MixedArrayPlan:
        route_source_ids = frozenset(
            entity.source_id
            for entity in self.selectable_entities
            if entity.id in request.source_ids and entity.owner is EntityOwner.ROUTE
        )
        return plan_mixed_array(
            self.selectable_entities,
            self.selection_ids,
            request,
            route=(
                self._route.materialize_selected(route_source_ids)
                if self._route is not None
                else None
            ),
            edit_safe=self.edit_safe,
        )


def _point(value: Point2D) -> Point2D:
    return (float(value[0]), float(value[1]))


def _selection_ids(value: object) -> frozenset[str]:
    if not isinstance(value, (set, frozenset, list, tuple)):
        return frozenset()
    return frozenset(str(item) for item in value)


def _selected_entities(
    value: object,
    selection_ids: frozenset[str],
) -> tuple[SelectableDesignEntity, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(entity, SelectableDesignEntity) for entity in value
    ):
        return ()
    return tuple(entity for entity in value if entity.id in selection_ids)


__all__ = ["DesignToolContext", "DocumentToken"]
