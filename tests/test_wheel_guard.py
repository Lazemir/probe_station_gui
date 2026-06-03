from __future__ import annotations

import pytest


pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.wheel_guard import (
    GuardedComboBox,
    GuardedDoubleSpinBox,
    GuardedSlider,
    allow_wheel_value_change,
    _wheel_changes_value_allowed,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_guard_blocks_unfocused_value_editors(qt_app: QApplication) -> None:
    spin = GuardedDoubleSpinBox()
    combo = GuardedComboBox()
    slider = GuardedSlider()

    assert not _wheel_changes_value_allowed(spin)
    assert not _wheel_changes_value_allowed(combo)
    assert not _wheel_changes_value_allowed(slider)


def test_guard_allows_marked_main_window_controls(qt_app: QApplication) -> None:
    spin = GuardedDoubleSpinBox()
    allow_wheel_value_change(spin)

    assert _wheel_changes_value_allowed(spin)
