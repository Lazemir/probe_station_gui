from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialog

from probe_station_gui.dialogs.camera_settings_dialog import CameraSettingsWidget
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.settings.manager import Settings


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def source() -> _ExposurePolicySource:
    return _ExposurePolicySource()


def test_adjust_exposure_applies_selected_auto_method_without_general_apply(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)

    controls = widget._exposure_controls
    assert controls is not None
    assert _combo_items(controls._control_combo) == ["Manual", "Auto"]
    assert _combo_items(controls._method_combo) == ["Software", "Camera"]
    assert controls._adjust_exposure_button.text() == "Adjust Exposure"

    controls._method_combo.setCurrentText("Camera")

    assert source.updates == []

    controls._adjust_exposure_button.click()

    assert source.updates == [(True, "camera")]
    assert source.once_calls == 0
    assert source.snapshot() == {
        "auto_enabled": True,
        "engine": "camera",
        "busy": False,
        "session_active": False,
    }
    widget.deleteLater()


def test_adjust_exposure_applies_manual_method_then_runs_once(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    source.set_state(auto_enabled=False, engine="software")
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    controls = widget._exposure_controls
    assert controls is not None
    widget._queue_setting("camera", "Gain", 4.5)
    controls._method_combo.setCurrentText("Camera")

    controls._adjust_exposure_button.click()

    assert source.updates == [(False, "camera")]
    assert source.once_calls == 1
    assert source.grabber.setting_updates == []
    assert widget._pending_settings == {("camera", "Gain"): 4.5}
    widget.deleteLater()


def test_adjust_exposure_does_not_apply_control_selection(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    controls = widget._exposure_controls
    assert controls is not None
    controls._control_combo.setCurrentText("Manual")
    controls._method_combo.setCurrentText("Camera")

    controls._adjust_exposure_button.click()

    assert source.updates == [(True, "camera")]
    assert source.once_calls == 0
    assert controls._control_combo.currentText() == "Manual"
    assert controls._pending_policy == (False, "camera")
    widget.deleteLater()


def test_camera_policy_state_does_not_overwrite_unapplied_selection(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    controls = widget._exposure_controls
    assert controls is not None

    controls._method_combo.setCurrentText("Camera")
    source.state_changed.emit(source.snapshot())

    assert controls._method_combo.currentText() == "Camera"
    assert source.updates == []
    widget.deleteLater()


def test_camera_settings_are_written_only_after_apply(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)

    widget._queue_setting("camera", "Gain", 4.5)

    assert source.grabber.setting_updates == []

    assert widget.apply_pending_settings() is True
    assert source.grabber.setting_updates == [("camera", "Gain", 4.5)]
    widget.deleteLater()


def test_synchronous_setting_completion_finishes_inside_apply_call(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    finished: list[bool] = []
    widget.apply_finished.connect(finished.append)

    def complete_immediately(
        map_key: str,
        node_name: str,
        value: object,
        *,
        request_id: str | None = None,
    ) -> None:
        source.grabber.camera_setting_changed.emit(
            {
                "ok": True,
                "message": "Camera setting updated.",
                "map_key": map_key,
                "node_name": node_name,
                "node": {
                    "name": node_name,
                    "value": value,
                    "available": True,
                    "writable": True,
                },
                "request_id": request_id,
            }
        )

    source.grabber.request_camera_setting_update = complete_immediately
    widget._queue_setting("camera", "Gain", 4.5)

    assert widget.apply_pending_settings() is True
    assert finished == [True]
    assert widget._is_applying() is False
    widget.deleteLater()


def test_camera_tab_ignores_snapshots_owned_by_other_camera_clients(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    widget.show()
    widget.refresh()
    request_id = source.grabber.snapshot_requests[-1]["request_id"]

    source.grabber.camera_settings_snapshot_ready.emit(
        {
            "ok": True,
            "request_id": "exposure-monitor-request",
            "maps": [
                {
                    "key": "camera",
                    "nodes": [
                        {
                            "name": "ExposureTime",
                            "type": "float",
                            "value": 1500.0,
                            "available": True,
                            "writable": True,
                        }
                    ],
                }
            ],
        }
    )

    assert widget.has_loaded() is False

    source.grabber.camera_settings_snapshot_ready.emit(
        {
            "ok": True,
            "request_id": request_id,
            "maps": [
                {
                    "key": "camera",
                    "nodes": [
                        {
                            "name": "Gain",
                            "type": "float",
                            "value": 0.0,
                            "available": True,
                            "writable": True,
                        }
                    ],
                }
            ],
        }
    )

    assert widget.has_loaded() is True
    assert widget._page.node_names() == ["Gain"]
    widget.deleteLater()


def test_save_waits_until_all_camera_settings_are_applied(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    dialog = SettingsDialog(
        Settings(),
        camera_settings_source=source.grabber,
        exposure_policy_source=source,
    )
    assert dialog._camera_tab is not None
    dialog._camera_tab._queue_setting("camera", "GainAuto", "Off")
    dialog._camera_tab._queue_setting("camera", "Gain", 3.0)
    dialog.show()

    dialog.accept()

    assert dialog.result() != QDialog.Accepted
    assert source.grabber.setting_updates == [("camera", "GainAuto", "Off")]
    source.finish_camera_setting("GainAuto", "Off")
    assert dialog.result() != QDialog.Accepted
    assert source.grabber.setting_updates[-1] == ("camera", "Gain", 3.0)
    source.finish_camera_setting("Gain", 3.0)

    assert dialog.result() == QDialog.Accepted
    dialog.deleteLater()


def test_low_level_exposure_controls_are_not_duplicated_in_operator_list(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)

    assert "ExposureMode" not in widget.OPERATOR_NODE_NAMES
    assert "ExposureCompensationAuto" not in widget.OPERATOR_NODE_NAMES
    assert "ExposureCompensation" not in widget.OPERATOR_NODE_NAMES

    widget.refresh()
    requested_names = source.grabber.snapshot_requests[-1]["node_names"]
    assert "ExposureMode" in requested_names
    assert "ExposureTime" in requested_names
    widget.deleteLater()


def test_trigger_width_is_only_available_for_camera_method(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    widget.refresh()
    source.grabber.camera_settings_snapshot_ready.emit(
        {
            "ok": True,
            "request_id": source.grabber.snapshot_requests[-1]["request_id"],
            "maps": [
                {
                    "key": "camera",
                    "nodes": [
                        {
                            "name": "ExposureMode",
                            "type": "enum",
                            "value": "Timed",
                            "entries": ["Timed", "TriggerWidth"],
                            "available": True,
                            "writable": True,
                        },
                        {
                            "name": "ExposureTime",
                            "type": "float",
                            "value": 1500.0,
                            "available": True,
                            "writable": True,
                            "unit": "us",
                        },
                    ],
                }
            ],
        }
    )

    controls = widget._exposure_controls
    assert controls is not None
    assert controls._timing_combo.currentText() == "Timed"
    assert controls._timing_combo.isEnabled() is False

    controls._method_combo.setCurrentText("Camera")
    assert controls._timing_combo.isEnabled() is True
    controls._timing_combo.setCurrentText("Trigger width")
    assert source.updates == []
    assert source.grabber.setting_updates == []

    assert widget.apply_pending_settings() is True
    assert source.updates[-1] == (True, "camera")
    assert source.grabber.setting_updates[-1] == (
        "camera",
        "ExposureMode",
        "TriggerWidth",
    )
    source.grabber.camera_setting_changed.emit(
        {
            "ok": True,
            "message": "Camera setting updated.",
            "map_key": "camera",
            "node_name": "ExposureMode",
            "node": {
                "name": "ExposureMode",
                "type": "enum",
                "value": "TriggerWidth",
                "entries": ["Timed", "TriggerWidth"],
                "available": True,
                "writable": True,
            },
            "request_id": source.grabber.setting_request_ids[-1],
        }
    )

    controls._method_combo.setCurrentText("Software")

    assert source.updates[-1] == (True, "camera")
    assert source.grabber.setting_updates[-1] == (
        "camera",
        "ExposureMode",
        "TriggerWidth",
    )
    assert controls._timing_combo.currentText() == "Timed"
    assert widget.apply_pending_settings() is True
    assert source.grabber.setting_updates[-1] == ("camera", "ExposureMode", "Timed")
    source.grabber.camera_setting_changed.emit(
        {
            "ok": True,
            "message": "Camera setting updated.",
            "map_key": "camera",
            "node_name": "ExposureMode",
            "node": {
                "name": "ExposureMode",
                "type": "enum",
                "value": "Timed",
                "entries": ["Timed", "TriggerWidth"],
                "available": True,
                "writable": True,
            },
            "request_id": source.grabber.setting_request_ids[-1],
        }
    )

    assert source.updates[-1] == (True, "software")
    assert controls.isEnabled() is True
    assert controls._timing_combo.isEnabled() is False
    widget.deleteLater()


def test_optical_session_disables_entire_exposure_block(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    controls = widget._exposure_controls
    assert controls is not None

    source.state_changed.emit(
        {"auto_enabled": True, "engine": "software", "busy": True}
    )

    assert controls.isEnabled() is False
    widget.deleteLater()


def test_exposure_time_is_only_manual_and_uses_the_policy_adapter(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    controls = widget._exposure_controls
    assert controls is not None
    widget.refresh()
    source.grabber.camera_settings_snapshot_ready.emit(
        {
            "ok": True,
            "request_id": source.grabber.snapshot_requests[-1]["request_id"],
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
    assert controls._exposure_time_edit.isEnabled() is False

    source.set_state(auto_enabled=False, engine="software")
    assert controls._exposure_time_edit.isEnabled() is True
    controls._exposure_time_edit.setText("2200.5")
    controls._exposure_time_edit.editingFinished.emit()
    assert source.exposure_time_requests == []
    assert widget.apply_pending_settings() is True
    assert source.exposure_time_requests == [2200.5]
    assert source.grabber.setting_updates == []
    source.finish_exposure_time(2201.0)
    assert controls._exposure_time_edit.text() == "2201.0"

    source.set_state(auto_enabled=True, engine="software")
    assert controls._exposure_time_edit.isEnabled() is False
    widget.deleteLater()


def test_exposure_time_latest_pending_write_replaces_stale_completion(
    app: QApplication,
    source: _ExposurePolicySource,
) -> None:
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    controls = widget._exposure_controls
    assert controls is not None
    _load_manual_exposure_time(widget, source)

    controls._exposure_time_edit.setText("2200.0")
    controls._exposure_time_edit.editingFinished.emit()
    controls._exposure_time_edit.setText("2300.0")
    controls._exposure_time_edit.editingFinished.emit()

    assert source.exposure_time_requests == []
    assert widget.apply_pending_settings() is True
    assert source.exposure_time_requests == [2300.0]
    assert source.grabber.setting_updates == []
    source.finish_exposure_time(2300.0)

    assert controls._exposure_time_edit.text() == "2300.0"
    assert source.exposure_time_requests == [2300.0]
    widget.deleteLater()


def _load_manual_exposure_time(
    widget: CameraSettingsWidget,
    source: _ExposurePolicySource,
) -> None:
    widget.refresh()
    source.grabber.camera_settings_snapshot_ready.emit(
        {
            "ok": True,
            "request_id": source.grabber.snapshot_requests[-1]["request_id"],
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


def _combo_items(combo) -> list[str]:
    return [combo.itemText(index) for index in range(combo.count())]


class _Grabber(QObject):
    camera_settings_snapshot_ready = Signal(object)
    camera_setting_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.snapshot_requests: list[dict[str, object]] = []
        self.setting_updates: list[tuple[str, str, object]] = []
        self.setting_request_ids: list[str] = []

    def request_camera_settings_snapshot(self, **kwargs) -> None:
        self.snapshot_requests.append(kwargs)

    def request_camera_setting_update(
        self,
        map_key: str,
        node_name: str,
        value: object,
        *,
        request_id: str | None = None,
    ) -> None:
        self.setting_updates.append((map_key, node_name, value))
        self.setting_request_ids.append(str(request_id or ""))

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
        self.command_finished.emit(
            {
                "accepted": True,
                "auto_enabled": auto_enabled,
                "engine": engine,
            }
        )

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

    def finish_camera_setting(self, node_name: str, value: object) -> None:
        self.grabber.camera_setting_changed.emit(
            {
                "ok": True,
                "message": "Camera setting updated.",
                "map_key": "camera",
                "node_name": node_name,
                "node": {
                    "name": node_name,
                    "value": value,
                    "available": True,
                    "writable": True,
                },
                "request_id": self.grabber.setting_request_ids[-1],
            }
        )

    def set_state(self, **updates: object) -> None:
        self._state.update(updates)
        self.state_changed.emit(self.snapshot())
