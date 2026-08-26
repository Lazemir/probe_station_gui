from __future__ import annotations

import logging
import re
from PySide6.QtCore import QTimer
from probe_station_gui.stage.exact_step import ExactStepClearReason
from probe_station_gui.views import main_window_coordinate_step as coordinate_step

logger = logging.getLogger("main")


class _MainMotionPredictionMixin:
    def _schedule_status_refreshes(self, delays_ms: tuple[int, ...]) -> None:
        for delay_ms in delays_ms:
            QTimer.singleShot(delay_ms, self.stage_controller.request_status_refresh)

    def _on_manual_terminal_command(self, command: str) -> None:
        stripped = command.strip().upper()
        if not stripped:
            return
        coordinate_step.clear_exact_steps(
            self,
            ExactStepClearReason.MANUAL_TERMINAL_COMMAND,
        )
        self.stage_controller.invalidate_needles_state()
        self.stage_controller.invalidate_coordinate_confidence(
            "Manual controller command."
        )
        if re.match(r"^G5(?:4|5|6|7|8|9(?:\.[123])?)$", stripped):
            self.stage_controller.request_startup_sync(auto_home_a=False)
            self._schedule_cancel_state_refresh()
            return
        if (
            stripped.startswith("$#")
            or stripped.startswith("$G")
            or stripped.startswith("$10")
            or stripped.startswith("G10")
        ):
            self.stage_controller.request_startup_sync(auto_home_a=False)
            self._schedule_cancel_state_refresh()

    def _on_stage_task_started(self) -> None:
        self._show_status("Moving stage...")
        self._update_stage_coordinate_apply_state()

    def on_autofocus_finished(self, success: bool, message: str) -> None:
        if message:
            self._show_status(message, 5000)
        if success:
            self._remember_sample_focus_from_latest()
        else:
            logger.error("Autofocus failed: %s", message)
        self._schedule_cancel_state_refresh()

    def on_calibration_changed(
        self, mm_per_pixel_x: float, mm_per_pixel_y: float
    ) -> None:
        self._show_status(
            f"Calibration: ΔX {mm_per_pixel_x:.6f} mm/px, ΔY {mm_per_pixel_y:.6f} mm/px",
            5000,
        )
        self.view.set_scale(mm_per_pixel_x, mm_per_pixel_y)

    def _on_measure_action_toggled(self, checked: bool) -> None:
        """Handle ruler/rect toggle — keep the two actions mutually exclusive."""
        sender = self.sender()
        if not checked:
            # Only exit if no other measure action is checked
            if (self._ruler_action is None or not self._ruler_action.isChecked()) and (
                self._rect_action is None or not self._rect_action.isChecked()
            ):
                self.view.set_measure_mode(None)
            return
        # Uncheck the other action without triggering this handler recursively
        if sender is self._ruler_action and self._rect_action is not None:
            self._rect_action.blockSignals(True)
            self._rect_action.setChecked(False)
            self._rect_action.blockSignals(False)
        elif sender is self._rect_action and self._ruler_action is not None:
            self._ruler_action.blockSignals(True)
            self._ruler_action.setChecked(False)
            self._ruler_action.blockSignals(False)
        mode = "ruler" if sender is self._ruler_action else "rect"
        self.view.set_measure_mode(mode)

    def _on_measure_mode_exited(self) -> None:
        """Exit measure mode (called on Esc or from the view's own signal)."""
        self.view.set_measure_mode(None)
        for action in (self._ruler_action, self._rect_action):
            if action is not None:
                action.blockSignals(True)
                action.setChecked(False)
                action.blockSignals(False)
