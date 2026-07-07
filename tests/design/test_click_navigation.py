import os
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QPointF, QRectF, Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication

import main as main_module
from main import Main
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.views.design_navigator_panel import (
    DesignNavigatorPanel,
    _DesignPlotPane,
)


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
    def __init__(self, *, double: bool) -> None:
        self._double = bool(double)

    def button(self) -> object:
        return Qt.LeftButton

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
        move_requested=move_requested,
        route_pick_requested=_FakeSignal(),
        route_point_requested=_FakeSignal(),
        calibration_point_selected=_FakeSignal(),
        _is_double_click_event=_DesignPlotPane._is_double_click_event,
        _set_hover_snap=lambda _snap: None,
        _resolve_snap_result=lambda raw: types.SimpleNamespace(
            point=(raw[0] + 1.0, raw[1] + 2.0),
            mode="raw",
            distance=0.0,
        ),
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


def test_design_navigation_single_click_does_not_move() -> None:
    pane = _navigation_pane()

    _DesignPlotPane._on_mouse_clicked(pane, _FakeClickEvent(double=False))

    assert pane.move_requested.emissions == []


def test_design_navigation_double_click_moves() -> None:
    pane = _navigation_pane()

    _DesignPlotPane._on_mouse_clicked(pane, _FakeClickEvent(double=True))

    assert pane.move_requested.emissions == [(11.0, 22.0)]


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


def test_route_array_request_includes_multi_selected_route_rows() -> None:
    _qt_app()
    panel = DesignNavigatorPanel()
    document = _make_document()
    route = MeasurementRoute.default_for_document(document)
    route.add_point((0.0, 0.0))
    route.add_point((1.0, 0.0))
    route.add_point((2.0, 0.0))
    panel.set_document(document)
    panel.set_route(route, selected_route_point_index=0)

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
    emitted: list[tuple[object, ...]] = []
    panel.route_array_requested.connect(lambda *args: emitted.append(args))

    panel._emit_route_array_requested()

    assert emitted[-1][-1] == [0, 2]
    assert panel._selected_route_point_index == 0
