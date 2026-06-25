import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QRectF
from PySide6.QtWidgets import QApplication

from probe_station_gui.views.microscope_view import MicroscopeView


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


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


if __name__ == "__main__":
    unittest.main()
