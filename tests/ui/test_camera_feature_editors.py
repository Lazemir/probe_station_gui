from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLineEdit

from probe_station_gui.dialogs.camera_feature_editors import CameraFeatureEditors


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_numeric_editor_queues_valid_value_through_its_page_interface(
    app: QApplication,
) -> None:
    applied: list[tuple[str, str, object]] = []
    editors = CameraFeatureEditors(
        "camera",
        lambda map_key, node_name, value: applied.append((map_key, node_name, value)),
        lambda _map_key, _node_name: None,
    )
    editors.set_nodes(
        [
            {
                "name": "Gain",
                "display_name": "Gain",
                "type": "float",
                "value": 1.0,
                "minimum": 0.0,
                "maximum": 10.0,
                "increment": 0.1,
                "available": True,
                "writable": True,
            }
        ]
    )

    field = editors.findChild(QLineEdit)
    assert field is not None
    field.setText("2.5")
    field.editingFinished.emit()

    assert applied == [("camera", "Gain", "2.5")]
    editors.deleteLater()
