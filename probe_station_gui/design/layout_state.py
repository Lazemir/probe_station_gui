"""Immutable Design Window layout and selection state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.selection_model import (
    MixedArrayPlan,
    MixedArrayRequest,
    MixedDeletePlan,
    SelectableDesignEntity,
    SelectionModel,
    plan_mixed_array,
    plan_mixed_delete,
    project_entities,
    route_entity_id,
)
from probe_station_gui.route.model import MeasurementRoute, RouteDesignBinding, RoutePoint


@dataclass(frozen=True, slots=True)
class _RouteSnapshot:
    """Isolated route input used by immutable layout-state transitions."""

    design: RouteDesignBinding
    points: tuple[RoutePoint, ...]

    @classmethod
    def from_route(cls, route: MeasurementRoute) -> _RouteSnapshot:
        return cls(
            design=route.design,
            points=tuple(deepcopy(route.points)),
        )

    def materialize(self) -> MeasurementRoute:
        return MeasurementRoute(
            name="Design layout snapshot",
            design=self.design,
            points=list(deepcopy(self.points)),
        )


@dataclass(frozen=True, slots=True)
class DesignLayoutState:
    """Own the Design Window models and their canonical shared selection."""

    document: DesignDocument | None = None
    _route_snapshot: _RouteSnapshot | None = None
    markup: MarkupDocument | None = None
    selection: SelectionModel = SelectionModel()
    selection_initialized: bool = False
    selectable_entities: tuple[SelectableDesignEntity, ...] = ()

    def with_document(self, document: DesignDocument | None) -> DesignLayoutState:
        return replace(self, document=document)

    def with_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> DesignLayoutState:
        route_snapshot = _RouteSnapshot.from_route(route) if route is not None else None
        state = self._with_projection(route=route_snapshot, markup=self.markup)
        if (
            not state.selection_initialized
            and route_snapshot is not None
            and 0 <= selected_route_point_index < len(route_snapshot.points)
        ):
            return state.with_selection(
                SelectionModel(
                    frozenset(
                        {
                            route_entity_id(
                                route_snapshot.points[selected_route_point_index].id
                            )
                        }
                    )
                )
            )
        return state

    def with_markup(self, markup: MarkupDocument | None) -> DesignLayoutState:
        return self._with_projection(route=self._route_snapshot, markup=markup)

    def with_selection(self, selection: SelectionModel) -> DesignLayoutState:
        return replace(
            self,
            selection=selection.prune(
                entity.id for entity in self.selectable_entities
            ),
            selection_initialized=True,
        )

    def apply_selection(self, entity_ids: object, mode: str) -> DesignLayoutState:
        ids = (
            {str(entity_id) for entity_id in entity_ids}
            if isinstance(entity_ids, (set, frozenset, list, tuple))
            else set()
        )
        return self.with_selection(self.selection.apply(ids, str(mode)))

    def plan_mixed_delete(self, *, edit_safe: bool) -> MixedDeletePlan:
        return plan_mixed_delete(
            self.selectable_entities,
            self.selection.ids,
            edit_safe=edit_safe,
        )

    def plan_mixed_array(
        self,
        request: MixedArrayRequest,
        *,
        edit_safe: bool,
    ) -> MixedArrayPlan:
        return plan_mixed_array(
            self.selectable_entities,
            self.selection.ids,
            request,
            route=(
                self._route_snapshot.materialize()
                if self._route_snapshot is not None
                else None
            ),
            edit_safe=edit_safe,
        )

    def _with_projection(
        self,
        *,
        route: _RouteSnapshot | None,
        markup: MarkupDocument | None,
    ) -> DesignLayoutState:
        entities = project_entities(
            route.materialize() if route is not None else None,
            markup,
        )
        return replace(
            self,
            _route_snapshot=route,
            markup=markup,
            selectable_entities=entities,
            selection=self.selection.prune(entity.id for entity in entities),
        )


__all__ = ["DesignLayoutState"]
