from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from probe_station_gui.views.alignment_panel import AlignmentPanel


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_alignment_capture_running_disables_duplicates_but_keeps_cancel(
    qt_app: QApplication,
) -> None:
    panel = AlignmentPanel()
    panel.set_pick_slot(0)

    panel.set_capture_running(True)

    assert panel._set_point_1_button.isEnabled() is False
    assert panel._set_point_2_button.isEnabled() is False
    assert panel._capture_mode_combo.isEnabled() is False
    assert panel._reset_points_button.isEnabled() is False
    assert panel._cancel_pick_button.isEnabled() is True

    panel.set_capture_running(False)

    assert panel._set_point_1_button.isEnabled() is True
    assert panel._set_point_2_button.isEnabled() is True
    assert panel._capture_mode_combo.isEnabled() is True
    assert panel._cancel_pick_button.isEnabled() is True
    panel.close()
