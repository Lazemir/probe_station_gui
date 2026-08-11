from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import probe_station_gui.design.tool_context as tool_contexts  # noqa: E402
from probe_station_gui.design.selection_geometry import (  # noqa: E402
    PointGeometry,
    SegmentGeometry,
)
from probe_station_gui.design.selection_model import (  # noqa: E402
    EntityOwner,
    SelectableDesignEntity,
    SelectionModel,
    markup_entity_id,
    route_entity_id,
)
from probe_station_gui.route.model import (  # noqa: E402
    MeasurementRoute,
    RouteDesignBinding,
    RoutePoint,
)
from probe_station_gui.views.design_navigator_panel import (  # noqa: E402
    DesignNavigatorPanel,
)



@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _route(*, path: str | None = None, point_count: int = 1) -> MeasurementRoute:
    route = MeasurementRoute(
        name="test route",
        design=RouteDesignBinding(
            path="design.gds",
            sha256="0" * 64,
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 100.0),
            dbu=0.001,
        ),
        points=[
            RoutePoint(
                id=f"p{index + 1:03d}",
                label=f"P{index + 1:03d}",
                camera_center=(float(index), float(index)),
            )
            for index in range(point_count)
        ],
    )
    if path is not None:
        route.path = Path(path)
    return route


def _set_document_available(panel: DesignNavigatorPanel) -> None:
    panel.document_controls._document = object()
    panel._replace_tool_context()
    panel._update_enabled_state()


def test_selection_refresh_reuses_the_route_snapshot(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_calls: list[MeasurementRoute] = []
    original_capture = tool_contexts._RouteSnapshot.capture.__func__

    def counted_capture(
        cls: type[tool_contexts._RouteSnapshot],
        route: MeasurementRoute,
    ) -> tool_contexts._RouteSnapshot:
        capture_calls.append(route)
        return original_capture(cls, route)

    monkeypatch.setattr(
        tool_contexts._RouteSnapshot,
        "capture",
        classmethod(counted_capture),
    )
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    route = _route(point_count=2)
    panel.set_route(route, selected_route_point_index=0)
    entities = [
        SelectableDesignEntity(
            route_entity_id(point.id),
            EntityOwner.ROUTE,
            point.id,
            PointGeometry(point.camera_center),
            route_index=index,
        )
        for index, point in enumerate(route.points)
    ]
    captures_after_route = len(capture_calls)

    panel.set_selectable_entities(entities)
    panel.set_selection(SelectionModel(frozenset({entities[0].id})))
    panel.set_selection(SelectionModel(frozenset({entities[1].id})))

    assert captures_after_route == 1
    assert len(capture_calls) == captures_after_route
    panel.deleteLater()


def test_hidden_markup_can_still_be_cleared_when_guides_exist(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)

    panel.set_markup_state(visible=False, guide_count=2)

    assert panel.tool_controls._guide_clear_button.isEnabled()
    assert not panel.tool_controls._route_array_create_button.isEnabled()
    panel.deleteLater()


def test_route_table_selection_and_shared_selection_are_bidirectional(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    route = _route(point_count=2)
    _set_document_available(panel)
    panel.set_route(route, selected_route_point_index=0)
    entities = [
        SelectableDesignEntity(
            route_entity_id(point.id),
            EntityOwner.ROUTE,
            point.id,
            PointGeometry(point.camera_center),
            route_index=index,
        )
        for index, point in enumerate(route.points)
    ]
    panel.set_selectable_entities(entities)
    panel.set_selection(
        SelectionModel(frozenset(entity.id for entity in entities))
    )

    assert panel.route_controls.selected_rows() == [0, 1]
    requests: list[tuple[set[str], str]] = []
    panel.selection_requested.connect(
        lambda ids, mode: requests.append((set(ids), mode))
    )
    panel.route_controls.route_table.clearSelection()
    selection_model = panel.route_controls.route_table.selectionModel()
    selection_model.select(
        panel.route_controls.route_table.model().index(1, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )

    assert requests[-1] == ({route_entity_id("p002")}, "replace")
    panel.deleteLater()


def test_route_table_shift_add_and_ctrl_invert_preserve_guide_selection(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    route = _route(point_count=2)
    _set_document_available(panel)
    panel.set_route(route, selected_route_point_index=0)
    entities = [
        SelectableDesignEntity(
            route_entity_id(point.id),
            EntityOwner.ROUTE,
            point.id,
            PointGeometry(point.camera_center),
            route_index=index,
        )
        for index, point in enumerate(route.points)
    ]
    guide_id = markup_entity_id("guide")
    entities.append(
        SelectableDesignEntity(
            guide_id,
            EntityOwner.MARKUP,
            "guide",
            SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
        )
    )
    panel.set_selectable_entities(entities)
    panel.set_selection(
        SelectionModel(frozenset({guide_id, route_entity_id("p001")}))
    )
    requests: list[tuple[set[str], str]] = []
    panel.selection_requested.connect(
        lambda ids, mode: requests.append((set(ids), mode))
    )
    selection_model = panel.route_controls.route_table.selectionModel()

    panel.route_controls.selection_modifiers = lambda: Qt.ShiftModifier
    selection_model.select(
        panel.route_controls.route_table.model().index(1, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )
    assert requests[-1] == (
        {route_entity_id("p001"), route_entity_id("p002")},
        "add",
    )

    panel.set_selection(
        SelectionModel(
            frozenset(
                {
                    guide_id,
                    route_entity_id("p001"),
                    route_entity_id("p002"),
                }
            )
        )
    )
    panel.route_controls.selection_modifiers = lambda: Qt.ControlModifier
    selection_model.select(
        panel.route_controls.route_table.model().index(0, 0),
        QItemSelectionModel.Deselect | QItemSelectionModel.Rows,
    )
    assert requests[-1] == ({route_entity_id("p001")}, "invert")
    panel.deleteLater()


def test_select_and_guide_actions_emit_laconic_commands(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.tool_controls._guide_undo_available = True
    panel.set_selectable_entities(
        [
            SelectableDesignEntity(
                route_entity_id("p001"),
                EntityOwner.ROUTE,
                "p001",
                PointGeometry((0.0, 0.0)),
                route_index=0,
            ),
            SelectableDesignEntity(
                markup_entity_id("g1"),
                EntityOwner.MARKUP,
                "g1",
                SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
            ),
        ]
    )
    panel.set_selection(SelectionModel(frozenset({"route:p001"})))
    panel.set_markup_state(visible=True, guide_count=1)
    panel._update_enabled_state()
    commands: list[str] = []
    panel.delete_selection_requested.connect(lambda: commands.append("delete"))
    panel.guide_undo_requested.connect(lambda: commands.append("undo"))
    panel.guide_clear_requested.connect(lambda: commands.append("clear"))

    panel.tool_controls._selection_delete_button.click()
    panel.tool_controls._guide_undo_button.click()
    panel.tool_controls._guide_clear_button.click()

    assert commands == ["delete", "undo", "clear"]
    panel.deleteLater()
