from __future__ import annotations

from types import SimpleNamespace

import pytest

import main as main_module
from main import Main
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)
from tests.app.lens_distortion_test_support import _FakeSettingsManager


@pytest.mark.parametrize("blocker", ("scan", "calibration"))
def test_api_lens_reset_returns_conflict_during_active_operation(
    blocker: str,
) -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    payload = {"model_version": 1, "camera_matrix": [[20.0]]}
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                distortion_correction=payload,
                distortion_correction_configured=True,
            )
        },
    )
    statuses: list[str] = []
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(is_busy=lambda: False)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(
            active_run_id="starting-lens" if blocker == "calibration" else None,
            parent_session_token=None,
        )
    )
    window._microscope_scan_thread = (
        SimpleNamespace(is_alive=lambda: True) if blocker == "scan" else None
    )
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    response = Main._api_lens_distortion_calibration(window, {"reset": True})

    assert response == {
        "accepted": False,
        "status_code": 409,
        "message": "Lens correction cannot be reset while a scan or calibration is active.",
    }
    assert manager.saved_count == 0
    assert manager.settings.objectives.objectives[
        "X20"
    ].distortion_correction == payload
    assert statuses == [response["message"]]

def test_api_lens_start_returns_flat_field_conflict_instead_of_202() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    statuses: list[str] = []
    window._flat_field_calibration_context = None
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(
            active_run_id="flat-running",
            active_kind="flat",
            parent_session_token=None,
        )
    )
    window._flat_field_calibration_thread = None
    window._lens_distortion_context = None
    window._lens_distortion_thread = None
    window._stage_serial_ready = lambda: True
    window.stage_controller = SimpleNamespace(is_busy=lambda: False)
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    response = Main._api_lens_distortion_calibration(window, {})

    assert response == {
        "accepted": False,
        "status_code": 409,
        "message": "Flat-field calibration is already running.",
    }
    assert statuses == [response["message"]]

def test_api_lens_start_returns_thread_failure_instead_of_202(monkeypatch) -> None:
    class _StartFailureThread:
        def __init__(self, **_kwargs) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def start(self) -> None:
            raise RuntimeError("thread start failed")

    window = Main.__new__(Main)
    window._start_lens_distortion_calibration = lambda: {
        "accepted": False,
        "status_code": 500,
        "message": "Lens distortion calibration could not start: thread start failed",
    }
    statuses: list[str] = []
    window.stage_controller = SimpleNamespace(is_busy=lambda: False)
    window._stage_serial_ready = lambda: True
    window._flat_field_calibration_context = None
    window._flat_field_calibration_thread = None
    window._lens_distortion_context = None
    window._lens_distortion_thread = None
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._coordinate_feedrate_for_axes = lambda _axes: 120.0
    window._current_needle_feedrate = lambda: 70.0
    window._lens_distortion_dialog = None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    monkeypatch.setattr(main_module.threading, "Thread", _StartFailureThread)

    response = Main._api_lens_distortion_calibration(window, {})

    assert response == {
        "accepted": False,
        "status_code": 500,
        "message": "Lens distortion calibration could not start: thread start failed",
    }

def test_api_force_click_reset_returns_conflict_during_scan_startup() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={"X20": ObjectiveCalibrationSettings(name="X20")},
    )
    resets: list[str] = []
    starts: list[tuple[float, float]] = []
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        reset_calibration=lambda reason: resets.append(str(reason)),
    )
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = SimpleNamespace(is_alive=lambda: True)
    window._stage_serial_ready = lambda: True
    window._start_click_to_move = lambda dx, dy: starts.append((dx, dy)) or True

    response = Main._api_click_to_move_calibration(
        window,
        {"dx_px": 1.0, "dy_px": -1.0, "force": True},
    )

    assert response["accepted"] is False
    assert response["status_code"] == 409
    assert resets == []
    assert starts == []

def test_click_to_move_start_is_rejected_during_scan_startup() -> None:
    from probe_station_gui.views.microscope_interaction import (
        ClickMoveBindings,
        ClickMoveConfig,
        MicroscopeInteraction,
    )

    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    requests: list[tuple[float, float]] = []
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        request_move=lambda dx, dy: requests.append((dx, dy)) or True,
    )
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = SimpleNamespace(is_alive=lambda: True)
    window._stage_serial_ready = lambda: True
    interaction = MicroscopeInteraction(
        ClickMoveBindings(
            request_move=window.stage_controller.request_move,
            stage_connected=window._stage_serial_ready,
            motion_blocked=window._objective_mutation_busy,
            mark_motion_axes=lambda _axes: None,
            show_status=lambda _message, _timeout_ms=0: None,
            repaint=lambda: None,
            preview_hover=lambda _dx, _dy: None,
            present_coordinates=lambda **_coordinates: None,
            manual_alignment_active=lambda: False,
            capture_manual_alignment=lambda _dx, _dy: None,
            pending_state_changed=lambda _pending: None,
        ),
        ClickMoveConfig(pending_timeout_s=lambda: 8.0),
    )
    window._microscope_interaction = interaction

    response = Main._api_click_to_move_calibration(
        window,
        {"dx_px": 4.0, "dy_px": -3.0},
    )

    assert response["accepted"] is False
    assert response["status_code"] == 409
    assert requests == []
    assert interaction.pending_move is None
    assert interaction.target_rel is None

def test_gui_click_reset_is_rejected_during_calibration_startup() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={"X20": ObjectiveCalibrationSettings(name="X20")},
    )
    resets: list[str] = []
    statuses: list[str] = []
    refreshes: list[str] = []
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        reset_calibration=lambda reason: resets.append(str(reason)),
    )
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(
            active_run_id="starting-lens",
            parent_session_token=None,
        )
    )
    window._microscope_scan_thread = None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))
    window._refresh_click_calibration_ui = lambda: refreshes.append("refresh")

    Main._reset_click_calibration(window)

    assert resets == []
    assert refreshes == ["refresh"]
    assert statuses == [
        "Click-to-move calibration cannot be reset while a scan or calibration is active."
    ]

def test_reset_lens_distortion_preserves_click_calibration() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    matrix = [[1.0, 0.0], [0.0, 1.0]]
    manager.settings.objectives = ObjectivesSettings(
        active_name="X50",
        objectives={
            "X50": ObjectiveCalibrationSettings(
                name="X50",
                magnification=50.0,
                pixels_to_mm=matrix,
                xy_calibration_configured=True,
                distortion_correction={"model_version": 1},
                distortion_correction_configured=True,
            )
        },
    )
    apply_calls: list[str] = []
    refresh_calls: list[str] = []
    window.settings_manager = manager
    window._apply_objective_settings = lambda: apply_calls.append("apply")
    window._refresh_objective_calibration_ui = lambda: refresh_calls.append("refresh")

    Main._save_active_objective_distortion(window, None)

    profile = manager.settings.objectives.objectives["X50"]
    assert profile.distortion_correction_configured is False
    assert profile.distortion_correction == {}
    assert profile.xy_calibration_configured is True
    assert profile.pixels_to_mm == matrix
    assert manager.saved_count == 1
    assert apply_calls == ["apply"]
    assert refresh_calls == ["refresh"]

def test_api_click_to_move_calibration_force_resets_before_start() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={"X20": ObjectiveCalibrationSettings(name="X20")},
    )
    events: list[tuple[object, ...]] = []
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        reset_calibration=lambda reason: events.append(("reset", reason)),
    )
    window._stage_serial_ready = lambda: True
    window._microscope_interaction = SimpleNamespace(
        try_start_api_move=(
            lambda dx_px, dy_px: events.append(("start", dx_px, dy_px)) or True
        )
    )

    response = Main._api_click_to_move_calibration(
        window,
        {"dx_px": "1.5", "dy_px": "-2.0", "force": True},
    )

    assert response["accepted"] is True
    assert response["status_code"] == 202
    assert events == [
        (
            "reset",
            "Click-to-move calibration cleared. Click in the microscope view "
            "to recalibrate the active objective.",
        ),
        ("start", 1.5, -2.0),
    ]

def test_camera_frame_distortion_correction_reuses_compiled_payload(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    payload = {"model_version": 1, "frame_size": [640, 480]}
    objective = SimpleNamespace(
        name="X50",
        distortion_correction_configured=True,
        distortion_correction=payload,
    )
    window.settings_manager = SimpleNamespace(
        active_objective_configuration=lambda: objective
    )
    compiled: list[object] = []
    applied: list[object] = []

    def compile_payload(raw_payload: object) -> object:
        compiled.append(raw_payload)
        return object()

    def apply_correction(frame: object, correction: object) -> object:
        applied.append(correction)
        return frame

    monkeypatch.setattr(main_module, "correction_from_payload", compile_payload)
    monkeypatch.setattr(main_module, "apply_distortion_correction", apply_correction)

    frame = object()
    assert Main._correct_camera_frame_for_active_objective(window, frame) is frame
    assert Main._correct_camera_frame_for_active_objective(window, frame) is frame

    assert compiled == [payload]
    assert len(applied) == 2
    assert applied[0] is applied[1]
