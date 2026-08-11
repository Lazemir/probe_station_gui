"""Operator-facing GenICam camera controls."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from probe_station_gui.dialogs.camera_exposure_controls import (
    CameraExposureControls,
)
from probe_station_gui.dialogs.camera_feature_page import CameraFeaturePage


NodePayload = dict[str, Any]


class CameraSettingsWidget(QWidget):
    """Operator-facing camera controls for probe-station work."""

    apply_finished = Signal(bool)

    LIVE_REFRESH_INTERVAL_MS = 1500

    OPERATOR_NODE_NAMES = (
        "AcquisitionMode",
        "TriggerSelector",
        "TriggerMode",
        "TriggerSource",
        "TriggerActivation",
        "TriggerOverlap",
        "TriggerDelay",
        "TriggerSoftware",
        "GainAuto",
        "Gain",
        "BlackLevel",
        "BalanceWhiteAuto",
        "BalanceRatioSelector",
        "BalanceRatio",
    )

    def __init__(
        self,
        grabber: object,
        parent: QWidget | None = None,
        *,
        exposure_policy_source: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._grabber = grabber
        self._pending_settings: dict[tuple[str, str], object] = {}
        self._applying_settings: dict[tuple[str, str], object] = {}
        self._snapshot_pending = False
        self._snapshot_request_id: str | None = None
        self._snapshot_show_status = False
        self._status_text = ""
        self._loaded_once = False
        self._apply_actions: list[tuple[object, ...]] = []
        self._active_apply_action: tuple[object, ...] | None = None
        self._active_setting_request_id: str | None = None
        self._adjust_exposure_apply = False
        self._adjust_once_after_apply = False

        layout = QVBoxLayout(self)

        self._exposure_controls: CameraExposureControls | None = None
        if exposure_policy_source is not None:
            self._exposure_controls = CameraExposureControls(
                exposure_policy_source,
                self,
            )
            self._exposure_controls.pending_changed.connect(self._set_pending_status)
            self._exposure_controls.adjust_requested.connect(
                self._start_exposure_adjustment
            )
            self._exposure_controls.command_finished.connect(
                self._on_exposure_policy_command_finished
            )
            self._exposure_controls.status_requested.connect(self._set_status)
            layout.addWidget(self._exposure_controls)

        self._page = CameraFeaturePage(
            "camera",
            self._queue_setting,
            self._execute_command,
            self,
        )
        layout.addWidget(self._page, 1)

        grabber.camera_settings_snapshot_ready.connect(self._on_snapshot)
        grabber.camera_setting_changed.connect(self._on_setting_changed)

        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.setInterval(self.LIVE_REFRESH_INTERVAL_MS)
        self._live_refresh_timer.timeout.connect(self._refresh_live_snapshot)

    def has_loaded(self) -> bool:
        return self._loaded_once

    def refresh(self) -> None:
        if self._has_pending_changes() or self._is_applying():
            self._set_status("Apply pending camera settings before refreshing.")
            return
        self._request_snapshot(
            show_status=True,
            node_names=self._snapshot_node_names(),
        )

    def _refresh_live_snapshot(self) -> None:
        if not self._loaded_once:
            return
        if not self.isVisible():
            return
        if self._has_pending_changes() or self._is_applying():
            return
        if self._has_edit_focus():
            return
        node_names = self._snapshot_node_names(self._page.node_names())
        if not node_names:
            return
        self._request_snapshot(
            show_status=False,
            node_names=node_names,
        )

    def _request_snapshot(
        self,
        *,
        show_status: bool,
        node_names: list[str] | None = None,
    ) -> None:
        if self._snapshot_pending:
            return
        self._snapshot_pending = True
        self._snapshot_request_id = uuid.uuid4().hex
        self._snapshot_show_status = show_status
        if show_status:
            self._set_status("Loading camera controls.")
        self._grabber.request_camera_settings_snapshot(
            map_key="camera",
            node_names=node_names or self._snapshot_node_names(),
            request_id=self._snapshot_request_id,
        )

    def _has_edit_focus(self) -> bool:
        return self._page.has_edit_focus()

    def _on_snapshot(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if str(payload.get("request_id") or "") != str(self._snapshot_request_id or ""):
            return
        self._snapshot_pending = False
        self._snapshot_request_id = None
        show_status = self._snapshot_show_status
        self._snapshot_show_status = False
        if self._has_pending_changes() or self._is_applying():
            self._set_pending_status()
            return
        if not payload.get("ok", False):
            self._set_status(str(payload.get("message") or "Camera error."))
            return

        maps = [
            map_payload
            for map_payload in payload.get("maps") or []
            if isinstance(map_payload, dict)
        ]
        camera_map = next(
            (
                map_payload
                for map_payload in maps
                if str(map_payload.get("key") or "") == "camera"
            ),
            None,
        )
        first_load = not self._loaded_once
        if camera_map is not None:
            nodes = camera_map.get("nodes")
            camera_nodes = nodes if isinstance(nodes, list) else []
            if self._exposure_controls is not None:
                self._exposure_controls.update_nodes(camera_nodes)
            operator_nodes = [
                node
                for node in camera_nodes
                if isinstance(node, dict)
                and str(node.get("name") or "")
                not in {"ExposureAuto", "ExposureMode", "ExposureTime"}
            ]
            if self._loaded_once:
                for node in operator_nodes:
                    if isinstance(node, dict):
                        self._page.update_node(node)
            else:
                self._page.set_nodes(operator_nodes)

        self._loaded_once = True
        if not self._live_refresh_timer.isActive():
            self._live_refresh_timer.start()
        if first_load or show_status:
            loaded_count = len(self._page.node_names())
            if loaded_count:
                self._set_status(f"Loaded {loaded_count} camera controls.")
            else:
                self._set_status("No supported camera controls found.")

    def _on_setting_changed(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self._set_status("Camera setting response was invalid.")
            return
        map_key = str(payload.get("map_key") or "")
        node_name = str(payload.get("node_name") or "")
        key = (map_key, node_name)
        active = self._active_apply_action
        if active is None or active[:3] != ("setting", map_key, node_name):
            self._set_status(str(payload.get("message") or "Camera setting updated."))
            if payload.get("ok", False):
                node = payload.get("node")
                if isinstance(node, dict):
                    self._update_pages_for_node(map_key, node)
            return
        if node_name != "ExposureTime" and str(payload.get("request_id") or "") != str(
            self._active_setting_request_id or ""
        ):
            return

        applied_value = active[3]
        self._applying_settings.pop(key, None)
        self._active_apply_action = None
        self._active_setting_request_id = None
        if not payload.get("ok", False):
            self._set_status(str(payload.get("message") or "Camera setting failed."))
            self._finish_apply(False)
            return
        node = payload.get("node")
        node_payload = node if isinstance(node, dict) else None
        if node_payload is not None:
            self._update_pages_for_node(map_key, node_payload)
        if self._exposure_controls is not None and node_name in {
            "ExposureMode",
            "ExposureTime",
        }:
            self._exposure_controls.complete_setting(
                node_name,
                applied_value,
            )
        elif self._pending_settings.get(key) == applied_value:
            self._pending_settings.pop(key, None)
        self._run_next_apply_action()

    def _queue_setting(self, map_key: str, node_name: str, value: object) -> None:
        if not node_name:
            return
        if self._exposure_controls is not None and node_name in {
            "ExposureMode",
            "ExposureTime",
        }:
            self._exposure_controls.queue_setting(node_name, value)
            return
        self._pending_settings[(map_key, node_name)] = value
        self._set_pending_status()

    def _set_pending_status(self) -> None:
        pending_count = len(self._pending_settings)
        if self._exposure_controls is not None:
            pending_count += self._exposure_controls.pending_count()
        if pending_count == 1:
            self._set_status("1 camera setting pending.")
        else:
            self._set_status(f"{pending_count} camera settings pending.")

    def apply_pending_settings(self) -> bool:
        self._page.queue_current_editor_value()
        if self._exposure_controls is not None:
            self._exposure_controls.queue_current_editor_value()
        if self._is_applying() or not self._has_pending_changes():
            return False
        self._apply_actions = self._build_apply_actions()
        if not self._apply_actions:
            return False
        self._adjust_exposure_apply = False
        self._adjust_once_after_apply = False
        self._page.setEnabled(False)
        if self._exposure_controls is not None:
            self._exposure_controls.begin_apply()
        self._set_status(f"Applying {len(self._apply_actions)} camera settings.")
        self._run_next_apply_action()
        return True

    def _build_apply_actions(self) -> list[tuple[object, ...]]:
        actions: list[tuple[object, ...]] = [
            ("setting", key[0], key[1], value)
            for key, value in self._pending_settings.items()
        ]
        if self._exposure_controls is not None:
            actions.extend(self._exposure_controls.build_apply_actions())
        return actions

    def _run_next_apply_action(self) -> None:
        if not self._apply_actions:
            self._finish_apply(True)
            return
        action = self._apply_actions.pop(0)
        self._active_apply_action = action
        if action[0] == "policy":
            if self._exposure_controls is None:
                self._set_status("Camera exposure policy is unavailable.")
                self._finish_apply(False)
                return
            self._exposure_controls.request_policy(bool(action[1]), str(action[2]))
            return

        _kind, map_key, node_name, value = action
        key = (str(map_key), str(node_name))
        self._applying_settings[key] = value
        if key == ("camera", "ExposureTime") and self._exposure_controls is not None:
            self._exposure_controls.request_exposure_time(value)
            return
        request_id = uuid.uuid4().hex
        self._active_setting_request_id = request_id
        self._grabber.request_camera_setting_update(
            key[0],
            key[1],
            value,
            request_id=request_id,
        )

    def _finish_apply(self, success: bool) -> None:
        adjust_exposure = self._adjust_exposure_apply
        run_once = bool(success and self._adjust_once_after_apply)
        self._adjust_exposure_apply = False
        self._adjust_once_after_apply = False
        self._apply_actions.clear()
        self._active_apply_action = None
        self._active_setting_request_id = None
        self._applying_settings.clear()
        self._page.setEnabled(True)
        if self._exposure_controls is not None:
            self._exposure_controls.finish_apply()
        if run_once:
            self._set_status("Adjusting exposure.")
        elif success and adjust_exposure:
            self._set_status("Exposure method applied.")
        elif success:
            self._set_status("Camera settings applied.")
        self.apply_finished.emit(bool(success))
        if run_once and self._exposure_controls is not None:
            self._exposure_controls.request_once()

    def _start_exposure_adjustment(
        self,
        actions: object,
        run_once: bool,
    ) -> None:
        if self._is_applying() or not isinstance(actions, list) or not actions:
            return
        self._apply_actions = [
            tuple(action) for action in actions if isinstance(action, tuple)
        ]
        if not self._apply_actions:
            return
        self._adjust_exposure_apply = True
        self._adjust_once_after_apply = bool(run_once)
        self._page.setEnabled(False)
        if self._exposure_controls is not None:
            self._exposure_controls.begin_apply()
        self._set_status("Applying exposure method.")
        self._run_next_apply_action()

    def _has_pending_changes(self) -> bool:
        return bool(
            self._pending_settings
            or (
                self._exposure_controls is not None
                and self._exposure_controls.pending_count() > 0
            )
        )

    def _is_applying(self) -> bool:
        return self._active_apply_action is not None or bool(self._apply_actions)

    def _update_pages_for_node(self, map_key: str, node: NodePayload) -> None:
        if map_key != "camera":
            return
        if self._exposure_controls is not None and self._exposure_controls.update_node(
            node
        ):
            return
        self._page.update_node(node)

    def _execute_command(self, map_key: str, node_name: str) -> None:
        if not node_name:
            return
        self._set_status(f"Executing {node_name}.")
        self._grabber.request_camera_command_execute(map_key, node_name)

    def _snapshot_node_names(
        self,
        operator_nodes: list[str] | None = None,
    ) -> list[str]:
        names = list(operator_nodes or self.OPERATOR_NODE_NAMES)
        if self._exposure_controls is not None:
            names = self._exposure_controls.extend_snapshot_node_names(names)
        return names

    def _on_exposure_policy_command_finished(self, result: object) -> None:
        if not isinstance(result, Mapping):
            return
        if result.get("operation") == "manual_exposure_write":
            self._on_exposure_time_write_finished(result)
            return
        active = self._active_apply_action
        if active is not None and active[0] == "policy":
            if not bool(result.get("accepted", False)):
                self._set_status(
                    str(result.get("message") or "Camera exposure failed.")
                )
                self._finish_apply(False)
                return
            if self._exposure_controls is not None:
                self._exposure_controls.complete_policy(
                    (bool(active[1]), str(active[2]))
                )
            self._active_apply_action = None
            self._run_next_apply_action()
            return
        if not bool(result.get("accepted", False)):
            self._set_status(str(result.get("message") or "Camera exposure failed."))
            if self._exposure_controls is not None:
                self._exposure_controls.refresh_policy_state()

    def _on_exposure_time_write_finished(
        self,
        result: Mapping[str, object],
    ) -> None:
        nodes = result.get("nodes")
        node = (
            next(
                (
                    item
                    for item in nodes
                    if isinstance(item, dict)
                    and str(item.get("name") or "") == "ExposureTime"
                ),
                None,
            )
            if isinstance(nodes, list)
            else None
        )
        self._on_setting_changed(
            {
                "ok": bool(result.get("accepted", False)),
                "message": str(
                    result.get("message") or "Camera exposure time updated."
                ),
                "map_key": "camera",
                "node_name": "ExposureTime",
                "node": node,
            }
        )
        if (
            not bool(result.get("accepted", False))
            and self._exposure_controls is not None
        ):
            self._exposure_controls.refresh_policy_state()

    def _set_status(self, message: str) -> None:
        self._status_text = message


__all__ = ["CameraSettingsWidget"]
