"""Axis-oriented editor for precision approach and calibration settings."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import (
    QLocale,
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
    Slot,
)
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

from probe_station_gui.settings.axis_calibration_npz import (
    AxisCalibrationImportError,
    ImportedAxisCalibration,
    LINEAR_INTERPOLATION_MODEL,
    load_axis_calibration_npz,
)
from probe_station_gui.settings.axis_calibration_config import (
    AxisACalibrationSettings,
    AxisZCalibrationSettings,
)
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.precision_approach import (
    PRECISION_APPROACH_AXES,
    PrecisionApproachProfile,
    PrecisionApproachSettings,
)
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox,
    GuardedDoubleSpinBox,
)


@dataclass(frozen=True)
class _AxisPrecisionControls:
    """Widgets that edit one axis's final loaded-side preference."""

    enabled_checkbox: QCheckBox
    backlash_spin: QDoubleSpinBox
    direction_combo: QComboBox


class _CalibrationImportSignals(QObject):
    """Deliver a completed calibration import to the GUI thread."""

    finished = Signal(str, object, str)


class _CalibrationImportTask(QRunnable):
    """Read and validate one calibration file outside the GUI thread."""

    def __init__(self, axis: str, path: str, final_direction: int) -> None:
        super().__init__()
        self._axis = axis
        self._path = path
        self._final_direction = final_direction
        self.signals = _CalibrationImportSignals()

    @Slot()
    def run(self) -> None:
        try:
            imported = load_axis_calibration_npz(
                self._path,
                axis=self._axis,
                final_direction=self._final_direction,
            )
        except AxisCalibrationImportError as exc:
            self.signals.finished.emit(self._axis, None, str(exc))
        else:
            self.signals.finished.emit(self._axis, imported, "")


class AxisSettingsWidget(QWidget):
    """Edit precision approach and coordinate calibration by stage axis."""

    calibration_imports_active_changed = Signal(bool)

    def __init__(
        self,
        axis_a_calibration: AxisACalibrationSettings,
        axis_z_calibration: AxisZCalibrationSettings,
        approach_settings: PrecisionApproachSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._axis_a_calibration = axis_a_calibration.clone()
        self._axis_z_calibration = axis_z_calibration.clone()
        self._pages: dict[str, QWidget] = {}
        self._rows: dict[str, _AxisPrecisionControls] = {}
        self._calibration_messages: dict[str, QLabel] = {}
        self._calibration_checkboxes: dict[str, QCheckBox] = {}
        self._calibration_file_edits: dict[str, QLineEdit] = {}
        self._calibration_browse_buttons: dict[str, QPushButton] = {}
        self._calibration_reset_buttons: dict[str, QPushButton] = {}
        self._calibration_status_labels: dict[str, QLabel] = {}
        self._calibration_tasks: set[_CalibrationImportTask] = set()
        self._calibration_tasks_running = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._axis_list = QListWidget(self)
        self._axis_list.addItems(PRECISION_APPROACH_AXES)
        self._axis_list.setFixedWidth(72)
        layout.addWidget(self._axis_list)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack, 1)

        for axis in PRECISION_APPROACH_AXES:
            page = self._create_axis_page(axis, approach_settings.profiles[axis])
            self._pages[axis] = page
            self._stack.addWidget(page)

        self._axis_list.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._axis_list.setCurrentRow(PRECISION_APPROACH_AXES.index("Z"))

    def selected_axis(self) -> str:
        """Return the currently displayed stage axis."""

        item = self._axis_list.currentItem()
        return item.text() if item is not None else "Z"

    def select_axis(self, axis: str) -> None:
        """Show the page for a supported stage axis."""

        try:
            index = PRECISION_APPROACH_AXES.index(axis)
        except ValueError as exc:
            raise ValueError(f"Unsupported axis: {axis}") from exc
        self._axis_list.setCurrentRow(index)

    def to_settings(self, settings: Settings) -> None:
        """Persist precision profiles and the current calibration snapshots."""

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
        for axis in ("A", "Z"):
            calibration = self._calibration_for_axis(axis)
            assert calibration is not None
            saved = calibration.clone()
            if saved.source.strip().lower().endswith(".png"):
                saved.source = ""
            enabled = self._calibration_checkboxes[axis].isChecked()
            if saved.model == LINEAR_INTERPOLATION_MODEL and not (
                self._has_valid_interpolation_points(saved)
                and self._direction_matches(axis, saved)
            ):
                enabled = False
                self._calibration_checkboxes[axis].setChecked(False)
            saved.configured = enabled
            if axis == "A":
                settings.axis_a_calibration = saved
            else:
                settings.axis_z_calibration = saved

    def _create_axis_page(
        self,
        axis: str,
        profile: PrecisionApproachProfile,
    ) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._create_precision_group(axis, profile))
        layout.addWidget(self._create_calibration_group(axis))
        layout.addStretch(1)
        return page

    def _create_precision_group(
        self,
        axis: str,
        profile: PrecisionApproachProfile,
    ) -> QGroupBox:
        group = QGroupBox("Precision approach", self)
        layout = QFormLayout(group)
        enabled_checkbox = QCheckBox("Enabled", group)
        enabled_checkbox.setChecked(profile.enabled)
        backlash_spin = GuardedDoubleSpinBox(group)
        backlash_spin.setLocale(QLocale.c())
        backlash_spin.setDecimals(3)
        backlash_spin.setRange(0.0, 1_000_000.0)
        backlash_spin.setSingleStep(0.001)
        backlash_spin.setSuffix(f" {_axis_unit(axis)}")
        backlash_spin.setValue(profile.backlash)
        direction_combo = GuardedComboBox(group)
        direction_combo.addItem("From lower coordinates (+)", 1)
        direction_combo.addItem("From higher coordinates (\N{MINUS SIGN})", -1)
        direction_combo.setCurrentIndex(
            max(0, direction_combo.findData(profile.final_direction))
        )
        row = _AxisPrecisionControls(
            enabled_checkbox,
            backlash_spin,
            direction_combo,
        )
        self._rows[axis] = row

        layout.addRow(enabled_checkbox)
        layout.addRow("Backlash", backlash_spin)
        layout.addRow("Final direction", direction_combo)

        enabled_checkbox.toggled.connect(
            lambda enabled, controls=row: self._set_precision_enabled(controls, enabled)
        )
        direction_combo.currentIndexChanged.connect(
            lambda _index, current_axis=axis: self._direction_changed(current_axis)
        )
        self._set_precision_enabled(row, profile.enabled)
        return group

    def _create_calibration_group(self, axis: str) -> QGroupBox:
        group = QGroupBox("Coordinate calibration", self)
        layout = QFormLayout(group)
        calibration = self._calibration_for_axis(axis)
        if calibration is None:
            message = QLabel("No calibration curve for this axis.", group)
            self._calibration_messages[axis] = message
            layout.addRow(message)
            return group

        checkbox = QCheckBox("Enabled", group)
        checkbox.setChecked(calibration.configured)
        file_edit = QLineEdit(calibration.calibration_file, group)
        file_edit.setReadOnly(True)
        browse_button = QPushButton("Browse", group)
        reset_button = QPushButton("Reset", group)
        file_row = QWidget(group)
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.addWidget(file_edit, 1)
        file_layout.addWidget(browse_button)
        file_layout.addWidget(reset_button)
        status = QLabel(self._calibration_status(calibration), group)
        status.setWordWrap(True)
        self._calibration_checkboxes[axis] = checkbox
        self._calibration_file_edits[axis] = file_edit
        self._calibration_browse_buttons[axis] = browse_button
        self._calibration_reset_buttons[axis] = reset_button
        self._calibration_status_labels[axis] = status
        layout.addRow(checkbox)
        layout.addRow("File", file_row)
        layout.addRow("Status", status)
        browse_button.clicked.connect(
            lambda _checked=False, current_axis=axis: self._browse_calibration(
                current_axis
            )
        )
        reset_button.clicked.connect(
            lambda _checked=False, current_axis=axis: self._reset_calibration(
                current_axis
            )
        )
        checkbox.toggled.connect(
            lambda enabled, current_axis=axis: self._calibration_toggled(
                current_axis, enabled
            )
        )
        self._direction_changed(axis)
        self._calibration_toggled(axis, checkbox.isChecked())
        return group

    def _calibration_for_axis(
        self, axis: str
    ) -> AxisACalibrationSettings | AxisZCalibrationSettings | None:
        if axis == "A":
            return self._axis_a_calibration
        if axis == "Z":
            return self._axis_z_calibration
        return None

    @staticmethod
    def _set_precision_enabled(
        row: _AxisPrecisionControls,
        enabled: bool,
    ) -> None:
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
        """Start an NPZ import without reading the file in the GUI thread."""

        final_direction = int(self._rows[axis].direction_combo.currentData() or 1)
        task = _CalibrationImportTask(axis, path, final_direction)
        task.signals.finished.connect(self._finish_calibration_import)
        was_inactive = not self._calibration_tasks
        self._calibration_tasks.add(task)
        self._calibration_tasks_running = len(self._calibration_tasks)
        if was_inactive:
            self.calibration_imports_active_changed.emit(True)
        self._set_calibration_controls_enabled(axis, False)
        self._calibration_status_labels[axis].setText("Loading calibration.")
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

        calibration = self._calibration_for_axis(axis)
        assert calibration is not None
        replacement = calibration.clone()
        replacement.configured = True
        replacement.model = LINEAR_INTERPOLATION_MODEL
        replacement.calibration_file = imported.calibration_file
        replacement.interpolation_gcode_mm = list(imported.gcode_points_mm)
        replacement.interpolation_display_mm = list(imported.display_points_mm)
        replacement.interpolation_direction = imported.branch_direction
        replacement.source = ""
        if axis == "A":
            self._axis_a_calibration = replacement
        else:
            self._axis_z_calibration = replacement
        self._calibration_file_edits[axis].setText(imported.calibration_file)
        self._calibration_status_labels[axis].setText(
            self._calibration_status(replacement)
        )
        self._calibration_checkboxes[axis].setChecked(True)
        self._direction_changed(axis)
        if became_inactive:
            self.calibration_imports_active_changed.emit(False)

    def _reset_calibration(self, axis: str) -> None:
        calibration = self._calibration_for_axis(axis)
        assert calibration is not None
        reset = calibration.clone()
        reset.configured = False
        reset.calibration_file = ""
        reset.interpolation_gcode_mm = []
        reset.interpolation_display_mm = []
        reset.interpolation_direction = None
        reset.source = ""
        if axis == "A":
            self._axis_a_calibration = reset
        else:
            self._axis_z_calibration = reset
        self._calibration_checkboxes[axis].setChecked(False)
        self._calibration_file_edits[axis].clear()
        self._calibration_status_labels[axis].clear()

    def _direction_changed(self, axis: str) -> None:
        calibration = self._calibration_for_axis(axis)
        if calibration is None or axis not in self._calibration_status_labels:
            return
        if not self._direction_matches(axis, calibration):
            self._calibration_checkboxes[axis].setChecked(False)
            self._calibration_status_labels[axis].setText(
                "Choose a curve for this direction."
            )
        elif self._has_valid_interpolation_points(calibration):
            self._calibration_status_labels[axis].setText(
                self._calibration_status(calibration)
            )

    def _calibration_toggled(self, axis: str, enabled: bool) -> None:
        if not enabled:
            return
        calibration = self._calibration_for_axis(axis)
        assert calibration is not None
        if calibration.model == LINEAR_INTERPOLATION_MODEL and not (
            self._has_valid_interpolation_points(calibration)
            and self._direction_matches(axis, calibration)
        ):
            self._calibration_checkboxes[axis].setChecked(False)
            if not self._direction_matches(axis, calibration):
                self._calibration_status_labels[axis].setText(
                    "Choose a curve for this direction."
                )

    def _direction_matches(
        self,
        axis: str,
        calibration: AxisACalibrationSettings | AxisZCalibrationSettings,
    ) -> bool:
        if calibration.model != LINEAR_INTERPOLATION_MODEL:
            return True
        direction = calibration.interpolation_direction
        selected = int(self._rows[axis].direction_combo.currentData() or 1)
        return direction is None or direction == selected

    @staticmethod
    def _has_valid_interpolation_points(
        calibration: AxisACalibrationSettings | AxisZCalibrationSettings,
    ) -> bool:
        if calibration.model != LINEAR_INTERPOLATION_MODEL:
            return False
        try:
            gcode = [float(value) for value in calibration.interpolation_gcode_mm]
            display = [float(value) for value in calibration.interpolation_display_mm]
        except (TypeError, ValueError):
            return False
        return (
            len(gcode) >= 2
            and len(gcode) == len(display)
            and all(math.isfinite(value) for value in (*gcode, *display))
            and all(right > left for left, right in zip(gcode, gcode[1:]))
            and all(right > left for left, right in zip(display, display[1:]))
        )

    @classmethod
    def _calibration_status(
        cls,
        calibration: AxisACalibrationSettings | AxisZCalibrationSettings,
    ) -> str:
        if not cls._has_valid_interpolation_points(calibration):
            return ""
        points = calibration.interpolation_gcode_mm
        return (
            f"{len(points)} points \N{MIDDLE DOT} "
            f"{min(points):.3f}\N{EN DASH}{max(points):.3f} mm"
        )

    def _set_calibration_controls_enabled(self, axis: str, enabled: bool) -> None:
        self._calibration_browse_buttons[axis].setEnabled(enabled)
        self._calibration_reset_buttons[axis].setEnabled(enabled)


def _axis_unit(axis: str) -> str:
    return "\N{DEGREE SIGN}" if axis in {"B", "C"} else "mm"


__all__ = ["AxisSettingsWidget"]
