from __future__ import annotations

from pathlib import Path

import pytest

from probe_station_gui.design.layout_state import DesignLayoutState
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.selection_model import (
    MixedArrayRequest,
    SelectionModel,
    markup_entity_id,
    route_entity_id,
)
from probe_station_gui.route.model import (
    MeasurementRoute,
    RouteDesignBinding,
    RoutePoint,
)


def _route(*point_ids: str) -> MeasurementRoute:
    return MeasurementRoute(
        name="Route",
        design=RouteDesignBinding(
            path="C:/chip.gds",
            sha256="hash",
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 100.0),
            dbu=0.001,
        ),
        points=[
            RoutePoint(point_id, point_id.upper(), (float(index), float(index)))
            for index, point_id in enumerate(point_ids)
        ],
    )


def _markup(source: Path, *, visible: bool = True) -> MarkupDocument:
    source.write_bytes(b"gds")
    return MarkupDocument.empty(source, visible=visible).append_guide(
        (0.0, 0.0),
        (2.0, 0.0),
        guide_id="g1",
    )


def _document(source: Path) -> DesignDocument:
    return DesignDocument(
        path=source,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 10.0, 10.0),
        polygons_by_layer={},
        visible_layers=frozenset(),
    )


def test_first_route_initializes_selection_from_selected_row() -> None:
    state = DesignLayoutState().with_route(
        _route("p1", "p2"),
        selected_route_point_index=1,
    )

    assert state.selection == SelectionModel(frozenset({route_entity_id("p2")}))
    assert state.selection_initialized


def test_route_refresh_preserves_initialized_selection_instead_of_reselecting() -> None:
    state = DesignLayoutState().with_route(
        _route("p1", "p2"),
        selected_route_point_index=1,
    )

    refreshed = state.with_route(
        _route("p1", "p2", "p3"),
        selected_route_point_index=0,
    )

    assert refreshed.selection.ids == frozenset({route_entity_id("p2")})


def test_removed_entities_are_pruned_without_reinitializing_selection() -> None:
    state = DesignLayoutState().with_route(
        _route("p1", "p2"),
        selected_route_point_index=1,
    )

    refreshed = state.with_route(
        _route("p1"),
        selected_route_point_index=0,
    )

    assert refreshed.selection.ids == frozenset()
    assert refreshed.selection_initialized


def test_hidden_markup_is_not_selectable_and_prunes_its_selection(
    tmp_path: Path,
) -> None:
    markup = _markup(tmp_path / "chip.gds")
    state = (
        DesignLayoutState()
        .with_markup(markup)
        .with_selection(SelectionModel(frozenset({markup_entity_id("g1")})))
    )

    hidden = state.with_markup(markup.with_visibility(False))

    assert hidden.selectable_entities == ()
    assert hidden.selection.ids == frozenset()


def test_selection_requests_use_canonical_replace_add_and_invert_modes(
    tmp_path: Path,
) -> None:
    state = (
        DesignLayoutState()
        .with_route(_route("p1", "p2"), selected_route_point_index=-1)
        .with_markup(_markup(tmp_path / "chip.gds"))
    )

    state = state.apply_selection({route_entity_id("p1")}, "replace")
    state = state.apply_selection({route_entity_id("p2")}, "add")
    state = state.apply_selection(
        {route_entity_id("p1"), markup_entity_id("g1")},
        "invert",
    )

    assert state.selection.ids == frozenset(
        {route_entity_id("p2"), markup_entity_id("g1")}
    )


def test_selection_requests_prune_unknown_ids_and_reject_unknown_modes() -> None:
    state = DesignLayoutState().with_route(
        _route("p1"),
        selected_route_point_index=-1,
    )

    replaced = state.apply_selection(
        {route_entity_id("p1"), "route:missing"},
        "replace",
    )
    unchanged = replaced.apply_selection(object(), "add")

    assert replaced.selection.ids == frozenset({route_entity_id("p1")})
    assert unchanged.selection == replaced.selection
    with pytest.raises(ValueError, match="Unknown selection update mode"):
        state.apply_selection({route_entity_id("p1")}, "append")


def test_document_survives_layout_updates_for_window_reopen(tmp_path: Path) -> None:
    document = _document(tmp_path / "chip.gds")
    state = DesignLayoutState().with_document(document)

    updated = state.with_route(
        _route("p1"),
        selected_route_point_index=0,
    )

    assert updated.document is document


def test_mixed_edit_plans_use_the_owned_projection_and_selection(
    tmp_path: Path,
) -> None:
    state = (
        DesignLayoutState()
        .with_route(_route("p1"), selected_route_point_index=0)
        .with_markup(_markup(tmp_path / "chip.gds"))
        .apply_selection({route_entity_id("p1"), markup_entity_id("g1")}, "replace")
    )

    delete_plan = state.plan_mixed_delete(edit_safe=True)
    array_plan = state.plan_mixed_array(
        MixedArrayRequest(
            direction_1=(5.0, 0.0),
            count_1=2,
            direction_2=(0.0, 0.0),
            count_2=1,
            source_ids=state.selection.ids,
        ),
        edit_safe=True,
    )

    assert delete_plan.required_route_ids == frozenset({"p1"})
    assert delete_plan.required_guide_ids == frozenset({"g1"})
    assert len(array_plan.route_copies) == 1
    assert len(array_plan.guide_copies) == 1


def test_mixed_array_uses_canonical_selection_not_request_source_ids() -> None:
    state = DesignLayoutState().with_route(
        _route("p1", "p2"),
        selected_route_point_index=0,
    )

    plan = state.plan_mixed_array(
        MixedArrayRequest(
            direction_1=(5.0, 0.0),
            count_1=2,
            direction_2=(0.0, 0.0),
            count_2=1,
            source_ids=frozenset({route_entity_id("p2")}),
        ),
        edit_safe=True,
    )

    assert plan.accepted
    assert [point.camera_center for point in plan.route_copies] == [(5.0, 0.0)]


def test_route_input_is_snapshotted_against_external_mutation() -> None:
    route = _route("p1")
    state = DesignLayoutState().with_route(
        route,
        selected_route_point_index=0,
    )

    route.clear_points()
    refreshed = state.with_markup(None)
    plan = refreshed.plan_mixed_array(
        MixedArrayRequest(
            direction_1=(5.0, 0.0),
            count_1=2,
            direction_2=(0.0, 0.0),
            count_2=1,
            source_ids=frozenset(),
        ),
        edit_safe=True,
    )

    assert refreshed.selection.ids == frozenset({route_entity_id("p1")})
    assert plan.accepted
    assert [point.camera_center for point in plan.route_copies] == [(5.0, 0.0)]
