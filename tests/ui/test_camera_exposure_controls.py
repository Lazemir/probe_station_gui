from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.camera_settings_dialog import CameraSettingsWidget


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def source() -> _ExposurePolicySource:
    return _ExposurePolicySource()


def test_controls_are_independent_and_once_does_not_change_them(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)

    widget._auto_group.button(0).click()
    widget._camera_engine_button.click()
    widget._once_button.click()

    assert source.updates[-1] == (False, "camera")
    assert source.once_calls == 1
    assert source.snapshot() == {
        "auto_enabled": False,
        "engine": "camera",
        "busy": False,
        "session_active": False,
    }
    widget.deleteLater()


def test_optical_session_disables_entire_exposure_block(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)

    source.state_changed.emit(
        {"auto_enabled": True, "engine": "software", "busy": True}
    )

    assert widget._exposure_group.isEnabled() is False
    widget.deleteLater()


def test_exposure_time_is_only_manual_and_uses_the_policy_adapter(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    widget.refresh()
    source.grabber.camera_settings_snapshot_ready.emit(
        {
            "ok": True,
            "maps": [
                {
                    "key": "camera",
                    "nodes": [
                        {
                            "name": "ExposureTime",
                            "type": "float",
                            "value": 1500.0,
                            "minimum": 10.0,
                            "maximum": 30000.0,
                            "available": True,
                            "writable": True,
                            "unit": "us",
                        }
                    ],
                }
            ],
        }
    )
    assert "ExposureAuto" not in widget.OPERATOR_NODE_NAMES
    assert "ExposureTime" not in widget.OPERATOR_NODE_NAMES
    assert widget._exposure_time_edit.isEnabled() is False

    source.set_state(auto_enabled=False, engine="software")
    assert widget._exposure_time_edit.isEnabled() is True
    widget._exposure_time_edit.setText("2200.5")
    widget._exposure_time_edit.editingFinished.emit()
    assert source.exposure_time_requests == [2200.5]
    assert source.grabber.setting_updates == []
    source.finish_exposure_time(2201.0)
    assert widget._exposure_time_edit.text() == "2201.0"

    source.set_state(auto_enabled=True, engine="software")
    assert widget._exposure_time_edit.isEnabled() is False
    widget.deleteLater()


def test_exposure_time_latest_pending_write_replaces_stale_completion(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    _load_manual_exposure_time(widget, source)

    widget._exposure_time_edit.setText("2200.0")
    widget._exposure_time_edit.editingFinished.emit()
    widget._exposure_time_edit.setText("2300.0")
    widget._exposure_time_edit.editingFinished.emit()

    assert source.exposure_time_requests == [2200.0]
    assert source.grabber.setting_updates == []
    source.finish_exposure_time(2200.0)

    assert widget._exposure_time_edit.text() == "2300.0"
    assert source.exposure_time_requests == [2200.0, 2300.0]
    widget.deleteLater()


def _load_manual_exposure_time(
    widget: CameraSettingsWidget,
    source: _ExposurePolicySource,
) -> None:
    widget.refresh()
    source.grabber.camera_settings_snapshot_ready.emit(
        {
            "ok": True,
            "maps": [
                {
                    "key": "camera",
                    "nodes": [
                        {
                            "name": "ExposureTime",
                            "type": "float",
                            "value": 1500.0,
                            "minimum": 10.0,
                            "maximum": 30000.0,
                            "available": True,
                            "writable": True,
                            "unit": "us",
                        }
                    ],
                }
            ],
        }
    )
    source.set_state(auto_enabled=False, engine="software")


def _exposure_time_changed_payload(value: float) -> dict[str, object]:
    return {
        "ok": True,
        "message": "Camera setting updated.",
        "map_key": "camera",
        "node_name": "ExposureTime",
        "node": {
            "name": "ExposureTime",
            "type": "float",
            "value": value,
            "available": True,
            "writable": True,
        },
    }


class _Grabber(QObject):
    camera_settings_snapshot_ready = Signal(object)
    camera_setting_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.snapshot_requests: list[dict[str, object]] = []
        self.setting_updates: list[tuple[str, str, object]] = []

    def request_camera_settings_snapshot(self, **kwargs) -> None:
        self.snapshot_requests.append(kwargs)

    def request_camera_setting_update(
        self,
        map_key: str,
        node_name: str,
        value: object,
    ) -> None:
        self.setting_updates.append((map_key, node_name, value))

    def request_camera_command_execute(self, _map_key: str, _node_name: str) -> None:
        pass


class _ExposurePolicySource(QObject):
    state_changed = Signal(object)
    command_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.grabber = _Grabber()
        self.updates: list[tuple[bool, str]] = []
        self.once_calls = 0
        self.exposure_time_requests: list[float] = []
        self._state: dict[str, object] = {
            "auto_enabled": True,
            "engine": "software",
            "busy": False,
            "session_active": False,
        }

    def snapshot(self) -> dict[str, object]:
        return dict(self._state)

    def request_update(self, auto_enabled: bool, engine: str) -> None:
        self.updates.append((auto_enabled, engine))
        self.set_state(auto_enabled=auto_enabled, engine=engine)

    def request_once(self) -> None:
        self.once_calls += 1

    def request_exposure_time(self, value: float) -> None:
        self.exposure_time_requests.append(float(value))

    def finish_exposure_time(self, value: float) -> None:
        payload = _exposure_time_changed_payload(value)
        self.command_finished.emit(
            {
                "accepted": True,
                "operation": "manual_exposure_write",
                "message": payload["message"],
                "nodes": [payload["node"]],
            }
        )

    def set_state(self, **updates: object) -> None:
        self._state.update(updates)
        self.state_changed.emit(self.snapshot())
