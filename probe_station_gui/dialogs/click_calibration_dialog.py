"""Click-to-move calibration dialog."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.design.objective_offsets import base_objective_name
from probe_station_gui.settings.manager import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
    normalize_objective_name,
    ordered_objective_names,
)
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox


class ClickCalibrationDialog(QDialog):
    """Small objective-aware click-to-move calibration editor."""

    objective_selected: Signal = Signal(str)
    reset_requested: Signal = Signal()
    add_requested: Signal = Signal()
    delete_requested: Signal = Signal(str)
    offset_reference_requested: Signal = Signal()
    offset_save_requested: Signal = Signal()
    offset_reset_requested: Signal = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Click-to-Move Calibration")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._updating = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._objective_combo = QComboBox(self)
        self._objective_combo.currentIndexChanged.connect(
            self._on_objective_changed
        )
        form.addRow(QLabel("Objective", self), self._objective_combo)

        self._matrix_cells: list[list[QLabel]] = []
        form.addRow(QLabel("pixels_to_mm", self), self._create_matrix_table())
        self._offset_label = QLabel("--", self)
        self._offset_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(QLabel("objective_xy_offset", self), self._offset_label)
        layout.addLayout(form)

        offset_button_layout = QHBoxLayout()
        self._set_offset_reference_button = QPushButton("Set Reference", self)
        self._save_offset_button = QPushButton("Save Offset", self)
        self._reset_offset_button = QPushButton("Reset Offset", self)
        offset_button_layout.addWidget(self._set_offset_reference_button)
        offset_button_layout.addWidget(self._save_offset_button)
        offset_button_layout.addWidget(self._reset_offset_button)
        offset_button_layout.addStretch(1)
        layout.addLayout(offset_button_layout)

        button_layout = QHBoxLayout()
        self._reset_button = QPushButton("Reset", self)
        self._add_button = QPushButton("Add", self)
        self._delete_button = QPushButton("Delete", self)
        close_button = QPushButton("Close", self)
        button_layout.addWidget(self._reset_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self._add_button)
        button_layout.addWidget(self._delete_button)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)

        self._reset_button.clicked.connect(self.reset_requested.emit)
        self._add_button.clicked.connect(self.add_requested.emit)
        self._delete_button.clicked.connect(self._request_delete)
        self._set_offset_reference_button.clicked.connect(
            self.offset_reference_requested.emit
        )
        self._save_offset_button.clicked.connect(self.offset_save_requested.emit)
        self._reset_offset_button.clicked.connect(self.offset_reset_requested.emit)
        close_button.clicked.connect(self.close)

    def set_objectives(self, objectives: ObjectivesSettings) -> None:
        names = ordered_objective_names(objectives.objectives)
        active_name = normalize_objective_name(objectives.active_name)
        if active_name and active_name not in names:
            names.append(active_name)

        self._updating = True
        try:
            self._objective_combo.clear()
            for name in names:
                self._objective_combo.addItem(name, name)
            index = self._objective_combo.findData(active_name)
            if index < 0:
                index = 0
            self._objective_combo.setCurrentIndex(index)
        finally:
            self._updating = False

        self._delete_button.setEnabled(len(names) > 1)
        name = str(self._objective_combo.currentData())
        profile = objectives.objectives.get(name)
        self._set_matrix(profile)
        self._set_offset(objectives, name, profile)

    def _on_objective_changed(self, _index: int) -> None:
        if self._updating:
            return
        name = str(self._objective_combo.currentData() or "")
        if name:
            self.objective_selected.emit(name)

    def _request_delete(self) -> None:
        name = str(self._objective_combo.currentData() or "")
        if name:
            self.delete_requested.emit(name)

    def _create_matrix_table(self) -> QWidget:
        table = QWidget(self)
        layout = QGridLayout(table)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(6)

        headers = (
            (0, 0, "bed \\ camera"),
            (0, 1, "X_camera px"),
            (0, 2, "Y_camera px"),
            (1, 0, "X_bed mm"),
            (2, 0, "Y_bed mm"),
        )
        for row, column, text in headers:
            label = QLabel(text, table)
            label.setStyleSheet("font-weight: 600;")
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label, row, column)

        for row in range(2):
            row_cells: list[QLabel] = []
            for column in range(2):
                value = QLabel("--", table)
                value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                value.setMinimumWidth(120)
                value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                value.setStyleSheet(
                    "font-family: Consolas, monospace; padding: 2px 6px;"
                )
                layout.addWidget(value, row + 1, column + 1)
                row_cells.append(value)
            self._matrix_cells.append(row_cells)
        return table

    def _set_matrix(self, profile: ObjectiveCalibrationSettings | None) -> None:
        matrix = self._matrix_from_profile(profile)
        for row, cells in enumerate(self._matrix_cells):
            for column, cell in enumerate(cells):
                if matrix is None:
                    cell.setText("--")
                else:
                    cell.setText(self._format_matrix_value(matrix[row][column]))

    def _set_offset(
        self,
        objectives: ObjectivesSettings,
        objective_name: str,
        profile: ObjectiveCalibrationSettings | None,
    ) -> None:
        name = normalize_objective_name(objective_name)
        if not name:
            self._offset_label.setText("--")
            return
        if name == base_objective_name(objectives.objectives):
            self._offset_label.setText("X +0.0000 mm, Y +0.0000 mm (base)")
            return
        if profile is None or not profile.xy_offset_configured:
            self._offset_label.setText("--")
            return
        self._offset_label.setText(
            f"X {float(profile.xy_offset_x_mm):+.4f} mm, "
            f"Y {float(profile.xy_offset_y_mm):+.4f} mm"
        )

    @staticmethod
    def _matrix_from_profile(
        profile: ObjectiveCalibrationSettings | None,
    ) -> tuple[tuple[float, float], tuple[float, float]] | None:
        if profile is None or not profile.xy_calibration_configured:
            return None
        try:
            row_x = profile.pixels_to_mm[0]
            row_y = profile.pixels_to_mm[1]
            matrix = (
                (float(row_x[0]), float(row_x[1])),
                (float(row_y[0]), float(row_y[1])),
            )
        except (TypeError, ValueError, IndexError):
            return None
        if not all(math.isfinite(value) for row in matrix for value in row):
            return None
        return matrix

    @staticmethod
    def _format_matrix_value(value: float) -> str:
        return f"{value:.9g}"
