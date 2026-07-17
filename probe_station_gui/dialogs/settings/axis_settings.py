"""Axis-oriented editor for precision approach and calibration settings."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
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
    preview_label: QLabel


class AxisSettingsWidget(QWidget):
    """Edit precision approach and coordinate calibration by stage axis."""

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
        self._calibration_sources: dict[str, QLabel] = {}
        self._calibration_errors: dict[str, QLabel] = {}

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
        """Persist edits while retaining read-only calibration metadata."""

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
        axis_a = self._axis_a_calibration.clone()
        axis_a.configured = self._calibration_checkboxes["A"].isChecked()
        settings.axis_a_calibration = axis_a
        axis_z = self._axis_z_calibration.clone()
        axis_z.configured = self._calibration_checkboxes["Z"].isChecked()
        settings.axis_z_calibration = axis_z

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
        preview_label = QLabel(group)
        row = _AxisPrecisionControls(
            enabled_checkbox,
            backlash_spin,
            direction_combo,
            preview_label,
        )
        self._rows[axis] = row

        layout.addRow(enabled_checkbox)
        layout.addRow("Backlash", backlash_spin)
        layout.addRow("Final direction", direction_combo)
        layout.addRow("Path", preview_label)

        enabled_checkbox.toggled.connect(
            lambda enabled, controls=row: self._set_precision_enabled(
                controls, enabled
            )
        )
        backlash_spin.valueChanged.connect(
            lambda _value, current_axis=axis: self._update_preview(current_axis)
        )
        direction_combo.currentIndexChanged.connect(
            lambda _index, current_axis=axis: self._update_preview(current_axis)
        )
        self._set_precision_enabled(row, profile.enabled)
        self._update_preview(axis)
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

        checkbox = QCheckBox(
            f"Use calibrated {axis}-axis coordinate curve",
            group,
        )
        checkbox.setChecked(calibration.configured)
        source = QLabel(calibration.source, group)
        source.setWordWrap(True)
        error = QLabel(
            f"RMSE {calibration.fit_rmse_mm:.6f} mm, "
            f"max {calibration.fit_max_abs_error_mm:.6f} mm",
            group,
        )
        self._calibration_checkboxes[axis] = checkbox
        self._calibration_sources[axis] = source
        self._calibration_errors[axis] = error
        layout.addRow(checkbox)
        layout.addRow("Curve source", source)
        layout.addRow("Fit error", error)
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
        row.preview_label.setEnabled(enabled)

    def _update_preview(self, axis: str) -> None:
        row = self._rows[axis]
        operator = "\N{MINUS SIGN}" if int(row.direction_combo.currentData() or 1) > 0 else "+"
        row.preview_label.setText(
            f"Target {operator} {row.backlash_spin.value():.3f} "
            f"{_axis_unit(axis)} \N{RIGHTWARDS ARROW} target"
        )


def _axis_unit(axis: str) -> str:
    return "\N{DEGREE SIGN}" if axis in {"B", "C"} else "mm"


__all__ = ["AxisSettingsWidget"]
