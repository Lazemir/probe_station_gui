import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QRect, Qt
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
