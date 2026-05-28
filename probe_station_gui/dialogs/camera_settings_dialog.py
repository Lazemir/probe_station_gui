"""Generic GenICam camera settings dialog."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QDoubleSpinBox,
    QSplitter,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


NodePayload = dict[str, Any]


class _NodeMapPage(QWidget):
    FALLBACK_CATEGORIES = (
        (
            "Acquisition Control",
            (
                "Acquisition",
                "Trigger",
                "Exposure",
                "FrameRate",
                "AcquisitionFrame",
            ),
        ),
        (
            "Analog Control",
            ("Gain", "Gamma", "BlackLevel", "BalanceWhite", "BalanceRatio"),
        ),
        (
            "Image Format Control",
            (
                "Width",
                "Height",
                "Offset",
                "PixelFormat",
                "Binning",
                "Decimation",
                "Reverse",
                "TestPattern",
            ),
        ),
        ("Device Control", ("Device", "UserSet", "Timestamp", "Temperature")),
        ("Digital IO Control", ("Line", "UserOutput")),
        ("Event Control", ("Event",)),
        ("Chunk Data Control", ("Chunk",)),
        ("Sequencer Control", ("Sequencer",)),
        ("File Access Control", ("File",)),
        ("Counter And Timer Control", ("Counter", "Timer")),
        ("Transfer Control", ("Transfer",)),
        ("Transport Layer Control", ("Gev", "Stream", "TL")),
    )

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
        self._current_node: NodePayload | None = None
        self._editor: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        filter_row = QHBoxLayout()
        self._filter_edit = QLineEdit(self)
        self._filter_edit.setPlaceholderText("Filter")
        self._filter_edit.setClearButtonEnabled(True)
        self._writable_only_check = QCheckBox("Writable", self)
        self._visibility_combo = QComboBox(self)
        self._visibility_combo.addItems(["All", "Beginner", "Expert", "Guru"])
        self._expand_button = QToolButton(self)
        self._expand_button.setText("Expand")
        self._collapse_button = QToolButton(self)
        self._collapse_button.setText("Collapse")
        filter_row.addWidget(self._filter_edit, 1)
        filter_row.addWidget(self._writable_only_check)
        filter_row.addWidget(self._expand_button)
        filter_row.addWidget(self._collapse_button)
        filter_row.addWidget(QLabel("Visibility", self))
        filter_row.addWidget(self._visibility_combo)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Horizontal, self)
        layout.addWidget(splitter, 1)

        self._tree = QTreeWidget(splitter)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Feature", "Value"])
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
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
        self._execute_button = QPushButton("Execute", self)
        button_row.addStretch(1)
        button_row.addWidget(self._execute_button)
        detail_layout.addLayout(button_row)
        splitter.addWidget(detail_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self._filter_edit.textChanged.connect(self._refresh_tree)
        self._writable_only_check.toggled.connect(self._refresh_tree)
        self._visibility_combo.currentIndexChanged.connect(self._refresh_tree)
        self._expand_button.clicked.connect(self._tree.expandAll)
        self._collapse_button.clicked.connect(self._tree.collapseAll)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        self._execute_button.clicked.connect(self._execute_current)
        self._show_node(None)

    def set_nodes(self, nodes: list[NodePayload]) -> None:
        self._nodes = list(nodes)
        self._nodes_by_name = {
            str(node.get("name") or ""): node
            for node in self._nodes
            if str(node.get("name") or "")
        }
        self._refresh_tree()

    def update_node(self, node: NodePayload) -> bool:
        node_name = str(node.get("name") or "")
        if not node_name:
            return False

        updated_node: NodePayload | None = None
        for index, existing in enumerate(self._nodes):
            if str(existing.get("name") or "") != node_name:
                continue
            merged = dict(existing)
            merged.update(node)
            self._nodes[index] = merged
            updated_node = merged

        if updated_node is None:
            return False

        self._nodes_by_name[node_name] = updated_node
        if (
            self._current_node is not None
            and str(self._current_node.get("name") or "") == node_name
        ):
            self._current_node = updated_node
        self._refresh_tree()
        return True

    def map_key(self) -> str:
        return self._map_key

    def _refresh_tree(self) -> None:
        selected_name = None
        if self._current_node is not None:
            selected_name = str(self._current_node.get("name") or "")

        self._tree.clear()
        item_to_select: QTreeWidgetItem | None = None
        item_to_select = self._populate_category_tree(selected_name)

        for index in range(self._tree.columnCount()):
            self._tree.resizeColumnToContents(index)
        self._tree.expandToDepth(1)

        if item_to_select is None and self._tree.topLevelItemCount() > 0:
            item_to_select = self._first_feature_item(self._tree.topLevelItem(0))
        if item_to_select is not None:
            self._tree.setCurrentItem(item_to_select)
        else:
            self._show_node(None)

    def _populate_category_tree(
        self,
        selected_name: str | None,
    ) -> QTreeWidgetItem | None:
        item_to_select: QTreeWidgetItem | None = None
        seen_features: set[str] = set()
        categories = self._category_nodes()
        category_children = self._category_children(categories)
        referenced_categories = {
            child_name
            for child_names in category_children.values()
            for child_name in child_names
            if child_name in categories
        }

        top_categories = self._top_categories(categories, category_children)
        if not top_categories:
            top_categories = [
                name for name in categories if name not in referenced_categories
            ]
        for category_name in top_categories:
            category_node = categories.get(category_name)
            if category_node is None:
                continue
            item = self._category_item(
                category_node,
                category_children,
                seen_features,
                selected_name,
                set(),
            )
            if item is None:
                continue
            self._tree.addTopLevelItem(item)
            item_to_select = item_to_select or self._find_item_by_name(
                item, selected_name
            )

        for group_name, group_nodes in self._fallback_groups(seen_features).items():
            if not group_nodes:
                continue
            group_item = self._synthetic_category_item(group_name)
            for node in group_nodes:
                feature_item = self._feature_item(node)
                group_item.addChild(feature_item)
                if selected_name and str(node.get("name") or "") == selected_name:
                    item_to_select = feature_item
            self._tree.addTopLevelItem(group_item)
        return item_to_select

    def _category_nodes(self) -> dict[str, NodePayload]:
        return {
            str(node.get("name") or ""): node
            for node in self._nodes
            if str(node.get("type") or "") == "category"
            and str(node.get("name") or "")
        }

    def _category_children(
        self,
        categories: dict[str, NodePayload],
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for name, node in categories.items():
            children: list[str] = []
            for child_name in node.get("children") or []:
                child_key = str(child_name)
                if child_key in self._nodes_by_name and child_key not in children:
                    children.append(child_key)
            if children:
                result[name] = children
        return result

    def _top_categories(
        self,
        categories: dict[str, NodePayload],
        category_children: dict[str, list[str]],
    ) -> list[str]:
        root = categories.get("Root")
        if root is None:
            return []
        top: list[str] = []
        for child_name in category_children.get("Root", []):
            if child_name in categories:
                top.append(child_name)
        return top

    def _category_item(
        self,
        category_node: NodePayload,
        category_children: dict[str, list[str]],
        seen_features: set[str],
        selected_name: str | None,
        stack: set[str],
    ) -> QTreeWidgetItem | None:
        category_name = str(category_node.get("name") or "")
        if not category_name or category_name in stack:
            return None
        stack = set(stack)
        stack.add(category_name)
        category_matches = self._filter_matches(category_node)
        item = self._feature_item(category_node, is_category=True)
        for child_name in category_children.get(category_name, []):
            child_node = self._nodes_by_name.get(child_name)
            if child_node is None:
                continue
            if str(child_node.get("type") or "") == "category":
                child_item = self._category_item(
                    child_node,
                    category_children,
                    seen_features,
                    selected_name,
                    stack,
                )
                if child_item is not None:
                    item.addChild(child_item)
                continue
            if not self._feature_visible(child_node, category_matches):
                continue
            item.addChild(self._feature_item(child_node))
            seen_features.add(str(child_node.get("name") or ""))
        if item.childCount() == 0 and not category_matches:
            return None
        return item

    def _fallback_groups(
        self,
        seen_features: set[str],
    ) -> dict[str, list[NodePayload]]:
        groups: dict[str, list[NodePayload]] = {}
        for node in self._nodes:
            name = str(node.get("name") or "")
            if not name or name in seen_features:
                continue
            if str(node.get("type") or "") == "category":
                continue
            if not self._feature_visible(node, False):
                continue
            group_name = self._fallback_category(node)
            groups.setdefault(group_name, []).append(node)
            seen_features.add(name)
        for group_nodes in groups.values():
            group_nodes.sort(key=self._sort_key)
        return groups

    def _fallback_category(self, node: NodePayload) -> str:
        name = str(node.get("name") or "")
        display_name = self._display_name(node)
        for category_name, prefixes in self.FALLBACK_CATEGORIES:
            if any(
                name.startswith(prefix) or display_name.startswith(prefix)
                for prefix in prefixes
            ):
                return category_name
        return "Other"

    def _synthetic_category_item(self, name: str) -> QTreeWidgetItem:
        node: NodePayload = {
            "name": name,
            "display_name": name,
            "type": "category",
            "value": "",
            "available": True,
            "readable": False,
            "writable": False,
        }
        return self._feature_item(node, is_category=True)

    def _feature_item(
        self,
        node: NodePayload,
        *,
        is_category: bool = False,
    ) -> QTreeWidgetItem:
        node_type = str(node.get("type") or "")
        category = is_category or node_type == "category"
        item = QTreeWidgetItem(
            [
                self._display_name(node),
                "" if category else self._value_text(node),
            ]
        )
        item.setData(0, Qt.UserRole, node)
        if category:
            font = QFont(item.font(0))
            font.setBold(True)
            item.setFont(0, font)
            item.setFirstColumnSpanned(True)
        elif not bool(node.get("available")):
            item.setText(1, "Unavailable")
            self._dim_item(item, "Unavailable")
        elif bool(node.get("readable")) and not bool(node.get("writable")):
            self._dim_item(item, "Read only")
        return item

    def _dim_item(self, item: QTreeWidgetItem, tooltip: str) -> None:
        brush = QBrush(QColor("#666666"))
        for column in range(item.columnCount()):
            item.setForeground(column, brush)
            item.setToolTip(column, tooltip)

    def _find_item_by_name(
        self,
        item: QTreeWidgetItem,
        name: str | None,
    ) -> QTreeWidgetItem | None:
        if not name:
            return None
        node = item.data(0, Qt.UserRole)
        if isinstance(node, dict) and str(node.get("name") or "") == name:
            return item
        for index in range(item.childCount()):
            found = self._find_item_by_name(item.child(index), name)
            if found is not None:
                return found
        return None

    def _first_feature_item(self, item: QTreeWidgetItem) -> QTreeWidgetItem | None:
        node = item.data(0, Qt.UserRole)
        if isinstance(node, dict) and str(node.get("type") or "") != "category":
            return item
        for index in range(item.childCount()):
            found = self._first_feature_item(item.child(index))
            if found is not None:
                return found
        return item

    def _feature_visible(
        self,
        node: NodePayload,
        category_matches: bool,
    ) -> bool:
        if self._writable_only_check.isChecked() and not bool(node.get("writable")):
            return False
        visibility = self._visibility_combo.currentText().strip().lower()
        node_visibility = str(node.get("visibility") or "").strip().lower()
        if not self._visibility_allows(visibility, node_visibility):
            return False
        if category_matches:
            return True
        return self._filter_matches(node)

    def _filter_matches(self, node: NodePayload) -> bool:
        filter_text = self._filter_edit.text().strip().lower()
        if not filter_text:
            return True
        return filter_text in self._search_text(node)

    @staticmethod
    def _visibility_allows(selected: str, node_visibility: str) -> bool:
        if selected == "all":
            return True
        levels = {
            "beginner": 0,
            "expert": 1,
            "guru": 2,
        }
        node_level = levels.get(node_visibility, 0)
        selected_level = levels.get(selected, 2)
        return node_level <= selected_level

    def _on_selection_changed(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            self._show_node(None)
            return
        node = items[0].data(0, Qt.UserRole)
        self._show_node(node if isinstance(node, dict) else None)

    def _show_context_menu(self, position: object) -> None:
        menu = QMenu(self._tree)
        node = self._current_node
        expand_action = QAction("Expand All", menu)
        collapse_action = QAction("Collapse All", menu)
        menu.addAction(expand_action)
        menu.addAction(collapse_action)
        menu.addSeparator()

        visibility_menu = menu.addMenu("Visibility")
        visibility_actions: dict[QAction, str] = {}
        for label in ("All", "Beginner", "Expert", "Guru"):
            action = QAction(label, visibility_menu)
            action.setCheckable(True)
            action.setChecked(self._visibility_combo.currentText() == label)
            visibility_menu.addAction(action)
            visibility_actions[action] = label

        node_info_action = QAction("Copy Node Information", menu)
        node_info_action.setEnabled(isinstance(node, dict))
        menu.addSeparator()
        menu.addAction(node_info_action)

        selected = menu.exec(self._tree.viewport().mapToGlobal(position))
        if selected is expand_action:
            self._tree.expandAll()
            return
        if selected is collapse_action:
            self._tree.collapseAll()
            return
        if selected is node_info_action and isinstance(node, dict):
            QApplication.clipboard().setText(self._node_info_text(node))
            return
        if selected in visibility_actions:
            self._visibility_combo.setCurrentText(visibility_actions[selected])

    def _node_info_text(self, node: NodePayload) -> str:
        fields = (
            ("Display", self._display_name(node)),
            ("Node", str(node.get("name") or "")),
            ("Type", str(node.get("type") or "")),
            ("Access", self._access_text(node)),
            ("Visibility", str(node.get("visibility") or "")),
            ("Value", self._value_text(node)),
            ("Range", self._range_text(node)),
            ("Description", self._description_text(node)),
        )
        return "\n".join(
            f"{label}: {value}" for label, value in fields if str(value).strip()
        )

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
            self._execute_button.setEnabled(False)
            self._execute_button.hide()
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
        if node_type == "category":
            self._execute_button.hide()
            return
        writable = bool(node.get("available")) and bool(node.get("writable"))
        if node_type == "command":
            self._execute_button.show()
            self._execute_button.setEnabled(writable)
            return

        self._execute_button.hide()
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
        elif node_type == "integer":
            editor = self._integer_editor(node)
        elif node_type == "float":
            editor = self._float_editor(node)
        else:
            editor = QLineEdit(self)
            editor.setText(self._value_text(node))
            editor.setPlaceholderText("Value")

        editor.setEnabled(writable)
        self._editor = editor
        self._editor_layout.addWidget(editor, 1)
        self._connect_editor_changed(editor)

    def _integer_editor(self, node: NodePayload) -> QWidget:
        value = self._value_text(node)
        minimum = self._numeric_value(node.get("minimum"))
        maximum = self._numeric_value(node.get("maximum"))
        increment = self._numeric_value(node.get("increment"))
        if (
            minimum is None
            or maximum is None
            or minimum < -2147483648
            or maximum > 2147483647
        ):
            editor = QLineEdit(self)
            editor.setText(value)
            editor.setPlaceholderText("Integer value")
            return editor
        spin = QSpinBox(self)
        spin.setRange(int(minimum), int(maximum))
        if increment is not None and increment > 0:
            spin.setSingleStep(max(1, int(increment)))
        try:
            spin.setValue(int(float(value)))
        except ValueError:
            spin.setValue(int(minimum))
        return spin

    def _float_editor(self, node: NodePayload) -> QWidget:
        value = self._value_text(node)
        editor = QDoubleSpinBox(self)
        editor.setDecimals(6)
        editor.setRange(
            self._numeric_value(node.get("minimum"), -1e12),
            self._numeric_value(node.get("maximum"), 1e12),
        )
        increment = self._numeric_value(node.get("increment"))
        if increment is not None and increment > 0:
            editor.setSingleStep(float(increment))
        try:
            editor.setValue(float(value))
        except ValueError:
            pass
        suffix = str(node.get("unit") or "")
        if suffix:
            editor.setSuffix(f" {suffix}")
        return editor

    def _clear_editor(self) -> None:
        while self._editor_layout.count():
            item = self._editor_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._editor = None

    def _connect_editor_changed(self, editor: QWidget) -> None:
        if isinstance(editor, QCheckBox):
            editor.toggled.connect(self._queue_current_value)
        elif isinstance(editor, QComboBox):
            editor.currentTextChanged.connect(self._queue_current_value)
        elif isinstance(editor, QSpinBox):
            editor.valueChanged.connect(self._queue_current_value)
        elif isinstance(editor, QDoubleSpinBox):
            editor.valueChanged.connect(self._queue_current_value)
        elif isinstance(editor, QLineEdit):
            editor.textEdited.connect(self._queue_current_value)

    def _queue_current_value(self, *_args: object) -> None:
        node = self._current_node
        if node is None:
            return
        editor = self._editor
        if self.sender() is not None and self.sender() is not editor:
            return
        if isinstance(editor, QCheckBox):
            value: object = editor.isChecked()
        elif isinstance(editor, QComboBox):
            value = editor.currentText()
        elif isinstance(editor, QSpinBox):
            value = editor.value()
        elif isinstance(editor, QDoubleSpinBox):
            value = editor.value()
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

    @staticmethod
    def _numeric_value(value: object, default: float | None = None) -> float | None:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _sort_key(node: NodePayload) -> tuple[str, str]:
        return (
            str(node.get("display_name") or node.get("name") or "").lower(),
            str(node.get("name") or "").lower(),
        )


class CameraSettingsWidget(QWidget):
    """Widget that exposes every implemented GenICam node reported by rotpy."""

    CAMERA_FEATURE_TABS = (
        (
            "Settings",
            (
                "AcquisitionControl",
                "AnalogControl",
                "DeviceControl",
                "ChunkDataControl",
            ),
            (
                "Acquisition",
                "Exposure",
                "Trigger",
                "Gain",
                "Gamma",
                "Balance",
                "Device",
                "Chunk",
            ),
        ),
        (
            "Image Format",
            ("ImageFormatControl",),
            (
                "Width",
                "Height",
                "Offset",
                "PixelFormat",
                "Binning",
                "Decimation",
                "Reverse",
                "TestPattern",
            ),
        ),
        (
            "Processing",
            (
                "ColorTransformationControl",
                "LUTControl",
                "SharpeningControl",
                "DefectCorrectionControl",
            ),
            (
                "Color",
                "BalanceWhite",
                "BalanceRatio",
                "Gamma",
                "LUT",
                "Sharpening",
                "Defect",
            ),
        ),
        (
            "GPIO",
            (
                "DigitalIOControl",
                "CounterAndTimerControl",
                "EventControl",
            ),
            ("Line", "UserOutput", "Counter", "Timer", "Event"),
        ),
        ("Sequencer", ("SequencerControl",), ("Sequencer",)),
        ("File Access", ("FileAccessControl",), ("File",)),
        (
            "Information",
            ("DeviceInformation",),
            (
                "DeviceModel",
                "DeviceVendor",
                "DeviceSerial",
                "DeviceVersion",
                "DeviceTemperature",
            ),
        ),
    )

    def __init__(self, grabber: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grabber = grabber
        self._pages: dict[str, _NodeMapPage] = {}
        self._pending_settings: dict[tuple[str, str], object] = {}
        self._applying_settings: dict[tuple[str, str], object] = {}
        self._loaded_once = False

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

        self._refresh_button.clicked.connect(self.refresh)
        grabber.camera_settings_snapshot_ready.connect(self._on_snapshot)
        grabber.camera_setting_changed.connect(self._on_setting_changed)

    def has_loaded(self) -> bool:
        return self._loaded_once

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

        self._loaded_once = True
        self._pending_settings.clear()
        self._applying_settings.clear()
        while self._tabs.count():
            widget = self._tabs.widget(0)
            self._tabs.removeTab(0)
            if widget is not None:
                widget.deleteLater()
        self._pages.clear()

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
        if camera_map is not None:
            nodes = camera_map.get("nodes")
            camera_nodes = nodes if isinstance(nodes, list) else []
            self._add_camera_feature_tabs(camera_nodes)

        for map_payload in payload.get("maps") or []:
            if not isinstance(map_payload, dict):
                continue
            map_key = str(map_payload.get("key") or "")
            if map_key == "camera":
                continue
            nodes = map_payload.get("nodes")
            title = str(map_payload.get("title") or map_key or "Node Map")
            page = self._create_page(map_key, nodes if isinstance(nodes, list) else [])
            self._pages[map_key] = page
            error = str(map_payload.get("error") or "")
            self._tabs.addTab(page, f"{title} (error)" if error else title)
        self._status_label.setText(str(payload.get("message") or "Camera settings loaded."))

    def _add_camera_feature_tabs(self, nodes: list[NodePayload]) -> None:
        added = False
        for title, category_terms, feature_prefixes in self.CAMERA_FEATURE_TABS:
            group_nodes = self._camera_group_nodes(
                nodes,
                category_terms,
                feature_prefixes,
            )
            if not self._has_visible_features(group_nodes):
                continue
            page = self._create_page("camera", group_nodes)
            self._pages[f"camera:{title}"] = page
            self._tabs.addTab(page, title)
            added = True

        page = self._create_page("camera", nodes)
        self._pages["camera:Features"] = page
        self._tabs.addTab(page, "Features" if added else "Camera")

    def _create_page(
        self,
        map_key: str,
        nodes: list[NodePayload],
    ) -> _NodeMapPage:
        page = _NodeMapPage(
            map_key,
            self._queue_setting,
            self._execute_command,
            self,
        )
        page.set_nodes(nodes)
        return page

    def _camera_group_nodes(
        self,
        nodes: list[NodePayload],
        category_terms: tuple[str, ...],
        feature_prefixes: tuple[str, ...],
    ) -> list[NodePayload]:
        categories = [node for node in nodes if self._is_category(node)]
        category_names = {
            self._node_name(node)
            for node in categories
            if self._node_matches(node, category_terms)
        }
        category_names.update(self._category_descendants(nodes, category_names))

        result: list[NodePayload] = []
        for node in nodes:
            if self._is_category(node):
                result.append(node)
                continue
            name = self._node_name(node)
            if name in category_names or self._node_matches(node, feature_prefixes):
                result.append(node)
        return result

    def _category_descendants(
        self,
        nodes: list[NodePayload],
        category_names: set[str],
    ) -> set[str]:
        nodes_by_name = {
            self._node_name(node): node
            for node in nodes
            if self._node_name(node)
        }
        pending = list(category_names)
        descendants: set[str] = set()
        while pending:
            name = pending.pop()
            node = nodes_by_name.get(name)
            if node is None:
                continue
            for child in node.get("children") or []:
                child_name = str(child)
                if child_name in descendants:
                    continue
                descendants.add(child_name)
                child_node = nodes_by_name.get(child_name)
                if child_node is not None and self._is_category(child_node):
                    pending.append(child_name)
        return descendants

    def _has_visible_features(self, nodes: list[NodePayload]) -> bool:
        return any(not self._is_category(node) for node in nodes)

    @staticmethod
    def _is_category(node: NodePayload) -> bool:
        return str(node.get("type") or "") == "category"

    @staticmethod
    def _node_name(node: NodePayload) -> str:
        return str(node.get("name") or "")

    @classmethod
    def _node_matches(
        cls,
        node: NodePayload,
        terms: tuple[str, ...],
    ) -> bool:
        text = cls._normalise(
            f"{node.get('name') or ''} {node.get('display_name') or ''}"
        )
        return any(cls._normalise(term) in text for term in terms)

    @staticmethod
    def _normalise(value: object) -> str:
        return "".join(ch for ch in str(value).lower() if ch.isalnum())

    def _on_setting_changed(self, payload: object) -> None:
        self._refresh_button.setEnabled(True)
        if not isinstance(payload, dict):
            self._status_label.setText("Camera setting response was invalid.")
            return
        self._status_label.setText(str(payload.get("message") or "Camera setting updated."))
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

    def _queue_setting(self, map_key: str, node_name: str, value: object) -> None:
        if not node_name:
            return
        self._pending_settings[(map_key, node_name)] = value
        self._set_pending_status()

    def _set_pending_status(self) -> None:
        pending_count = len(self._pending_settings)
        if pending_count == 1:
            self._status_label.setText("1 camera setting pending.")
        else:
            self._status_label.setText(f"{pending_count} camera settings pending.")

    def apply_pending_settings(self) -> bool:
        if not self._pending_settings:
            return False
        pending_items = [
            item
            for item in self._pending_settings.items()
            if item[0] not in self._applying_settings
        ]
        if not pending_items:
            return False
        self._status_label.setText(f"Writing {len(pending_items)} camera settings.")
        self._refresh_button.setEnabled(False)
        for (map_key, node_name), value in pending_items:
            self._applying_settings[(map_key, node_name)] = value
            self._grabber.request_camera_setting_update(map_key, node_name, value)
        return True

    def _update_pages_for_node(self, map_key: str, node: NodePayload) -> None:
        for page in self._pages.values():
            if page.map_key() == map_key:
                page.update_node(node)

    def _execute_command(self, map_key: str, node_name: str) -> None:
        if not node_name:
            return
        self._status_label.setText(f"Executing {node_name}.")
        self._refresh_button.setEnabled(False)
        self._grabber.request_camera_command_execute(map_key, node_name)


__all__ = ["CameraSettingsWidget"]
