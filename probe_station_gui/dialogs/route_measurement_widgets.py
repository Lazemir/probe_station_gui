"""Reusable widgets for the route measurement dialog."""

from __future__ import annotations

import math

from PySide6.QtCore import QLocale, QSignalBlocker
from PySide6.QtWidgets import QHBoxLayout, QWidget

from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
    GuardedSpinBox as QSpinBox,
)


class SIPrefixSpinBox(QWidget):
    """Numeric editor that stores values in base SI units."""

    def __init__(
        self,
        *,
        prefixes: tuple[tuple[str, float], ...],
        base_minimum: float,
        base_maximum: float,
        base_value: float,
        decimals: int = 3,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._prefixes = tuple(prefixes)
        self._base_minimum = float(base_minimum)
        self._base_maximum = float(base_maximum)
        self._base_value = float(base_value)
        self._updating = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._spin = QDoubleSpinBox(self)
        self._spin.setLocale(QLocale.c())
        self._spin.setDecimals(decimals)
        self._spin.setKeyboardTracking(False)
        self._spin.setMinimumWidth(110)
        self._prefix_combo = QComboBox(self)
        for label, factor in self._prefixes:
            self._prefix_combo.addItem(label, float(factor))
        layout.addWidget(self._spin, 1)
        layout.addWidget(self._prefix_combo)

        self._prefix_combo.currentIndexChanged.connect(
            lambda _index: self._on_prefix_changed()
        )
        self._spin.valueChanged.connect(lambda _value: self._on_display_value_changed())
        self._spin.editingFinished.connect(self._normalize_prefix)
        self.set_base_value(base_value)

    def base_value(self) -> float:
        return float(self._base_value)

    def set_base_value(self, value: object) -> None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(numeric):
            return
        self._base_value = self._clamp_base_value(numeric)
        self._set_prefix_for_value(self._base_value)
        self._refresh_display()

    def _on_display_value_changed(self) -> None:
        if self._updating:
            return
        factor = self._current_factor()
        self._base_value = self._clamp_base_value(self._spin.value() * factor)

    def _on_prefix_changed(self) -> None:
        if self._updating:
            return
        self._refresh_display()

    def _normalize_prefix(self) -> None:
        self._on_display_value_changed()
        self._set_prefix_for_value(self._base_value)
        self._refresh_display()

    def _refresh_display(self) -> None:
        factor = self._current_factor()
        self._updating = True
        try:
            spin_blocker = QSignalBlocker(self._spin)
            self._spin.setRange(
                self._base_minimum / factor,
                self._base_maximum / factor,
            )
            self._spin.setValue(self._base_value / factor)
            del spin_blocker
        finally:
            self._updating = False

    def _set_prefix_for_value(self, value: float) -> None:
        index = self._best_prefix_index(value)
        if index == self._prefix_combo.currentIndex():
            return
        blocker = QSignalBlocker(self._prefix_combo)
        self._prefix_combo.setCurrentIndex(index)
        del blocker

    def _best_prefix_index(self, value: float) -> int:
        absolute = abs(float(value))
        if absolute <= 0.0:
            return self._unit_prefix_index()
        best = 0
        for index, (_label, factor) in enumerate(self._prefixes):
            scaled = absolute / factor
            if 1.0 <= scaled < 1000.0:
                return index
            if scaled >= 1.0:
                best = index
        return best

    def _unit_prefix_index(self) -> int:
        for index, (_label, factor) in enumerate(self._prefixes):
            if factor == 1.0:
                return index
        return 0

    def _current_factor(self) -> float:
        factor = self._prefix_combo.currentData()
        try:
            numeric = float(factor)
        except (TypeError, ValueError):
            numeric = 1.0
        return numeric if numeric > 0.0 else 1.0

    def _clamp_base_value(self, value: float) -> float:
        return max(self._base_minimum, min(self._base_maximum, float(value)))


def apply_profile_numeric_value(
    widget: SIPrefixSpinBox | QSpinBox | QDoubleSpinBox,
    value: object,
) -> None:
    """Apply a finite profile value through the widget's native unit contract."""

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return
    if not math.isfinite(numeric):
        return
    if isinstance(widget, SIPrefixSpinBox):
        widget.set_base_value(numeric)
    elif isinstance(widget, QSpinBox):
        widget.setValue(int(round(numeric)))
    else:
        widget.setValue(numeric)
