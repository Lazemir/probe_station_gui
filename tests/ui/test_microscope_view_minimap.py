import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

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


class MicroscopeViewMinimapTest(unittest.TestCase):
    def test_minimap_click_delay_leaves_room_for_double_click_event(self) -> None:
        app = _app()
        original_interval = app.doubleClickInterval()
        try:
            app.setDoubleClickInterval(250)

            self.assertEqual(
                MicroscopeView._minimap_click_delay_ms(),
                250 + MicroscopeView._MINIMAP_CLICK_DELAY_PADDING_MS,
            )
        finally:
            app.setDoubleClickInterval(original_interval)

    def test_visible_minimap_fov_rect_stays_inside_content_rect(self) -> None:
        content = QRect(10, 20, 100, 80)
        raw_fov = QRectF(-25.0, 30.0, 170.0, 60.0)

        visible = MicroscopeView._visible_minimap_fov_rect(raw_fov, content)

        bounds = QRectF(content).adjusted(1.0, 1.0, -1.0, -1.0)
        self.assertGreater(visible.width(), 0.0)
        self.assertGreater(visible.height(), 0.0)
        self.assertGreaterEqual(visible.left(), bounds.left())
        self.assertLessEqual(visible.right(), bounds.right())
        self.assertGreaterEqual(visible.top(), bounds.top())
        self.assertLessEqual(visible.bottom(), bounds.bottom())

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
        self.assertIsNone(view._target_rel)

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
        self.assertIsNone(view._target_rel)


if __name__ == "__main__":
    unittest.main()
