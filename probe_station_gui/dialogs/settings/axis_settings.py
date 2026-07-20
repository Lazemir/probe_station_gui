"""Axis-oriented precision approach and coordinate calibration editor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from PySide6.QtCore import QLocale, QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.dialogs.settings.axis_calibration_preview import (
    AxisCalibrationPreview,
)
from probe_station_gui.settings.axis_calibration_config import (
    CALIBRATION_AXES,
    AxisCalibrationSettings,
    axis_unit,
    default_axis_calibrations,
    is_valid_calibration_curve,
)
from probe_station_gui.settings.axis_calibration_npz import (
    AxisCalibrationImportError,
    ImportedAxisCalibration,
    load_axis_calibration_npz,
)
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.precision_approach import (
    PrecisionApproachProfile,
    PrecisionApproachSettings,
)
from probe_station_gui.shared.wheel_guard import GuardedComboBox, GuardedDoubleSpinBox
from probe_station_gui.stage.axis_mapping import (
    AxisCalibrationCurve,
    CalibrationOutOfDomain,
    controller_to_physical,
)


@dataclass(frozen=True)
class _AxisPrecisionControls:
    enabled_checkbox: QCheckBox
    backlash_spin: QDoubleSpinBox
    direction_combo: QComboBox


class _CalibrationImportSignals(QObject):
    finished = Signal(str, object, str)


class _CalibrationImportTask(QRunnable):
    """Read one NPZ on the global worker pool."""

    def __init__(self, axis: str, path: str) -> None:
        super().__init__()
        self._axis = axis
        self._path = path
        self.signals = _CalibrationImportSignals()

    @Slot()
    def run(self) -> None:
        try:
            imported = load_axis_calibration_npz(
                self._path,
                expected_axis=self._axis,
            )
        except AxisCalibrationImportError as error:
            self.signals.finished.emit(self._axis, None, str(error))
        else:
            self.signals.finished.emit(self._axis, imported, "")


class AxisSettingsWidget(QWidget):
    """Edit identical calibration and precision controls for every axis."""

    calibration_imports_active_changed = Signal(bool)

    def __init__(
        self,
        calibrations: Mapping[str, AxisCalibrationSettings],
        approach_settings: PrecisionApproachSettings,
        parent: QWidget | None = None,
        *,
        position_source: object | None = None,
    ) -> None:
        super().__init__(parent)
        defaults = default_axis_calibrations()
        self._calibrations = {
            axis: calibrations.get(axis, defaults[axis]).clone()
            for axis in CALIBRATION_AXES
        }
        self._position_source = position_source
        self._pages: dict[str, QWidget] = {}
        self._page_layouts: dict[str, QVBoxLayout] = {}
        self._rows: dict[str, _AxisPrecisionControls] = {}
        self._calibration_checkboxes: dict[str, QCheckBox] = {}
        self._calibration_file_edits: dict[str, QLineEdit] = {}
        self._calibration_browse_buttons: dict[str, QPushButton] = {}
        self._calibration_reset_buttons: dict[str, QPushButton] = {}
        self._calibration_status_labels: dict[str, QLabel] = {}
        self._calibration_previews: dict[str, AxisCalibrationPreview] = {}
        self._calibration_tasks: set[_CalibrationImportTask] = set()
        self._calibration_tasks_running = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._axis_list = QListWidget(self)
        self._axis_list.addItems(CALIBRATION_AXES)
        self._axis_list.setFixedWidth(72)
        layout.addWidget(self._axis_list)
        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack, 1)

        for axis in CALIBRATION_AXES:
            page = self._create_axis_page(axis, approach_settings.profiles[axis])
            self._pages[axis] = page
            self._stack.addWidget(page)

        self._axis_list.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._axis_list.setCurrentRow(CALIBRATION_AXES.index("Z"))
        signal = getattr(position_source, "stage_position_changed", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(self._on_stage_position_changed)
        self._refresh_current_positions()

    def selected_axis(self) -> str:
        item = self._axis_list.currentItem()
        return item.text() if item is not None else "Z"

    def select_axis(self, axis: str) -> None:
        normalized = str(axis).upper()
        if normalized not in CALIBRATION_AXES:
            raise ValueError(f"Unsupported axis: {axis}")
        self._axis_list.setCurrentRow(CALIBRATION_AXES.index(normalized))

    def to_settings(self, settings: Settings) -> None:
        settings.precision_approach = PrecisionApproachSettings(
            {
                axis: PrecisionApproachProfile(
                    enabled=row.enabled_checkbox.isChecked(),
                    backlash=row.backlash_spin.value(),
                    final_direction=int(row.direction_combo.currentData() or 1),
                )
                for axis, row in self._rows.items()
            }
        )
        saved: dict[str, AxisCalibrationSettings] = {}
        for axis in CALIBRATION_AXES:
            calibration = self._calibrations[axis].clone()
            enabled = self._calibration_checkboxes[axis].isChecked()
            if enabled and not self._has_valid_curve(calibration):
                enabled = False
                self._calibration_checkboxes[axis].setChecked(False)
            calibration.enabled = enabled
            saved[axis] = calibration
        settings.axis_calibrations = saved

    def _create_axis_page(
        self,
        axis: str,
        profile: PrecisionApproachProfile,
    ) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self._page_layouts[axis] = layout
        layout.addWidget(self._create_precision_group(axis, profile))
        layout.addWidget(self._create_calibration_group(axis))
        layout.addStretch(1)
        if self._has_valid_curve(self._calibrations[axis]):
            self._ensure_preview(axis)
        return page

    def _create_precision_group(
        self,
        axis: str,
        profile: PrecisionApproachProfile,
    ) -> QGroupBox:
        group = QGroupBox("Precision approach", self)
        layout = QFormLayout(group)
        checkbox = QCheckBox("Enabled", group)
        checkbox.setChecked(profile.enabled)
        backlash = GuardedDoubleSpinBox(group)
        backlash.setLocale(QLocale.c())
        backlash.setDecimals(3)
        backlash.setRange(0.0, 1_000_000.0)
        backlash.setSingleStep(0.001)
        backlash.setSuffix(f" {axis_unit(axis)}")
        backlash.setValue(profile.backlash)
        direction = GuardedComboBox(group)
        direction.addItem("From lower coordinates (+)", 1)
        direction.addItem("From higher coordinates (−)", -1)
        direction.setCurrentIndex(max(0, direction.findData(profile.final_direction)))
        row = _AxisPrecisionControls(checkbox, backlash, direction)
        self._rows[axis] = row
        layout.addRow(checkbox)
        layout.addRow("Backlash", backlash)
        layout.addRow("Final direction", direction)
        checkbox.toggled.connect(
            lambda enabled, controls=row: self._set_precision_enabled(controls, enabled)
        )
        self._set_precision_enabled(row, profile.enabled)
        return group

    def _create_calibration_group(self, axis: str) -> QGroupBox:
        group = QGroupBox("Coordinate calibration", self)
        layout = QFormLayout(group)
        calibration = self._calibrations[axis]
        checkbox = QCheckBox("Enabled", group)
        checkbox.setChecked(calibration.enabled and self._has_valid_curve(calibration))
        file_edit = QLineEdit(calibration.calibration_file, group)
        file_edit.setReadOnly(True)
        browse = QPushButton("Browse", group)
        reset = QPushButton("Reset", group)
        file_row = QWidget(group)
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.addWidget(file_edit, 1)
        file_layout.addWidget(browse)
        file_layout.addWidget(reset)
        status = QLabel(self._calibration_status(axis, calibration), group)
        status.setWordWrap(True)
        self._calibration_checkboxes[axis] = checkbox
        self._calibration_file_edits[axis] = file_edit
        self._calibration_browse_buttons[axis] = browse
        self._calibration_reset_buttons[axis] = reset
        self._calibration_status_labels[axis] = status
        layout.addRow(checkbox)
        layout.addRow("File", file_row)
        layout.addRow("Status", status)
        browse.clicked.connect(
            lambda _checked=False, current_axis=axis: self._browse_calibration(current_axis)
        )
        reset.clicked.connect(
            lambda _checked=False, current_axis=axis: self._reset_calibration(current_axis)
        )
        checkbox.toggled.connect(
            lambda enabled, current_axis=axis: self._calibration_toggled(
                current_axis,
                enabled,
            )
        )
        return group

    @staticmethod
    def _set_precision_enabled(row: _AxisPrecisionControls, enabled: bool) -> None:
        row.backlash_spin.setEnabled(enabled)
        row.direction_combo.setEnabled(enabled)

    def _browse_calibration(self, axis: str) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose calibration file",
            self._calibration_file_edits[axis].text(),
            "NumPy calibration (*.npz)",
        )
        if path:
            self._start_calibration_import(axis, path)

    def _start_calibration_import(self, axis: str, path: str) -> None:
        task = _CalibrationImportTask(axis, path)
        task.signals.finished.connect(self._finish_calibration_import)
        was_inactive = not self._calibration_tasks
        self._calibration_tasks.add(task)
        self._calibration_tasks_running = len(self._calibration_tasks)
        if was_inactive:
            self.calibration_imports_active_changed.emit(True)
        self._set_calibration_controls_enabled(axis, False)
        self._calibration_status_labels[axis].setText("Loading calibration")
        QThreadPool.globalInstance().start(task)

    @Slot(str, object, str)
    def _finish_calibration_import(
        self,
        axis: str,
        imported: ImportedAxisCalibration | None,
        error: str,
    ) -> None:
        sender = self.sender()
        self._calibration_tasks = {
            task for task in self._calibration_tasks if task.signals is not sender
        }
        self._calibration_tasks_running = len(self._calibration_tasks)
        became_inactive = not self._calibration_tasks
        self._set_calibration_controls_enabled(axis, True)
        if imported is None:
            self._calibration_status_labels[axis].setText(error)
            if became_inactive:
                self.calibration_imports_active_changed.emit(False)
            return
        replacement = AxisCalibrationSettings(
            enabled=True,
            calibration_file=imported.calibration_file,
            controller_points=list(imported.controller_points),
            physical_points=list(imported.physical_points),
        )
        self._calibrations[axis] = replacement
        self._calibration_file_edits[axis].setText(imported.calibration_file)
        self._calibration_status_labels[axis].setText(
            self._calibration_status(axis, replacement)
        )
        self._calibration_checkboxes[axis].setChecked(True)
        preview = self._ensure_preview(axis)
        preview.set_curve(replacement.controller_points, replacement.physical_points)
        self._refresh_current_position(axis)
        if became_inactive:
            self.calibration_imports_active_changed.emit(False)

    def _reset_calibration(self, axis: str) -> None:
        self._calibrations[axis] = AxisCalibrationSettings()
        self._calibration_checkboxes[axis].setChecked(False)
        self._calibration_file_edits[axis].clear()
        self._calibration_status_labels[axis].clear()
        preview = self._calibration_previews.pop(axis, None)
        if preview is not None:
            self._page_layouts[axis].removeWidget(preview)
            preview.clear_curve()
            preview.hide()
            preview.deleteLater()

    def _calibration_toggled(self, axis: str, enabled: bool) -> None:
        if enabled and not self._has_valid_curve(self._calibrations[axis]):
            self._calibration_checkboxes[axis].setChecked(False)
            self._calibration_status_labels[axis].setText("Choose a calibration file")
            return
        self._refresh_current_position(axis)

    def _ensure_preview(self, axis: str) -> AxisCalibrationPreview:
        existing = self._calibration_previews.get(axis)
        if existing is not None:
            return existing
        preview = AxisCalibrationPreview(axis, self._pages.get(axis, self))
        calibration = self._calibrations[axis]
        preview.set_curve(calibration.controller_points, calibration.physical_points)
        layout = self._page_layouts[axis]
        layout.insertWidget(max(0, layout.count() - 1), preview)
        self._calibration_previews[axis] = preview
        return preview

    @Slot(object)
    def _on_stage_position_changed(self, _position: object) -> None:
        self._refresh_current_positions()

    def _refresh_current_positions(self) -> None:
        for axis in tuple(self._calibration_previews):
            self._refresh_current_position(axis)

    def _refresh_current_position(self, axis: str) -> None:
        preview = self._calibration_previews.get(axis)
        if preview is None:
            return
        calibration = self._calibrations[axis]
        if not self._calibration_checkboxes[axis].isChecked():
            preview.set_outside_range(False)
            return
        getter = getattr(self._position_source, "latest_machine_position", None)
        machine_position = getter() if callable(getter) else None
        index = CALIBRATION_AXES.index(axis)
        if not isinstance(machine_position, (tuple, list)) or index >= len(machine_position):
            preview.set_outside_range(False)
            return
        controller_value = float(machine_position[index])
        curve = AxisCalibrationCurve(
            tuple(calibration.controller_points),
            tuple(calibration.physical_points),
        )
        try:
            physical_value = controller_to_physical(curve, controller_value)
        except CalibrationOutOfDomain:
            preview.set_outside_range(True)
            return
        preview.set_current_position(controller_value, physical_value, visible=True)

    @staticmethod
    def _has_valid_curve(calibration: AxisCalibrationSettings) -> bool:
        return is_valid_calibration_curve(
            calibration.controller_points,
            calibration.physical_points,
        )

    @classmethod
    def _calibration_status(
        cls,
        axis: str,
        calibration: AxisCalibrationSettings,
    ) -> str:
        if not cls._has_valid_curve(calibration):
            return ""
        points = calibration.controller_points
        return (
            f"{len(points)} points · "
            f"{points[0]:.3f}–{points[-1]:.3f} {axis_unit(axis)}"
        )

    def _set_calibration_controls_enabled(self, axis: str, enabled: bool) -> None:
        self._calibration_browse_buttons[axis].setEnabled(enabled)
        self._calibration_reset_buttons[axis].setEnabled(enabled)


__all__ = ["AxisSettingsWidget"]
