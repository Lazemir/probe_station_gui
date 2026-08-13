"""Ownership contracts for the first four ``Main`` application domains."""

from __future__ import annotations

import ast
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
            "_clear_waiting_route_measurement_state": "(self) -> 'None'",
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
}


def _load_owner(module_name: str) -> tuple[object, type, dict[str, str]]:
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


def _assert_owner(module_name: str) -> None:
    from main import Main

    module, owner, signatures = _load_owner(module_name)
    assert owner.__module__ == module.__name__
    assert {
        node.name
        for node in ast.parse(inspect.getsource(module)).body
        if isinstance(node, ast.ClassDef)
    } == {owner.__name__}
    assert {
        name
        for name, value in owner.__dict__.items()
        if inspect.isroutine(value) or isinstance(value, (staticmethod, classmethod))
    } == set(signatures)
    for name in signatures:
        assert name not in Main.__dict__
        assert inspect.getattr_static(Main, name) is owner.__dict__[name]
    assert {
        name: str(inspect.signature(getattr(owner, name))) for name in signatures
    } == signatures


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
