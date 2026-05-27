"""Generic GenICam camera settings dialog."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


NodePayload = dict[str, Any]


class _NodeMapPage(QWidget):
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
        self._current_node: NodePayload | None = None
        self._editor: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        filter_row = QHBoxLayout()
        self._filter_edit = QLineEdit(self)
        self._filter_edit.setPlaceholderText("Filter")
        self._filter_edit.setClearButtonEnabled(True)
        self._writable_only_check = QCheckBox("Writable", self)
        filter_row.addWidget(self._filter_edit, 1)
        filter_row.addWidget(self._writable_only_check)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Horizontal, self)
        layout.addWidget(splitter, 1)

        self._tree = QTreeWidget(splitter)
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(["Name", "Value", "Type", "Access", "Visibility"])
        splitter.addWidget(self._tree)

        detail_widget = QWidget(splitter)
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(8, 0, 0, 0)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._name_label = self._detail_label()
        self._internal_name_label = self._detail_label()
        self._type_label = self._detail_label()
        self._access_label = self._detail_label()
        self._range_label = self._detail_label()
        form.addRow(QLabel("Display", self), self._name_label)
        form.addRow(QLabel("Node", self), self._internal_name_label)
        form.addRow(QLabel("Type", self), self._type_label)
        form.addRow(QLabel("Access", self), self._access_label)
        form.addRow(QLabel("Range", self), self._range_label)
        detail_layout.addLayout(form)

        self._description_edit = QPlainTextEdit(self)
        self._description_edit.setReadOnly(True)
        self._description_edit.setMaximumBlockCount(80)
        detail_layout.addWidget(self._description_edit, 1)

        self._editor_container = QWidget(self)
        self._editor_layout = QHBoxLayout(self._editor_container)
        self._editor_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self._editor_container)

        button_row = QHBoxLayout()
        self._apply_button = QPushButton("Apply", self)
        self._execute_button = QPushButton("Execute", self)
        button_row.addStretch(1)
        button_row.addWidget(self._apply_button)
        button_row.addWidget(self._execute_button)
        detail_layout.addLayout(button_row)
        splitter.addWidget(detail_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self._filter_edit.textChanged.connect(self._refresh_tree)
        self._writable_only_check.toggled.connect(self._refresh_tree)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._apply_button.clicked.connect(self._apply_current)
        self._execute_button.clicked.connect(self._execute_current)
        self._show_node(None)

    def set_nodes(self, nodes: list[NodePayload]) -> None:
        self._nodes = list(nodes)
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        selected_name = None
        if self._current_node is not None:
            selected_name = str(self._current_node.get("name") or "")

        self._tree.clear()
        filter_text = self._filter_edit.text().strip().lower()
        writable_only = self._writable_only_check.isChecked()
        item_to_select: QTreeWidgetItem | None = None
        for node in self._nodes:
            if writable_only and not bool(node.get("writable")):
                continue
            if filter_text and filter_text not in self._search_text(node):
                continue
            item = QTreeWidgetItem(
                [
                    self._display_name(node),
                    self._value_text(node),
                    str(node.get("type") or ""),
                    self._access_text(node),
                    str(node.get("visibility") or ""),
                ]
            )
            item.setData(0, Qt.UserRole, node)
            self._tree.addTopLevelItem(item)
            if selected_name and str(node.get("name") or "") == selected_name:
                item_to_select = item

        for index in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(index)

        if item_to_select is None and self._tree.topLevelItemCount() > 0:
            item_to_select = self._tree.topLevelItem(0)
        if item_to_select is not None:
            self._tree.setCurrentItem(item_to_select)
        else:
            self._show_node(None)

    def _on_selection_changed(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            self._show_node(None)
            return
        node = items[0].data(0, Qt.UserRole)
        self._show_node(node if isinstance(node, dict) else None)

    def _show_node(self, node: NodePayload | None) -> None:
        self._current_node = node
        self._clear_editor()
        if node is None:
            self._name_label.setText("")
            self._internal_name_label.setText("")
            self._type_label.setText("")
            self._access_label.setText("")
            self._range_label.setText("")
            self._description_edit.clear()
            self._apply_button.setEnabled(False)
            self._execute_button.setEnabled(False)
            self._execute_button.hide()
            self._apply_button.show()
            return

        self._name_label.setText(self._display_name(node))
        self._internal_name_label.setText(str(node.get("name") or ""))
        self._type_label.setText(str(node.get("type") or ""))
        self._access_label.setText(self._access_text(node))
        self._range_label.setText(self._range_text(node))
        self._description_edit.setPlainText(self._description_text(node))
        self._build_editor(node)

    def _build_editor(self, node: NodePayload) -> None:
        node_type = str(node.get("type") or "")
        writable = bool(node.get("available")) and bool(node.get("writable"))
        if node_type == "command":
            self._apply_button.hide()
            self._execute_button.show()
            self._execute_button.setEnabled(writable)
            return

        self._execute_button.hide()
        self._apply_button.show()
        if node_type == "boolean":
            editor = QCheckBox("Enabled", self)
            editor.setChecked(self._bool_value(node.get("value")))
        elif node_type == "enum":
            editor = QComboBox(self)
            value = self._value_text(node)
            entries = [str(entry) for entry in node.get("entries") or []]
            if value and value not in entries:
                entries.insert(0, value)
            editor.addItems(entries)
            if value:
                editor.setCurrentText(value)
        else:
            editor = QLineEdit(self)
            editor.setText(self._value_text(node))
            editor.setPlaceholderText("Value")

        editor.setEnabled(writable)
        self._editor = editor
        self._editor_layout.addWidget(editor, 1)
        self._apply_button.setEnabled(writable)

    def _clear_editor(self) -> None:
        while self._editor_layout.count():
            item = self._editor_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._editor = None

    def _apply_current(self) -> None:
        node = self._current_node
        if node is None:
            return
        editor = self._editor
        if isinstance(editor, QCheckBox):
            value: object = editor.isChecked()
        elif isinstance(editor, QComboBox):
            value = editor.currentText()
        elif isinstance(editor, QLineEdit):
            value = editor.text()
        else:
            return
        self._apply_callback(self._map_key, str(node.get("name") or ""), value)

    def _execute_current(self) -> None:
        node = self._current_node
        if node is None:
            return
        if (
            QMessageBox.question(
                self,
                "Execute Command",
                f"Execute {self._display_name(node)}?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        self._execute_callback(self._map_key, str(node.get("name") or ""))

    def _detail_label(self) -> QLabel:
        label = QLabel(self)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setWordWrap(True)
        return label

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
        available = bool(node.get("available"))
        readable = bool(node.get("readable"))
        writable = bool(node.get("writable"))
        if not available:
            return "Unavailable"
        if readable and writable:
            return "RW"
        if readable:
            return "RO"
        if writable:
            return "WO"
        return "--"

    @staticmethod
    def _range_text(node: NodePayload) -> str:
        parts: list[str] = []
        minimum = node.get("minimum")
        maximum = node.get("maximum")
        increment = node.get("increment")
        unit = str(node.get("unit") or "")
        if minimum is not None or maximum is not None:
            parts.append(f"{minimum} to {maximum}".strip())
        if increment is not None:
            parts.append(f"step {increment}")
        if unit:
            parts.append(unit)
        return ", ".join(parts)

    @staticmethod
    def _description_text(node: NodePayload) -> str:
        texts = [
            str(node.get("description") or ""),
            str(node.get("short_description") or ""),
            str(node.get("tooltip") or ""),
        ]
        return "\n\n".join(text for text in texts if text)

    @staticmethod
    def _search_text(node: NodePayload) -> str:
        values = [
            node.get("display_name"),
            node.get("name"),
            node.get("value"),
            node.get("type"),
            node.get("visibility"),
            node.get("description"),
            node.get("short_description"),
            node.get("tooltip"),
        ]
        return " ".join(str(value).lower() for value in values if value is not None)

    @staticmethod
    def _bool_value(value: object) -> bool:
        text = str(value).strip().lower()
        return text in {"1", "true", "yes", "on", "enabled"}


class CameraSettingsDialog(QDialog):
    """Dialog that exposes every implemented GenICam node reported by rotpy."""

    def __init__(self, grabber: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Camera Settings")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.resize(980, 680)
        self._grabber = grabber
        self._pages: dict[str, _NodeMapPage] = {}

        layout = QVBoxLayout(self)
        status_row = QHBoxLayout()
        self._status_label = QLabel("Camera settings not loaded.", self)
        self._status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._refresh_button = QPushButton("Refresh", self)
        status_row.addWidget(self._status_label, 1)
        status_row.addWidget(self._refresh_button)
        layout.addLayout(status_row)

        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._refresh_button.clicked.connect(self.refresh)
        grabber.camera_settings_snapshot_ready.connect(self._on_snapshot)
        grabber.camera_setting_changed.connect(self._on_setting_changed)

    def refresh(self) -> None:
        self._status_label.setText("Loading camera settings.")
        self._refresh_button.setEnabled(False)
        self._grabber.request_camera_settings_snapshot()

    def _on_snapshot(self, payload: object) -> None:
        self._refresh_button.setEnabled(True)
        if not isinstance(payload, dict):
            self._status_label.setText("Camera settings response was invalid.")
            return
        if not payload.get("ok", False):
            self._status_label.setText(str(payload.get("message") or "Camera error."))
            return

        while self._tabs.count():
            widget = self._tabs.widget(0)
            self._tabs.removeTab(0)
            if widget is not None:
                widget.deleteLater()
        self._pages.clear()
        for map_payload in payload.get("maps") or []:
            if not isinstance(map_payload, dict):
                continue
            map_key = str(map_payload.get("key") or "")
            title = str(map_payload.get("title") or map_key or "Node Map")
            page = _NodeMapPage(
                map_key,
                self._apply_setting,
                self._execute_command,
                self,
            )
            nodes = map_payload.get("nodes")
            page.set_nodes(nodes if isinstance(nodes, list) else [])
            self._pages[map_key] = page
            error = str(map_payload.get("error") or "")
            self._tabs.addTab(page, f"{title} (error)" if error else title)
        self._status_label.setText(str(payload.get("message") or "Camera settings loaded."))

    def _on_setting_changed(self, payload: object) -> None:
        self._refresh_button.setEnabled(True)
        if not isinstance(payload, dict):
            self._status_label.setText("Camera setting response was invalid.")
            return
        self._status_label.setText(str(payload.get("message") or "Camera setting updated."))
        if payload.get("ok", False):
            self.refresh()

    def _apply_setting(self, map_key: str, node_name: str, value: object) -> None:
        if not node_name:
            return
        self._status_label.setText(f"Applying {node_name}.")
        self._refresh_button.setEnabled(False)
        self._grabber.request_camera_setting_update(map_key, node_name, value)

    def _execute_command(self, map_key: str, node_name: str) -> None:
        if not node_name:
            return
        self._status_label.setText(f"Executing {node_name}.")
        self._refresh_button.setEnabled(False)
        self._grabber.request_camera_command_execute(map_key, node_name)


__all__ = ["CameraSettingsDialog"]
