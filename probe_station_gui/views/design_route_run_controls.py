"""Qt adapter for Design route-run controls."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from probe_station_gui.route.run_ui import (
    RouteRunControlPresentation,
    RouteRunControlState,
)
from probe_station_gui.views.design_navigator_enablement import (
    DesignNavigatorEnablement,
)


class DesignRouteRunControls(QWidget):
    """Own route-run UI state and translate button clicks into run intent."""

    measure_requested = Signal()
    stop_requested = Signal()
    pause_requested = Signal()
    interrupt_requested = Signal()
    resume_requested = Signal()
    save_shift_requested = Signal()
    confirmation_requested = Signal(str)
    move_requested = Signal()
    jump_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = RouteRunControlState()

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(8)

        run_buttons = QHBoxLayout()
        self.measure_button = QPushButton("Measure", self)
        self.pause_button = QPushButton("Pause", self)
        self.pause_button.setEnabled(False)
        self.interrupt_button = QPushButton("Interrupt", self)
        self.interrupt_button.setEnabled(False)
        self.interrupt_button.hide()
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setEnabled(False)
        run_buttons.addWidget(self.measure_button)
        run_buttons.addWidget(self.pause_button)
        run_buttons.addWidget(self.interrupt_button)
        run_buttons.addWidget(self.stop_button)
        root_layout.addLayout(run_buttons)

        confirmation_buttons = QHBoxLayout()
        self.save_shift_button = QPushButton("Save Shift", self)
        self.remeasure_button = QPushButton("Remeasure", self)
        self.skip_button = QPushButton("Skip", self)
        self.next_button = QPushButton("Next", self)
        self.move_selected_button = QPushButton("Move", self)
        self.jump_selected_button = QPushButton("Jump Selected", self)
        for button in (
            self.save_shift_button,
            self.remeasure_button,
            self.skip_button,
            self.next_button,
            self.move_selected_button,
            self.jump_selected_button,
        ):
            button.setEnabled(False)
        self.remeasure_button.hide()
        self.next_button.hide()
        self.jump_selected_button.hide()
        confirmation_buttons.addWidget(self.save_shift_button)
        confirmation_buttons.addWidget(self.skip_button)
        confirmation_buttons.addWidget(self.move_selected_button)
        root_layout.addLayout(confirmation_buttons)

        self.status_label = QLabel("Route measurement idle.", self)
        self.status_label.setWordWrap(True)
        root_layout.addWidget(self.status_label)

        self.measure_button.clicked.connect(self.measure_requested.emit)
        self.pause_button.clicked.connect(self._emit_pause_action)
        self.interrupt_button.clicked.connect(self._emit_pause_action)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.save_shift_button.clicked.connect(self.save_shift_requested.emit)
        self.remeasure_button.clicked.connect(
            lambda _checked=False: self.confirmation_requested.emit("remeasure")
        )
        self.skip_button.clicked.connect(
            lambda _checked=False: self.confirmation_requested.emit("skip")
        )
        self.next_button.clicked.connect(
            lambda _checked=False: self.confirmation_requested.emit("next")
        )
        self.move_selected_button.clicked.connect(self.move_requested.emit)
        self.jump_selected_button.clicked.connect(self.jump_requested.emit)

    @property
    def running(self) -> bool:
        return self._state.running

    @property
    def waiting(self) -> bool:
        return self._state.waiting

    @property
    def presentation(self) -> RouteRunControlPresentation:
        return self._state.presentation()

    def set_running(self, running: bool) -> None:
        self._state = self._state.with_running(running)
        if self._state.running:
            self.status_label.setText("Route measurement running.")
        elif self.status_label.text() == "Route measurement running.":
            self.status_label.setText("Route measurement idle.")
        self._render_state_controls()

    def set_waiting(self, waiting: bool, reason: str = "") -> None:
        self._state = self._state.with_waiting(waiting, reason)
        self._render_state_controls()

    def set_pause_request_pending(self, pending: bool) -> None:
        self._state = self._state.with_pause_request_pending(pending)
        self._render_state_controls()

    def set_interrupt_request_pending(self, pending: bool) -> None:
        self._state = self._state.with_interrupt_request_pending(pending)
        self._render_state_controls()

    def set_status(self, text: str) -> None:
        self.status_label.setText(text or "Route measurement idle.")

    def apply_enablement(self, state: DesignNavigatorEnablement) -> None:
        self.measure_button.setEnabled(state.can_run_selected)
        self.stop_button.setEnabled(state.route_running)
        self.save_shift_button.setEnabled(state.can_save_shift)
        self.remeasure_button.setEnabled(state.can_confirm_waiting)
        self.skip_button.setEnabled(state.can_confirm_waiting)
        self.next_button.setEnabled(state.can_confirm_waiting)
        self.move_selected_button.setEnabled(state.can_move_selected)
        self.jump_selected_button.setEnabled(state.can_jump_selected)
        self._render_state_controls()

    def _emit_pause_action(self) -> None:
        action = self._state.pause_action()
        if action == "resume":
            self.resume_requested.emit()
            return
        if action == "interrupt":
            self._state = self._state.with_interrupt_request_pending(True)
            self._render_state_controls()
            self.interrupt_requested.emit()
            return
        self._state = self._state.with_pause_request_pending(True)
        self._render_state_controls()
        self.pause_requested.emit()

    def _render_state_controls(self) -> None:
        presentation = self._state.presentation()
        self.pause_button.setText(presentation.pause_text)
        self.pause_button.setEnabled(presentation.pause_enabled)
        self.interrupt_button.setText(presentation.interrupt_text)
        self.interrupt_button.setEnabled(presentation.interrupt_enabled)


__all__ = ["DesignRouteRunControls"]
