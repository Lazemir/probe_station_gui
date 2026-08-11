"""Editable GenICam feature grid for camera settings pages."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedSlider as QSlider,
)


NodePayload = dict[str, Any]


class CameraFeatureEditors(QWidget):
    """Build and update the editable grid for one GenICam node map."""

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

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(6)
        self._grid.setColumnStretch(1, 1)

    def set_nodes(self, nodes: list[NodePayload]) -> None:
        old_signature = self._layout_signature(self._nodes)
        new_signature = self._layout_signature(nodes)
        self._nodes = list(nodes)
        self._nodes_by_name = {
            str(node.get("name") or ""): node
            for node in self._nodes
            if str(node.get("name") or "")
        }
        if self._widgets_by_name and old_signature == new_signature:
            for node in self._nodes:
                self._update_editor(node)
            return
        self._rebuild()

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
            self._update_editor(merged)
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
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._widgets_by_name.clear()
        self._editors_by_name.clear()

        for row, node in enumerate(self._nodes):
            label = QLabel(self._display_name(node), self)
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
            access = QLabel("" if bool(node.get("writable")) else "RO", self)
            access.setToolTip(self._access_text(node))
            self._grid.addWidget(access, row, 3)
        self._grid.setRowStretch(len(self._nodes), 1)

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
        editor = QComboBox(self)
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
        container = QWidget(self)
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
        editor = QCheckBox(self)
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
        button = QPushButton("Execute", self)
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
            label = QLabel(self._value_text(node), self)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            return label
        editor = QLineEdit(self)
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
    def _value_to_slider(
        value: float,
        minimum: float,
        maximum: float,
        steps: int,
    ) -> int:
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


__all__ = ["CameraFeatureEditors"]
