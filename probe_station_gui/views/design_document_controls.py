"""Qt adapter for the Design document and layer controls."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.navigation_geometry import format_bounds
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox
from probe_station_gui.views.design_navigator_enablement import (
    DesignNavigatorEnablement,
)


class DesignDocumentControls(QWidget):
    """Render document metadata and translate document-control input."""

    load_requested = Signal(str)
    unload_requested = Signal()
    top_cell_changed = Signal(str)
    layer_visibility_changed = Signal(int, int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document: DesignDocument | None = None
        self._dialog_directory = ""
        self._load_pending = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(8)

        file_group = QGroupBox("Design", self)
        file_layout = QGridLayout(file_group)
        self.load_button = QPushButton("Load GDS...", file_group)
        self.unload_button = QPushButton("Unload", file_group)
        self.top_cell_combo = QComboBox(file_group)
        self.document_label = QLabel("No design loaded.", file_group)
        self.document_label.setWordWrap(True)
        file_layout.addWidget(self.load_button, 0, 0)
        file_layout.addWidget(self.unload_button, 0, 1)
        file_layout.addWidget(QLabel("Top cell:", file_group), 1, 0)
        file_layout.addWidget(self.top_cell_combo, 1, 1)
        file_layout.addWidget(self.document_label, 2, 0, 1, 2)
        root_layout.addWidget(file_group)

        layer_group = QGroupBox("Layers", self)
        layer_layout = QVBoxLayout(layer_group)
        self.layer_list = QListWidget(layer_group)
        layer_layout.addWidget(self.layer_list)
        root_layout.addWidget(layer_group)

        self.load_button.clicked.connect(self._choose_file)
        self.unload_button.clicked.connect(self.unload_requested.emit)
        self.top_cell_combo.currentTextChanged.connect(self._on_top_cell_changed)
        self.layer_list.itemChanged.connect(self._on_layer_item_changed)

    @property
    def document(self) -> DesignDocument | None:
        return self._document

    @property
    def load_pending(self) -> bool:
        return self._load_pending

    def set_document(self, document: DesignDocument | None) -> None:
        self._document = document
        if document is None:
            self.document_label.setText("No design loaded.")
            self.top_cell_combo.blockSignals(True)
            self.top_cell_combo.clear()
            self.top_cell_combo.blockSignals(False)
            self.layer_list.blockSignals(True)
            self.layer_list.clear()
            self.layer_list.blockSignals(False)
            return

        self.document_label.setText(
            f"{document.path.name} | bounds {format_bounds(document.bounds)}"
        )
        self.top_cell_combo.blockSignals(True)
        self.top_cell_combo.clear()
        self.top_cell_combo.addItems(document.cell_names)
        self.top_cell_combo.setCurrentText(document.top_cell_name)
        self.top_cell_combo.blockSignals(False)
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for layer_key in document.layer_keys():
            item = QListWidgetItem(f"Layer {layer_key[0]}/{layer_key[1]}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, layer_key)
            item.setCheckState(
                Qt.Checked if layer_key in document.visible_layers else Qt.Unchecked
            )
            self.layer_list.addItem(item)
        self.layer_list.blockSignals(False)

    def set_load_pending(self, pending: bool) -> None:
        self._load_pending = bool(pending)

    def set_dialog_directory(self, directory: str | Path | None) -> None:
        self._dialog_directory = "" if directory is None else str(Path(directory))

    def apply_enablement(self, state: DesignNavigatorEnablement) -> None:
        enabled = state.can_use_document_controls
        self.unload_button.setEnabled(enabled)
        self.top_cell_combo.setEnabled(enabled)
        self.layer_list.setEnabled(enabled)

    def _choose_file(self) -> None:  # pragma: no cover - native UI interaction
        start_directory = self._dialog_directory
        if not start_directory and self._document is not None:
            start_directory = str(self._document.path.parent)
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Load GDS design",
            start_directory,
            "Layout files (*.gds *.gds2 *.oas *.oasis);;All files (*.*)",
        )
        if path:
            self.load_requested.emit(path)

    def _on_top_cell_changed(self, cell_name: str) -> None:
        if self._document is None or not cell_name:
            return
        if cell_name == self._document.top_cell_name:
            return
        self.top_cell_changed.emit(cell_name)

    def _on_layer_item_changed(self, item: QListWidgetItem) -> None:
        layer_key = item.data(Qt.UserRole)
        if (
            not isinstance(layer_key, tuple)
            or len(layer_key) != 2
            or not all(isinstance(value, int) for value in layer_key)
        ):
            return
        self.layer_visibility_changed.emit(
            layer_key[0],
            layer_key[1],
            item.checkState() == Qt.Checked,
        )


__all__ = ["DesignDocumentControls"]
