from __future__ import annotations

from types import SimpleNamespace

import pytest

from main import Main
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)
from probe_station_gui.stage.types import StageTaskToken
from tests.app.lens_distortion_test_support import (
    _CalibrationTokenStage,
    _FakeSettingsManager,
)


def test_click_calibration_update_keeps_stage_calibrated_distortion_matrix() -> None:
    window = Main.__new__(Main)
    window._api_stage_command_runtime = SimpleNamespace(active=lambda: False)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    calibrated = [[-0.000117, 0.0], [0.0, -0.000117]]
    manager.settings.objectives = ObjectivesSettings(
        active_name="X50",
        objectives={
            "X50": ObjectiveCalibrationSettings(
                name="X50",
                magnification=50.0,
                pixels_to_mm=calibrated,
                xy_calibration_configured=True,
                distortion_correction={
                    "model_version": 1,
                    "frame_size": [640, 480],
                    "calibrated_pixels_to_mm": calibrated,
                },
                distortion_correction_configured=True,
            )
        },
    )
    persisted = []
    task_token = StageTaskToken(1, "_run_move")
    window.settings_manager = manager
    window.stage_controller = _CalibrationTokenStage(task_token, busy=False)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = None
    window._persist_objective_plan = lambda plan, **_kwargs: persisted.append(plan) or True
    window._show_status = lambda *_args: None

    Main._on_objective_calibration_updated(
        window,
        "X50",
        [[-0.000119, 0.0], [0.0, -0.000112]],
        task_token,
    )

    profile = persisted[0].settings.objectives.objectives["X50"]
    assert profile.pixels_to_mm == calibrated
    assert window.stage_controller.accepted_candidates == [(task_token, calibrated)]
    assert window.stage_controller.published_candidates == [task_token]

@pytest.mark.parametrize("blocker", ("scan", "calibration"))
def test_click_calibration_active_update_is_rejected_after_new_operation_starts(
    blocker: str,
) -> None:
    window = Main.__new__(Main)
    window._api_stage_command_runtime = SimpleNamespace(active=lambda: False)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    newer_matrix = [[0.03, 0.0], [0.0, 0.03]]
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                magnification=20.0,
                pixels_to_mm=newer_matrix,
                xy_calibration_configured=True,
            )
        },
    )
    persisted: list[object] = []
    restored: list[list[list[float]]] = []
    statuses: list[str] = []
    window.settings_manager = manager
    task_token = StageTaskToken(1, "_run_move")
    window.stage_controller = _CalibrationTokenStage(task_token)
    window.stage_controller.apply_objective_configuration = (
        lambda objective, _objectives: restored.append(objective.pixels_to_mm)
    )
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(
            active_run_id="new-lens" if blocker == "calibration" else None,
            parent_session_token=None,
        )
    )
    window._microscope_scan_thread = (
        SimpleNamespace(is_alive=lambda: True) if blocker == "scan" else None
    )
    window._persist_objective_plan = lambda plan: persisted.append(plan)
    window._sync_objective_combo = lambda _name: None
    window._refresh_objective_calibration_ui = lambda: None
    window._design_session = SimpleNamespace(document=None)
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
        task_token,
    )

    assert persisted == []
    assert manager.settings.objectives.objectives["X20"].pixels_to_mm == newer_matrix
    assert restored == []
    assert window.stage_controller.accepted_candidates == []
    assert window.stage_controller.rejected_candidates == [task_token]
    assert statuses == [
        "Click-to-move calibration result ignored because a scan or calibration is active."
    ]

def test_click_calibration_active_update_allows_own_stage_task() -> None:
    window = Main.__new__(Main)
    window._api_stage_command_runtime = SimpleNamespace(active=lambda: False)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={"X20": ObjectiveCalibrationSettings(name="X20")},
    )
    persisted: list[object] = []
    task_token = StageTaskToken(1, "_run_move")
    window.settings_manager = manager
    window.stage_controller = _CalibrationTokenStage(task_token)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = None
    window._persist_objective_plan = lambda plan, **_kwargs: persisted.append(plan) or True
    window._show_status = lambda *_args: None

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
        task_token,
    )

    assert len(persisted) == 1
    assert window.stage_controller.accepted_candidates == [
        (task_token, [[0.02, 0.0], [0.0, 0.02]])
    ]
    assert window.stage_controller.published_candidates == [task_token]

def test_click_calibration_token_loss_before_publish_rolls_back_settings() -> None:
    window = Main.__new__(Main)
    window._api_stage_command_runtime = SimpleNamespace(active=lambda: False)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    original_matrix = [[0.01, 0.0], [0.0, 0.01]]
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                pixels_to_mm=original_matrix,
                xy_calibration_configured=True,
            )
        },
    )
    token_a = StageTaskToken(1, "_run_move")
    token_b = StageTaskToken(2, "_run_move")
    stage = _CalibrationTokenStage(token_a)
    stage.runtime_matrix = None
    statuses: list[str] = []
    window.settings_manager = manager
    window.stage_controller = stage
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    def persist_then_cancel(plan, **_kwargs) -> bool:
        manager.replace_and_save(plan.settings, preserve_exposure_policy=True)
        stage.current_token = token_b
        return True

    window._persist_objective_plan = persist_then_cancel

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
        token_a,
    )

    profile = manager.settings.objectives.objectives["X20"]
    assert profile.pixels_to_mm == original_matrix
    assert stage.runtime_matrix is None
    assert stage.accepted_candidates == [
        (token_a, [[0.02, 0.0], [0.0, 0.02]])
    ]
    assert stage.published_candidates == []
    assert stage.rejected_candidates == [token_a]
    assert "cancelled" in statuses[-1].lower()

def test_click_calibration_inactive_update_is_allowed_during_scan() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X5",
        objectives={
            "X5": ObjectiveCalibrationSettings(name="X5"),
            "X20": ObjectiveCalibrationSettings(name="X20"),
        },
    )
    persisted: list[object] = []
    task_token = StageTaskToken(1, "_run_move")
    window.settings_manager = manager
    window.stage_controller = _CalibrationTokenStage(task_token)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = SimpleNamespace(is_alive=lambda: True)
    window._persist_objective_plan = lambda plan, **_kwargs: persisted.append(plan) or True
    window._show_status = lambda *_args: None

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
        task_token,
    )

    assert len(persisted) == 1
    updated = persisted[0].settings.objectives.objectives["X20"]
    assert updated.pixels_to_mm == [[0.02, 0.0], [0.0, 0.02]]
    assert window.stage_controller.accepted_candidates == [
        (task_token, [[0.02, 0.0], [0.0, 0.02]])
    ]
    assert window.stage_controller.published_candidates == [task_token]

def test_stale_click_calibration_a_does_not_overwrite_runtime_owned_by_b() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    persisted_matrix = [[0.01, 0.0], [0.0, 0.01]]
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                pixels_to_mm=persisted_matrix,
                xy_calibration_configured=True,
            )
        },
    )
    token_a = StageTaskToken(1, "_run_move")
    token_b = StageTaskToken(2, "_run_move")
    stage = _CalibrationTokenStage(token_b)
    stage.runtime_matrix = None
    persisted: list[object] = []
    statuses: list[str] = []
    window.settings_manager = manager
    window.stage_controller = stage
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = None
    window._persist_objective_plan = persisted.append
    window._apply_objective_settings = lambda: pytest.fail(
        "stale A callback reapplied persisted state over calibration B"
    )
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
        token_a,
    )

    assert persisted == []
    assert stage.runtime_matrix is None
    assert stage.accepted_candidates == []
    assert stage.rejected_candidates == [token_a]
    assert "no longer current" in statuses[-1].lower()

def test_tokenless_click_calibration_callback_is_never_accepted() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={"X20": ObjectiveCalibrationSettings(name="X20")},
    )
    persisted: list[object] = []
    statuses: list[str] = []
    window.settings_manager = manager
    window.stage_controller = _CalibrationTokenStage(object(), busy=False)
    window._persist_objective_plan = persisted.append
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
        None,
    )

    assert persisted == []
    assert "no longer current" in statuses[-1].lower()

def test_wrong_source_click_calibration_callback_is_never_accepted() -> None:
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None, parent_session_token=None)
    )
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={"X20": ObjectiveCalibrationSettings(name="X20")},
    )
    task_token = StageTaskToken(1, "unrelated-stage-task")
    stage = _CalibrationTokenStage(task_token, busy=False)
    persisted: list[object] = []
    statuses: list[str] = []
    window.settings_manager = manager
    window.stage_controller = stage
    window._persist_objective_plan = persisted.append
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
        task_token,
    )

    assert persisted == []
    assert stage.accepted_candidates == []
    assert stage.rejected_candidates == [task_token]
    assert "no longer current" in statuses[-1].lower()
