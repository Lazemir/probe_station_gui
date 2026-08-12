"""Route measurement point, session, and safety-critical run controls."""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from probe_station_gui.route.run_ui import RouteRunControlState
from probe_station_gui.shared.wheel_guard import GuardedSpinBox as QSpinBox


class RouteMeasurementRunControls(QWidget):
    """Own route-run state and translate button clicks into narrow intents."""

    measure_requested = Signal()
    start_session_requested = Signal()
    cancel_session_requested = Signal()
    next_requested = Signal()
    remeasure_requested = Signal()
    measure_current_requested = Signal()
    skip_requested = Signal()
    save_shift_requested = Signal()
    interrupt_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()
    jump_requested = Signal(int)
    move_requested = Signal(int)
    current_point_changed = Signal(int)
    close_requested = Signal()

    def __init__(
        self,
        *,
        route_point_count: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = RouteRunControlState()
        self._route_point_count = max(1, int(route_point_count))
        self._measurement_session_active = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        point_row = QHBoxLayout()
        self._current_point_spin = QSpinBox(self)
        self._current_point_spin.setRange(1, self._route_point_count)
        self._current_point_spin.setValue(1)
        self._move_button = QPushButton("Move", self)
        self._jump_button = QPushButton("Jump", self)
        self._jump_button.hide()
        point_row.addWidget(QLabel("Current point", self))
        point_row.addWidget(self._current_point_spin)
        point_row.addStretch(1)
        layout.addLayout(point_row)

        button_row = QHBoxLayout()
        self._start_session_button = QPushButton("Start Session", self)
        self._cancel_session_button = QPushButton("Cancel Session", self)
        self._measure_button = QPushButton("Measure", self)
        self._pause_button = QPushButton("Pause", self)
        self._stop_button = QPushButton("Stop", self)
        self._save_shift_button = QPushButton("Save Shift", self)
        self._remeasure_button = QPushButton("Remeasure", self)
        self._skip_button = QPushButton("Skip", self)
        self._close_button = QPushButton("Close", self)
        self._start_session_button.hide()
        self._cancel_session_button.hide()
        self._remeasure_button.hide()
        button_row.addWidget(self._measure_button)
        button_row.addWidget(self._move_button)
        button_row.addWidget(self._save_shift_button)
        button_row.addWidget(self._skip_button)
        button_row.addWidget(self._pause_button)
        button_row.addWidget(self._stop_button)
        button_row.addStretch(1)
        button_row.addWidget(self._close_button)
        layout.addLayout(button_row)

        self._current_point_spin.valueChanged.connect(self._on_current_point_changed)
        self._start_session_button.clicked.connect(self.start_session_requested.emit)
        self._cancel_session_button.clicked.connect(self.cancel_session_requested.emit)
        self._measure_button.clicked.connect(self._emit_measure_intent)
        self._pause_button.clicked.connect(self._emit_pause_intent)
        self._stop_button.clicked.connect(self.stop_requested.emit)
        self._save_shift_button.clicked.connect(self.save_shift_requested.emit)
        self._remeasure_button.clicked.connect(self.remeasure_requested.emit)
        self._skip_button.clicked.connect(self.skip_requested.emit)
        self._move_button.clicked.connect(self._emit_move_requested)
        self._jump_button.clicked.connect(self._emit_jump_requested)
        self._close_button.clicked.connect(self.close_requested.emit)
        self.set_running(False)

    def running(self) -> bool:
        return self._state.running

    def waiting(self) -> bool:
        return self._state.waiting

    def can_confirm_waiting(self) -> bool:
        return self._state.can_confirm_waiting

    def current_point(self) -> int:
        return int(self._current_point_spin.value())

    def measurement_session_active(self) -> bool:
        return bool(self._measurement_session_active)

    def set_running(self, running: bool) -> None:
        self._state = self._state.with_running(running)
        idle = not self._state.running
        self._current_point_spin.setEnabled(idle)
        self._stop_button.setEnabled(self._state.running)
        self._close_button.setEnabled(idle)
        self.set_waiting(False)

    def set_waiting(self, waiting: bool, reason: str = "") -> None:
        self._state = self._state.with_waiting(waiting, reason)
        can_confirm = self._state.can_confirm_waiting
        editable = (not self._state.running) or can_confirm
        self._current_point_spin.setEnabled(editable)
        self._move_button.setEnabled(editable)
        self._jump_button.setEnabled(can_confirm)
        self._save_shift_button.setEnabled(can_confirm)
        self._remeasure_button.setEnabled(can_confirm)
        self._skip_button.setEnabled(can_confirm)
        self._measure_button.setEnabled(editable)
        self._stop_button.setEnabled(self._state.running)
        self._render_pause_control()
        self._update_session_buttons()

    def set_pause_request_pending(self, pending: bool) -> None:
        self._state = self._state.with_pause_request_pending(pending)
        self._render_pause_control()

    def set_interrupt_request_pending(self, pending: bool) -> None:
        self._state = self._state.with_interrupt_request_pending(pending)
        self._render_pause_control()

    def set_measurement_session_active(self, active: bool) -> None:
        self._measurement_session_active = bool(active)
        self._update_session_buttons()

    def set_current_point(self, point_number: int) -> None:
        """Synchronize the displayed point without publishing an operator intent."""

        value = min(max(1, int(point_number)), self._route_point_count)
        with QSignalBlocker(self._current_point_spin):
            self._current_point_spin.setValue(value)

    def set_current_point_and_publish(self, point_number: int) -> None:
        """Select a changed point and publish the finalized selection once."""

        previous = self.current_point()
        self.set_current_point(point_number)
        current = self.current_point()
        if current != previous:
            self.current_point_changed.emit(current)

    def set_route_point_count(self, route_point_count: int) -> None:
        previous = self.current_point()
        self._route_point_count = max(1, int(route_point_count))
        current = min(previous, self._route_point_count)
        with QSignalBlocker(self._current_point_spin):
            self._current_point_spin.setRange(1, self._route_point_count)
            self._current_point_spin.setValue(current)
        if current != previous:
            self.current_point_changed.emit(current)

    def _emit_measure_intent(self) -> None:
        if self._state.running and self._state.waiting:
            self.measure_current_requested.emit()
            return
        self.measure_requested.emit()

    def _emit_pause_intent(self) -> None:
        action = self._state.pause_action()
        if action == "resume":
            self.next_requested.emit()
            return
        if action == "interrupt":
            self.set_interrupt_request_pending(True)
            self.interrupt_requested.emit()
            return
        self.set_pause_request_pending(True)
        self.pause_requested.emit()

    def _emit_move_requested(self) -> None:
        self.move_requested.emit(self.current_point())

    def _emit_jump_requested(self) -> None:
        self.jump_requested.emit(self.current_point())

    def _on_current_point_changed(self, value: int) -> None:
        self.current_point_changed.emit(
            min(max(1, int(value)), self._route_point_count)
        )

    def _render_pause_control(self) -> None:
        presentation = self._state.presentation()
        self._pause_button.setText(presentation.pause_text)
        self._pause_button.setEnabled(presentation.pause_enabled)

    def _update_session_buttons(self) -> None:
        can_change_session = not self._state.running
        self._start_session_button.setEnabled(
            can_change_session and not self._measurement_session_active
        )
        self._cancel_session_button.setEnabled(
            can_change_session and self._measurement_session_active
        )


__all__ = ["RouteMeasurementRunControls"]
