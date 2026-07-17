from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.views.alignment_panel import AlignmentPanel


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_alignment_panel_builds_dynamic_design_and_stage_rows(
    qt_app: QApplication,
) -> None:
    panel = AlignmentPanel()
    panel.set_design_marks(((0.0, 0.0), (10.0, 0.0), (2.0, 5.0)))
    panel.set_captured_points((None, (11.0, 2.0), None))

    assert panel.row_count == 3
    assert panel.next_incomplete_slot == 0
    assert panel.capture_button(0).text() == "Capture"
    assert panel.capture_button(1).text() == "Replace"
    assert panel.capture_button(2).text() == "Capture"
    assert "D3" in panel.design_label(2).text()
    assert "S2" in panel.stage_label(1).text()
    assert not panel.fit_ready

    panel.deleteLater()


def test_alignment_panel_emits_arbitrary_capture_slot_and_becomes_fit_ready(
    qt_app: QApplication,
) -> None:
    panel = AlignmentPanel()
    panel.set_design_marks(((0.0, 0.0), (10.0, 0.0), (2.0, 5.0)))
    panel.set_captured_points(((1.0, 2.0), (11.0, 2.0), None))
    requests: list[tuple[int, str]] = []
    panel.capture_point_requested.connect(
        lambda slot, mode: requests.append((slot, mode))
    )

    panel.capture_button(2).click()
    panel.set_captured_points(((1.0, 2.0), (11.0, 2.0), (3.0, 7.0)))

    assert requests == [(2, "center")]
    assert panel.next_incomplete_slot is None
    assert panel.fit_ready
    assert "Ready to fit" in panel.fit_status_label.text()
    panel.deleteLater()


def test_alignment_panel_shows_fit_residuals(qt_app: QApplication) -> None:
    panel = AlignmentPanel()

    panel.set_fit_residuals(0.0123, 0.0456)

    assert "RMS 0.0123 mm" in panel.fit_status_label.text()
    assert "max 0.0456 mm" in panel.fit_status_label.text()
    panel.deleteLater()
