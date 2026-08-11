"""Exposure-policy controls and ordered camera exposure drafts."""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox


NodePayload = dict[str, object]
ApplyAction = tuple[object, ...]


class CameraExposureControls(QGroupBox):
    """Own exposure widgets, policy state, and their ordered pending actions."""

    pending_changed = Signal()
    adjust_requested = Signal(object, bool)
    command_finished = Signal(object)
    status_requested = Signal(str)

    def __init__(
        self,
        exposure_policy_source: object,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Exposure", parent)
        self._exposure_policy_source = exposure_policy_source
        self._policy_state: dict[str, object] = {}
        self._exposure_time_node: NodePayload | None = None
        self._exposure_mode_node: NodePayload | None = None
        self._pending_settings: dict[str, object] = {}
        self._pending_policy: tuple[bool, str] | None = None
        self._updating_controls = False
        self._apply_busy = False

        self._create_controls()
        exposure_policy_source.state_changed.connect(
            self._on_exposure_policy_state_changed
        )
        exposure_policy_source.command_finished.connect(self.command_finished)
        self._apply_exposure_policy_state(self._exposure_policy_snapshot())

    def extend_snapshot_node_names(self, names: list[str]) -> list[str]:
        result = list(names)
        for node_name in ("ExposureMode", "ExposureTime"):
            if node_name not in result:
                result.append(node_name)
        return result

    def update_nodes(self, nodes: list[object]) -> None:
        self._update_exposure_time_node(nodes)
        self._update_exposure_mode_node(nodes)

    def update_node(self, node: NodePayload) -> bool:
        node_name = str(node.get("name") or "")
        if node_name == "ExposureTime":
            self._update_exposure_time_node([node])
            return True
        if node_name == "ExposureMode":
            self._update_exposure_mode_node([node])
            return True
        return False

    def queue_setting(self, node_name: str, value: object) -> None:
        if node_name not in {"ExposureMode", "ExposureTime"}:
            raise KeyError(node_name)
        self._pending_settings[node_name] = value
        self.pending_changed.emit()

    def queue_current_editor_value(self) -> None:
        if self._exposure_time_edit.hasFocus():
            self._queue_exposure_time()

    def pending_count(self) -> int:
        return len(self._pending_settings) + int(self._pending_policy is not None)

    def build_apply_actions(self) -> list[ApplyAction]:
        mode_present = "ExposureMode" in self._pending_settings
        mode_value = self._pending_settings.get("ExposureMode")
        exposure_present = "ExposureTime" in self._pending_settings
        exposure_value = self._pending_settings.get("ExposureTime")
        actual_auto = bool(self._policy_state.get("auto_enabled", True))
        desired_auto, desired_engine = self._selected_exposure_policy()
        actions: list[ApplyAction] = []
        if mode_present and str(mode_value) == "Timed":
            actions.append(("setting", "camera", "ExposureMode", mode_value))
        if exposure_present and not actual_auto:
            actions.append(("setting", "camera", "ExposureTime", exposure_value))
        if self._pending_policy is not None:
            actions.append(("policy", desired_auto, desired_engine))
        if mode_present and str(mode_value) != "Timed":
            actions.append(("setting", "camera", "ExposureMode", mode_value))
        if exposure_present and actual_auto and not desired_auto:
            actions.append(("setting", "camera", "ExposureTime", exposure_value))
        return actions

    def begin_apply(self) -> None:
        self._apply_busy = True
        self._apply_exposure_policy_state(self._policy_state)

    def finish_apply(self) -> None:
        self._apply_busy = False
        self._apply_exposure_policy_state(self._exposure_policy_snapshot())

    def request_policy(self, auto_enabled: bool, engine: str) -> None:
        self._exposure_policy_source.request_update(bool(auto_enabled), str(engine))

    def request_exposure_time(self, value: object) -> None:
        self._exposure_policy_source.request_exposure_time(float(value))

    def request_once(self) -> None:
        self._exposure_policy_source.request_once()

    def complete_policy(self, applied_policy: tuple[bool, str]) -> None:
        if self._pending_policy == applied_policy:
            self._pending_policy = None
        self._policy_state.update(self._exposure_policy_snapshot())

    def complete_setting(
        self,
        node_name: str,
        applied_value: object,
    ) -> None:
        if self._pending_settings.get(node_name) == applied_value:
            self._pending_settings.pop(node_name, None)

    def refresh_policy_state(self) -> None:
        self._apply_exposure_policy_state(self._exposure_policy_snapshot())

    def _create_controls(self) -> None:
        controls = QGridLayout(self)
        controls.setColumnStretch(1, 1)
        controls.setHorizontalSpacing(12)
        controls.setVerticalSpacing(6)

        controls.addWidget(QLabel("Control", self), 0, 0)
        self._control_combo = QComboBox(self)
        self._control_combo.addItem("Manual", False)
        self._control_combo.addItem("Auto", True)
        controls.addWidget(self._control_combo, 0, 1)

        controls.addWidget(QLabel("Method", self), 1, 0)
        self._method_combo = QComboBox(self)
        self._method_combo.addItem("Software", "software")
        self._method_combo.addItem("Camera", "camera")
        controls.addWidget(self._method_combo, 1, 1)

        controls.addWidget(QLabel("Timing", self), 2, 0)
        self._timing_combo = QComboBox(self)
        self._timing_combo.addItem("Timed", "Timed")
        self._timing_combo.setToolTip(
            "Trigger width is available only with the Camera method."
        )
        self._timing_combo.setEnabled(False)
        controls.addWidget(self._timing_combo, 2, 1)

        controls.addWidget(QLabel("Exposure time", self), 3, 0)
        exposure_time_row = QHBoxLayout()
        self._exposure_time_edit = QLineEdit(self)
        self._exposure_time_edit.setFixedWidth(96)
        self._exposure_time_edit.setPlaceholderText("Exposure time")
        self._exposure_time_unit = QLabel(self)
        exposure_time_row.addWidget(self._exposure_time_edit)
        exposure_time_row.addWidget(self._exposure_time_unit)
        exposure_time_row.addStretch(1)
        controls.addLayout(exposure_time_row, 3, 1)

        self._adjust_exposure_button = QPushButton("Adjust Exposure", self)
        self._adjust_exposure_button.setToolTip(
            "Run one exposure adjustment with the selected method."
        )
        controls.addWidget(self._adjust_exposure_button, 4, 1, alignment=Qt.AlignLeft)

        self._control_combo.currentIndexChanged.connect(
            self._request_exposure_policy_update
        )
        self._method_combo.currentIndexChanged.connect(
            self._request_exposure_policy_update
        )
        self._timing_combo.currentIndexChanged.connect(self._queue_exposure_mode)
        self._adjust_exposure_button.clicked.connect(self._request_exposure_once)
        self._exposure_time_edit.editingFinished.connect(self._queue_exposure_time)

    def _exposure_policy_snapshot(self) -> dict[str, object]:
        try:
            state = self._exposure_policy_source.snapshot()
        except Exception:
            return {}
        return dict(state) if isinstance(state, Mapping) else {}

    def _on_exposure_policy_state_changed(self, state: object) -> None:
        if isinstance(state, Mapping):
            self._apply_exposure_policy_state(dict(state))

    def _apply_exposure_policy_state(self, state: Mapping[str, object]) -> None:
        self._policy_state.update(state)
        auto_enabled = bool(self._policy_state.get("auto_enabled", True))
        engine = str(self._policy_state.get("engine") or "software")
        if self._pending_policy is None and not self._apply_busy:
            self._updating_controls = True
            try:
                self._set_combo_data(self._control_combo, auto_enabled)
                self._set_combo_data(
                    self._method_combo,
                    "camera" if engine == "camera" else "software",
                )
            finally:
                self._updating_controls = False
        busy = (
            bool(self._policy_state.get("busy", False))
            or bool(self._policy_state.get("session_active", False))
            or self._apply_busy
        )
        self.setEnabled(not busy)
        self._sync_timing_control()
        self._set_exposure_time_edit_enabled()

    def _request_exposure_policy_update(self, _index: int) -> None:
        if self._updating_controls or not self.isEnabled():
            return
        engine = str(self._method_combo.currentData() or "software")
        auto_enabled = bool(self._control_combo.currentData())
        if (
            engine == "software"
            and str(self._timing_combo.currentData() or "Timed") != "Timed"
        ):
            node = self._exposure_mode_node
            if not (node and node.get("available") and node.get("writable")):
                self.status_requested.emit("Software exposure requires timed exposure.")
                self._apply_exposure_policy_state(self._exposure_policy_snapshot())
                return
            self._use_timed_exposure_mode()
        current = (
            bool(self._policy_state.get("auto_enabled", True)),
            str(self._policy_state.get("engine") or "software"),
        )
        selected = (auto_enabled, engine)
        self._pending_policy = None if selected == current else selected
        self.pending_changed.emit()
        self._sync_timing_control()
        self._set_exposure_time_edit_enabled()

    def _request_exposure_once(self) -> None:
        if not self.isEnabled() or self._apply_busy:
            return
        selected_engine = self._selected_exposure_engine()
        actual_auto = bool(self._policy_state.get("auto_enabled", True))
        actual_engine = str(self._policy_state.get("engine") or "software")
        actions: list[ApplyAction] = []
        if (
            selected_engine == "software"
            and self._pending_settings.get("ExposureMode") == "Timed"
        ):
            actions.append(("setting", "camera", "ExposureMode", "Timed"))
        method_changed = selected_engine != actual_engine
        if method_changed:
            actions.append(("policy", actual_auto, selected_engine))
        if not actions:
            self.status_requested.emit("Adjusting exposure.")
            self.request_once()
            return
        run_once = not (actual_auto and method_changed)
        self.adjust_requested.emit(actions, run_once)

    def _update_exposure_mode_node(self, nodes: list[object]) -> None:
        node = next(
            (
                item
                for item in nodes
                if isinstance(item, dict)
                and str(item.get("name") or "") == "ExposureMode"
            ),
            None,
        )
        if node is None:
            return
        self._exposure_mode_node = dict(node)
        entries = [str(entry) for entry in node.get("entries") or []]
        value = str(node.get("value") or "")
        if value and value not in entries:
            entries.insert(0, value)
        self._updating_controls = True
        try:
            self._timing_combo.clear()
            for entry in entries:
                self._timing_combo.addItem(
                    "Trigger width" if entry == "TriggerWidth" else entry,
                    entry,
                )
            if not entries:
                self._timing_combo.addItem("Timed", "Timed")
            self._set_combo_data(self._timing_combo, value or "Timed")
        finally:
            self._updating_controls = False
        self._sync_timing_control()

    def _queue_exposure_mode(self, _index: int) -> None:
        if self._updating_controls or not self._timing_combo.isEnabled():
            return
        value = str(self._timing_combo.currentData() or "Timed")
        if value == "TriggerWidth" and self._selected_exposure_engine() != "camera":
            self._use_timed_exposure_mode()
            return
        self.queue_setting("ExposureMode", value)

    def _sync_timing_control(self) -> None:
        node = self._exposure_mode_node
        camera_method = self._selected_exposure_engine() == "camera"
        writable = bool(node and node.get("available") and node.get("writable"))
        has_choices = self._timing_combo.count() > 1
        if not camera_method:
            self._use_timed_exposure_mode()
        self._timing_combo.setEnabled(
            self.isEnabled() and camera_method and writable and has_choices
        )

    def _use_timed_exposure_mode(self) -> None:
        current = str(self._timing_combo.currentData() or "Timed")
        if current == "Timed":
            return
        self._updating_controls = True
        try:
            self._set_combo_data(self._timing_combo, "Timed")
        finally:
            self._updating_controls = False
        node = self._exposure_mode_node
        if node and node.get("available") and node.get("writable"):
            self.queue_setting("ExposureMode", "Timed")

    def _selected_exposure_engine(self) -> str:
        return str(self._method_combo.currentData() or "software")

    def _selected_exposure_policy(self) -> tuple[bool, str]:
        return bool(self._control_combo.currentData()), self._selected_exposure_engine()

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _update_exposure_time_node(self, nodes: list[object]) -> None:
        node = next(
            (
                item
                for item in nodes
                if isinstance(item, dict)
                and str(item.get("name") or "") == "ExposureTime"
            ),
            None,
        )
        if node is None:
            return
        self._exposure_time_node = dict(node)
        if not self._exposure_time_edit.hasFocus():
            value = node.get("value")
            self._exposure_time_edit.setText("" if value is None else str(value))
        minimum = self._exposure_time_value(node.get("minimum"))
        maximum = self._exposure_time_value(node.get("maximum"))
        validator = QDoubleValidator(self._exposure_time_edit)
        if minimum is not None:
            validator.setBottom(minimum)
        if maximum is not None:
            validator.setTop(maximum)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._exposure_time_edit.setValidator(validator)
        self._exposure_time_unit.setText(str(node.get("unit") or ""))
        self._set_exposure_time_edit_enabled()

    def _set_exposure_time_edit_enabled(self) -> None:
        node = self._exposure_time_node
        manual = not bool(self._control_combo.currentData())
        busy = bool(self._policy_state.get("busy", False)) or bool(
            self._policy_state.get("session_active", False)
        )
        writable = bool(node and node.get("available") and node.get("writable"))
        self._exposure_time_edit.setEnabled(
            manual and not busy and not self._apply_busy and writable
        )

    def _queue_exposure_time(self) -> None:
        if not self._exposure_time_edit.isEnabled():
            return
        if not self._exposure_time_edit.hasAcceptableInput():
            return
        value = self._exposure_time_value(self._exposure_time_edit.text())
        if value is None:
            return
        self.queue_setting("ExposureTime", value)

    @staticmethod
    def _exposure_time_value(value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


__all__ = ["CameraExposureControls"]
