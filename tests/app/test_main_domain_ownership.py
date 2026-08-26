"""Ownership contracts for all twenty-two ``Main`` application domains."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPLICATION_ROOT = ROOT / "probe_station_gui" / "application"

OWNER_SPECS = {
    "bootstrap_api": (
        "_MainBootstrapApiMixin",
        {
            "_start_camera_thread": "(self) -> 'None'",
            "_start_api_server": "(self) -> 'None'",
            "_compose_camera_exposure_policy": "(self) -> 'None'",
            "_create_stage_controller": "(self) -> 'StageController'",
            "_compose_optical_calibration_runtime": "(self) -> 'None'",
            "_on_exposure_policy_command_finished": "(self, result: 'object') -> 'None'",
            "_configure_api_server_from_settings": "(self, *, start_if_enabled: 'bool') -> 'None'",
            "_authorize_api_request": "(self, api_key: 'str | None', permission: 'str') -> 'dict[str, Any]'",
            "_submit_camera_settings_snapshot": "(self, request_id: 'str', names: 'list[str]') -> 'None'",
            "_submit_camera_settings_batch": "(self, request_id: 'str', settings: 'list[tuple[str, object]]') -> 'None'",
            "_write_camera_auto_exposure_settings": "(self, settings: 'list[tuple[str, object]]') -> 'dict[str, Any]'",
            "_read_camera_auto_exposure_frame": "(self, after_counter: 'int', timeout_s: 'float') -> 'AutoExposureFrame'",
            "_api_camera_frame": "(self, space: 'str', after_counter: 'int | None', timeout_s: 'float') -> 'dict[str, Any]'",
            "_telegram_command_snapshot": "(self) -> 'TelegramCommandSnapshot'",
            "_telegram_status_snapshot": "(self) -> 'telegram_commands.TelegramStatusSnapshot'",
            "_submit_api_move_request": "(self, move_request: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_submit_api_status_request": "(self) -> 'dict[str, Any]'",
            "_submit_api_command_request": "(self, command_request: 'dict[str, Any]') -> 'dict[str, Any] | DeferredApiResponse'",
            "_submit_api_command_request_from_api_thread": "(self, command_request: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_submit_api_command_request_on_gui_thread": "(self, command_request: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_submit_probe_route_window_guard_on_gui_thread": "(self, action: 'str', payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_command_action_payload": "(command_request: 'dict[str, Any]') -> 'tuple[str, dict[str, Any]]'",
            "_api_command_dispatch_handlers": "(self) -> 'ApiCommandDispatchHandlers'",
            "_dispatch_api_command_request": "(self, command_request: 'dict[str, Any]', *, apply_route_control_guard: 'bool') -> 'dict[str, Any]'",
            "_probe_route_api_window_guard": "(self, action: 'str', payload: 'dict[str, Any]') -> 'dict[str, Any] | None'",
            "_probe_route_api_requires_window": "(self, action: 'str', payload: 'dict[str, Any]') -> 'bool'",
            "_route_control_window_is_open": "(self) -> 'bool'",
            "_handle_api_request": "(self, request: 'dict[str, Any]') -> 'dict[str, Any] | DeferredApiResponse'",
        },
    ),
    "api_stage_contact": (
        "_MainApiStageContactMixin",
        {
            "_api_move_to_coordinates": "(self, targets: 'object', *, mode: 'object' = 'G90', feedrate: 'object' = None) -> 'dict[str, Any]'",
            "_api_stage_status": "(self) -> 'dict[str, Any]'",
            "_surface_map_stage_status": "(self) -> 'dict[str, Any]'",
            "_stage_status_payload": "(self, latest_position: 'object', *, display_position: 'dict[str, float]', accepted: 'bool') -> 'dict[str, Any]'",
            "_surface_map_move_to_xy": "(self, x_mm: 'float', y_mm: 'float') -> 'dict[str, Any]'",
            "_api_list_contacts": "(self) -> 'dict[str, Any]'",
            "_api_move_to_contact": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_contact_needles": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_check_contact": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_focus_settings_from_payload": "(self, payload: 'dict[str, Any]') -> 'tuple[float, float | None]'",
            "_api_focus_needles_rejection": "(self, message: 'str', *, contact: 'Any | None' = None) -> 'dict[str, Any] | None'",
            "_api_contact_context_from_payload": "(self, payload: 'dict[str, Any]') -> 'tuple[int | None, Any | None, dict[str, Any] | None]'",
            "_api_stage_local_focus": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_route_contact_focus": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_route_contact_photo": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_contact_seek": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_route_control_state_snapshot": "(self) -> 'ApiRouteControlState'",
            "_set_api_route_control_state": "(self, state: 'ApiRouteControlState') -> 'None'",
            "_api_route_control_status": "(self) -> 'dict[str, Any]'",
            "_api_route_control_operation_adapters": "(self) -> 'ApiRouteControlOperationAdapters'",
            "_api_route_control_action": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_clear_waiting_route_runner_before_api_control": "(self) -> 'dict[str, Any] | None'",
            "_request_api_route_control_pause": "(self, message: 'str') -> 'dict[str, Any]'",
            "_ack_api_route_control_pause": "(self, message: 'str') -> 'dict[str, Any]'",
            "_interrupt_api_route_controlled_operation": "(self, reason: 'str', *, planned_state: 'ApiRouteControlState | None' = None, planned_message: 'str' = '') -> 'dict[str, Any]'",
            "_update_api_route_control_ui": "(self, message: 'str') -> 'None'",
            "_route_runtime_presenter": "(self) -> 'RouteRuntimePresentationSink'",
        },
    ),
    "api_meter_visa": (
        "_MainApiMeterVisaMixin",
        {
            "_api_measure_current_contact": "(self, payload: 'dict[str, Any]', *, seek: 'bool') -> 'dict[str, Any]'",
            "_api_ensure_measurement_instrument_connected": "(self) -> 'dict[str, Any] | None'",
            "_api_prepare_route_meter_controller": "(self, configuration: 'RouteMeterConfiguration', *, prefix: 'str' = 'Measurement instrument setup failed') -> 'dict[str, Any] | None'",
            "_api_instrument_exception_response": "(prefix: 'str', exc: 'Exception', **extra: 'Any') -> 'dict[str, Any]'",
            "_api_configure_meter": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_raw_voltage_sweep": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_visa_list_resources": "(self) -> 'dict[str, Any]'",
            "_api_visa_operation": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_visa_controller": "(self) -> 'object'",
            "_api_visa_meter_type": "(resources: 'list[dict[str, object]]') -> 'str'",
            "_api_optional_timeout_ms": "(payload: 'dict[str, Any]') -> 'int | None'",
        },
    ),
    "api_route_scan": (
        "_MainApiRouteScanMixin",
        {
            "_api_route_session_thread_preflight": "(self, payload: 'dict[str, Any]') -> 'tuple[dict[str, Any] | None, tuple[float, float]]'",
            "_build_api_route_session_runner": "(self, *, session_id: 'str', points: 'list[RouteMeasurementPoint]', selected_point: 'RouteMeasurementPoint', start_settings: 'RouteExternalSessionStartSettings', meter_configuration: 'RouteMeterConfiguration', needle_feedrate: 'float | None', design_frame_snapshot: 'object | None' = None) -> 'RouteExternalMeasurementSessionRunner'",
            "_cleanup_failed_api_route_session_start": "(self, runner: 'RouteExternalMeasurementSessionRunner') -> 'dict[str, Any]'",
            "_api_start_route_session": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_show_route_measurement_dialog_for_api_session": "(self) -> 'bool'",
            "_api_route_session_status": "(self) -> 'dict[str, Any]'",
            "_api_route_session_action": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_route_session_result": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_route_session_seek": "(self) -> 'dict[str, Any]'",
            "_api_route_session_artifact": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_lens_distortion_calibration": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_click_to_move_calibration": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_api_microscope_area_scan": "(self, payload: 'dict[str, Any]') -> 'dict[str, Any]'",
            "_microscope_area_scan_pattern": "(payload: 'dict[str, Any]') -> 'str'",
            "_microscope_area_scan_count": "(value: 'object', label: 'str') -> 'int'",
            "_microscope_area_scan_float": "(value: 'object', label: 'str', *, minimum: 'float', maximum: 'float') -> 'float'",
            "_microscope_area_scan_default_output_dir": "() -> 'str'",
            "_api_route_artifacts_payload": "(self) -> 'list[dict[str, object]]'",
            "_api_route_artifacts_store": "(self) -> 'ApiRouteArtifactsStore'",
            "_capture_api_route_photo_artifact": "(self, point: 'RouteMeasurementPoint', position: 'int', total: 'int', focus_result: 'object | None') -> 'str'",
            "_api_route_photo_autofocus": "(self, _point: 'RouteMeasurementPoint', position: 'int', total: 'int', *, range_mm: 'float') -> 'object'",
            "_api_contact_context": "(self, contact_number: 'int') -> 'dict[str, Any]'",
            "_api_route_adjusted_stage_xy": "(self, point: 'RouteMeasurementPoint') -> 'tuple[float, float]'",
            "_api_route_meter_configuration": "(self, payload: 'object', *, voltages_v: 'list[float] | None') -> 'RouteMeterConfiguration'",
            "_api_needle_feedrate": "(self, payload: 'dict[str, Any]') -> 'float | None'",
            "_api_structure_number_for_measurement_point": "(point: 'RouteMeasurementPoint') -> 'int'",
            "_api_structure_number_for_route_point": "(route_index: 'int', route_point: 'object') -> 'int'",
            "_api_timestamp_utc": "() -> 'str'",
            "_api_json_ready": "(value: 'object') -> 'Any'",
        },
    ),
    "camera_pipeline": (
        "_MainCameraPipelineMixin",
        {
            "_preload_design_layout_window": "(self) -> 'None'",
            "_preload_lazy_dialog_modules": "(self) -> 'None'",
            "_on_design_layout_module_ready": "(self, design_layout_window_class: 'object', error: 'object') -> 'None'",
            "_stage_serial_ready": "(self) -> 'bool'",
            "on_error": "(self, message: 'str') -> 'None'",
            "_on_camera_frame": "(self, qimg: 'QImage') -> 'None'",
            "_on_live_camera_frame_processed": "(self, result: 'LiveCameraCorrectionResult') -> 'None'",
            "_on_live_camera_frame_processing_error": "(self, message: 'str') -> 'None'",
            "_on_camera_frame_gap_suppressed": "(self) -> 'None'",
            "_correct_camera_frame_for_active_objective": "(self, qimg: 'QImage') -> 'QImage'",
            "_distortion_correction_for_objective": "(self, objective: 'object', payload: 'object') -> 'DistortionCorrection'",
            "_distortion_payload_signature": "(payload: 'object') -> 'str'",
            "_clear_distortion_correction_cache": "(self) -> 'None'",
            "_latest_camera_counter": "(self) -> 'int'",
            "_latest_raw_camera_counter": "(self) -> 'int'",
            "_wait_for_camera_frame": "(self, *, after_counter: 'int | None' = None, timeout_s: 'float' = 2.0) -> 'tuple[QImage | None, int]'",
            "_wait_for_raw_camera_frame": "(self, *, after_counter: 'int | None' = None, timeout_s: 'float' = 2.0) -> 'tuple[QImage | None, int]'",
            "_active_microscope_scale": "(self)",
            "_active_objective_metadata": "(self) -> 'tuple[str, float | None]'",
            "_capture_microscope_scan_launch_snapshot": "(self, *, scale: 'object', document: 'object | None', frame_usability_snapshot: 'DesignCoordinateLease | None' = None, registration: 'object | None' = None) -> '_MicroscopeScanLaunchSnapshot'",
            "_stage_position_for_image_metadata": "(self, *, stage_xy: 'tuple[float, float] | None' = None) -> 'tuple[float, ...] | None'",
            "_show_status": "(self, message: 'str', timeout_ms: 'int' = 0) -> 'None'",
        },
    ),
    "status_coordinate_ui": (
        "_MainStatusCoordinateUiMixin",
        {
            "_show_route_runtime_status": "(self, message: 'str', timeout_ms: 'int' = 0) -> 'None'",
            "_show_route_dialog_status": "(self, message: 'str', timeout_ms: 'int' = 0) -> 'None'",
            "_create_objective_widget": "(self) -> 'QWidget'",
            "_objective_names": "(self) -> 'list[str]'",
            "_active_objective_xy_offset": "(self) -> 'tuple[float, float]'",
            "_camera_stage_xy_from_raw_stage_xy": "(self, raw_stage_xy: 'tuple[float, float]') -> 'tuple[float, float]'",
            "_raw_stage_xy_from_camera_stage_xy": "(self, camera_stage_xy: 'tuple[float, float]') -> 'tuple[float, float]'",
            "_rotation_geometry_snapshot": "(self) -> 'RotationGeometrySnapshot'",
            "_design_navigation_xy_from_physical_machine_xy": "(self, machine_xy: 'tuple[float, float]') -> 'tuple[float, float]'",
            "_design_xy_from_raw_stage_xy": "(self, raw_stage_xy: 'tuple[float, float]') -> 'tuple[float, float] | None'",
            "_raw_stage_xy_from_design_xy": "(self, design_xy: 'tuple[float, float]') -> 'tuple[float, float] | None'",
            "_project_gui_coordinate_motion": "(self, axis_values: 'tuple[tuple[str, float], ...]', *, mode: 'str', lease: 'CoordinateMotionLease | None' = None, allow_pose_rebase: 'bool' = False) -> 'CoordinateMotionProjection | None'",
            "_project_gui_relative_motion": "(self, requested_distances: 'tuple[tuple[str, float], ...]', lease: 'object | None') -> 'CoordinateMotionProjection'",
            "_on_stage_axis_escape_pressed": "(self, axis_name: 'str') -> 'None'",
            "_on_stage_coordinate_mode_changed": "(self) -> 'None'",
            "_on_software_coordinate_system_changed": "(self, frame_id: 'str') -> 'None'",
            "_refresh_software_coordinate_display": "(self) -> 'None'",
            "_surface_map_capture_running": "(self) -> 'bool'",
            "_microscope_scan_running": "(self) -> 'bool'",
            "_controller_reports_active_motion": "(self) -> 'bool'",
            "_controller_latest_state_blocks_motion": "(self) -> 'bool'",
            "_controller_latest_state_age_s": "(self) -> 'float | None'",
            "_controller_latest_state_is_stale": "(self) -> 'bool'",
            "_schedule_cancel_state_refresh": "(self) -> 'None'",
            "_update_stage_coordinate_apply_state": "(self, _pending: 'bool | None' = None) -> 'None'",
            "_append_status_log": "(self, message: 'str') -> 'None'",
            "_open_status_log": "(self) -> 'None'",
            "_prime_keyboard_focus": "(self) -> 'None'",
        },
    ),
    "settings_apply": (
        "_MainSettingsApplyMixin",
        {
            "_on_design_layout_point_selected": "(self, slot: 'int', x_value: 'float', y_value: 'float') -> 'None'",
            "_start_fresh_design_frame_for_source_replacement": "(self) -> 'bool'",
            "_on_alignment_draft_accepted": "(self, points: 'object') -> 'None'",
            "_on_alignment_draft_discarded": "(self) -> 'None'",
            "_apply_settings": "(self, *, apply_objective_runtime: 'bool' = True) -> 'None'",
            "_apply_settings_from_dialog": "(self, new_settings: 'object') -> 'None'",
            "_design_metadata_with_calibration_fingerprints": "(self, metadata: 'DesignFrameMetadata | None') -> 'DesignFrameMetadata | None'",
            "_latest_camera_frame_photo": "(self) -> 'tuple[bytes, str] | None'",
            "_qimage_telegram_photo": "(frame: 'QImage | None') -> 'tuple[bytes, str] | None'",
            "_route_attention_status": "(message: 'str') -> 'bool'",
            "_sync_objective_combo": "(self, objective_name: 'str') -> 'None'",
            "_on_objective_combo_changed": "(self, _index: 'int') -> 'None'",
            "_set_active_objective": "(self, objective_name: 'str', *, apply_motion: 'bool', allow_stage_task: 'bool' = False) -> 'None'",
            "_objective_mutation_busy": "(self, *, allow_stage_task: 'bool' = False, optical_context: 'OpticalCalibrationOutcome | None' = None) -> 'bool'",
            "_objective_profile_mutation_busy": "(self, objective_name: 'str', *, allow_stage_task: 'bool' = False, optical_context: 'OpticalCalibrationOutcome | None' = None) -> 'bool'",
            "_apply_objective_change_offset": "(self, old_name: 'str', new_name: 'str') -> 'None'",
            "_apply_objective_settings": "(self) -> 'None'",
            "_show_plan_status": "(self, plan) -> 'None'",
            "_persist_objective_plan": "(self, plan, *, show_status: 'bool' = True, apply_objective_runtime: 'bool' = True) -> 'bool'",
        },
    ),
    "objective_tools": (
        "_MainObjectiveToolsMixin",
        {
            "_show_click_calibration_dialog": "(self) -> 'None'",
            "_show_optical_calibration_wizard": "(self, mode: 'OpticalCalibrationMode | None' = None) -> 'None'",
            "_show_lens_distortion_dialog": "(self) -> 'None'",
            "_add_objective_profile": "(self) -> 'None'",
            "_delete_objective_profile": "(self, objective_name: 'str') -> 'None'",
            "_set_objective_offset_reference": "(self) -> 'None'",
            "_save_active_objective_offset": "(self) -> 'None'",
            "_reset_active_objective_offset": "(self) -> 'None'",
            "_refresh_click_calibration_ui": "(self) -> 'None'",
            "_refresh_lens_distortion_ui": "(self) -> 'None'",
            "_refresh_objective_calibration_ui": "(self) -> 'None'",
        },
    ),
    "optical_calibration": (
        "_MainOpticalCalibrationMixin",
        {
            "_start_flat_field_calibration_from_wizard": "(self) -> 'None'",
            "_cancel_optical_calibration_wizard": "(self, _run_id: 'object' = None) -> 'None'",
            "_stop_lens_distortion_dialog": "(self) -> 'None'",
            "_start_lens_distortion_calibration_from_wizard": "(self) -> 'None'",
            "_optical_calibration_objective_matches_wizard": "(self, wizard: 'OpticalCalibrationWizard') -> 'bool'",
            "_start_flat_field_calibration": "(self, *, wizard_run_id: 'int | None' = None, full_wizard: 'bool' = False) -> 'bool'",
            "_on_flat_field_calibration_progress": "(self, event: 'object', message: 'str') -> 'None'",
            "_on_flat_field_calibration_finished": "(self, outcome: 'object', success: 'bool', message: 'str', _payload: 'object') -> 'None'",
            "_start_lens_distortion_calibration": "(self, *, wizard_run_id: 'int | None' = None, parent_session_token: 'str | None' = None, full_wizard: 'bool' = False) -> 'dict[str, object]'",
            "_optical_calibration_preflight": "(self, kind: 'str') -> 'str'",
            "_lens_fit_limits": "(self) -> 'LensFitLimits'",
            "_reset_lens_distortion_calibration": "(self) -> 'tuple[bool, str]'",
            "_on_lens_distortion_calibration_progress": "(self, event: 'object', message: 'str') -> 'None'",
            "_on_lens_distortion_calibration_finished": "(self, outcome: 'object', success: 'bool', message: 'str', artifact: 'object') -> 'None'",
            "_save_active_objective_distortion": "(self, payload: 'object | None', *, optical_context: 'OpticalCalibrationOutcome | None' = None, allow_stage_task: 'bool' = False) -> 'None'",
            "_save_objective_distortion": "(self, payload: 'object | None', objective_name: 'str', *, optical_context: 'OpticalCalibrationOutcome | None' = None, allow_stage_task: 'bool' = False) -> 'None'",
            "_calibrated_pixels_to_mm_from_distortion_payload": "(payload: 'dict[str, object]') -> 'list[list[float]]'",
            "_lens_distortion_payload_invalidates_click_calibration": "(payload: 'object') -> 'bool'",
            "_append_click_recalibration_message": "(message: 'str') -> 'str'",
            "_on_objective_calibration_updated": "(self, objective_name: 'str', pixels_to_mm: 'object', task_token: 'object') -> 'None'",
            "_reject_objective_calibration_candidate": "(self, task_token: 'object') -> 'None'",
            "_calibration_callback_token_is_current": "(self, task_token: 'object', *, expected_sources: 'set[str] | frozenset[str] | None' = None) -> 'bool'",
            "_objective_pixels_to_mm_for_calibration_update": "(self, objective_name: 'str', pixels_to_mm: 'object') -> 'object'",
            "_on_objective_mismatch_detected": "(self, suggested_name: 'str', message: 'str', task_token: 'object') -> 'None'",
        },
    ),
    "alignment": (
        "_MainAlignmentMixin",
        {
            "_design_backed_alignment_active": "(self) -> 'bool'",
            "_alignment_capture_slot_count": "(self) -> 'int'",
            "_design_window_is_open": "(self) -> 'bool'",
            "_collapse_alignment_panel_if_ready": "(self) -> 'None'",
            "_collapse_alignment_panel_if_design_open": "(self) -> 'None'",
            "_set_alignment_panel_expanded": "(self) -> 'None'",
            "_arm_manual_alignment_pick": "(self, slot: 'int') -> 'None'",
            "_cancel_manual_alignment_pick": "(self) -> 'None'",
            "_reset_manual_alignment": "(self, *, cancel_pick: 'bool' = True) -> 'None'",
            "_reset_alignment_capture_points": "(self) -> 'None'",
            "_capture_manual_alignment_center_shortcut": "(self) -> 'None'",
            "_resolve_alignment_capture_stage_position": "(self) -> 'tuple[float, float] | None'",
            "_capture_manual_alignment_center": "(self, slot: 'int') -> 'None'",
            "_request_alignment_capture": "(self, slot: 'int', mode: 'str') -> 'None'",
            "_zero_b_axis": "(self) -> 'None'",
            "_reset_click_calibration": "(self) -> 'tuple[bool, str]'",
            "_capture_manual_alignment_clicked": "(self, dx_pixels: 'float' = 0.0, dy_pixels: 'float' = 0.0) -> 'None'",
            "_on_manual_alignment_point_resolved": "(self, request_id: 'object', success: 'bool', center_xy: 'object', captured_xy: 'object', message: 'str') -> 'None'",
            "_capture_manual_alignment_point": "(self, slot: 'int', captured: 'tuple[float, float]', *, source: 'str') -> 'None'",
            "_request_operator_alignment_machine_capture": "(self, slot: 'int', *, configured_target_xy: 'tuple[float, float] | None', source: 'str') -> 'None'",
            "_apply_alignment_capture_plan": "(self, plan) -> 'None'",
            "_on_alignment_b_rotation_started": "(self) -> 'None'",
            "_finish_alignment_draft": "(self) -> 'None'",
            "_refresh_manual_alignment_ui": "(self) -> 'None'",
            "_update_coordinate_display": "(self, *, center_xy: 'tuple[float, float] | None' = None, cursor_xy: 'tuple[float, float] | None' = None) -> 'None'",
            "_can_display_design_position": "(self) -> 'bool'",
            "_format_coordinate_label": "(self, prefix: 'str', fluidnc_xy: 'tuple[float, float] | None') -> 'str'",
            "_format_active_coordinate_label": "(self, prefix: 'str', fluidnc_xy: 'tuple[float, float] | None') -> 'str'",
            "_resolve_coordinate_systems": "(self, fluidnc_xy: 'tuple[float, float]') -> 'dict[str, tuple[float, float]]'",
            "_resolve_chip_coordinates": "(self, fluidnc_xy: 'tuple[float, float]') -> 'tuple[float, float] | None'",
            "_resolve_design_coordinates": "(self, fluidnc_xy: 'tuple[float, float]') -> 'tuple[float, float] | None'",
        },
    ),
    "manual_jog": (
        "_MainManualJogMixin",
        {
            "show_joystick_window": "(self) -> 'None'",
            "show_serial_terminal_window": "(self) -> 'None'",
            "_on_manual_motion_axis": "(self, axis: 'str') -> 'None'",
            "_save_manual_axis_jog_settings": "(self, axis: 'str', distance_mm: 'float', mode: 'str', feedrate_mm_min: 'float') -> 'None'",
            "_save_jog_control_mode": "(self, mode: 'str') -> 'None'",
            "_save_jog_feedrate_setting": "(self, key: 'str', feedrate_mm_min: 'float') -> 'None'",
            "_on_step_feedrate_changed": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_on_focus_feedrate_changed": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_on_focus_step_feedrate_changed": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_on_turntable_feedrate_changed": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_on_turntable_step_feedrate_changed": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_schedule_linear_feedrate_save": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_on_linear_feedrate_changed": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_on_needle_feedrate_changed": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_on_needle_step_feedrate_changed": "(self, feedrate_mm_min: 'float') -> 'None'",
            "_save_pending_linear_feedrate_default": "(self) -> 'None'",
            "_current_linear_feedrate": "(self) -> 'float'",
            "_coordinate_feedrate_for_axes": "(self, axes: 'object') -> 'float'",
            "_current_needle_feedrate": "(self) -> 'float'",
        },
    ),
    "motion_prediction": (
        "_MainMotionPredictionMixin",
        {
            "_schedule_status_refreshes": "(self, delays_ms: 'tuple[int, ...]') -> 'None'",
            "_on_manual_terminal_command": "(self, command: 'str') -> 'None'",
            "_on_stage_task_started": "(self) -> 'None'",
            "on_autofocus_finished": "(self, success: 'bool', message: 'str') -> 'None'",
            "on_calibration_changed": "(self, mm_per_pixel_x: 'float', mm_per_pixel_y: 'float') -> 'None'",
            "_on_measure_action_toggled": "(self, checked: 'bool') -> 'None'",
            "_on_measure_mode_exited": "(self) -> 'None'",
        },
    ),
    "design_load": (
        "_MainDesignLoadMixin",
        {
            "_load_design_document": "(self, design_path: 'str') -> 'None'",
            "_start_design_document_load": "(self, design_path: 'str', *, restore_state: 'dict[str, object] | None', show_window: 'bool') -> 'None'",
            "_on_design_document_loaded": "(self, generation: 'int', document: 'object', error: 'object') -> 'None'",
            "_snapshot_design_session": "(self) -> 'DesignSession'",
            "_apply_design_load_success_plan": "(self, plan: 'design_navigation.DesignLoadResultPlan', show_window: 'bool') -> 'None'",
            "_apply_design_load_failure_plan": "(self, plan: 'design_navigation.DesignLoadResultPlan', show_window: 'bool') -> 'None'",
            "_ensure_design_markup_store": "(self) -> 'MarkupStoreWorker'",
            "_next_design_markup_request_id": "(self) -> 'int'",
            "_begin_design_markup_load": "(self, document: 'DesignDocument', *, generation: 'int', candidate_session: 'DesignSession', plan: 'design_navigation.DesignLoadResultPlan', show_window: 'bool', previous_markup: 'MarkupDocument | None', frame_metadata: 'DesignFrameMetadata | None' = None) -> 'None'",
            "_commit_pending_design_load": "(self, context: '_PendingDesignMarkupLoad', markup: 'MarkupDocument') -> 'None'",
            "_set_design_load_pending_ui": "(self, pending: 'bool') -> 'None'",
        },
    ),
    "design_markup": (
        "_MainDesignMarkupMixin",
        {
            "_finish_design_markup_load_ui": "(self, document: 'DesignDocument | None', *, show_window: 'bool') -> 'None'",
            "_invalidate_pending_design_markup_load": "(self) -> 'None'",
            "_on_design_markup_loaded": "(self, result: 'object') -> 'None'",
            "_prompt_changed_markup_choice": "(self, source_path: 'Path') -> 'MarkupLoadChoice'",
            "_on_design_markup_store_failed": "(self, failure: 'object') -> 'None'",
            "_refresh_design_markup_ui": "(self) -> 'None'",
            "_prune_design_guide_undo_stack": "(self) -> 'list[str]'",
            "_publish_design_markup": "(self) -> 'None'",
            "_delete_persisted_design_markup": "(self, source_path: 'str | Path') -> 'None'",
            "_stop_design_markup_store": "(self) -> 'None'",
            "_stop_coordinate_frame_store": "(self) -> 'None'",
            "_stop_software_coordinate_selection_store": "(self) -> 'None'",
            "_on_software_coordinate_selection_store_failed": "(self, failure: 'object') -> 'None'",
            "_on_coordinate_frame_document_loaded": "(self, result: 'object') -> 'None'",
            "_on_coordinate_frame_document_saved": "(self, result: 'object') -> 'None'",
            "_on_coordinate_frame_store_failed": "(self, failure: 'object') -> 'None'",
            "_show_navigation_status": "(self, plan: 'object') -> 'None'",
            "_apply_route_edit_plan": "(self, plan: 'route_editing.RouteEditPlan', *, empty_selection: 'bool' = False, update_selection: 'bool' = True) -> 'bool'",
            "_unload_design_document": "(self) -> 'None'",
            "_set_design_top_cell": "(self, top_cell_name: 'str') -> 'None'",
            "_set_design_layer_visibility": "(self, layer: 'int', datatype: 'int', visible: 'bool') -> 'None'",
            "_rotate_design_document": "(self, quarter_turn_delta: 'int') -> 'None'",
        },
    ),
    "design_edit_dialog": (
        "_MainDesignEditDialogMixin",
        {
            "_create_measurement_route": "(self) -> 'None'",
            "_load_measurement_route": "(self, route_path: 'str') -> 'None'",
            "_save_measurement_route": "(self) -> 'None'",
            "_save_measurement_route_as": "(self, route_path: 'str') -> 'None'",
            "_add_design_route_point": "(self, x_value: 'float', y_value: 'float') -> 'None'",
            "_design_edit_safe": "(self) -> 'bool'",
            "_design_mutation_ready": "(self) -> 'bool'",
            "_markup_mutation_ready": "(self) -> 'bool'",
            "_add_design_guide": "(self, start: 'object', end: 'object') -> 'None'",
            "_set_design_markup_visibility": "(self, visible: 'bool') -> 'None'",
            "_undo_last_design_guide": "(self) -> 'None'",
            "_clear_design_guides": "(self) -> 'None'",
            "_delete_design_selection": "(self) -> 'None'",
            "_apply_mixed_design_array": "(self, request: 'object') -> 'None'",
            "_commit_mixed_design_edit": "(self, plan: 'object', *, selection_after: 'SelectionModel') -> 'None'",
            "_add_current_design_route_point": "(self) -> 'None'",
            "_add_route_array_points": "(self, origin_x: 'float', origin_y: 'float', step_x_dx: 'float', step_x_dy: 'float', count_x: 'int', step_y_dx: 'float', step_y_dy: 'float', count_y: 'int', serpentine: 'bool', replace_existing: 'bool', selected_indices: 'object' = None) -> 'None'",
            "_remove_selected_route_point": "(self) -> 'None'",
            "_clear_measurement_route_points": "(self) -> 'None'",
            "_open_route_measurement_dialog": "(self, *, start_context: 'bool' = True) -> 'None'",
            "_restore_route_measurement_state_after_design_load": "(self) -> 'None'",
            "_route_measurement_settings_store": "(self) -> 'RouteMeasurementSettingsStore'",
            "_clear_route_measurement_dialog": "(self) -> 'None'",
        },
    ),
    "route_launch_setup": (
        "_MainRouteLaunchSetupMixin",
        """_start_route_measurement_session _cancel_route_measurement_session
        _start_route_measurement _prepare_route_measurement_launch
        _apply_gui_route_start_preflight _route_measurement_photo_preflight
        _build_route_measurement_runner _start_route_measurement_runner
        _route_measurement_start_plan _snapshot_active_route_design_frame
        _design_contact_success_callback _request_design_contact_arm
        _on_design_contact_arm_requested _arm_design_contact_on_gui
        _on_design_contact_a_read_finished""".split(),
    ),
    "route_capture_run": (
        "_MainRouteCaptureRunMixin",
        """_route_measurement_points _capture_route_photo _route_photo_autofocus
        _route_optical_session_token _record_route_photo
        _capture_route_pre_contact_photo _capture_route_contact_photo
        _record_route_contact_height _current_route_name _run_route_measurement
        _on_route_measurement_started _request_stop_route_measurement
        _request_route_measurement_point_correction
        _submit_route_measurement_confirmation
        _apply_route_measurement_confirmation_runtime
        _submit_route_measurement_jump _request_route_contact_move
        _run_route_contact_move _on_route_contact_move_finished""".split(),
    ),
    "route_control": (
        "_MainRouteControlMixin",
        """_request_pause_route_measurement _save_route_measurement_shift
        _route_shift_save_plan _route_shift_adjustment_point
        _route_shift_current_stage_xy _update_api_route_offset_from_runner
        _apply_route_shift_save_status _interrupt_route_measurement_runner
        _on_route_measurement_status _on_route_measurement_progress
        _on_route_measurement_current_point_changed
        _on_route_measurement_waiting_changed""".split(),
    ),
    "route_results": (
        "_MainRouteResultsMixin",
        """_on_route_measurement_result _route_record_needs_contact_attention
        _send_route_waiting_attention_from_last_result _send_route_attention_alert
        _on_route_measurement_recorded _format_route_measurement_record
        _format_route_contact_diagnostics _on_route_measurement_finished
        _store_final_api_route_session_status
        _clear_finished_route_measurement_state _route_measurement_csv_record_count
        _route_measurement_next_point_number _set_route_measurement_resume_point
        _select_route_point_for_measurement _save_route_measurement_current_point
        _set_route_measurement_pending _save_route_measurement_pending
        _save_route_measurement_session_metadata _select_route_point
        _set_route_needle_offsets _set_route_edit_enabled""".split(),
    ),
    "registration_focus": (
        "_MainRegistrationFocusMixin",
        """_add_design_source_mark _add_design_check_mark
        _capture_stage_source_mark _capture_stage_check_mark
        _capture_stage_registration_mark
        _on_registration_machine_coordinate_snapshot_finished
        _design_spacing_ratio_is_reasonable _clear_design_registration
        _design_registration_instances _reconcile_missing_design_registration_instance
        _select_design_registration_instance _new_design_registration_instance
        _invalidate_design_registration _on_design_target_selected
        _select_next_design_target _select_previous_design_target
        _move_to_design_target _move_to_minimap_design_point
        _open_design_window_from_minimap_point _move_to_design_coordinate
        _find_design_focus_reference _ensure_focus_structure_bounds_worker
        _on_focus_structure_bounds_ready _on_focus_structure_bounds_failed
        _design_focus_optical_context_key _clear_design_focus_overlay_state
        _use_selected_design_focus_reference _observe_design_focus_context
        _on_registration_focus_move_finished _on_registration_focus_move_signal
        _on_registration_focus_autofocus_finished _registration_optical_observation
        _reset_design_focus_reference _connect_design_focus_signals
        _refresh_design_panel""".split(),
    ),
    "stage_design_position": (
        "_MainStageDesignPositionMixin",
        """_apply_stage_motion_presentation
        _raw_target_from_display_value
        _resolve_stage_axis_target _api_machine_display_position
        _api_machine_coordinate_snapshot _resolve_api_stage_axis_target
        _stage_axis_target_limit_error _machine_axis_target_limit_error
        _refresh_controller_status _refresh_design_position
        _flush_pending_design_position _update_design_position
        _log_design_position_reconcile _format_optional_point
        _resolve_design_fov_size""".split(),
    ),
    "scan_sample_meter": (
        "_MainScanSampleMeterMixin",
        """_clear_microscope_scan_dialog _request_stop_microscope_scan
        _show_microscope_scan_start_rejection _start_microscope_scan
        _run_microscope_scan _on_microscope_scan_status
        _on_microscope_scan_finished _move_to_design_window_point
        _on_needle_height_changed _on_lcr_connection_changed
        _on_lcr_reading_updated _on_lcr_reading_started
        _on_lcr_reading_summary_updated _on_resistance_standby_enabled_changed
        _resume_resistance_standby_polling _cancel_contact_seek
        _contact_seek_measure_quality _display_a_for_needle_lowering
        _set_design_snap_enabled _on_design_snap_enabled_changed
        _sample_handling_active _latest_stage_z _active_sample_objective_name
        _remember_sample_focus_from_latest _sample_load_focus_z
        _sample_focus_cache _sample_workflow_can_start
        _design_registration_is_active _run_sample_unload _run_sample_load
        _on_oscillation_state_changed _save_oscillation_configuration""".split(),
    ),
}

OWNER_SIGNATURE_SHA256 = {
    "route_launch_setup": "5f0e999f41d060f52f710c263a90fbfb23bba5bcf67a53ee7060504cdf47c502",
    "route_capture_run": "881ba99c4c6fe0d55e21b88d0443ad7ee5f969e3e42891eefd487d680f2f6a6d",
    "route_control": "0dca50743dfaa361fcaf76645b7a0da216640bc1f8700081a9e0ad0c5b17aa5b",
    "route_results": "ec4552e53a60da60698f6cb03e28221150572bafaeec4da96301a82ffdbc82aa",
    "registration_focus": "8bf43873a2ca6d69216bc91e8f523336d506bc03cd6d27cad7b2eb26780c85d7",
    "stage_design_position": "53272b1f669e95ab5962959ac8771f41409fd8ad0e585b25b5dcb58c716a7bbe",
    "scan_sample_meter": "4981a0c643979b59f0391a315cc6083810d6418f72f150355a38a29edb619f89",
}

OWNER_SUPPORT_CLASSES = {
    "camera_pipeline": {"_MicroscopeScanLaunchSnapshot"},
    "alignment": {"_ManualAlignmentCaptureContext"},
    "design_load": {"_LoadedDesignDocument", "_PendingDesignMarkupLoad"},
    "route_launch_setup": {"_DesignContactArmDispatch"},
}


def _load_owner(
    module_name: str,
) -> tuple[object, type, dict[str, str] | list[str]]:
    class_name, signatures = OWNER_SPECS[module_name]
    module = importlib.import_module(f"probe_station_gui.application.{module_name}")
    return module, getattr(module, class_name), signatures


def _is_one_line_delegate(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = method.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 1:
        return False
    statement = body[0]
    value = statement.value if isinstance(statement, (ast.Expr, ast.Return)) else None
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == method.name
    )


def test_main_direct_base_order_is_exact() -> None:
    from main import Main

    assert tuple(base.__name__ for base in Main.__bases__) == (
        "_MainBootstrapApiMixin",
        "_MainApiStageContactMixin",
        "_MainApiMeterVisaMixin",
        "_MainApiRouteScanMixin",
        "_MainCameraPipelineMixin",
        "_MainStatusCoordinateUiMixin",
        "_MainSettingsApplyMixin",
        "_MainObjectiveToolsMixin",
        "_MainOpticalCalibrationMixin",
        "_MainAlignmentMixin",
        "_MainManualJogMixin",
        "_MainMotionPredictionMixin",
        "_MainDesignLoadMixin",
        "_MainDesignMarkupMixin",
        "_MainDesignEditDialogMixin",
        "_MainRouteLaunchSetupMixin",
        "_MainRouteCaptureRunMixin",
        "_MainRouteControlMixin",
        "_MainRouteResultsMixin",
        "_MainRegistrationFocusMixin",
        "_MainStageDesignPositionMixin",
        "_MainScanSampleMeterMixin",
        "QMainWindow",
    )
    assert Main.__module__ == "main"


def test_bootstrap_api_owner_is_direct_and_canonical() -> None:
    _assert_owner("bootstrap_api")


def test_api_stage_contact_owner_is_direct_and_canonical() -> None:
    _assert_owner("api_stage_contact")


def test_api_meter_visa_owner_is_direct_and_canonical() -> None:
    _assert_owner("api_meter_visa")


def test_api_route_scan_owner_is_direct_and_canonical() -> None:
    _assert_owner("api_route_scan")


def test_camera_pipeline_owner_is_direct_and_canonical() -> None:
    _assert_owner("camera_pipeline")


def test_status_coordinate_ui_owner_is_direct_and_canonical() -> None:
    _assert_owner("status_coordinate_ui")


def test_settings_apply_owner_is_direct_and_canonical() -> None:
    _assert_owner("settings_apply")


def test_objective_tools_owner_is_direct_and_canonical() -> None:
    _assert_owner("objective_tools")


def test_optical_calibration_owner_is_direct_and_canonical() -> None:
    _assert_owner("optical_calibration")


def test_alignment_owner_is_direct_and_canonical() -> None:
    _assert_owner("alignment")


def test_manual_jog_owner_is_direct_and_canonical() -> None:
    _assert_owner("manual_jog")


def test_motion_prediction_owner_is_direct_and_canonical() -> None:
    _assert_owner("motion_prediction")


def test_design_load_owner_is_direct_and_canonical() -> None:
    _assert_owner("design_load")


def test_design_markup_owner_is_direct_and_canonical() -> None:
    _assert_owner("design_markup")


def test_design_edit_dialog_owner_is_direct_and_canonical() -> None:
    _assert_owner("design_edit_dialog")


def test_route_launch_setup_owner_is_direct_and_canonical() -> None:
    _assert_owner("route_launch_setup")


def test_route_capture_run_owner_is_direct_and_canonical() -> None:
    _assert_owner("route_capture_run")


def test_route_control_owner_is_direct_and_canonical() -> None:
    _assert_owner("route_control")


def test_route_results_owner_is_direct_and_canonical() -> None:
    _assert_owner("route_results")


def test_registration_focus_owner_is_direct_and_canonical() -> None:
    _assert_owner("registration_focus")


def test_stage_design_position_owner_is_direct_and_canonical() -> None:
    _assert_owner("stage_design_position")


def test_scan_sample_meter_owner_is_direct_and_canonical() -> None:
    _assert_owner("scan_sample_meter")


def _assert_owner(module_name: str) -> None:
    from main import Main

    module, owner, signatures = _load_owner(module_name)
    assert owner.__module__ == module.__name__
    assert {
        node.name
        for node in ast.parse(inspect.getsource(module)).body
        if isinstance(node, ast.ClassDef)
    } == {owner.__name__, *OWNER_SUPPORT_CLASSES.get(module_name, set())}
    expected_names = set(signatures)
    assert {
        name
        for name, value in owner.__dict__.items()
        if inspect.isroutine(value) or isinstance(value, (staticmethod, classmethod))
    } == expected_names
    for name in expected_names:
        assert name not in Main.__dict__
        assert inspect.getattr_static(Main, name) is owner.__dict__[name]
    actual_signatures = {
        name: str(inspect.signature(getattr(owner, name))) for name in signatures
    }
    if isinstance(signatures, dict):
        assert actual_signatures == signatures
    else:
        payload = "\n".join(f"{name}:{actual_signatures[name]}" for name in signatures)
        assert (
            hashlib.sha256(payload.encode()).hexdigest()
            == OWNER_SIGNATURE_SHA256[module_name]
        )


def test_application_package_has_no_owner_re_exports() -> None:
    package = importlib.import_module("probe_station_gui.application")

    for class_name, _signatures in OWNER_SPECS.values():
        assert class_name not in package.__dict__


def test_owner_modules_have_no_reverse_imports_or_dynamic_facades() -> None:
    owner_module_names = {
        f"probe_station_gui.application.{name}" for name in OWNER_SPECS
    }
    for module_name in OWNER_SPECS:
        module, owner, _signatures = _load_owner(module_name)
        tree = ast.parse(inspect.getsource(module))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "main" not in imports
        assert "probe_station_gui.main" not in imports
        assert not imports.intersection(owner_module_names)
        assert not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "__getattr__"
            for node in ast.walk(tree)
        )
        owner_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == owner.__name__
        )
        assert not any(
            isinstance(node, (ast.Assign, ast.AnnAssign)) for node in owner_node.body
        )
        assert not any(
            _is_one_line_delegate(node)
            for node in owner_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
