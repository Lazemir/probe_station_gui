import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QAbstractItemView, QApplication

import main as main_module
from main import Main
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.selection_model import (
    MixedArrayRequest,
    SelectionModel,
    project_entities,
    route_entity_id,
)
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel


REPO_ROOT = Path(__file__).resolve().parents[2]


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_document() -> DesignDocument:
    return DesignDocument(
        path=REPO_ROOT / "tests" / "fixtures" / "synthetic.gds",
        library=object(),
        top_cell=object(),
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 100.0, 200.0),
        polygons_by_layer={},
        visible_layers=frozenset(),
    )


def test_minimap_single_click_handler_opens_design_window(monkeypatch) -> None:
    window = Main.__new__(Main)
    calls: list[bool] = []
    monkeypatch.setattr(
        main_module,
        "toggle_design_layout_window",
        lambda _owner, show: calls.append(bool(show)),
    )

    Main._open_design_window_from_minimap_point(window, 1.25, 2.5)

    assert calls == [True]


def test_route_array_request_uses_shared_selected_entity_ids() -> None:
    _qt_app()
    panel = DesignNavigatorPanel()
    document = _make_document()
    route = MeasurementRoute.default_for_document(document)
    route.add_point((0.0, 0.0))
    route.add_point((1.0, 0.0))
    route.add_point((2.0, 0.0))
    panel.set_document(document)
    panel.set_route(route, selected_route_point_index=0)
    panel.set_selectable_entities(project_entities(route, None))

    assert (
        panel.route_controls.route_table.selectionMode()
        == QAbstractItemView.ExtendedSelection
    )

    selection_model = panel.route_controls.route_table.selectionModel()
    model = panel.route_controls.route_table.model()
    selection_model.select(
        model.index(0, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )
    selection_model.select(
        model.index(2, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )
    panel.set_selection(
        SelectionModel(
            frozenset({route_entity_id("p001"), route_entity_id("p003")})
        )
    )
    emitted: list[MixedArrayRequest] = []
    panel.mixed_array_requested.connect(emitted.append)

    panel.tool_controls._route_array_create_button.click()

    assert emitted[-1].source_ids == frozenset(
        {route_entity_id("p001"), route_entity_id("p003")}
    )
    assert panel.route_controls.selected_index == 0


def test_route_multi_selection_survives_route_refresh() -> None:
    _qt_app()
    panel = DesignNavigatorPanel()
    document = _make_document()
    route = MeasurementRoute.default_for_document(document)
    route.add_point((0.0, 0.0))
    route.add_point((1.0, 0.0))
    route.add_point((2.0, 0.0))
    panel.set_document(document)
    panel.set_route(route, selected_route_point_index=0)
    panel.route_selected.connect(
        lambda index: panel.set_route(route, selected_route_point_index=index)
    )

    selection_model = panel.route_controls.route_table.selectionModel()
    model = panel.route_controls.route_table.model()
    selection_model.select(
        model.index(2, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )

    assert panel.route_controls.selected_rows() == [0, 2]
