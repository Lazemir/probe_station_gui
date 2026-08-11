"""Qt adapter for Design registration and focus-reference controls."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.design.model import Point2D
from probe_station_gui.design.navigation_geometry import format_mark_label
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox
from probe_station_gui.views.design_navigator_enablement import (
    DesignNavigatorEnablement,
)


class DesignRegistrationControls(QGroupBox):
    """Render registration state and emit registration workflow intent."""

    registration_instance_selected = Signal(str)
    new_registration_requested = Signal()
    find_focus_reference_requested = Signal()
    use_selected_focus_requested = Signal()
    reset_focus_reference_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Registration", parent)
        self._active = False
        self._focus_z_ready = False
        self._contact_a_ready = False
        self._focus_selection_available = False
        self._instances: tuple[tuple[str, str], ...] = ()
        self._updating_instances = False
        self._source_design_marks: list[Point2D | None] = [None, None]
        self._source_stage_marks: list[Point2D | None] = [None, None]

        layout = QVBoxLayout(self)
        instance_row = QHBoxLayout()
        self.instance_combo = QComboBox(self)
        self.instance_combo.setToolTip(
            "Select a saved registration for this design and top cell."
        )
        self.new_button = QPushButton("New registration", self)
        instance_row.addWidget(self.instance_combo, 1)
        instance_row.addWidget(self.new_button)
        layout.addLayout(instance_row)

        self.hint_label = QLabel(
            "Use left click for design point 1 and right click for design point 2 in the layout view.",
            self,
        )
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        self.calibration_prompt_label = QLabel(self)
        self.calibration_prompt_label.setWordWrap(True)
        layout.addWidget(self.calibration_prompt_label)

        self.mark_1_label = self._mark_label()
        self.mark_2_label = self._mark_label()
        self.chip_1_label = self._mark_label()
        self.chip_2_label = self._mark_label()
        for label in (
            self.mark_1_label,
            self.mark_2_label,
            self.chip_1_label,
            self.chip_2_label,
        ):
            layout.addWidget(label)

        self.status_label = QLabel("No design registration.", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        focus_buttons = QHBoxLayout()
        self.find_focus_button = QPushButton("Find focus reference", self)
        self.use_selected_focus_button = QPushButton("Use selected point", self)
        self.reset_focus_button = QPushButton("Reset focus reference", self)
        focus_buttons.addWidget(self.find_focus_button)
        focus_buttons.addWidget(self.use_selected_focus_button)
        focus_buttons.addWidget(self.reset_focus_button)
        layout.addLayout(focus_buttons)

        self.focus_status_label = QLabel("Focus reference not set.", self)
        self.focus_status_label.setWordWrap(True)
        layout.addWidget(self.focus_status_label)

        self.instance_combo.currentIndexChanged.connect(self._on_instance_changed)
        self.new_button.clicked.connect(self.new_registration_requested.emit)
        self.find_focus_button.clicked.connect(self.find_focus_reference_requested.emit)
        self.use_selected_focus_button.clicked.connect(
            self.use_selected_focus_requested.emit
        )
        self.reset_focus_button.clicked.connect(
            self.reset_focus_reference_requested.emit
        )
        self._update_mark_labels()

    @property
    def active(self) -> bool:
        return self._active

    def set_status(self, text: str) -> None:
        self.status_label.setText(text or "No design registration.")

    def set_active(self, active: bool) -> None:
        self._active = bool(active)

    def set_instances(
        self,
        instances: object,
        *,
        selected_frame_id: str | None,
    ) -> None:
        normalized = tuple(
            (str(frame_id), str(name))
            for frame_id, name in instances
            if str(frame_id).strip()
        )
        self._instances = normalized
        self._updating_instances = True
        try:
            self.instance_combo.clear()
            selected_index = -1
            for index, (frame_id, name) in enumerate(normalized):
                self.instance_combo.addItem(name, frame_id)
                if frame_id == selected_frame_id:
                    selected_index = index
            self.instance_combo.setCurrentIndex(selected_index)
        finally:
            self._updating_instances = False

    def set_focus_selection_available(self, available: bool) -> None:
        self._focus_selection_available = bool(available)

    def set_focus_reference_state(self, *, z_ready: bool, a_ready: bool) -> None:
        self._focus_z_ready = bool(z_ready)
        self._contact_a_ready = bool(a_ready) and self._focus_z_ready
        if self._contact_a_ready:
            text = "Focus and contact references ready."
        elif self._focus_z_ready:
            text = "Focus reference ready. Contact reference not set."
        else:
            text = "Focus reference not set."
        self.focus_status_label.setText(text)

    def set_calibration_prompt(self, text: str) -> None:
        self.calibration_prompt_label.setText(text)

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
    ) -> None:
        self._source_design_marks = self._normalized_marks(source_design_marks)
        self._update_mark_labels()

    def set_stage_registration_marks(
        self,
        source_stage_marks: list[Point2D | None],
    ) -> None:
        self._source_stage_marks = self._normalized_marks(source_stage_marks)
        self._update_mark_labels()

    def reset_marks(self) -> None:
        self._source_design_marks = [None, None]
        self._source_stage_marks = [None, None]
        self._update_mark_labels()

    def apply_enablement(self, state: DesignNavigatorEnablement) -> None:
        can_set_focus = state.can_edit_design and self._active
        self.instance_combo.setEnabled(state.can_edit_design and bool(self._instances))
        self.new_button.setEnabled(state.can_edit_design)
        self.find_focus_button.setEnabled(can_set_focus and not self._focus_z_ready)
        self.use_selected_focus_button.setEnabled(
            can_set_focus
            and not self._focus_z_ready
            and self._focus_selection_available
        )
        self.reset_focus_button.setEnabled(can_set_focus and self._focus_z_ready)

    def _mark_label(self) -> QLabel:
        label = QLabel(self)
        label.setWordWrap(True)
        label.setStyleSheet("QLabel { font-weight: 600; }")
        return label

    @staticmethod
    def _normalized_marks(
        marks: list[Point2D | None],
    ) -> list[Point2D | None]:
        normalized = list(marks[:2])
        while len(normalized) < 2:
            normalized.append(None)
        return normalized

    def _update_mark_labels(self) -> None:
        self.mark_1_label.setText(
            format_mark_label("Mark 1 (LMB)", self._source_design_marks[0])
        )
        self.mark_2_label.setText(
            format_mark_label("Mark 2 (RMB)", self._source_design_marks[1])
        )
        self.chip_1_label.setText(
            format_mark_label("Chip 1", self._source_stage_marks[0])
        )
        self.chip_2_label.setText(
            format_mark_label("Chip 2", self._source_stage_marks[1])
        )

    def _on_instance_changed(self, index: int) -> None:
        if self._updating_instances or index < 0:
            return
        frame_id = self.instance_combo.itemData(index)
        if frame_id:
            self.registration_instance_selected.emit(str(frame_id))


__all__ = ["DesignRegistrationControls"]
