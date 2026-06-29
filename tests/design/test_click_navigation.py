import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt

import main as main_module
from main import Main
from probe_station_gui.views.design_navigator_panel import _DesignPlotPane


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
