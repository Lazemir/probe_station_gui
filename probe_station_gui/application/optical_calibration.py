from __future__ import annotations

import logging
import uuid

from probe_station_gui.camera.optical_calibration_adapters import (
    optical_calibration_preflight_message,
    prepare_lens_completion,
)
from probe_station_gui.camera.optical_calibration_geometry import LensFitLimits
from probe_station_gui.camera.optical_calibration_runtime import (
    FlatFieldCalibrationRequest,
    LensDistortionCalibrationRequest,
    OpticalCalibrationOutcome,
    OpticalCalibrationProgress,
)
from probe_station_gui.design import objective_alignment as alignment
from probe_station_gui.dialogs.optical_calibration_wizard import (
    OpticalCalibrationMode,
    OpticalCalibrationWizard,
)
from probe_station_gui.settings.objective_config import (
    normalize_objective_name,
    parse_pixels_to_mm_matrix,
)
from probe_station_gui.stage.types import StageTaskToken

logger = logging.getLogger("main")


class _MainOpticalCalibrationMixin:
    def on_calibration_changed(
        self,
        mm_per_pixel_x: float,
        mm_per_pixel_y: float,
    ) -> None:
        self._show_status(
            f"Calibration: ΔX {mm_per_pixel_x:.6f} mm/px, "
            f"ΔY {mm_per_pixel_y:.6f} mm/px",
            5000,
        )
        self.view.set_scale(mm_per_pixel_x, mm_per_pixel_y)

    def _start_flat_field_calibration_from_wizard(self) -> None:
        wizard = self._optical_calibration_wizard
        if wizard is None:
            return
        run_id = wizard.active_run_id()
        full_wizard = wizard.mode() is OpticalCalibrationMode.FULL
        if not self._optical_calibration_objective_matches_wizard(wizard):
            wizard.set_flat_field_result(
                False,
                "Active objective changed. Reopen optical calibration.",
                run_id=run_id,
            )
            return
        if self._start_flat_field_calibration(
            wizard_run_id=run_id, full_wizard=full_wizard
        ):
            return
        if full_wizard:
            self._cancel_optical_calibration_wizard(run_id)
        wizard.set_flat_field_result(
            False, "Flat-field calibration did not start.", run_id=run_id
        )

    def _cancel_optical_calibration_wizard(self, _run_id: object = None) -> None:
        wizard = self._optical_calibration_wizard
        if _run_id is not None and (
            wizard is None or wizard.active_run_id() != _run_id
        ):
            return
        self._optical_calibration_runtime.cancel()
        self._stop_lens_distortion_dialog()

    def _stop_lens_distortion_dialog(self) -> None:
        dialog = self._lens_distortion_dialog
        if dialog is not None:
            dialog.set_running(False)
            dialog.set_status("Lens distortion calibration stopped.")

    def _start_lens_distortion_calibration_from_wizard(self) -> None:
        wizard = self._optical_calibration_wizard
        if wizard is None:
            return
        run_id = wizard.active_run_id()
        full_wizard = wizard.mode() is OpticalCalibrationMode.FULL
        if not self._optical_calibration_objective_matches_wizard(wizard):
            if full_wizard:
                self._cancel_optical_calibration_wizard(run_id)
            wizard.set_lens_distortion_result(
                False,
                "Active objective changed. Reopen optical calibration.",
                run_id=run_id,
            )
            return
        state = self._optical_calibration_runtime.state()
        parent_token = state.parent_session_token if full_wizard else None
        if full_wizard and parent_token is None:
            wizard.set_lens_distortion_result(
                False,
                "Optical calibration exposure session is unavailable.",
                run_id=run_id,
            )
            return
        result = self._start_lens_distortion_calibration(
            wizard_run_id=run_id,
            parent_session_token=parent_token,
            full_wizard=full_wizard,
        )
        if bool(result["accepted"]):
            return
        if full_wizard:
            self._cancel_optical_calibration_wizard(run_id)
        wizard.set_lens_distortion_result(False, str(result["message"]), run_id=run_id)

    def _optical_calibration_objective_matches_wizard(
        self,
        wizard: OpticalCalibrationWizard,
    ) -> bool:
        current_name, _magnification = self._active_objective_metadata()
        return normalize_objective_name(current_name) == normalize_objective_name(
            wizard.objective_text()
        )

    def _start_flat_field_calibration(
        self,
        *,
        wizard_run_id: int | None = None,
        full_wizard: bool = False,
    ) -> bool:
        unavailable = self._optical_calibration_preflight("flat")
        if unavailable:
            self._show_status(unavailable, 5000)
            return False
        try:
            data = self._optical_calibration_request_adapter.capture()
        except RuntimeError as exc:
            self._show_status(str(exc), 5000)
            return False
        request = FlatFieldCalibrationRequest(
            run_id=uuid.uuid4().hex,
            wizard_run_id=wizard_run_id,
            objective_name=data.objective_name,
            magnification=data.magnification,
            pixels_to_mm=data.pixels_to_mm,
            pixel_size_mm=data.pixel_size_mm,
            linear_feedrate=self._coordinate_feedrate_for_axes(("X", "Y")),
            needle_feedrate=self._current_needle_feedrate(),
            full_wizard=bool(full_wizard),
            grid_size=self.FLAT_FIELD_CAPTURE_GRID_SIZE,
            overlap_fraction=self.FLAT_FIELD_CAPTURE_OVERLAP_FRACTION,
            settle_s=self.FLAT_FIELD_CAPTURE_SETTLE_S,
            camera_timeout_s=self.FLAT_FIELD_CAMERA_TIMEOUT_S,
        )
        decision = self._optical_calibration_runtime.start_flat(request)
        self._show_status(decision.message, 4000 if decision.accepted else 8000)
        return decision.accepted

    def _on_flat_field_calibration_progress(
        self,
        event: object,
        message: str,
    ) -> None:
        if not isinstance(event, OpticalCalibrationProgress):
            return
        wizard = getattr(self, "_optical_calibration_wizard", None)
        if wizard is not None and event.wizard_run_id is not None:
            wizard.set_progress(str(message), run_id=event.wizard_run_id)

    def _on_flat_field_calibration_finished(
        self,
        outcome: object,
        success: bool,
        message: str,
        _payload: object,
    ) -> None:
        if not isinstance(outcome, OpticalCalibrationOutcome):
            return
        if not self._optical_calibration_runtime.consume(
            outcome,
            blocked=self._microscope_scan_running(),
        ):
            return
        wizard = getattr(self, "_optical_calibration_wizard", None)
        if wizard is not None and outcome.wizard_run_id is not None:
            wizard.set_flat_field_result(
                bool(success), str(message), run_id=outcome.wizard_run_id
            )
        self._show_status(str(message), 10000 if success else 8000)

    def _start_lens_distortion_calibration(
        self,
        *,
        wizard_run_id: int | None = None,
        parent_session_token: str | None = None,
        full_wizard: bool = False,
    ) -> dict[str, object]:
        unavailable = self._optical_calibration_preflight("lens")
        if unavailable:
            self._show_status(unavailable, 5000)
            return {"accepted": False, "status_code": 409, "message": unavailable}
        try:
            data = self._optical_calibration_request_adapter.capture()
        except RuntimeError as exc:
            message = str(exc)
            self._show_status(message, 5000)
            return {"accepted": False, "status_code": 409, "message": message}
        request = LensDistortionCalibrationRequest(
            run_id=uuid.uuid4().hex,
            wizard_run_id=wizard_run_id,
            objective_name=data.objective_name,
            magnification=data.magnification,
            pixels_to_mm=data.pixels_to_mm,
            pixel_size_mm=data.pixel_size_mm,
            linear_feedrate=self._coordinate_feedrate_for_axes(("X", "Y")),
            needle_feedrate=self._current_needle_feedrate(),
            full_wizard=bool(full_wizard),
            parent_session_token=parent_session_token,
            grid_size=self.LENS_DISTORTION_CAPTURE_GRID_SIZE,
            fov_fraction=self.LENS_DISTORTION_FOV_FRACTION,
            settle_s=self.LENS_DISTORTION_CAPTURE_SETTLE_S,
            camera_timeout_s=self.LENS_DISTORTION_CAMERA_TIMEOUT_S,
            fit_limits=self._lens_fit_limits(),
        )
        decision = self._optical_calibration_runtime.start_lens(request)
        if decision.accepted and self._lens_distortion_dialog is not None:
            self._lens_distortion_dialog.set_running(True)
            self._lens_distortion_dialog.set_status(decision.message)
        self._show_status(decision.message, 4000 if decision.accepted else 8000)
        return {
            "accepted": decision.accepted,
            "status_code": decision.status_code,
            "message": decision.message,
        }

    def _optical_calibration_preflight(self, kind: str) -> str:
        return optical_calibration_preflight_message(
            kind,
            self._optical_calibration_runtime.state(),
            stage_ready=self._stage_serial_ready(),
            stage_busy=self._stage_serial_ready() and self.stage_controller.is_busy(),
        )

    def _lens_fit_limits(self) -> LensFitLimits:
        return LensFitLimits(
            cluster_tolerance_px=self.LENS_DISTORTION_CLUSTER_TOLERANCE_PX,
            min_feature_count=self.LENS_DISTORTION_MIN_FEATURE_COUNT,
            min_observation_count=self.LENS_DISTORTION_MIN_OBSERVATION_COUNT,
            max_residual_mean_px=self.LENS_DISTORTION_MAX_RESIDUAL_MEAN_PX,
            max_residual_max_px=self.LENS_DISTORTION_MAX_RESIDUAL_MAX_PX,
        )

    def _reset_lens_distortion_calibration(self) -> tuple[bool, str]:
        active_name = normalize_objective_name(
            self.settings_manager.settings.objectives.active_name
        )
        if self._objective_profile_mutation_busy(active_name):
            message = (
                "Lens correction cannot be reset while a scan or calibration is active."
            )
            self._show_status(message, 5000)
            return False, message
        try:
            self._save_active_objective_distortion(None)
        except Exception as exc:
            logger.exception("Unable to reset lens distortion correction")
            message = f"Lens correction reset failed: {exc}"
            self._show_status(message, 8000)
            return False, message
        message = "Lens correction cleared. Recalibrate click-to-move."
        self._show_status(message, 8000)
        return True, message

    def _on_lens_distortion_calibration_progress(
        self,
        event: object,
        message: str,
    ) -> None:
        if not isinstance(event, OpticalCalibrationProgress):
            return
        wizard = getattr(self, "_optical_calibration_wizard", None)
        if wizard is not None and event.wizard_run_id is not None:
            wizard.set_progress(str(message), run_id=event.wizard_run_id)

    def _on_lens_distortion_calibration_finished(
        self,
        outcome: object,
        success: bool,
        message: str,
        artifact: object,
    ) -> None:
        if not isinstance(outcome, OpticalCalibrationOutcome):
            return
        if not self._optical_calibration_runtime.consume(
            outcome,
            blocked=self._microscope_scan_running(),
            on_discarded=self._stop_lens_distortion_dialog,
        ):
            return
        presentation = prepare_lens_completion(
            outcome,
            bool(success),
            str(message),
            artifact,
            limits=self._lens_fit_limits(),
            save=lambda payload, objective_name, context: (
                self._save_objective_distortion(
                    payload,
                    objective_name,
                    optical_context=context,
                    allow_stage_task=True,
                )
            ),
        )
        if self._lens_distortion_dialog is not None:
            self._lens_distortion_dialog.set_running(False)
            self._lens_distortion_dialog.set_status(presentation.message)
        wizard = getattr(self, "_optical_calibration_wizard", None)
        if wizard is not None and outcome.wizard_run_id is not None:
            wizard.set_lens_distortion_result(
                presentation.success,
                presentation.message,
                run_id=outcome.wizard_run_id,
                **presentation.wizard_kwargs(),
            )
        self._show_status(
            presentation.message,
            10000 if presentation.success else 8000,
        )

    def _save_active_objective_distortion(
        self,
        payload: object | None,
        *,
        optical_context: OpticalCalibrationOutcome | None = None,
        allow_stage_task: bool = False,
    ) -> None:
        active_name = normalize_objective_name(
            self.settings_manager.settings.objectives.active_name
        )
        self._save_objective_distortion(
            payload,
            active_name,
            optical_context=optical_context,
            allow_stage_task=allow_stage_task,
        )

    def _save_objective_distortion(
        self,
        payload: object | None,
        objective_name: str,
        *,
        optical_context: OpticalCalibrationOutcome | None = None,
        allow_stage_task: bool = False,
    ) -> None:
        settings = self.settings_manager.settings.clone()
        objectives = settings.objectives
        objective_name = normalize_objective_name(objective_name)
        if not objective_name:
            raise RuntimeError("No active objective selected.")
        if optical_context is not None and not isinstance(
            optical_context, OpticalCalibrationOutcome
        ):
            raise RuntimeError(
                "Optical calibration result was canceled or is no longer current."
            )
        if self._objective_profile_mutation_busy(
            objective_name,
            allow_stage_task=allow_stage_task,
            optical_context=optical_context,
        ):
            raise RuntimeError(
                "Active objective correction cannot change while a scan or "
                "calibration is active."
            )
        profile = objectives.objectives.get(objective_name)
        if profile is None:
            raise RuntimeError(f"Objective profile {objective_name} is missing.")

        updated = profile.clone()
        if payload is None:
            updated.distortion_correction = {}
            updated.distortion_correction_configured = False
        elif isinstance(payload, dict):
            updated.distortion_correction = dict(payload)
            updated.distortion_correction_configured = True
            calibrated_matrix = self._calibrated_pixels_to_mm_from_distortion_payload(
                payload
            )
            if calibrated_matrix:
                updated.pixels_to_mm = calibrated_matrix
                updated.xy_calibration_configured = True
            else:
                updated.pixels_to_mm = []
                updated.xy_calibration_configured = False
        else:
            raise RuntimeError("Invalid lens correction payload.")
        objectives.objectives[objective_name] = updated
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )
        self._apply_objective_settings()
        self._refresh_objective_calibration_ui()

    @staticmethod
    def _calibrated_pixels_to_mm_from_distortion_payload(
        payload: dict[str, object],
    ) -> list[list[float]]:
        return parse_pixels_to_mm_matrix(payload.get("calibrated_pixels_to_mm"))

    @staticmethod
    def _lens_distortion_payload_invalidates_click_calibration(payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        return not bool(
            _MainOpticalCalibrationMixin._calibrated_pixels_to_mm_from_distortion_payload(
                payload
            )
        )

    @staticmethod
    def _append_click_recalibration_message(message: str) -> str:
        suffix = "Recalibrate click-to-move."
        text = str(message)
        if suffix in text:
            return text
        if not text:
            return suffix
        return f"{text} {suffix}"

    def _on_objective_calibration_updated(
        self,
        objective_name: str,
        pixels_to_mm: object,
        task_token: object,
    ) -> None:
        reset_callback = (
            isinstance(task_token, StageTaskToken)
            and task_token.source == "click_calibration_reset"
        )
        expected_sources = (
            {"click_calibration_reset"}
            if reset_callback
            else {
                "_run_move",
                "click_calibration",
                "_run_clicked_point_resolution",
            }
        )
        if not self._calibration_callback_token_is_current(
            task_token,
            expected_sources=expected_sources,
        ):
            self._reject_objective_calibration_candidate(task_token)
            message = (
                "Click-to-move calibration result ignored because its stage task "
                "is no longer current."
            )
            logger.warning(message)
            self._show_status(message, 7000)
            return
        objective_name = normalize_objective_name(objective_name)
        if self._objective_profile_mutation_busy(
            objective_name,
            allow_stage_task=True,
        ):
            self._reject_objective_calibration_candidate(task_token)
            message = (
                "Click-to-move calibration result ignored because a scan or "
                "calibration is active."
            )
            logger.warning(message)
            self._show_status(message, 7000)
            return
        pixels_to_mm = self._objective_pixels_to_mm_for_calibration_update(
            objective_name,
            pixels_to_mm,
        )
        plan = alignment.update_objective_calibration(
            self.settings_manager.settings, objective_name, pixels_to_mm
        )
        if plan.settings is None:
            self._reject_objective_calibration_candidate(task_token)
            return
        if reset_callback:
            self._persist_objective_plan(plan)
            return

        accept = getattr(
            self.stage_controller,
            "accept_objective_calibration_candidate",
            None,
        )
        accepted = False
        if callable(accept):
            try:
                accepted = bool(accept(task_token, pixels_to_mm))
            except Exception:
                logger.exception("Unable to accept click calibration candidate")
        if not accepted:
            self._reject_objective_calibration_candidate(task_token)
            self._show_status(
                "Click-to-move calibration was cancelled before it could be applied.",
                7000,
            )
            return

        previous_settings = self.settings_manager.settings.clone()
        try:
            persisted = self._persist_objective_plan(
                plan,
                apply_objective_runtime=False,
            )
        except Exception as exc:
            logger.exception("Unable to persist accepted click calibration")
            self._reject_objective_calibration_candidate(task_token)
            message = f"Click-to-move calibration could not be saved: {exc}"
            try:
                self.settings_manager.replace_and_save(
                    previous_settings,
                    preserve_exposure_policy=True,
                )
            except Exception as restore_exc:
                logger.exception(
                    "Unable to restore settings after click calibration save failure"
                )
                message = f"{message} Settings restore failed: {restore_exc}"
            self._show_status(message, 7000)
            return
        if not persisted:
            self._reject_objective_calibration_candidate(task_token)
            return

        publish = getattr(
            self.stage_controller,
            "publish_objective_calibration_candidate",
            None,
        )
        published = False
        if callable(publish):
            try:
                published = bool(publish(task_token))
            except Exception:
                logger.exception("Unable to publish click calibration candidate")
        if published:
            return

        self._reject_objective_calibration_candidate(task_token)
        try:
            self.settings_manager.replace_and_save(
                previous_settings,
                preserve_exposure_policy=True,
            )
        except Exception as exc:
            logger.exception("Unable to roll back rejected click calibration settings")
            message = (
                "Click-to-move calibration was cancelled before it could be applied. "
                f"Settings restore failed: {exc}"
            )
        else:
            message = (
                "Click-to-move calibration was cancelled before it could be applied."
            )
        self._show_status(message, 7000)

    def _reject_objective_calibration_candidate(self, task_token: object) -> None:
        reject = getattr(
            getattr(self, "stage_controller", None),
            "reject_objective_calibration_candidate",
            None,
        )
        if not callable(reject):
            return
        try:
            reject(task_token)
        except Exception:
            logger.exception("Unable to reject click calibration candidate")

    def _calibration_callback_token_is_current(
        self,
        task_token: object,
        *,
        expected_sources: set[str] | frozenset[str] | None = None,
    ) -> bool:
        if not isinstance(task_token, StageTaskToken):
            return False
        if expected_sources is not None and task_token.source not in expected_sources:
            return False
        stage_controller = getattr(self, "stage_controller", None)
        validator = getattr(
            stage_controller,
            "is_calibration_task_token_current",
            None,
        )
        if not callable(validator):
            return False
        try:
            return bool(validator(task_token))
        except Exception:
            logger.exception("Unable to validate stage calibration callback token")
            return False

    def _objective_pixels_to_mm_for_calibration_update(
        self,
        objective_name: str,
        pixels_to_mm: object,
    ) -> object:
        if not parse_pixels_to_mm_matrix(pixels_to_mm):
            return pixels_to_mm
        settings = self.settings_manager.settings
        name = normalize_objective_name(objective_name)
        profile = settings.objectives.objectives.get(name)
        if profile is None or not bool(
            getattr(profile, "distortion_correction_configured", False)
        ):
            return pixels_to_mm
        payload = getattr(profile, "distortion_correction", {})
        if not isinstance(payload, dict):
            return pixels_to_mm
        calibrated = self._calibrated_pixels_to_mm_from_distortion_payload(payload)
        return calibrated or pixels_to_mm

    def _on_objective_mismatch_detected(
        self,
        suggested_name: str,
        message: str,
        task_token: object,
    ) -> None:
        if not self._calibration_callback_token_is_current(
            task_token,
            expected_sources={"_run_move", "click_calibration_check"},
        ):
            stale_message = (
                "Objective mismatch ignored because its calibration task is no "
                "longer current."
            )
            logger.warning(stale_message)
            self._show_status(stale_message, 7000)
            return
        name = normalize_objective_name(suggested_name)
        objective_settings = self.settings_manager.objectives_configuration()
        if name in objective_settings.objectives:
            self._set_active_objective(
                name,
                apply_motion=False,
                allow_stage_task=True,
            )
        if message:
            self._show_status(message, 7000)
