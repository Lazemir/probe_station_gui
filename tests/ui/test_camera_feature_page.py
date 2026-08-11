from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from probe_station_gui.dialogs.camera_feature_page import CameraFeaturePage


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _gain_node(value: str) -> dict[str, object]:
    return {
        "name": "Gain",
        "display_name": "Gain",
        "type": "string",
        "value": value,
        "available": True,
        "writable": True,
    }


def test_hidden_page_defers_editor_build_until_it_becomes_visible(
    app: QApplication,
) -> None:
    page = CameraFeaturePage("camera", lambda *_args: None, lambda *_args: None)

    page.set_nodes([_gain_node("1.0")])

    assert page.findChildren(QLineEdit) == []
    page.show()
    app.processEvents()
    assert len(page.findChildren(QLineEdit)) == 1
    page.deleteLater()


def test_live_update_does_not_overwrite_the_focused_editor(
    app: QApplication,
) -> None:
    page = CameraFeaturePage("camera", lambda *_args: None, lambda *_args: None)
    page.show()
    page.set_nodes([_gain_node("1.0")])
    app.processEvents()
    page.activateWindow()
    field = page.findChild(QLineEdit)
    assert field is not None
    QTest.mouseClick(field, Qt.LeftButton)
    field.setText("draft")
    app.processEvents()
    assert page.has_edit_focus() is True

    assert page.update_node(_gain_node("2.0")) is True

    assert field.text() == "draft"
    page.deleteLater()
