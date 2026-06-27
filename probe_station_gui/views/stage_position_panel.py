"""Stage-position status bar widget and field-local UI state."""

from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QLocale, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox
from probe_station_gui.stage.api_moves import normalize_api_coordinate_input_mode
from probe_station_gui.stage.position_presenter import StagePositionDisplayPlan


DISABLED_BACKGROUND = "#e6e6e6"
DISABLED_FOREGROUND = "#666666"
EDITED_BACKGROUND = "#d7b8ff"
EDITED_FOREGROUND = "#1f1233"
DIMMED_BACKGROUNDS = {
    "#1565c0": "#6f9dd3",
    "#f0b429": "#f7d98a",
    "#c62828": "#e57373",
    "#d7b8ff": "#ead6ff",
}


def format_stage_axis_value(value: float) -> str:
    numeric_value = float(value)
    if abs(numeric_value) < 0.0005:
        numeric_value = 0.0
    text = f"{numeric_value:.3f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


class StagePositionPanel(QWidget):
    """Own the stage-position fields and their local Qt state."""

    axis_return_pressed = Signal(str)
    axis_escape_pressed = Signal(str)
    axis_editing_finished = Signal(str)
    axis_text_edited = Signal(str)
    input_mode_changed = Signal()
    apply_requested = Signal()
    cancel_requested = Signal()

    def __init__(
        self,
        axis_names: tuple[str, ...] | list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._axis_names = tuple(str(axis).strip().upper() for axis in axis_names)
        self._axis_fields: dict[str, QLineEdit] = {}
        self._axis_base_styles: dict[str, tuple[str, str]] = {}
        self._pending_targets: dict[str, tuple[float, float]] = {}
        self._return_commits: set[str] = set()
        self._axis_escape_shortcuts: dict[str, QShortcut] = {}
        self._motion_axes: set[str] = set()
        self._motion_blink_dimmed = False
        self._programmatic_update_depth = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        label = QLabel("Position:", self)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(label)

        for axis_name in self._axis_names:
            axis_label = QLabel(axis_name, self)
            axis_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            layout.addWidget(axis_label)

            field = QLineEdit(self)
            field.setAlignment(Qt.AlignCenter)
            field.setFixedWidth(72)
            field.setPlaceholderText("---")
            field.setToolTip(
                f"Current {axis_name} coordinate. Enter target and press Enter."
            )
            validator = QDoubleValidator(-1000000.0, 1000000.0, 6, field)
            validator.setNotation(QDoubleValidator.StandardNotation)
            validator.setLocale(QLocale.c())
            field.setValidator(validator)
            field.returnPressed.connect(
                lambda axis=axis_name: self._on_axis_return_pressed(axis)
            )
            escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), field)
            escape_shortcut.setContext(Qt.WidgetShortcut)
            escape_shortcut.setAutoRepeat(False)
            escape_shortcut.activated.connect(
                lambda axis=axis_name: self._on_axis_escape_pressed(axis)
            )
            field.editingFinished.connect(
                lambda axis=axis_name: self._on_axis_editing_finished(axis)
            )
            field.textEdited.connect(
                lambda _text, axis=axis_name: self._on_axis_text_edited(axis)
            )

            self._axis_fields[axis_name] = field
            self._axis_escape_shortcuts[axis_name] = escape_shortcut
            layout.addWidget(field)

        mode_label = QLabel("Input:", self)
        mode_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(mode_label)

        self._input_mode_combo = QComboBox(self)
        self._input_mode_combo.addItem("Absolute", "G90")
        self._input_mode_combo.addItem("Relative", "G91")
        self._input_mode_combo.setToolTip(
            "Coordinate input mode. Idle fields always show absolute coordinates."
        )
        self._input_mode_combo.currentIndexChanged.connect(self.input_mode_changed.emit)
        layout.addWidget(self._input_mode_combo)

        self._apply_button = QPushButton("Apply", self)
        self._apply_button.setEnabled(False)
        self._apply_button.setToolTip("Apply changed coordinate fields as one move.")
        self._apply_button.clicked.connect(self.apply_requested.emit)
        layout.addWidget(self._apply_button)

        self._cancel_button = QPushButton("Cancel", self)
        self._cancel_button.setEnabled(False)
        self._cancel_button.setToolTip(
            "Clear edited fields or cancel the active stage workflow."
        )
        self._cancel_button.clicked.connect(self.cancel_requested.emit)
        layout.addWidget(self._cancel_button)

        self.set_fields_available(False)

    @property
    def axis_fields(self) -> dict[str, QLineEdit]:
        return self._axis_fields

    @property
    def input_mode_combo(self) -> QComboBox:
        return self._input_mode_combo

    @property
    def axis_escape_shortcuts(self) -> dict[str, QShortcut]:
        return self._axis_escape_shortcuts

    @property
    def apply_button(self) -> QPushButton:
        return self._apply_button

    @property
    def cancel_button(self) -> QPushButton:
        return self._cancel_button

    @property
    def pending_targets(self) -> dict[str, tuple[float, float]]:
        return self._pending_targets

    @property
    def base_styles(self) -> dict[str, tuple[str, str]]:
        return self._axis_base_styles

    @property
    def return_commits(self) -> set[str]:
        return self._return_commits

    @property
    def is_programmatic_update(self) -> bool:
        return self._programmatic_update_depth > 0

    def field(self, axis_name: str) -> QLineEdit | None:
        return self._axis_fields.get(self._normalize_axis(axis_name))

    def pending_targets_copy(self) -> dict[str, tuple[float, float]]:
        return dict(self._pending_targets)

    def pop_pending_target(self, axis_name: str) -> tuple[float, float] | None:
        return self._pending_targets.pop(self._normalize_axis(axis_name), None)

    def discard_return_commit(self, axis_name: str) -> None:
        self._return_commits.discard(self._normalize_axis(axis_name))

    def consume_return_commit(self, axis_name: str) -> bool:
        axis = self._normalize_axis(axis_name)
        had_commit = axis in self._return_commits
        self._return_commits.discard(axis)
        return had_commit

    def selected_input_mode(self) -> str:
        mode = normalize_api_coordinate_input_mode(self._input_mode_combo.currentData())
        return mode or "G90"

    def has_pending_or_modified_fields(self) -> bool:
        return bool(self._pending_targets) or any(
            field.isEnabled() and field.isModified()
            for field in self._axis_fields.values()
        )

    def set_pending_target(
        self,
        axis_name: str,
        raw_target: float,
        display_target: float,
    ) -> None:
        axis = self._normalize_axis(axis_name)
        if axis not in self._axis_fields:
            return
        self._pending_targets[axis] = (float(raw_target), float(display_target))
        field = self._axis_fields[axis]
        if not field.hasFocus():
            with self._programmatic_update():
                field.blockSignals(True)
                field.setText(format_stage_axis_value(display_target))
                field.setModified(False)
                field.blockSignals(False)
        self._apply_axis_style(axis, field)

    def set_fields_available(self, available: bool) -> None:
        with self._programmatic_update():
            for axis_name, field in self._axis_fields.items():
                field.blockSignals(True)
                if not available:
                    field.clear()
                    field.setPlaceholderText("---")
                    field.setEnabled(False)
                    field.setModified(False)
                    self._set_field_style(
                        field,
                        DISABLED_BACKGROUND,
                        DISABLED_FOREGROUND,
                    )
                else:
                    field.setEnabled(True)
                    self._apply_axis_style(axis_name, field)
                field.blockSignals(False)

    def apply_display_plan(self, plan: StagePositionDisplayPlan) -> None:
        if not plan.valid:
            self.set_fields_available(False)
            return
        with self._programmatic_update():
            for axis_plan in plan.axis_updates:
                field = self._axis_fields[axis_plan.axis]
                self._axis_base_styles[axis_plan.axis] = (
                    axis_plan.base_background,
                    axis_plan.base_foreground,
                )
                field.blockSignals(True)
                field.setEnabled(True)
                if not field.hasFocus():
                    field.setText(format_stage_axis_value(axis_plan.visible_value))
                    field.setModified(False)
                field.setToolTip(axis_plan.tooltip)
                field.blockSignals(False)
            for axis_name in plan.missing_axes:
                field = self._axis_fields[axis_name]
                self._axis_base_styles.pop(axis_name, None)
                self._pending_targets.pop(axis_name, None)
                self._return_commits.discard(axis_name)
                field.blockSignals(True)
                field.clear()
                field.setEnabled(False)
                field.setModified(False)
                self._set_field_style(
                    field,
                    DISABLED_BACKGROUND,
                    DISABLED_FOREGROUND,
                )
                field.blockSignals(False)
        if not plan.fields_available:
            self.set_fields_available(False)
            return
        self.refresh_axis_styles(self._motion_axes, self._motion_blink_dimmed)

    def reset_axis_field(self, axis_name: str, display_value: float | None) -> None:
        axis = self._normalize_axis(axis_name)
        field = self._axis_fields.get(axis)
        if field is None:
            return
        with self._programmatic_update():
            field.blockSignals(True)
            if display_value is None:
                field.clear()
            else:
                field.setText(format_stage_axis_value(display_value))
            field.setModified(False)
            field.blockSignals(False)
        self._apply_axis_style(axis, field)

    def clear_pending_targets(
        self,
        display_values: dict[str, float] | None = None,
    ) -> bool:
        had_changes = self.has_pending_or_modified_fields()
        self.clear_pending_target_state()
        for axis_name in self._axis_names:
            self.reset_axis_field(
                axis_name,
                None if display_values is None else display_values.get(axis_name),
            )
        self.refresh_axis_styles(self._motion_axes, self._motion_blink_dimmed)
        return had_changes

    def clear_pending_target_state(self) -> bool:
        had_changes = self.has_pending_or_modified_fields()
        self._return_commits.clear()
        self._pending_targets.clear()
        self.refresh_axis_styles(self._motion_axes, self._motion_blink_dimmed)
        return had_changes

    def refresh_axis_styles(
        self,
        motion_axes: set[str] | list[str] | tuple[str, ...],
        motion_blink_dimmed: bool,
    ) -> None:
        self._motion_axes = {
            self._normalize_axis(axis)
            for axis in motion_axes
            if self._normalize_axis(axis) in self._axis_fields
        }
        self._motion_blink_dimmed = bool(motion_blink_dimmed)
        for axis_name, field in self._axis_fields.items():
            if axis_name not in self._axis_base_styles:
                continue
            self._apply_axis_style(axis_name, field)

    def set_action_buttons_enabled(
        self,
        apply_enabled: bool,
        cancel_enabled: bool,
    ) -> None:
        self._apply_button.setEnabled(bool(apply_enabled))
        self._cancel_button.setEnabled(bool(cancel_enabled))

    def _on_axis_return_pressed(self, axis_name: str) -> None:
        axis = self._normalize_axis(axis_name)
        self._return_commits.add(axis)
        self.axis_return_pressed.emit(axis)

    def _on_axis_escape_pressed(self, axis_name: str) -> None:
        self.axis_escape_pressed.emit(self._normalize_axis(axis_name))

    def _on_axis_editing_finished(self, axis_name: str) -> None:
        if self.is_programmatic_update:
            return
        self.axis_editing_finished.emit(self._normalize_axis(axis_name))

    def _on_axis_text_edited(self, axis_name: str) -> None:
        if self.is_programmatic_update:
            return
        self.axis_text_edited.emit(self._normalize_axis(axis_name))

    def _apply_axis_style(self, axis_name: str, field: QLineEdit | None = None) -> None:
        axis = self._normalize_axis(axis_name)
        target = field or self._axis_fields.get(axis)
        if target is None:
            return
        background, foreground = self._axis_base_styles.get(
            axis,
            (DISABLED_BACKGROUND, DISABLED_FOREGROUND),
        )
        if axis in self._pending_targets:
            background = EDITED_BACKGROUND
            foreground = EDITED_FOREGROUND
        elif axis in self._motion_axes and self._motion_blink_dimmed:
            background = DIMMED_BACKGROUNDS.get(background, background)
        self._set_field_style(target, background, foreground)

    @staticmethod
    def _set_field_style(field: QLineEdit, background: str, foreground: str) -> None:
        field.setStyleSheet(
            "QLineEdit {"
            f"background-color: {background}; color: {foreground}; "
            f"border: 1px solid {background}; border-radius: 4px; "
            "padding: 2px 5px;"
            "}"
            "QLineEdit:disabled {"
            f"background-color: {background}; color: {foreground};"
            "}"
        )

    @staticmethod
    def _normalize_axis(axis_name: str) -> str:
        return str(axis_name).strip().upper()

    @contextmanager
    def _programmatic_update(self):
        self._programmatic_update_depth += 1
        try:
            yield
        finally:
            self._programmatic_update_depth -= 1


__all__ = ["StagePositionPanel", "format_stage_axis_value"]
