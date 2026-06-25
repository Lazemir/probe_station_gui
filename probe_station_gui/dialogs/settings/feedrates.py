"""Feedrate preset widgets for the settings dialog."""

from __future__ import annotations

import math
from typing import List, Sequence

from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.settings.manager import FeedrateGroup, FeedrateSettings, Settings
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox


class FeedrateGroupEditor(QWidget):
    """Editor for a single feedrate group including presets and default selection."""

    def __init__(
        self,
        title: str,
        units: str,
        group: FeedrateGroup,
        fallback_presets: Sequence[float],
        fallback_default: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._fallback_presets = [float(value) for value in fallback_presets]
        self._fallback_presets.sort()
        self._fallback_default = float(fallback_default)
        self._presets: List[float] = (
            sorted(group.presets) if group.presets else list(self._fallback_presets)
        )
        if not self._presets:
            self._presets = list(self._fallback_presets)
        self._default_value: float = group.default
        if not self._presets:
            self._default_value = self._fallback_default

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(title_label)

        units_label = QLabel(f"Preset feed rates for {units} (positive values):", self)
        units_label.setWordWrap(True)
        layout.addWidget(units_label)

        self._list = QListWidget(self)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self._list)

        input_row = QHBoxLayout()
        self._value_edit = QLineEdit(self)
        self._value_edit.setPlaceholderText("Enter feed rate (e.g. 1)")
        validator = QDoubleValidator(1.0, 1000000.0, 6, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._value_edit.setValidator(validator)
        input_row.addWidget(self._value_edit)

        self._add_button = QPushButton("Add", self)
        input_row.addWidget(self._add_button)
        layout.addLayout(input_row)

        action_row = QHBoxLayout()
        self._remove_button = QPushButton("Remove Selected", self)
        action_row.addWidget(self._remove_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Default preset:", self))
        self._default_combo = QComboBox(self)
        default_row.addWidget(self._default_combo)
        default_row.addStretch(1)
        layout.addLayout(default_row)

        self._add_button.clicked.connect(self._add_value)
        self._remove_button.clicked.connect(self._remove_selected)
        self._list.itemSelectionChanged.connect(self._update_buttons)
        self._default_combo.currentIndexChanged.connect(self._on_default_changed)

        self._refresh_list()
        self._update_buttons()

    def group(self) -> FeedrateGroup:
        """Return the configured feedrate group."""

        presets = list(self._presets)
        if not presets:
            presets = list(self._fallback_presets)
        default_value = self._default_value
        if default_value <= 0:
            default_value = presets[0] if presets else self._fallback_default
        return FeedrateGroup(presets=presets, default=default_value)

    def _refresh_list(self) -> None:
        self._presets.sort()
        self._list.clear()
        for value in self._presets:
            self._list.addItem(self._format_value(value))
        if self._default_value <= 0:
            self._default_value = (
                self._presets[0] if self._presets else self._fallback_default
            )
        self._refresh_default_options()

    def _refresh_default_options(self) -> None:
        values = list(self._presets) if self._presets else list(self._fallback_presets)
        if not values:
            values = [self._fallback_default]
        if self._default_value > 0 and not any(
            math.isclose(self._default_value, value, rel_tol=1e-9, abs_tol=1e-9)
            for value in values
        ):
            values.append(self._default_value)
            values.sort()
        texts = [self._format_value(value) for value in values]
        desired_text = self._format_value(self._default_value)

        self._default_choices = values
        self._default_combo.blockSignals(True)
        self._default_combo.clear()
        self._default_combo.addItems(texts)
        if desired_text in texts:
            self._default_combo.setCurrentText(desired_text)
        else:
            self._default_combo.setCurrentIndex(0)
            self._default_value = values[0]
        self._default_combo.blockSignals(False)

    def _update_buttons(self) -> None:
        self._remove_button.setEnabled(bool(self._list.selectedItems()))

    def _add_value(self) -> None:
        text = self._value_edit.text().strip()
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            return
        if value < 1.0:
            return
        if any(
            math.isclose(value, existing, rel_tol=1e-9, abs_tol=1e-9)
            for existing in self._presets
        ):
            return
        insert_index = len(self._presets)
        for index, existing in enumerate(self._presets):
            if value < existing:
                insert_index = index
                break
        self._presets.insert(insert_index, value)
        self._value_edit.clear()
        self._refresh_list()

    def _remove_selected(self) -> None:
        selected_indexes = self._list.selectedIndexes()
        if not selected_indexes:
            return
        for index in sorted((idx.row() for idx in selected_indexes), reverse=True):
            if 0 <= index < len(self._presets):
                del self._presets[index]
        self._refresh_list()

    def _on_default_changed(self) -> None:
        index = self._default_combo.currentIndex()
        if 0 <= index < len(self._default_choices):
            self._default_value = self._default_choices[index]

    @staticmethod
    def _format_value(value: float) -> str:
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"


class FeedrateSettingsWidget(QWidget):
    """Tab that lets users manage linear feed rates."""

    DEFAULT_PRESETS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0)
    DEFAULT_VALUE = 1.0

    def __init__(
        self, feedrates: FeedrateSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._linear_editor = FeedrateGroupEditor(
            "Linear feed rates",
            "mm/min",
            feedrates.linear,
            self.DEFAULT_PRESETS,
            self.DEFAULT_VALUE,
            self,
        )
        layout.addWidget(self._linear_editor)

        layout.addStretch(1)

    def to_settings(self, settings: Settings) -> None:
        """Write the configured presets back to the settings container."""

        settings.feedrates = FeedrateSettings(
            linear=self._linear_editor.group(),
            rotary=settings.feedrates.rotary,
        )
