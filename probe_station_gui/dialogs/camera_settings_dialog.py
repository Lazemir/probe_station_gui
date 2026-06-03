"""Operator-facing GenICam camera controls."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.wheel_guard import (
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
        "ExposureMode",
        "ExposureAuto",
        "ExposureTime",
        "ExposureCompensationAuto",
        "ExposureCompensation",
        "GainAuto",
        "Gain",
        "BlackLevel",
        "BalanceWhiteAuto",
        "BalanceRatioSelector",
        "BalanceRatio",
    )

    def __init__(self, grabber: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grabber = grabber
        self._pending_settings: dict[tuple[str, str], object] = {}
        self._applying_settings: dict[tuple[str, str], object] = {}
        self._snapshot_pending = False
        self._snapshot_show_status = False
        self._status_text = ""
        self._loaded_once = False

        layout = QVBoxLayout(self)

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

    def has_loaded(self) -> bool:
        return self._loaded_once

    def refresh(self) -> None:
        if self._pending_settings or self._applying_settings:
            self._set_status("Apply pending camera settings before refreshing.")
            return
        self._request_snapshot(
            show_status=True,
            node_names=list(self.OPERATOR_NODE_NAMES),
        )

    def _refresh_live_snapshot(self) -> None:
        if not self._loaded_once:
            return
        if not self.isVisible():
            return
        if self._pending_settings or self._applying_settings:
            return
        if self._has_edit_focus():
            return
        node_names = self._page.node_names()
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
        self._snapshot_show_status = show_status
        if show_status:
            self._set_status("Loading camera controls.")
        self._grabber.request_camera_settings_snapshot(
            map_key="camera",
            node_names=node_names or list(self.OPERATOR_NODE_NAMES),
        )

    def _has_edit_focus(self) -> bool:
        return self._page.has_edit_focus()

    def _on_snapshot(self, payload: object) -> None:
        self._snapshot_pending = False
        show_status = self._snapshot_show_status
        self._snapshot_show_status = False
        if self._pending_settings or self._applying_settings:
            self._set_pending_status()
            return
        if not isinstance(payload, dict):
            self._set_status("Camera settings response was invalid.")
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
            if self._loaded_once:
                for node in camera_nodes:
                    if isinstance(node, dict):
                        self._page.update_node(node)
            else:
                self._page.set_nodes(camera_nodes)

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
        self._set_status(str(payload.get("message") or "Camera setting updated."))
        map_key = str(payload.get("map_key") or "")
        node_name = str(payload.get("node_name") or "")
        key = (map_key, node_name)
        had_applied_value = key in self._applying_settings
        applied_value = self._applying_settings.pop(key, None)
        if payload.get("ok", False):
            node = payload.get("node")
            if isinstance(node, dict):
                self._update_pages_for_node(map_key, node)
            if had_applied_value and self._pending_settings.get(key) == applied_value:
                self._pending_settings.pop(key, None)
            if self._pending_settings:
                self._set_pending_status()
        else:
            self._pending_settings.pop(key, None)

    def _queue_setting(self, map_key: str, node_name: str, value: object) -> None:
        if not node_name:
            return
        self._pending_settings[(map_key, node_name)] = value
        self._set_pending_status()
        self._write_pending_settings()

    def _set_pending_status(self) -> None:
        pending_count = len(self._pending_settings)
        if pending_count == 1:
            self._set_status("1 camera setting pending.")
        else:
            self._set_status(f"{pending_count} camera settings pending.")

    def apply_pending_settings(self) -> bool:
        self._page.queue_current_editor_value()
        return self._write_pending_settings()

    def _write_pending_settings(self) -> bool:
        if not self._pending_settings:
            return False
        pending_items = [
            item
            for item in self._pending_settings.items()
            if item[0] not in self._applying_settings
        ]
        if not pending_items:
            return False
        self._set_status(f"Writing {len(pending_items)} camera settings.")
        for (map_key, node_name), value in pending_items:
            self._applying_settings[(map_key, node_name)] = value
            self._grabber.request_camera_setting_update(map_key, node_name, value)
        return True

    def _update_pages_for_node(self, map_key: str, node: NodePayload) -> None:
        if map_key == "camera":
            self._page.update_node(node)

    def _execute_command(self, map_key: str, node_name: str) -> None:
        if not node_name:
            return
        self._set_status(f"Executing {node_name}.")
        self._grabber.request_camera_command_execute(map_key, node_name)

    def _set_status(self, message: str) -> None:
        self._status_text = message


__all__ = ["CameraSettingsWidget"]
