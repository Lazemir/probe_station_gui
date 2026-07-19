import os
from pathlib import Path
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    PendingClick,
    SnapResponse,
)
from probe_station_gui.design.model import SnapResult
from probe_station_gui.views.design_plot_pane import _DesignPlotPane


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _ClickEvent:
    def __init__(
        self,
        *,
        double: bool,
        modifiers: Qt.KeyboardModifiers = Qt.NoModifier,
        x_value: float = 10.0,
        y_value: float = 20.0,
    ) -> None:
        self._double = bool(double)
        self._modifiers = modifiers
        self._position = QPointF(x_value, y_value)

    def button(self):
        return Qt.LeftButton

    def double(self) -> bool:
        return self._double

    def modifiers(self):
        return self._modifiers

    def scenePos(self) -> QPointF:
        return QPointF(self._position)


class _SceneEvent:
    def __init__(self, event_type, x_value: float, y_value: float) -> None:
        self._event_type = event_type
        self._position = QPointF(x_value, y_value)

    def type(self):
        return self._event_type

    def button(self):
        return Qt.LeftButton

    def scenePos(self) -> QPointF:
        return QPointF(self._position)


@pytest.fixture
def click_event():
    return lambda *, double, modifiers=Qt.NoModifier, x=10.0, y=20.0: _ClickEvent(
        double=double,
        modifiers=modifiers,
        x_value=x,
        y_value=y,
    )


@pytest.fixture
def move_scene_event():
    return SimpleNamespace(
        press=lambda x, y: _SceneEvent(QEvent.GraphicsSceneMousePress, x, y),
        move=lambda x, y: _SceneEvent(QEvent.GraphicsSceneMouseMove, x, y),
        release=lambda x, y: _SceneEvent(QEvent.GraphicsSceneMouseRelease, x, y),
    )


@pytest.fixture
def pane(qt_app):
    widget = _DesignPlotPane()
    widget.resize(640, 480)
    widget.show()
    qt_app.processEvents()
    widget._document = SimpleNamespace(file_backed=False)
    widget._resolve_snap_result = lambda point: SnapResult(point, "free", 0.0)
    yield widget
    widget.shutdown()
    widget.deleteLater()


def test_move_single_click_emits_once_and_stays_active(pane, click_event) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")
    pane._resolve_snap_result = lambda point: SnapResult(
        (point[0] + 1.0, point[1] + 2.0), "vertex", 1.0
    )

    event = click_event(double=False)
    raw = pane._plot.getViewBox().mapSceneToView(event.scenePos())
    expected = (float(raw.x()) + 1.0, float(raw.y()) + 2.0)
    pane._on_mouse_clicked(event)
    pane._on_mouse_clicked(event)

    assert emitted == [expected, expected]
    assert pane.active_design_tool == "move"


def test_move_double_click_second_event_does_not_emit_again(pane, click_event) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")

    pane._on_mouse_clicked(click_event(double=False))
    pane._on_mouse_clicked(click_event(double=True))

    assert len(emitted) == 1


def test_modified_move_click_does_not_emit(pane, click_event) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")

    pane._on_mouse_clicked(click_event(double=False, modifiers=Qt.ControlModifier))

    assert emitted == []


def test_move_click_outside_plot_does_not_emit(pane, click_event) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")

    pane._on_mouse_clicked(click_event(double=False, x=-10_000.0, y=-10_000.0))

    assert emitted == []


def test_move_drag_suppresses_click_and_leaves_event_for_viewbox(
    pane, move_scene_event, click_event
) -> None:
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))

    assert pane._handle_move_scene_event(move_scene_event.press(10, 10)) is False
    assert pane._handle_move_scene_event(move_scene_event.move(40, 10)) is False
    assert pane._handle_move_scene_event(move_scene_event.release(40, 10)) is False
    pane._on_mouse_clicked(click_event(double=False))

    assert emitted == []


def test_real_move_drag_pans_viewbox_without_move_request(pane, qt_app) -> None:
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    viewport = pane._plot.viewport()
    start = viewport.rect().center()
    before = pane._plot.getViewBox().viewRange()

    QTest.mousePress(viewport, Qt.LeftButton, pos=start)
    QTest.mouseMove(viewport, pos=start + QPoint(50, 0), delay=20)
    QTest.mouseRelease(viewport, Qt.LeftButton, pos=start + QPoint(50, 0))
    qt_app.processEvents()

    after = pane._plot.getViewBox().viewRange()
    assert after != before
    assert emitted == []


def test_cancel_active_interaction_clears_pending_move_press(
    pane, move_scene_event
) -> None:
    pane.set_active_design_tool("move")
    pane._handle_move_scene_event(move_scene_event.press(10, 10))

    pane.cancel_active_interaction()

    assert pane._move_press_scene_pos is None
    assert pane._move_dragging is False
    assert pane._suppress_move_scene_click is False


def test_escape_cancels_pending_file_backed_move(pane) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_active_design_tool("move")
    config = KLayoutConfig(
        path=Path("layout.gds"),
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 50.0),
        display_bounds=(0.0, 0.0, 100.0, 50.0),
        rotation_quarter_turns=0,
        generation=3,
    )
    pane._klayout_config = config
    pane._pending_clicks[7] = PendingClick(
        request_id=7,
        config_generation=3,
        action="move",
        raw_point=(10.0, 20.0),
        markup_generation=pane._markup_generation,
    )
    pane.cancel_active_interaction()
    pane.set_active_design_tool("select")
    pane._on_file_backed_snap_ready(
        SnapResponse(
            request_id=7,
            config_generation=3,
            raw_point=(10.0, 20.0),
            result=SnapResult((10.0, 20.0), "free", 0.0),
            elapsed_ms=1.0,
            shapes_inspected=0,
            purpose="click",
        )
    )

    assert emitted == []
    assert pane._pending_clicks == {}
