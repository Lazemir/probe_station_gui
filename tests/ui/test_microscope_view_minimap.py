import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.model import DesignDocument
from probe_station_gui.views.microscope_view import MicroscopeView


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


def _view_with_frame() -> MicroscopeView:
    _app()
    view = MicroscopeView()
    view.resize(220, 180)
    frame = QImage(100, 80, QImage.Format_RGB32)
    frame.fill(0)
    view.set_frame(frame)
    view._display_rect = QRect(10, 20, 200, 160)
    return view


def _mouse_event(
    event_type: QEvent.Type,
    x: float,
    y: float,
    *,
    button: Qt.MouseButton = Qt.LeftButton,
    buttons: Qt.MouseButtons = Qt.NoButton,
) -> QMouseEvent:
    point = QPointF(x, y)
    return QMouseEvent(
        event_type,
        point,
        point,
        point,
        button,
        buttons,
        Qt.NoModifier,
    )


def _design_document() -> DesignDocument:
    return DesignDocument(
        path=Path("integration.gds"),
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 100.0, 100.0),
        polygons_by_layer={},
        visible_layers=frozenset(),
    )


class MicroscopeViewMinimapTest(unittest.TestCase):
    def test_real_widget_render_executes_ruler_and_rectangle_overlays(self) -> None:
        view = _view_with_frame()
        view.resize(220, 180)
        baseline = QImage(220, 180, QImage.Format_ARGB32)
        baseline.fill(QColor("black"))
        view.render(baseline)

        view.set_measure_mode("ruler")
        view.mousePressEvent(
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                50,
                60,
                button=Qt.LeftButton,
                buttons=Qt.LeftButton,
            )
        )
        view.mouseMoveEvent(
            _mouse_event(QEvent.Type.MouseMove, 170, 140, buttons=Qt.NoButton)
        )
        ruler = QImage(220, 180, QImage.Format_ARGB32)
        ruler.fill(QColor("black"))
        view.render(ruler)

        view.set_measure_mode("rect")
        view.mousePressEvent(
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                50,
                60,
                button=Qt.LeftButton,
                buttons=Qt.LeftButton,
            )
        )
        view.mouseMoveEvent(
            _mouse_event(QEvent.Type.MouseMove, 170, 140, buttons=Qt.NoButton)
        )
        rectangle = QImage(220, 180, QImage.Format_ARGB32)
        rectangle.fill(QColor("black"))
        view.render(rectangle)

        self.assertNotEqual(baseline, ruler)
        self.assertNotEqual(baseline, rectangle)
        self.assertNotEqual(ruler, rectangle)
        view.shutdown()

    def test_widget_paint_renders_configured_minimap_panel(self) -> None:
        _app()
        view = MicroscopeView()
        view.resize(1000, 1000)
        frame = QImage(1000, 1000, QImage.Format_RGB32)
        frame.fill(QColor("white"))
        view.set_frame(frame)
        view.set_design_minimap_data(
            document=_design_document(),
            targets=[],
            selected_target_id=None,
            probe_route=None,
            selected_route_point_index=-1,
            selected_design_point=None,
            current_design_position=None,
            fov_design_size=None,
            source_design_marks=[],
            check_design_marks=[],
        )

        canvas = QImage(1000, 1000, QImage.Format_ARGB32)
        canvas.fill(QColor("black"))
        view.render(canvas)

        panel_pixel = canvas.pixelColor(760, 60)
        self.assertLess(panel_pixel.red(), 80)
        self.assertLess(panel_pixel.green(), 80)
        self.assertLess(panel_pixel.blue(), 80)
        view.shutdown()

    def test_minimap_press_delegates_point_and_display_rect(self) -> None:
        view = _view_with_frame()
        calls: list[tuple[object, object]] = []

        class _MinimapAdapter:
            @staticmethod
            def contains(point, display_rect) -> bool:
                return True

            @staticmethod
            def queue_click(point, display_rect) -> None:
                calls.append((point, display_rect))

        view._minimap = _MinimapAdapter()  # type: ignore[assignment]
        view.mousePressEvent(
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                150,
                100,
                button=Qt.LeftButton,
                buttons=Qt.LeftButton,
            )
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], QPointF(150, 100).toPoint())
        self.assertEqual(calls[0][1], QRect(10, 20, 200, 160))

    def test_click_to_move_emits_on_release_not_press(self) -> None:
        view = _view_with_frame()
        clicked: list[tuple[float, float, float, float]] = []
        view.clicked.connect(lambda *args: clicked.append(args))

        view.mousePressEvent(
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                150,
                100,
                button=Qt.LeftButton,
                buttons=Qt.LeftButton,
            )
        )

        self.assertEqual(clicked, [])

        view.mouseReleaseEvent(
            _mouse_event(
                QEvent.Type.MouseButtonRelease,
                150,
                100,
                button=Qt.LeftButton,
                buttons=Qt.NoButton,
            )
        )

        self.assertEqual(len(clicked), 1)
        dx, dy, rel_x, rel_y = clicked[0]
        self.assertAlmostEqual(dx, 20.0)
        self.assertAlmostEqual(dy, 0.0)
        self.assertAlmostEqual(rel_x, 0.7)
        self.assertAlmostEqual(rel_y, 0.5)

    def test_click_to_move_release_position_is_used_after_inside_drag(self) -> None:
        view = _view_with_frame()
        clicked: list[tuple[float, float, float, float]] = []
        view.clicked.connect(lambda *args: clicked.append(args))

        view.mousePressEvent(
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                50,
                60,
                button=Qt.LeftButton,
                buttons=Qt.LeftButton,
            )
        )
        view.mouseMoveEvent(
            _mouse_event(
                QEvent.Type.MouseMove,
                170,
                140,
                button=Qt.NoButton,
                buttons=Qt.LeftButton,
            )
        )
        view.mouseReleaseEvent(
            _mouse_event(
                QEvent.Type.MouseButtonRelease,
                170,
                140,
                button=Qt.LeftButton,
                buttons=Qt.NoButton,
            )
        )

        self.assertEqual(len(clicked), 1)
        dx, dy, rel_x, rel_y = clicked[0]
        self.assertAlmostEqual(dx, 30.0)
        self.assertAlmostEqual(dy, -20.0)
        self.assertAlmostEqual(rel_x, 0.8)
        self.assertAlmostEqual(rel_y, 0.75)

    def test_click_to_move_is_cancelled_after_drag_outside_image(self) -> None:
        view = _view_with_frame()
        clicked: list[tuple[float, float, float, float]] = []
        view.clicked.connect(lambda *args: clicked.append(args))

        view.mousePressEvent(
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                150,
                100,
                button=Qt.LeftButton,
                buttons=Qt.LeftButton,
            )
        )
        view.mouseMoveEvent(
            _mouse_event(
                QEvent.Type.MouseMove,
                5,
                100,
                button=Qt.NoButton,
                buttons=Qt.LeftButton,
            )
        )
        view.mouseReleaseEvent(
            _mouse_event(
                QEvent.Type.MouseButtonRelease,
                150,
                100,
                button=Qt.LeftButton,
                buttons=Qt.NoButton,
            )
        )

        self.assertEqual(clicked, [])
        self.assertIsNone(view.interaction.target_rel)

    def test_click_to_move_release_outside_image_is_cancelled(self) -> None:
        view = _view_with_frame()
        clicked: list[tuple[float, float, float, float]] = []
        view.clicked.connect(lambda *args: clicked.append(args))

        view.mousePressEvent(
            _mouse_event(
                QEvent.Type.MouseButtonPress,
                150,
                100,
                button=Qt.LeftButton,
                buttons=Qt.LeftButton,
            )
        )
        view.mouseReleaseEvent(
            _mouse_event(
                QEvent.Type.MouseButtonRelease,
                5,
                100,
                button=Qt.LeftButton,
                buttons=Qt.NoButton,
            )
        )

        self.assertEqual(clicked, [])
        self.assertIsNone(view.interaction.target_rel)


if __name__ == "__main__":
    unittest.main()
