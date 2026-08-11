import os
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QPointF, QRectF, Qt
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
from probe_station_gui.views.design_plot_pane import _DesignPlotPane


REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeSignal:
    def __init__(self) -> None:
        self.emissions: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.emissions.append(args)


class _FakeViewBox:
    def mapSceneToView(self, position: QPointF) -> QPointF:  # noqa: N802 - Qt style
        return QPointF(position)


class _FakePlot:
    def sceneBoundingRect(self) -> QRectF:  # noqa: N802 - Qt style
        return QRectF(0.0, 0.0, 100.0, 100.0)

    def getViewBox(self) -> _FakeViewBox:  # noqa: N802 - Qt style
        return _FakeViewBox()


class _FakeClickEvent:
    def __init__(self, *, double: bool, button=Qt.LeftButton) -> None:
        self._double = bool(double)
        self._button = button

    def button(self) -> object:
        return self._button

    def double(self) -> bool:
        return self._double

    def scenePos(self) -> QPointF:  # noqa: N802 - Qt style
        return QPointF(10.0, 20.0)


def _navigation_pane() -> types.SimpleNamespace:
    move_requested = _FakeSignal()
    pane = types.SimpleNamespace(
        _plot=_FakePlot(),
        _document=object(),
        _route_pick_mode=None,
        _route_edit_enabled=False,
        _navigation_enabled=True,
        _active_design_tool="select",
        move_requested=move_requested,
        route_pick_requested=_FakeSignal(),
        route_point_requested=_FakeSignal(),
        calibration_point_selected=_FakeSignal(),
        selected_focus_point_changed=_FakeSignal(),
        _is_double_click_event=_DesignPlotPane._is_double_click_event,
        _set_hover_snap=lambda _snap, **_kwargs: None,
        _resolve_snap_result=lambda raw: types.SimpleNamespace(
            point=(raw[0] + 1.0, raw[1] + 2.0),
            mode="raw",
            distance=0.0,
        ),
        _emit_click_selection=lambda _point, _modifiers: None,
    )
    pane._execute_click_action = types.MethodType(
        _DesignPlotPane._execute_click_action,
        pane,
    )
    return pane


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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


def test_design_select_single_click_does_not_move() -> None:
    pane = _navigation_pane()
    pane._active_design_tool = "select"

    _DesignPlotPane._on_mouse_clicked(pane, _FakeClickEvent(double=False))

    assert pane.move_requested.emissions == []


def test_design_select_double_click_does_not_move() -> None:
    pane = _navigation_pane()
    pane._active_design_tool = "select"

    _DesignPlotPane._on_mouse_clicked(pane, _FakeClickEvent(double=True))

    assert pane.move_requested.emissions == []


@pytest.mark.parametrize("tool", ["array", "point", "guide"])
def test_unhandled_explicit_tool_click_does_not_calibrate_or_move(tool: str) -> None:
    pane = _navigation_pane()
    pane._active_design_tool = tool
    pane._route_edit_enabled = False

    _DesignPlotPane._on_mouse_clicked(pane, _FakeClickEvent(double=False))

    assert pane.calibration_point_selected.emissions == []
    assert pane.move_requested.emissions == []


@pytest.mark.parametrize(
    ("button", "expected_slot"),
    [(Qt.LeftButton, 0), (Qt.RightButton, 1)],
)
def test_explicit_legacy_click_keeps_calibration_fallback(
    button: Qt.MouseButton,
    expected_slot: int,
) -> None:
    pane = _navigation_pane()
    pane._active_design_tool = "legacy"

    _DesignPlotPane._on_mouse_clicked(
        pane,
        _FakeClickEvent(double=False, button=button),
    )

    assert pane.calibration_point_selected.emissions == [
        (expected_slot, 11.0, 22.0)
    ]
    assert pane.move_requested.emissions == []


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

    assert panel._route_table.selectionMode() == QAbstractItemView.ExtendedSelection

    selection_model = panel._route_table.selectionModel()
    model = panel._route_table.model()
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

    panel._route_array_create_button.click()

    assert emitted[-1].source_ids == frozenset(
        {route_entity_id("p001"), route_entity_id("p003")}
    )
    assert panel._selected_route_point_index == 0


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

    selection_model = panel._route_table.selectionModel()
    model = panel._route_table.model()
    selection_model.select(
        model.index(2, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )

    assert panel._selected_route_row_indices() == [0, 2]
