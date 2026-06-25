import math
import sys


def _restore_real_qt_imports() -> None:
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]


_restore_real_qt_imports()

from PySide6.QtWidgets import QApplication, QFrame

from probe_station_gui.views.resistance_monitor_panel import (
    ResistanceMonitorPanel,
    _format_resistance,
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


def _digit_count(text: str) -> int:
    return sum(1 for char in text if char.isdigit())


def test_resistance_display_uses_fixed_digit_count() -> None:
    values = [
        0.0,
        0.0123,
        1.23456,
        12.3456,
        123.456,
        1234.56,
        298000.0,
        1_234_567.0,
        -42.0,
    ]

    for value in values:
        text, unit = _format_resistance(value)
        assert _digit_count(text) == 5
        assert unit

    assert _format_resistance(math.nan) == ("------", "Ohm")


def test_resistance_panel_copies_last_reading_in_ohms() -> None:
    app = _app()
    panel = ResistanceMonitorPanel()
    screen = panel.findChild(QFrame, "ResistanceScreen")
    assert screen is not None

    panel.set_reading_summary(298000.0, False, 10)
    screen.clicked.emit()

    assert panel.value_label.text() == "298.00"
    assert panel.unit_label.text() == "kOhm"
    assert app.clipboard().text() == "298000 Ohm"
    assert panel.status_label.text() == "Copied"

    panel.set_reading_pending(240)
    screen.clicked.emit()

    assert app.clipboard().text() == "298000 Ohm"


def test_resistance_panel_off_state_copy() -> None:
    _app()
    panel = ResistanceMonitorPanel()

    panel.set_standby_enabled(False)

    assert panel.status_label.text() == "Off"
