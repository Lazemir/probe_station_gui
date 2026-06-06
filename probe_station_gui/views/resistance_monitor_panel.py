"""Compact resistance display."""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_BLANK_READING = "------"
_DISPLAY_DIGITS = 5


class ResistanceMonitorPanel(QWidget):
    """Display the current resistance reading."""

    standby_enabled_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._standby_enabled = True
        self._connected = False
        self._has_reading = False
        self._pending_count: int | None = None
        self._last_resistance_ohm: float | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header_row = QHBoxLayout()
        title_label = QLabel("Resistance", self)
        title_label.setStyleSheet("font-weight: 600;")
        self.standby_button = QPushButton("On", self)
        self.standby_button.setCheckable(True)
        self.standby_button.setChecked(True)
        self.standby_button.setMinimumWidth(72)
        header_row.addWidget(title_label)
        header_row.addStretch(1)
        header_row.addWidget(self.standby_button)
        layout.addLayout(header_row)

        screen = _ResistanceScreen(self)
        screen.setObjectName("ResistanceScreen")
        screen.setMinimumHeight(126)
        screen.clicked.connect(self._copy_reading)
        screen_layout = QVBoxLayout(screen)
        screen_layout.setContentsMargins(8, 8, 8, 8)
        screen_layout.setSpacing(4)

        value_row = QHBoxLayout()
        value_row.setSpacing(6)
        self.value_label = QLabel(_BLANK_READING, screen)
        self.value_label.setObjectName("ResistanceValue")
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.value_label.setMinimumHeight(74)
        self.unit_label = QLabel("Ohm", screen)
        self.unit_label.setObjectName("ResistanceUnit")
        self.unit_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.unit_label.setMinimumWidth(46)
        value_row.addWidget(self.value_label, 1)
        value_row.addWidget(self.unit_label)
        screen_layout.addLayout(value_row)

        self.status_label = QLabel("Instrument disconnected", screen)
        self.status_label.setObjectName("ResistanceStatus")
        self.status_label.setWordWrap(True)
        screen_layout.addWidget(self.status_label)
        layout.addWidget(screen)

        self.setStyleSheet(
            """
            QFrame#ResistanceScreen {
                background: #101810;
                border: 1px solid #2c4a30;
                border-radius: 4px;
            }
            QLabel#ResistanceValue {
                color: #8cff76;
                font-family: Consolas, "Courier New", monospace;
                font-size: 58px;
                font-weight: 700;
            }
            QLabel#ResistanceUnit {
                color: #8cff76;
                font-family: Consolas, "Courier New", monospace;
                font-size: 16px;
            }
            QLabel#ResistanceStatus {
                color: #b7d7b0;
                font-family: Consolas, "Courier New", monospace;
                font-size: 12px;
            }
            """
        )

        self.standby_button.toggled.connect(self._on_standby_toggled)
        self._refresh_standby_button()

    def set_standby_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._standby_enabled == enabled:
            self._refresh_state_text()
            return
        self._standby_enabled = enabled
        self.standby_button.blockSignals(True)
        self.standby_button.setChecked(enabled)
        self.standby_button.blockSignals(False)
        self._refresh_standby_button()
        self._refresh_state_text()

    def set_connection_state(
        self, connected: bool, backend_name: str, description: str
    ) -> None:
        self._connected = bool(connected)
        self._has_reading = False
        self._pending_count = None
        self._last_resistance_ohm = None
        self.value_label.setText(_BLANK_READING)
        self.unit_label.setText("Ohm")
        detail = description.strip() or backend_name.strip()
        if detail and self._connected:
            self.status_label.setText(detail)
        self._refresh_state_text()

    def set_status_message(self, message: str) -> None:
        message = message.strip()
        if message and not self._has_reading:
            self.status_label.setText(message)

    def set_reading(self, resistance_ohm: Optional[float], is_short: bool) -> None:
        self.set_reading_summary(resistance_ohm, is_short, None)

    def set_reading_pending(self, sample_count: int) -> None:
        count = max(1, int(sample_count))
        self._pending_count = count
        self._has_reading = False
        self._last_resistance_ohm = None
        self.value_label.setText(_BLANK_READING)
        self.unit_label.setText("Ohm")
        self.status_label.setText(
            "Measuring" if count == 1 else f"Measuring {count}"
        )

    def set_reading_summary(
        self,
        resistance_ohm: Optional[float],
        is_short: bool,
        sample_count: int | None,
    ) -> None:
        if (
            not self._standby_enabled
            and sample_count is None
            and self._pending_count is None
        ):
            return
        if resistance_ohm is None:
            self._has_reading = False
            self._pending_count = None
            self._last_resistance_ohm = None
            self.value_label.setText(_BLANK_READING)
            self.unit_label.setText("Ohm")
            self._refresh_state_text()
            return
        value = float(resistance_ohm)
        value_text, unit_text = _format_resistance(value)
        self._has_reading = math.isfinite(value)
        self._last_resistance_ohm = value if self._has_reading else None
        self.value_label.setText(value_text)
        self.unit_label.setText(unit_text)
        count = (
            max(1, int(sample_count))
            if sample_count is not None
            else self._pending_count
        )
        self._pending_count = None
        if not self._has_reading:
            self.status_label.setText("No reading")
        elif is_short:
            self.status_label.setText("Short")
        elif count is not None and count > 1:
            self.status_label.setText(f"Mean of {count}")
        elif count == 1:
            self.status_label.setText("Measured")
        else:
            self.status_label.setText("Live")

    def _on_standby_toggled(self, checked: bool) -> None:
        self._standby_enabled = bool(checked)
        self._refresh_standby_button()
        self._refresh_state_text()
        self.standby_enabled_changed.emit(self._standby_enabled)

    def _copy_reading(self) -> None:
        value = self._last_resistance_ohm
        if value is None or not math.isfinite(value):
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(f"{value:.12g} Ohm")
        self.status_label.setText("Copied")

    def _refresh_standby_button(self) -> None:
        self.standby_button.setText("Stop" if self._standby_enabled else "Start")

    def _refresh_state_text(self) -> None:
        if not self._standby_enabled:
            self.status_label.setText("Off")
            return
        if not self._connected:
            self.status_label.setText("Instrument disconnected")
            return
        if not self._has_reading:
            self.status_label.setText("Waiting for reading")


class _ResistanceScreen(QFrame):
    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def _format_resistance(value: float) -> tuple[str, str]:
    if not math.isfinite(value):
        return _BLANK_READING, "Ohm"
    abs_value = abs(value)
    scales = (
        (1e-6, "uOhm"),
        (1e-3, "mOhm"),
        (1.0, "Ohm"),
        (1e3, "kOhm"),
        (1e6, "MOhm"),
        (1e9, "GOhm"),
    )
    scale_index = 2
    for index, (scale, _unit) in enumerate(scales):
        if abs_value >= scale:
            scale_index = index
    scale, unit = scales[scale_index]
    scaled = value / scale
    while abs(scaled) >= 999.995 and scale_index < len(scales) - 1:
        scale_index += 1
        scale, unit = scales[scale_index]
        scaled = value / scale
    return _format_fixed_digit_value(scaled), unit


def _format_fixed_digit_value(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 100:
        decimals = max(0, _DISPLAY_DIGITS - 3)
    elif abs_value >= 10:
        decimals = max(0, _DISPLAY_DIGITS - 2)
    else:
        decimals = max(0, _DISPLAY_DIGITS - 1)
    return f"{value:.{decimals}f}"


__all__ = ["ResistanceMonitorPanel"]
