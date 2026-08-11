"""Qt adapter for Design route files, offsets, edits, and table selection."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.design.navigation_geometry import format_point
from probe_station_gui.design.selection_model import route_entity_id
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.shared.wheel_guard import GuardedDoubleSpinBox as QDoubleSpinBox
from probe_station_gui.views.design_navigator_enablement import (
    DesignNavigatorEnablement,
)


class DesignRouteControls(QGroupBox):
    """Render the current route and translate route-edit input."""

    new_requested = Signal()
    open_requested = Signal(str)
    save_requested = Signal()
    save_as_requested = Signal(str)
    add_current_requested = Signal()
    remove_selected_requested = Signal()
    clear_requested = Signal()
    route_selected = Signal(int)
    selection_requested = Signal(object, str)
    offsets_changed = Signal(float, float, float, float)

    def __init__(
        self,
        *,
        tool_controls: QWidget,
        run_controls: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Probe Route", parent)
        self._route: MeasurementRoute | None = None
        self._selected_index = -1
        self._selected_route_ids: frozenset[str] = frozenset()
        self._design_directory: Path | None = None
        self._updating = False

        layout = QVBoxLayout(self)
        self.content_layout = layout
        file_buttons = QHBoxLayout()
        self.new_button = QPushButton("New", self)
        self.open_button = QPushButton("Open...", self)
        self.save_button = QPushButton("Save", self)
        self.save_as_button = QPushButton("Save As...", self)
        file_buttons.addWidget(self.new_button)
        file_buttons.addWidget(self.open_button)
        file_buttons.addWidget(self.save_button)
        file_buttons.addWidget(self.save_as_button)
        layout.addLayout(file_buttons)
        self.route_label = QLabel("No route loaded.", self)
        self.route_label.setWordWrap(True)
        layout.addWidget(self.route_label)

        layout.addWidget(tool_controls)

        offset_layout = QGridLayout()
        offset_layout.addWidget(QLabel("Needle", self), 0, 0)
        offset_layout.addWidget(QLabel("dx", self), 0, 1)
        offset_layout.addWidget(QLabel("dy", self), 0, 2)
        offset_layout.addWidget(QLabel("1", self), 1, 0)
        offset_layout.addWidget(QLabel("2", self), 2, 0)
        self.needle_1_dx_spin = self._make_offset_spinbox()
        self.needle_1_dy_spin = self._make_offset_spinbox()
        self.needle_2_dx_spin = self._make_offset_spinbox()
        self.needle_2_dy_spin = self._make_offset_spinbox()
        offset_layout.addWidget(self.needle_1_dx_spin, 1, 1)
        offset_layout.addWidget(self.needle_1_dy_spin, 1, 2)
        offset_layout.addWidget(self.needle_2_dx_spin, 2, 1)
        offset_layout.addWidget(self.needle_2_dy_spin, 2, 2)
        layout.addLayout(offset_layout)

        self.route_table = QTableWidget(0, 5, self)
        self.route_table.setHorizontalHeaderLabels(["#", "Label", "Center", "N1", "N2"])
        self.route_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.route_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.route_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.route_table)

        edit_buttons = QHBoxLayout()
        self.add_current_button = QPushButton("Add Current", self)
        self.remove_button = QPushButton("Remove", self)
        self.clear_button = QPushButton("Clear", self)
        edit_buttons.addWidget(self.add_current_button)
        edit_buttons.addWidget(self.remove_button)
        edit_buttons.addWidget(self.clear_button)
        layout.addLayout(edit_buttons)
        layout.addWidget(run_controls)

        self.new_button.clicked.connect(self.new_requested.emit)
        self.open_button.clicked.connect(self._choose_open_file)
        self.save_button.clicked.connect(self.save_requested.emit)
        self.save_as_button.clicked.connect(self._choose_save_file)
        self.add_current_button.clicked.connect(self.add_current_requested.emit)
        self.remove_button.clicked.connect(self.remove_selected_requested.emit)
        self.clear_button.clicked.connect(self.clear_requested.emit)
        self.route_table.itemSelectionChanged.connect(self._on_selection_changed)
        for spinbox in self._offset_spinboxes():
            spinbox.valueChanged.connect(self._emit_offsets_changed)

    @property
    def route(self) -> MeasurementRoute | None:
        return self._route

    @property
    def selected_index(self) -> int:
        return self._selected_index

    def set_design_directory(self, directory: Path | None) -> None:
        self._design_directory = directory

    def set_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> None:
        previous_route = self._route
        previous_count = len(previous_route.points) if previous_route is not None else 0
        previous_rows = self.selected_rows()
        self._route = route
        self._selected_index = selected_route_point_index
        self._updating = True
        try:
            self.route_table.blockSignals(True)
            self.route_table.clearSelection()
            if route is None:
                self.route_label.setText("No route loaded.")
                self.route_table.setRowCount(0)
                self._set_offset_values(0.0, 0.0, 0.0, 0.0)
                return

            rows_to_select = (
                previous_rows
                if route is previous_route
                and previous_count == len(route.points)
                and previous_rows
                else [selected_route_point_index]
            )
            path_text = str(route.path) if route.path is not None else "Unsaved route."
            self.route_label.setText(f"{route.name} | {path_text}")
            self.route_table.setRowCount(len(route.points))
            for row, point in enumerate(route.points):
                hits = route.needle_hits_for_point(point)
                needle_1 = hits[0][1] if len(hits) > 0 else point.camera_center
                needle_2 = hits[1][1] if len(hits) > 1 else point.camera_center
                values = (
                    str(row + 1),
                    point.label,
                    format_point(point.camera_center),
                    format_point(needle_1),
                    format_point(needle_2),
                )
                for column, value in enumerate(values):
                    self.route_table.setItem(row, column, QTableWidgetItem(value))
            self._select_rows(rows_to_select)
            offsets = route.needle_offsets[:2]
            if len(offsets) >= 2:
                self._set_offset_values(
                    offsets[0].dx,
                    offsets[0].dy,
                    offsets[1].dx,
                    offsets[1].dy,
                )
            else:
                self._set_offset_values(0.0, 0.0, 0.0, 0.0)
        finally:
            self.route_table.blockSignals(False)
            self._updating = False

    def set_selection(
        self,
        rows: list[int],
        *,
        selected_route_ids: frozenset[str],
    ) -> None:
        self._selected_route_ids = selected_route_ids
        self._updating = True
        try:
            self.route_table.blockSignals(True)
            self.route_table.clearSelection()
            self._select_rows(rows)
        finally:
            self.route_table.blockSignals(False)
            self._updating = False
        self._selected_index = rows[0] if rows else -1

    def apply_enablement(self, state: DesignNavigatorEnablement) -> None:
        self.new_button.setEnabled(state.can_edit_design)
        self.open_button.setEnabled(state.can_edit_design)
        self.save_button.setEnabled(state.can_save_route)
        self.save_as_button.setEnabled(state.can_save_route_as)
        for spinbox in self._offset_spinboxes():
            spinbox.setEnabled(state.can_edit_route_offsets)
        self.route_table.setEnabled(state.has_route and not state.design_load_pending)
        self.add_current_button.setEnabled(state.can_add_current_route_point)
        self.remove_button.setEnabled(state.can_remove_route_point)
        self.clear_button.setEnabled(state.can_clear_route)

    def selected_rows(self) -> list[int]:
        selection_model = self.route_table.selectionModel()
        if selection_model is None:
            return []
        return sorted({index.row() for index in selection_model.selectedRows()})

    def current_offset_vectors(self) -> list[tuple[float, float]]:
        if self._route is not None and len(self._route.needle_offsets) >= 2:
            return [
                (self._route.needle_offsets[0].dx, self._route.needle_offsets[0].dy),
                (self._route.needle_offsets[1].dx, self._route.needle_offsets[1].dy),
            ]
        return [
            (self.needle_1_dx_spin.value(), self.needle_1_dy_spin.value()),
            (self.needle_2_dx_spin.value(), self.needle_2_dy_spin.value()),
        ]

    def selection_modifiers(self) -> Qt.KeyboardModifiers:
        return QApplication.keyboardModifiers()

    def _make_offset_spinbox(self) -> QDoubleSpinBox:
        spinbox = QDoubleSpinBox(self)
        spinbox.setRange(-1_000_000_000.0, 1_000_000_000.0)
        spinbox.setDecimals(4)
        spinbox.setSingleStep(1.0)
        spinbox.setAlignment(Qt.AlignRight)
        spinbox.setMaximumWidth(96)
        return spinbox

    def _offset_spinboxes(self) -> tuple[QDoubleSpinBox, ...]:
        return (
            self.needle_1_dx_spin,
            self.needle_1_dy_spin,
            self.needle_2_dx_spin,
            self.needle_2_dy_spin,
        )

    def _set_offset_values(
        self,
        needle_1_dx: float,
        needle_1_dy: float,
        needle_2_dx: float,
        needle_2_dy: float,
    ) -> None:
        values = (needle_1_dx, needle_1_dy, needle_2_dx, needle_2_dy)
        for spinbox, value in zip(self._offset_spinboxes(), values):
            spinbox.blockSignals(True)
            spinbox.setValue(float(value))
            spinbox.blockSignals(False)

    def _emit_offsets_changed(self, *_unused: object) -> None:
        if self._updating or self._route is None:
            return
        self.offsets_changed.emit(
            self.needle_1_dx_spin.value(),
            self.needle_1_dy_spin.value(),
            self.needle_2_dx_spin.value(),
            self.needle_2_dy_spin.value(),
        )

    def _select_rows(self, rows: list[int]) -> None:
        if self._route is None:
            return
        selection_model = self.route_table.selectionModel()
        if selection_model is None:
            return
        for row in sorted({row for row in rows if 0 <= row < len(self._route.points)}):
            selection_model.select(
                self.route_table.model().index(row, 0),
                QItemSelectionModel.Select | QItemSelectionModel.Rows,
            )

    def _on_selection_changed(self) -> None:
        if self._updating:
            return
        rows = self.selected_rows()
        self._selected_index = rows[0] if rows else -1
        self.route_selected.emit(self._selected_index)
        route_ids = {
            route_entity_id(self._route.points[index].id)
            for index in rows
            if self._route is not None and 0 <= index < len(self._route.points)
        }
        modifiers = self.selection_modifiers()
        if modifiers & Qt.ControlModifier:
            self.selection_requested.emit(
                self._selected_route_ids ^ route_ids,
                "invert",
            )
        elif modifiers & Qt.ShiftModifier:
            self.selection_requested.emit(route_ids, "add")
        else:
            self.selection_requested.emit(route_ids, "replace")

    def _choose_open_file(self) -> None:  # pragma: no cover - native UI interaction
        start_directory = ""
        if self._route is not None and self._route.path is not None:
            start_directory = str(self._route.path.parent)
        elif self._design_directory is not None:
            start_directory = str(self._design_directory)
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Open Probe Route",
            start_directory,
            "Probe routes (*.probe-route.json *.json);;All files (*)",
        )
        if path:
            self.open_requested.emit(path)

    def _choose_save_file(self) -> None:  # pragma: no cover - native UI interaction
        if self._route is None:
            return
        start_directory = ""
        if self._route.path is not None:
            start_directory = str(self._route.path.parent)
        elif self._design_directory is not None:
            start_directory = str(self._design_directory)
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Probe Route",
            start_directory,
            "Probe routes (*.probe-route.json);;JSON files (*.json);;All files (*)",
        )
        if path:
            self.save_as_requested.emit(path)


__all__ = ["DesignRouteControls"]
