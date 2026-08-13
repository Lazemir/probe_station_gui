from __future__ import annotations

import logging

from PySide6.QtWidgets import QInputDialog, QMessageBox

from probe_station_gui.design import objective_alignment as alignment
from probe_station_gui.dialogs.click_calibration_dialog import ClickCalibrationDialog
from probe_station_gui.dialogs.lens_distortion_dialog import LensDistortionDialog
from probe_station_gui.dialogs.optical_calibration_wizard import (
    OpticalCalibrationMode,
    OpticalCalibrationWizard,
)
from probe_station_gui.settings.objective_config import normalize_objective_name

logger = logging.getLogger("main")


class _MainObjectiveToolsMixin:
    def _show_click_calibration_dialog(self) -> None:
        if self._click_calibration_dialog is None:
            dialog = ClickCalibrationDialog(self)
            dialog.objective_selected.connect(
                lambda name: self._set_active_objective(name, apply_motion=True)
            )
            dialog.reset_requested.connect(self._reset_click_calibration)
            dialog.add_requested.connect(self._add_objective_profile)
            dialog.delete_requested.connect(self._delete_objective_profile)
            dialog.offset_reference_requested.connect(
                self._set_objective_offset_reference
            )
            dialog.offset_save_requested.connect(self._save_active_objective_offset)
            dialog.offset_reset_requested.connect(self._reset_active_objective_offset)
            self._click_calibration_dialog = dialog
        self._refresh_click_calibration_ui()
        self._click_calibration_dialog.show()
        self._click_calibration_dialog.raise_()
        self._click_calibration_dialog.activateWindow()

    def _show_optical_calibration_wizard(
        self,
        mode: OpticalCalibrationMode | None = None,
    ) -> None:
        if self._optical_calibration_wizard is None:
            wizard = OpticalCalibrationWizard(self)
            wizard.start_flat_field_requested.connect(
                self._start_flat_field_calibration_from_wizard
            )
            wizard.start_lens_distortion_requested.connect(
                self._start_lens_distortion_calibration_from_wizard
            )
            wizard.cancel_requested.connect(self._cancel_optical_calibration_wizard)
            self._optical_calibration_wizard = wizard

        if self._optical_calibration_runtime.state().parent_session_token is not None:
            self._show_status("Optical calibration is still active.", 5000)
            self._optical_calibration_wizard.show()
            self._optical_calibration_wizard.raise_()
            self._optical_calibration_wizard.activateWindow()
            return

        objectives = self.settings_manager.objectives_configuration()
        active_name = normalize_objective_name(objectives.active_name)
        profile = objectives.objectives.get(active_name)
        flat_field_configured = False
        if active_name:
            try:
                flat_field_configured = (
                    self._flat_field_calibration_store.current_manifest_path(
                        active_name
                    ).is_file()
                )
            except (OSError, ValueError):
                flat_field_configured = False
        self._optical_calibration_wizard.set_objective(
            active_name,
            flat_field_configured=flat_field_configured,
            lens_configured=bool(
                profile is not None and profile.distortion_correction_configured
            ),
        )
        if not self._optical_calibration_wizard.prepare(mode):
            self._show_status("Optical calibration is already running.", 4000)
        self._optical_calibration_wizard.show()
        self._optical_calibration_wizard.raise_()
        self._optical_calibration_wizard.activateWindow()

    def _show_lens_distortion_dialog(self) -> None:
        if self._lens_distortion_dialog is None:
            dialog = LensDistortionDialog(self)
            dialog.calibrate_requested.connect(
                lambda: self._show_optical_calibration_wizard(
                    OpticalCalibrationMode.LENS_DISTORTION
                )
            )
            dialog.reset_requested.connect(self._reset_lens_distortion_calibration)
            self._lens_distortion_dialog = dialog
        self._refresh_lens_distortion_ui()
        self._lens_distortion_dialog.show()
        self._lens_distortion_dialog.raise_()
        self._lens_distortion_dialog.activateWindow()

    def _add_objective_profile(self) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective not added.", 4000)
            return
        raw_name, accepted = QInputDialog.getText(
            self, "Add Objective", "Objective name"
        )
        if not accepted:
            return
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective not added.", 4000)
            return
        plan = alignment.profile_add_plan(self.settings_manager.settings, raw_name)
        if plan.select_existing_name is not None:
            self._set_active_objective(plan.select_existing_name, apply_motion=True)
            self._show_plan_status(plan)
            return
        self._persist_objective_plan(plan)

    def _delete_objective_profile(self, objective_name: str) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective not deleted.", 4000)
            self._refresh_click_calibration_ui()
            return
        name = normalize_objective_name(objective_name)
        if not name:
            return
        preflight = alignment.profile_delete_plan(
            self.settings_manager.settings, name, confirmed=False
        )
        if preflight.status:
            self._show_plan_status(preflight)
            return
        response = QMessageBox.question(
            self,
            "Delete Objective",
            f"Delete objective profile {name}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if response != QMessageBox.Yes:
            return
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective not deleted.", 4000)
            self._refresh_click_calibration_ui()
            return
        plan = alignment.profile_delete_plan(
            self.settings_manager.settings, name, confirmed=True
        )
        self._persist_objective_plan(plan)

    def _set_objective_offset_reference(self) -> None:
        if self._objective_mutation_busy():
            self._show_status(
                "Stage is busy; objective offset reference not set.", 4000
            )
            return
        raw_stage_xy = self._resolve_alignment_capture_stage_position()
        if raw_stage_xy is None:
            return
        plan = alignment.objective_offset_reference_plan(
            self.settings_manager.settings, raw_stage_xy
        )
        if plan.settings is not None:
            self._persist_objective_plan(plan, show_status=False)
        if plan.reference is not None:
            self._objective_offset_reference = plan.reference
            self._refresh_click_calibration_ui()
        elif plan.refresh_calibration_ui:
            self._refresh_click_calibration_ui()
        self._show_plan_status(plan)

    def _save_active_objective_offset(self) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective offset not saved.", 4000)
            return
        if self._objective_offset_reference is None:
            self._show_status("Set an objective offset reference first.", 5000)
            return
        raw_stage_xy = self._resolve_alignment_capture_stage_position()
        if raw_stage_xy is None:
            return
        plan = alignment.save_active_objective_offset(
            self.settings_manager.settings,
            self._objective_offset_reference,
            raw_stage_xy,
        )
        self._persist_objective_plan(plan)

    def _reset_active_objective_offset(self) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective offset not reset.", 4000)
            return
        plan = alignment.reset_active_objective_offset(self.settings_manager.settings)
        if plan.settings is None:
            return
        self._persist_objective_plan(plan)

    def _refresh_click_calibration_ui(self) -> None:
        click_action = getattr(self, "_click_calibration_action", None)
        if click_action is not None:
            click_action.setText("Click-to-Move Calibration")
        click_dialog = getattr(self, "_click_calibration_dialog", None)
        if click_dialog is not None:
            click_dialog.set_objectives(
                self.settings_manager.objectives_configuration()
            )

    def _refresh_lens_distortion_ui(self) -> None:
        lens_action = getattr(self, "_lens_distortion_calibration_action", None)
        if lens_action is not None:
            lens_action.setText("Lens Distortion Calibration")
        lens_dialog = getattr(self, "_lens_distortion_dialog", None)
        if lens_dialog is not None:
            lens_dialog.set_objectives(self.settings_manager.objectives_configuration())

    def _refresh_objective_calibration_ui(self) -> None:
        self._refresh_click_calibration_ui()
        self._refresh_lens_distortion_ui()
