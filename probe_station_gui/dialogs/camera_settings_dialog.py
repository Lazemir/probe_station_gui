"""Operator-facing GenICam camera controls."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedSlider as QSlider,
)


NodePayload = dict[str, Any]


class _FeatureListPage(QWidget):
    SLIDER_STEPS = 10000

    def __init__(
        self,
        map_key: str,
        apply_callback: Callable[[str, str, object], None],
        execute_callback: Callable[[str, str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._map_key = map_key
        self._apply_callback = apply_callback
        self._execute_callback = execute_callback
        self._nodes: list[NodePayload] = []
        self._nodes_by_name: dict[str, NodePayload] = {}
        self._widgets_by_name: dict[str, QWidget] = {}
        self._editors_by_name: dict[str, QWidget] = {}
        self._updating = False
        self._layout_dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setFrameShape(QFrame.NoFrame)
        self._scroll_area.setWidgetResizable(True)
        layout.addWidget(self._scroll_area, 1)

        self._content = QWidget(self)
        self._grid = QGridLayout(self._content)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)
        self._scroll_area.setWidget(self._content)

    def map_key(self) -> str:
        return self._map_key

    def showEvent(self, event: object) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._sync_layout_if_visible()

    def node_names(self) -> list[str]:
        return [
            str(node.get("name") or "")
            for node in self._nodes
            if str(node.get("name") or "")
        ]

    def set_nodes(self, nodes: list[NodePayload]) -> None:
        old_signature = self._layout_signature(self._nodes)
        new_nodes = [
            node for node in nodes if str(node.get("type") or "") != "category"
        ]
        new_signature = self._layout_signature(new_nodes)
        self._nodes = new_nodes
        self._nodes_by_name = {
            str(node.get("name") or ""): node
            for node in self._nodes
            if str(node.get("name") or "")
        }
        if not self.isVisible():
            self._layout_dirty = True
            return
        if self._widgets_by_name and old_signature == new_signature:
            for node in self._nodes:
                self._update_editor(node)
            self._layout_dirty = False
            return
        self._rebuild()
        self._layout_dirty = False

    def update_node(self, node: NodePayload) -> bool:
        node_name = str(node.get("name") or "")
        if not node_name:
            return False
        for index, existing in enumerate(self._nodes):
            if str(existing.get("name") or "") != node_name:
                continue
            merged = dict(existing)
            merged.update(node)
            if self._layout_signature([existing]) != self._layout_signature([merged]):
                updated_nodes = list(self._nodes)
                updated_nodes[index] = merged
                self.set_nodes(updated_nodes)
                return True
            self._nodes[index] = merged
            self._nodes_by_name[node_name] = merged
            if self.isVisible():
                self._update_editor(merged)
            else:
                self._layout_dirty = True
            return True
        return False

    def queue_current_editor_value(self) -> None:
        focused = QApplication.focusWidget()
        for node_name, editor in self._editors_by_name.items():
            if self._editor_has_focus(editor, focused):
                self._queue_editor_value(node_name, editor)
                return

    def has_edit_focus(self) -> bool:
        focused = QApplication.focusWidget()
        return any(
            self._editor_has_focus(editor, focused)
            for editor in self._editors_by_name.values()
        )

    def _rebuild(self) -> None:
        if not self.isVisible():
            self._layout_dirty = True
            return
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._widgets_by_name.clear()
        self._editors_by_name.clear()

        for row, node in enumerate(self._nodes):
            label = QLabel(self._display_name(node), self._content)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setToolTip(str(node.get("description") or node.get("tooltip") or ""))
            self._grid.addWidget(label, row, 0)
            editor, span = self._editor_for_node(node)
            self._grid.addWidget(editor, row, 1, 1, span)
            node_name = str(node.get("name") or "")
            if node_name:
                self._widgets_by_name[node_name] = editor
            if node_name and not self._read_only_display(node):
                self._editors_by_name[node_name] = editor
            access = QLabel("" if bool(node.get("writable")) else "RO", self._content)
            access.setToolTip(self._access_text(node))
            self._grid.addWidget(access, row, 3)
        self._grid.setRowStretch(len(self._nodes), 1)
        self._layout_dirty = False

    def _sync_layout_if_visible(self) -> None:
        if not self.isVisible():
            return
        if self._layout_dirty or not self._widgets_by_name:
            self._rebuild()

    def _editor_for_node(self, node: NodePayload) -> tuple[QWidget, int]:
        node_type = str(node.get("type") or "")
        writable = bool(node.get("available")) and bool(node.get("writable"))
        if node_type == "enum":
            return self._enum_editor(node, writable), 2
        if node_type == "integer":
            return self._numeric_editor(node, writable, integer=True), 2
        if node_type == "float":
            return self._numeric_editor(node, writable, integer=False), 2
        if node_type == "boolean":
            return self._boolean_editor(node, writable), 1
        if node_type == "command":
            return self._command_editor(node, writable), 2
        return self._text_editor(node, writable), 2

    def _enum_editor(self, node: NodePayload, writable: bool) -> QWidget:
        editor = QComboBox(self._content)
        value = self._value_text(node)
        entries = [str(entry) for entry in node.get("entries") or []]
        if value and value not in entries:
            entries.insert(0, value)
        editor.addItems(entries)
        if value:
            editor.setCurrentText(value)
        editor.setEnabled(writable)
        node_name = str(node.get("name") or "")
        editor.currentTextChanged.connect(
            lambda _value, name=node_name, combo=editor: self._queue_editor_value(
                name, combo
            )
        )
        return editor

    def _numeric_editor(
        self,
        node: NodePayload,
        writable: bool,
        *,
        integer: bool,
    ) -> QWidget:
        container = QWidget(self._content)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        slider = QSlider(Qt.Horizontal, container)
        edit = QLineEdit(container)
        edit.setClearButtonEnabled(True)
        edit.setFixedWidth(96)
        edit.setText(self._value_text(node))
        minimum = self._numeric_value(node.get("minimum"))
        maximum = self._numeric_value(node.get("maximum"))
        value = self._numeric_value(node.get("value"))
        has_range = minimum is not None and maximum is not None and maximum > minimum
        if has_range:
            steps = self._slider_steps(node, minimum, maximum)
            slider.setRange(0, steps)
            if value is not None:
                slider.setValue(self._value_to_slider(value, minimum, maximum, steps))
        else:
            slider.setRange(0, 0)
        slider.setEnabled(writable and has_range)
        edit.setEnabled(writable)
        if integer:
            edit.setPlaceholderText("Integer value")
        else:
            edit.setPlaceholderText("Float value")
            validator = QDoubleValidator(edit)
            if minimum is not None:
                validator.setBottom(minimum)
            if maximum is not None:
                validator.setTop(maximum)
            validator.setNotation(QDoubleValidator.StandardNotation)
            edit.setValidator(validator)
        unit = str(node.get("unit") or "")
        layout.addWidget(slider, 1)
        layout.addWidget(edit)
        if unit:
            layout.addWidget(QLabel(unit, container))

        node_name = str(node.get("name") or "")

        def sync_edit(slider_value: int) -> None:
            if self._updating or not has_range:
                return
            self._updating = True
            value_from_slider = self._slider_to_value(
                slider_value,
                minimum,
                maximum,
                slider.maximum(),
                integer=integer,
            )
            edit.setText(str(value_from_slider))
            self._updating = False

        slider.valueChanged.connect(sync_edit)
        slider.sliderReleased.connect(
            lambda name=node_name, field=edit: self._queue_editor_value(name, field)
        )
        edit.editingFinished.connect(
            lambda name=node_name, field=edit, slide=slider, lo=minimum, hi=maximum: (
                self._sync_slider_from_edit(field, slide, lo, hi),
                self._queue_editor_value(name, field),
            )
        )
        container.setEnabled(bool(node.get("available")))
        return container

    def _boolean_editor(self, node: NodePayload, writable: bool) -> QWidget:
        editor = QCheckBox(self._content)
        editor.setChecked(self._bool_value(node.get("value")))
        editor.setEnabled(writable)
        node_name = str(node.get("name") or "")
        editor.toggled.connect(
            lambda _checked, name=node_name, checkbox=editor: self._queue_editor_value(
                name, checkbox
            )
        )
        return editor

    def _command_editor(self, node: NodePayload, writable: bool) -> QWidget:
        button = QPushButton("Execute", self._content)
        button.setEnabled(writable)
        node_name = str(node.get("name") or "")
        button.clicked.connect(
            lambda _checked=False, name=node_name: self._execute_callback(
                self._map_key,
                name,
            )
        )
        return button

    def _text_editor(self, node: NodePayload, writable: bool) -> QWidget:
        if self._read_only_display(node):
            label = QLabel(self._value_text(node), self._content)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            return label
        editor = QLineEdit(self._content)
        editor.setText(self._value_text(node))
        editor.setEnabled(writable)
        node_name = str(node.get("name") or "")
        editor.editingFinished.connect(
            lambda name=node_name, field=editor: self._queue_editor_value(name, field)
        )
        return editor

    def _update_editor(self, node: NodePayload) -> None:
        editor = self._widgets_by_name.get(str(node.get("name") or ""))
        focused = QApplication.focusWidget()
        if editor is None or self._editor_has_focus(editor, focused):
            return
        writable = bool(node.get("available")) and bool(node.get("writable"))
        available = bool(node.get("available"))
        self._updating = True
        try:
            if isinstance(editor, QComboBox):
                entries = [str(entry) for entry in node.get("entries") or []]
                current_entries = [
                    editor.itemText(index) for index in range(editor.count())
                ]
                if entries and current_entries != entries:
                    editor.clear()
                    editor.addItems(entries)
                value = self._value_text(node)
                if value and editor.findText(value) < 0:
                    editor.addItem(value)
                editor.setCurrentText(value)
                editor.setEnabled(writable)
            elif isinstance(editor, QCheckBox):
                editor.setChecked(self._bool_value(node.get("value")))
                editor.setEnabled(writable)
            elif isinstance(editor, QPushButton):
                editor.setEnabled(writable)
            elif isinstance(editor, QLineEdit):
                editor.setText(self._value_text(node))
                editor.setEnabled(writable)
            elif isinstance(editor, QLabel):
                editor.setText(self._value_text(node))
            else:
                editor.setEnabled(available)
                for line_edit in editor.findChildren(QLineEdit):
                    line_edit.setText(self._value_text(node))
                    line_edit.setEnabled(writable)
                for slider in editor.findChildren(QSlider):
                    self._update_numeric_slider(slider, node, writable)
        finally:
            self._updating = False

    def _queue_editor_value(self, node_name: str, editor: QWidget) -> None:
        if self._updating or not node_name:
            return
        value = self._editor_value(editor)
        if value is None:
            return
        self._apply_callback(self._map_key, node_name, value)

    def _editor_value(self, editor: QWidget) -> object | None:
        if isinstance(editor, QComboBox):
            return editor.currentText()
        if isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, QLineEdit):
            if editor.validator() is not None and not editor.hasAcceptableInput():
                return None
            return editor.text()
        for line_edit in editor.findChildren(QLineEdit):
            if line_edit.validator() is not None and not line_edit.hasAcceptableInput():
                return None
            return line_edit.text()
        return None

    @classmethod
    def _slider_steps(cls, node: NodePayload, minimum: float, maximum: float) -> int:
        increment = cls._numeric_value(node.get("increment"))
        if increment is not None and increment > 0:
            return max(
                1,
                min(cls.SLIDER_STEPS, int(round((maximum - minimum) / increment))),
            )
        return cls.SLIDER_STEPS

    @staticmethod
    def _value_to_slider(value: float, minimum: float, maximum: float, steps: int) -> int:
        if maximum <= minimum:
            return 0
        ratio = (value - minimum) / (maximum - minimum)
        return max(0, min(steps, int(round(ratio * steps))))

    @staticmethod
    def _slider_to_value(
        slider_value: int,
        minimum: float | None,
        maximum: float | None,
        steps: int,
        *,
        integer: bool,
    ) -> object:
        if minimum is None or maximum is None or steps <= 0:
            return int(slider_value) if integer else float(slider_value)
        value = minimum + (maximum - minimum) * (slider_value / steps)
        return int(round(value)) if integer else round(value, 6)

    def _sync_slider_from_edit(
        self,
        edit: QLineEdit,
        slider: QSlider,
        minimum: float | None,
        maximum: float | None,
    ) -> None:
        if minimum is None or maximum is None or maximum <= minimum:
            return
        value = self._numeric_value(edit.text())
        if value is None:
            return
        self._updating = True
        slider.setValue(
            self._value_to_slider(value, minimum, maximum, slider.maximum())
        )
        self._updating = False

    def _update_numeric_slider(
        self,
        slider: QSlider,
        node: NodePayload,
        writable: bool,
    ) -> None:
        minimum = self._numeric_value(node.get("minimum"))
        maximum = self._numeric_value(node.get("maximum"))
        value = self._numeric_value(node.get("value"))
        has_range = minimum is not None and maximum is not None and maximum > minimum
        if has_range:
            steps = self._slider_steps(node, minimum, maximum)
            slider.setRange(0, steps)
            if value is not None:
                slider.setValue(self._value_to_slider(value, minimum, maximum, steps))
        else:
            slider.setRange(0, 0)
        slider.setEnabled(writable and has_range)

    @staticmethod
    def _editor_has_focus(editor: QWidget, focused: QWidget | None) -> bool:
        return focused is editor or (
            focused is not None and editor.isAncestorOf(focused)
        )

    @staticmethod
    def _layout_signature(nodes: list[NodePayload]) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                str(node.get("name") or ""),
                str(node.get("type") or ""),
                bool(node.get("available")),
                bool(node.get("writable")),
                str(node.get("unit") or ""),
                node.get("minimum"),
                node.get("maximum"),
                node.get("increment"),
                tuple(str(entry) for entry in node.get("entries") or []),
            )
            for node in nodes
        )

    @staticmethod
    def _read_only_display(node: NodePayload) -> bool:
        return not bool(node.get("available")) or not bool(node.get("writable"))

    @staticmethod
    def _display_name(node: NodePayload) -> str:
        return str(node.get("display_name") or node.get("name") or "")

    @staticmethod
    def _value_text(node: NodePayload) -> str:
        value = node.get("value")
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _access_text(node: NodePayload) -> str:
        if not bool(node.get("available")):
            return "Unavailable"
        if bool(node.get("writable")):
            return "Writable"
        return "Read only"

    @staticmethod
    def _bool_value(value: object) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}

    @staticmethod
    def _numeric_value(value: object, default: float | None = None) -> float | None:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


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
        self._exposure_policy_source = exposure_policy_source
        self._pending_settings: dict[tuple[str, str], object] = {}
        self._applying_settings: dict[tuple[str, str], object] = {}
        self._snapshot_pending = False
        self._snapshot_request_id: str | None = None
        self._snapshot_show_status = False
        self._status_text = ""
        self._loaded_once = False
        self._policy_state: dict[str, object] = {}
        self._exposure_time_node: NodePayload | None = None
        self._exposure_mode_node: NodePayload | None = None
        self._updating_exposure_controls = False
        self._pending_policy: tuple[bool, str] | None = None
        self._apply_actions: list[tuple[object, ...]] = []
        self._active_apply_action: tuple[object, ...] | None = None
        self._active_setting_request_id: str | None = None
        self._adjust_exposure_apply = False
        self._adjust_once_after_apply = False

        layout = QVBoxLayout(self)

        if exposure_policy_source is not None:
            self._create_exposure_controls(layout)

        self._page = _FeatureListPage(
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

        if exposure_policy_source is not None:
            exposure_policy_source.state_changed.connect(
                self._on_exposure_policy_state_changed
            )
            exposure_policy_source.command_finished.connect(
                self._on_exposure_policy_command_finished
            )
            self._apply_exposure_policy_state(self._exposure_policy_snapshot())

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
        if str(payload.get("request_id") or "") != str(
            self._snapshot_request_id or ""
        ):
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
            self._update_exposure_time_node(camera_nodes)
            self._update_exposure_mode_node(camera_nodes)
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
        if isinstance(node, dict):
            self._update_pages_for_node(map_key, node)
        if self._pending_settings.get(key) == applied_value:
            self._pending_settings.pop(key, None)
        self._run_next_apply_action()

    def _queue_setting(self, map_key: str, node_name: str, value: object) -> None:
        if not node_name:
            return
        self._pending_settings[(map_key, node_name)] = value
        self._set_pending_status()

    def _set_pending_status(self) -> None:
        pending_count = len(self._pending_settings) + int(self._pending_policy is not None)
        if pending_count == 1:
            self._set_status("1 camera setting pending.")
        else:
            self._set_status(f"{pending_count} camera settings pending.")

    def apply_pending_settings(self) -> bool:
        self._page.queue_current_editor_value()
        if self._exposure_time_edit.hasFocus():
            self._queue_exposure_time()
        if self._is_applying() or not self._has_pending_changes():
            return False
        self._apply_actions = self._build_apply_actions()
        if not self._apply_actions:
            return False
        self._adjust_exposure_apply = False
        self._adjust_once_after_apply = False
        self._page.setEnabled(False)
        self._exposure_group.setEnabled(False)
        self._set_status(f"Applying {len(self._apply_actions)} camera settings.")
        self._run_next_apply_action()
        return True

    def _build_apply_actions(self) -> list[tuple[object, ...]]:
        pending = list(self._pending_settings.items())
        mode_item = next(
            (item for item in pending if item[0] == ("camera", "ExposureMode")),
            None,
        )
        exposure_item = next(
            (item for item in pending if item[0] == ("camera", "ExposureTime")),
            None,
        )
        regular = [
            item
            for item in pending
            if item is not mode_item and item is not exposure_item
        ]
        actions: list[tuple[object, ...]] = [
            ("setting", key[0], key[1], value) for key, value in regular
        ]
        actual_auto = bool(self._policy_state.get("auto_enabled", True))
        desired_auto, desired_engine = self._selected_exposure_policy()
        if mode_item is not None and str(mode_item[1]) == "Timed":
            key, value = mode_item
            actions.append(("setting", key[0], key[1], value))
        if exposure_item is not None and not actual_auto:
            key, value = exposure_item
            actions.append(("setting", key[0], key[1], value))
        if self._pending_policy is not None:
            actions.append(("policy", desired_auto, desired_engine))
        if mode_item is not None and str(mode_item[1]) != "Timed":
            key, value = mode_item
            actions.append(("setting", key[0], key[1], value))
        if exposure_item is not None and actual_auto and not desired_auto:
            key, value = exposure_item
            actions.append(("setting", key[0], key[1], value))
        return actions

    def _run_next_apply_action(self) -> None:
        if not self._apply_actions:
            self._finish_apply(True)
            return
        action = self._apply_actions.pop(0)
        self._active_apply_action = action
        if action[0] == "policy":
            if self._exposure_policy_source is None:
                self._set_status("Camera exposure policy is unavailable.")
                self._finish_apply(False)
                return
            self._exposure_policy_source.request_update(bool(action[1]), str(action[2]))
            return

        _kind, map_key, node_name, value = action
        key = (str(map_key), str(node_name))
        self._applying_settings[key] = value
        if key == ("camera", "ExposureTime") and self._exposure_policy_source is not None:
            self._exposure_policy_source.request_exposure_time(float(value))
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
        self._apply_exposure_policy_state(self._exposure_policy_snapshot())
        if run_once:
            self._set_status("Adjusting exposure.")
        elif success and adjust_exposure:
            self._set_status("Exposure method applied.")
        elif success:
            self._set_status("Camera settings applied.")
        self.apply_finished.emit(bool(success))
        if run_once and self._exposure_policy_source is not None:
            self._exposure_policy_source.request_once()

    def _has_pending_changes(self) -> bool:
        return bool(self._pending_settings or self._pending_policy is not None)

    def _is_applying(self) -> bool:
        return self._active_apply_action is not None or bool(self._apply_actions)

    def _update_pages_for_node(self, map_key: str, node: NodePayload) -> None:
        if map_key == "camera":
            node_name = str(node.get("name") or "")
            if node_name == "ExposureTime":
                self._update_exposure_time_node([node])
                return
            if node_name == "ExposureMode":
                self._update_exposure_mode_node([node])
                return
            self._page.update_node(node)

    def _execute_command(self, map_key: str, node_name: str) -> None:
        if not node_name:
            return
        self._set_status(f"Executing {node_name}.")
        self._grabber.request_camera_command_execute(map_key, node_name)

    def _create_exposure_controls(self, layout: QVBoxLayout) -> None:
        self._exposure_group = QGroupBox("Exposure", self)
        controls = QGridLayout(self._exposure_group)
        controls.setColumnStretch(1, 1)
        controls.setHorizontalSpacing(12)
        controls.setVerticalSpacing(6)

        controls.addWidget(QLabel("Control", self._exposure_group), 0, 0)
        self._control_combo = QComboBox(self._exposure_group)
        self._control_combo.addItem("Manual", False)
        self._control_combo.addItem("Auto", True)
        controls.addWidget(self._control_combo, 0, 1)

        controls.addWidget(QLabel("Method", self._exposure_group), 1, 0)
        self._method_combo = QComboBox(self._exposure_group)
        self._method_combo.addItem("Software", "software")
        self._method_combo.addItem("Camera", "camera")
        controls.addWidget(self._method_combo, 1, 1)

        controls.addWidget(QLabel("Timing", self._exposure_group), 2, 0)
        self._timing_combo = QComboBox(self._exposure_group)
        self._timing_combo.addItem("Timed", "Timed")
        self._timing_combo.setToolTip(
            "Trigger width is available only with the Camera method."
        )
        self._timing_combo.setEnabled(False)
        controls.addWidget(self._timing_combo, 2, 1)

        controls.addWidget(QLabel("Exposure time", self._exposure_group), 3, 0)
        exposure_time_row = QHBoxLayout()
        self._exposure_time_edit = QLineEdit(self._exposure_group)
        self._exposure_time_edit.setFixedWidth(96)
        self._exposure_time_edit.setPlaceholderText("Exposure time")
        self._exposure_time_unit = QLabel(self._exposure_group)
        exposure_time_row.addWidget(self._exposure_time_edit)
        exposure_time_row.addWidget(self._exposure_time_unit)
        exposure_time_row.addStretch(1)
        controls.addLayout(exposure_time_row, 3, 1)

        self._adjust_exposure_button = QPushButton(
            "Adjust Exposure", self._exposure_group
        )
        self._adjust_exposure_button.setToolTip(
            "Run one exposure adjustment with the selected method."
        )
        controls.addWidget(
            self._adjust_exposure_button, 4, 1, alignment=Qt.AlignLeft
        )
        layout.addWidget(self._exposure_group)

        self._control_combo.currentIndexChanged.connect(
            self._request_exposure_policy_update
        )
        self._method_combo.currentIndexChanged.connect(
            self._request_exposure_policy_update
        )
        self._timing_combo.currentIndexChanged.connect(
            self._queue_exposure_mode
        )
        self._adjust_exposure_button.clicked.connect(self._request_exposure_once)
        self._exposure_time_edit.editingFinished.connect(self._queue_exposure_time)

    def _snapshot_node_names(
        self,
        operator_nodes: list[str] | None = None,
    ) -> list[str]:
        names = list(operator_nodes or self.OPERATOR_NODE_NAMES)
        if self._exposure_policy_source is not None:
            for node_name in ("ExposureMode", "ExposureTime"):
                if node_name not in names:
                    names.append(node_name)
        return names

    def _exposure_policy_snapshot(self) -> dict[str, object]:
        source = self._exposure_policy_source
        if source is None:
            return {}
        try:
            state = source.snapshot()
        except Exception:
            return {}
        return dict(state) if isinstance(state, Mapping) else {}

    def _on_exposure_policy_state_changed(self, state: object) -> None:
        if isinstance(state, Mapping):
            self._apply_exposure_policy_state(dict(state))

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
            applied_policy = (bool(active[1]), str(active[2]))
            if self._pending_policy == applied_policy:
                self._pending_policy = None
            self._policy_state.update(self._exposure_policy_snapshot())
            self._active_apply_action = None
            self._run_next_apply_action()
            return
        if not bool(result.get("accepted", False)):
            self._set_status(str(result.get("message") or "Camera exposure failed."))
            self._apply_exposure_policy_state(self._exposure_policy_snapshot())

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
        if not bool(result.get("accepted", False)):
            self._apply_exposure_policy_state(self._exposure_policy_snapshot())

    def _apply_exposure_policy_state(self, state: Mapping[str, object]) -> None:
        if self._exposure_policy_source is None:
            return
        self._policy_state.update(state)
        auto_enabled = bool(self._policy_state.get("auto_enabled", True))
        engine = str(self._policy_state.get("engine") or "software")
        active_policy = bool(
            self._active_apply_action is not None
            and self._active_apply_action[0] == "policy"
        )
        if self._pending_policy is None and not active_policy:
            self._updating_exposure_controls = True
            try:
                self._set_combo_data(self._control_combo, auto_enabled)
                self._set_combo_data(
                    self._method_combo,
                    "camera" if engine == "camera" else "software",
                )
            finally:
                self._updating_exposure_controls = False
        busy = (
            bool(self._policy_state.get("busy", False))
            or bool(self._policy_state.get("session_active", False))
            or self._is_applying()
        )
        self._exposure_group.setEnabled(not busy)
        self._sync_timing_control()
        self._set_exposure_time_edit_enabled()

    def _request_exposure_policy_update(self, _index: int) -> None:
        if (
            self._updating_exposure_controls
            or self._exposure_policy_source is None
            or not self._exposure_group.isEnabled()
        ):
            return
        engine = str(self._method_combo.currentData() or "software")
        auto_enabled = bool(self._control_combo.currentData())
        if (
            engine == "software"
            and str(self._timing_combo.currentData() or "Timed") != "Timed"
        ):
            node = self._exposure_mode_node
            if not (node and node.get("available") and node.get("writable")):
                self._set_status("Software exposure requires timed exposure.")
                self._apply_exposure_policy_state(self._exposure_policy_snapshot())
                return
            self._use_timed_exposure_mode()
        current = (
            bool(self._policy_state.get("auto_enabled", True)),
            str(self._policy_state.get("engine") or "software"),
        )
        selected = (auto_enabled, engine)
        self._pending_policy = None if selected == current else selected
        self._set_pending_status()
        self._sync_timing_control()
        self._set_exposure_time_edit_enabled()

    def _request_exposure_once(self) -> None:
        if (
            self._exposure_policy_source is None
            or not self._exposure_group.isEnabled()
            or self._is_applying()
        ):
            return
        selected_engine = self._selected_exposure_engine()
        actual_auto = bool(self._policy_state.get("auto_enabled", True))
        actual_engine = str(self._policy_state.get("engine") or "software")
        actions: list[tuple[object, ...]] = []
        mode_key = ("camera", "ExposureMode")
        if (
            selected_engine == "software"
            and self._pending_settings.get(mode_key) == "Timed"
        ):
            actions.append(("setting", *mode_key, "Timed"))
        method_changed = selected_engine != actual_engine
        if method_changed:
            actions.append(("policy", actual_auto, selected_engine))
        if not actions:
            self._set_status("Adjusting exposure.")
            self._exposure_policy_source.request_once()
            return
        self._apply_actions = actions
        self._adjust_exposure_apply = True
        self._adjust_once_after_apply = not (actual_auto and method_changed)
        self._page.setEnabled(False)
        self._exposure_group.setEnabled(False)
        self._set_status("Applying exposure method.")
        self._run_next_apply_action()

    def _update_exposure_mode_node(self, nodes: list[object]) -> None:
        if self._exposure_policy_source is None:
            return
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
        self._updating_exposure_controls = True
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
            self._updating_exposure_controls = False
        self._sync_timing_control()

    def _queue_exposure_mode(self, _index: int) -> None:
        if self._updating_exposure_controls or not self._timing_combo.isEnabled():
            return
        value = str(self._timing_combo.currentData() or "Timed")
        if value == "TriggerWidth" and self._selected_exposure_engine() != "camera":
            self._use_timed_exposure_mode()
            return
        self._queue_setting("camera", "ExposureMode", value)

    def _sync_timing_control(self) -> None:
        node = self._exposure_mode_node
        camera_method = self._selected_exposure_engine() == "camera"
        writable = bool(node and node.get("available") and node.get("writable"))
        has_choices = self._timing_combo.count() > 1
        if not camera_method:
            self._use_timed_exposure_mode()
        self._timing_combo.setEnabled(
            self._exposure_group.isEnabled()
            and camera_method
            and writable
            and has_choices
        )

    def _use_timed_exposure_mode(self) -> None:
        current = str(self._timing_combo.currentData() or "Timed")
        if current == "Timed":
            return
        self._updating_exposure_controls = True
        try:
            self._set_combo_data(self._timing_combo, "Timed")
        finally:
            self._updating_exposure_controls = False
        node = self._exposure_mode_node
        if node and node.get("available") and node.get("writable"):
            self._queue_setting("camera", "ExposureMode", "Timed")

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
        if self._exposure_policy_source is None:
            return
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
        if self._exposure_policy_source is None:
            return
        node = self._exposure_time_node
        manual = not bool(self._control_combo.currentData())
        busy = bool(self._policy_state.get("busy", False)) or bool(
            self._policy_state.get("session_active", False)
        )
        writable = bool(node and node.get("available") and node.get("writable"))
        self._exposure_time_edit.setEnabled(
            manual and not busy and not self._is_applying() and writable
        )

    def _queue_exposure_time(self) -> None:
        if self._exposure_policy_source is None or not self._exposure_time_edit.isEnabled():
            return
        if not self._exposure_time_edit.hasAcceptableInput():
            return
        value = self._exposure_time_value(self._exposure_time_edit.text())
        if value is None:
            return
        self._queue_setting("camera", "ExposureTime", value)

    @staticmethod
    def _exposure_time_value(value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _set_status(self, message: str) -> None:
        self._status_text = message


__all__ = ["CameraSettingsWidget"]
