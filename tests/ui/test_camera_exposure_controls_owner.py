from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.camera_exposure_controls import (
    CameraExposureControls,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_manual_exposure_actions_preserve_mode_policy_time_order(
    app: QApplication,
) -> None:
    source = _ExposurePolicySource()
    controls = CameraExposureControls(source)
    controls.queue_setting("ExposureMode", "Timed")
    controls.queue_setting("ExposureTime", 2200.0)
    controls._control_combo.setCurrentText("Manual")

    assert controls.build_apply_actions() == [
        ("setting", "camera", "ExposureMode", "Timed"),
        ("policy", False, "software"),
        ("setting", "camera", "ExposureTime", 2200.0),
    ]
    controls.deleteLater()


def test_setting_completion_only_clears_the_matching_latest_draft(
    app: QApplication,
) -> None:
    source = _ExposurePolicySource()
    controls = CameraExposureControls(source)
    controls.queue_setting("ExposureTime", 2200.0)
    controls.queue_setting("ExposureTime", 2300.0)

    controls.complete_setting("ExposureTime", 2200.0)

    assert controls.pending_count() == 1
    controls.complete_setting("ExposureTime", 2300.0)
    assert controls.pending_count() == 0
    controls.deleteLater()


class _ExposurePolicySource(QObject):
    state_changed = Signal(object)
    command_finished = Signal(object)

    def snapshot(self) -> dict[str, object]:
        return {
            "auto_enabled": True,
            "engine": "software",
            "busy": False,
            "session_active": False,
        }

    def request_update(self, _auto_enabled: bool, _engine: str) -> None:
        pass

    def request_once(self) -> None:
        pass

    def request_exposure_time(self, _value: float) -> None:
        pass
