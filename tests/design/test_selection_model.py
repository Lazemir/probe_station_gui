from __future__ import annotations

from pathlib import Path

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.navigation_adapter import apply_route_entity_changes
from probe_station_gui.design.selection_geometry import SelectionRect
from probe_station_gui.design.selection_model import (
    EntityOwner,
    MixedArrayRequest,
    SelectionModel,
    apply_markup_entity_changes,
    entities_in_rect,
    markup_entity_id,
    plan_mixed_array,
    plan_mixed_delete,
    project_entities,
    route_entity_id,
)
from probe_station_gui.design.session import DesignSession
from probe_station_gui.route.model import (
    MeasurementRoute,
    RouteDesignBinding,
    RoutePoint,
)


def _route(*points: RoutePoint) -> MeasurementRoute:
    return MeasurementRoute(
        name="Route",
        design=RouteDesignBinding(
            path="C:/chip.gds",
            sha256="hash",
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 100.0),
            dbu=0.001,
        ),
        points=list(points),
    )


def _point(
    point_id: str,
    center: tuple[float, float],
    *,
    metadata: dict[str, object] | None = None,
) -> RoutePoint:
    return RoutePoint(
        id=point_id,
        label=point_id.upper(),
        camera_center=center,
        metadata=dict(metadata or {}),
    )


def _markup(tmp_path: Path, *, visible: bool = True) -> MarkupDocument:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"gds")
    return (
        MarkupDocument.empty(source, visible=visible)
        .append_guide((0.0, 0.0), (2.0, 0.0), guide_id="g1")
        .append_guide((20.0, 20.0), (30.0, 20.0), guide_id="g2")
    )


def test_selection_replace_add_invert_clear_and_prune() -> None:
    selection = SelectionModel().replace({"a"})

    assert selection.ids == frozenset({"a"})
    selection = selection.add({"b"})
    assert selection.ids == frozenset({"a", "b"})
    assert selection.invert({"a", "c"}).ids == frozenset({"b", "c"})
    assert selection.prune({"a"}).ids == frozenset({"a"})
    assert selection.clear().ids == frozenset()


def test_selection_apply_uses_replace_add_and_invert_modes() -> None:
    selection = SelectionModel(frozenset({"old"}))

    assert selection.apply({"new"}, "replace").ids == frozenset({"new"})
    assert selection.apply({"new"}, "add").ids == frozenset({"old", "new"})
    assert selection.apply({"old", "new"}, "invert").ids == frozenset({"new"})


def test_project_entities_namespaces_route_and_visible_markup(tmp_path: Path) -> None:
    point = _point("p001", (5.0, 6.0))
    route = _route(point)
    markup = _markup(tmp_path)

    entities = project_entities(route, markup)

    assert [entity.id for entity in entities] == [
        "route:p001",
        "markup:g1",
        "markup:g2",
    ]
    assert entities[0].owner is EntityOwner.ROUTE
    assert entities[0].route_index == 0
    assert entities[1].owner is EntityOwner.MARKUP


def test_hidden_markup_is_excluded_from_entity_projection(tmp_path: Path) -> None:
    entities = project_entities(_route(_point("p001", (1.0, 1.0))), _markup(tmp_path, visible=False))

    assert [entity.id for entity in entities] == ["route:p001"]


def test_box_and_cross_matching_share_entity_geometry(tmp_path: Path) -> None:
    route = _route(_point("inside", (5.0, 5.0)), _point("outside", (15.0, 15.0)))
    markup = _markup(tmp_path)
    rect = SelectionRect.from_drag((0.0, -1.0), (1.0, 1.0))
    entities = project_entities(route, markup)

    assert entities_in_rect(entities, rect, crossing=False) == set()
    assert entities_in_rect(entities, rect, crossing=True) == {"markup:g1"}


def test_mixed_delete_plan_and_markup_application(tmp_path: Path) -> None:
    route = _route(_point("p001", (1.0, 1.0)))
    markup = _markup(tmp_path)
    entities = project_entities(route, markup)
    selected = {route_entity_id("p001"), markup_entity_id("g1")}

    plan = plan_mixed_delete(entities, selected, edit_safe=True)
    updated_markup = apply_markup_entity_changes(markup, plan)

    assert plan.accepted
    assert plan.route_remove_ids == frozenset({"p001"})
    assert plan.guide_remove_ids == frozenset({"g1"})
    assert [guide.id for guide in updated_markup.guides] == ["g2"]


def test_mixed_delete_rejects_unsafe_or_stale_selection(tmp_path: Path) -> None:
    markup = _markup(tmp_path)
    entities = project_entities(None, markup)

    unsafe = plan_mixed_delete(
        entities,
        {markup_entity_id("g1")},
        edit_safe=False,
    )
    stale = plan_mixed_delete(entities, {"markup:missing"}, edit_safe=True)

    assert not unsafe.accepted and not unsafe.guide_remove_ids
    assert not stale.accepted and not stale.guide_remove_ids


def test_mixed_array_excludes_zero_cell_and_preserves_relative_geometry(
    tmp_path: Path,
) -> None:
    source = _point("p001", (4.0, 5.0), metadata={"structure": 7})
    route = _route(source)
    markup = _markup(tmp_path)
    entities = project_entities(route, markup)
    request = MixedArrayRequest(
        direction_1=(10.0, 0.0),
        count_1=2,
        direction_2=(0.0, 20.0),
        count_2=2,
        serpentine=False,
    )

    plan = plan_mixed_array(
        entities,
        {route_entity_id(source.id), markup_entity_id("g1")},
        request,
        route=route,
        edit_safe=True,
    )

    assert plan.accepted
    assert [point.camera_center for point in plan.route_copies] == [
        (14.0, 5.0),
        (4.0, 25.0),
        (14.0, 25.0),
    ]
    assert [guide.start for guide in plan.guide_copies] == [
        (10.0, 0.0),
        (0.0, 20.0),
        (10.0, 20.0),
    ]
    assert [guide.end for guide in plan.guide_copies] == [
        (12.0, 0.0),
        (2.0, 20.0),
        (12.0, 20.0),
    ]
    assert [point.label for point in plan.route_copies] == ["P002", "P003", "P004"]
    assert all(point.metadata["structure"] == 7 for point in plan.route_copies)
    assert all(
        point.metadata["array_source_point_id"] == "p001"
        for point in plan.route_copies
    )


def test_one_selected_guide_is_a_valid_array_source(tmp_path: Path) -> None:
    markup = _markup(tmp_path)
    entities = project_entities(None, markup)
    request = MixedArrayRequest((5.0, 0.0), 2, (0.0, 0.0), 1, False)

    plan = plan_mixed_array(
        entities,
        {markup_entity_id("g1")},
        request,
        route=None,
        edit_safe=True,
    )

    assert plan.accepted
    assert len(plan.guide_copies) == 1
    assert plan.route_copies == ()


def test_array_rejects_no_selection_stale_ids_and_unsafe_edit(tmp_path: Path) -> None:
    markup = _markup(tmp_path)
    entities = project_entities(None, markup)
    request = MixedArrayRequest((5.0, 0.0), 2, (0.0, 0.0), 1, False)

    assert not plan_mixed_array(entities, set(), request, route=None, edit_safe=True).accepted
    assert not plan_mixed_array(
        entities,
        {"markup:missing"},
        request,
        route=None,
        edit_safe=True,
    ).accepted
    assert not plan_mixed_array(
        entities,
        {markup_entity_id("g1")},
        request,
        route=None,
        edit_safe=False,
    ).accepted


def test_serpentine_changes_route_cell_order(tmp_path: Path) -> None:
    source = _point("p001", (0.0, 0.0))
    route = _route(source)
    entities = project_entities(route, _markup(tmp_path))
    request = MixedArrayRequest((10.0, 0.0), 3, (0.0, 100.0), 2, True)

    plan = plan_mixed_array(
        entities,
        {route_entity_id(source.id)},
        request,
        route=route,
        edit_safe=True,
    )

    assert [point.camera_center for point in plan.route_copies] == [
        (10.0, 0.0),
        (20.0, 0.0),
        (20.0, 100.0),
        (10.0, 100.0),
        (0.0, 100.0),
    ]


def test_route_plan_application_is_all_or_nothing(tmp_path: Path) -> None:
    source = _point("p001", (0.0, 0.0))
    retained = _point("p002", (1.0, 1.0))
    route = _route(source, retained)
    session = DesignSession(route=route, selected_route_point_index=0)
    markup = _markup(tmp_path)
    entities = project_entities(route, markup)
    plan = plan_mixed_delete(
        entities,
        {route_entity_id(source.id), markup_entity_id("g1")},
        edit_safe=True,
    )

    result = apply_route_entity_changes(session, plan)

    assert result.accepted
    assert route.points == [retained]
    assert session.selected_route_point_index == 0


def test_stale_route_before_application_rejects_without_mutation(tmp_path: Path) -> None:
    source = _point("p001", (0.0, 0.0))
    route = _route(source)
    session = DesignSession(route=route, selected_route_point_index=0)
    entities = project_entities(route, _markup(tmp_path))
    plan = plan_mixed_delete(
        entities,
        {route_entity_id(source.id)},
        edit_safe=True,
    )
    replacement = _point("replacement", (9.0, 9.0))
    route.points[:] = [replacement]

    result = apply_route_entity_changes(session, plan)

    assert not result.accepted
    assert route.points == [replacement]


def test_markup_only_plan_does_not_touch_route_timestamp(tmp_path: Path) -> None:
    route = _route(_point("p001", (0.0, 0.0)))
    route.updated_at_utc = "unchanged"
    session = DesignSession(route=route, selected_route_point_index=0)
    markup = _markup(tmp_path)
    plan = plan_mixed_delete(
        project_entities(route, markup),
        {markup_entity_id("g1")},
        edit_safe=True,
    )

    result = apply_route_entity_changes(session, plan)

    assert result.accepted
    assert route.updated_at_utc == "unchanged"
