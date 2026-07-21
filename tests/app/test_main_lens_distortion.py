from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage

import main as main_module
from main import Main
from probe_station_gui.camera.optical_calibration_adapters import (
    OpticalCalibrationRequestAdapter,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    CalibrationStartDecision,
    LensCalibrationArtifact,
    OpticalCalibrationOutcome,
)
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)
from probe_station_gui.stage.types import StageTaskToken


class _FakeStage:
    def __init__(self, *, position_error: Exception | None = None) -> None:
        self.events: list[tuple[object, ...]] = []
        self.position_error = position_error

    def begin_external_task(self, label: str) -> None:
        self.events.append(("begin", label))

    def run_external_current_stage_position(self) -> tuple[float, ...]:
        self.events.append(("position",))
        if self.position_error is not None:
            raise self.position_error
        return (10.0, 20.0, 3.0)

    def run_external_needles_action(self, action: str, feedrate: float) -> None:
        self.events.append(("needles", action, feedrate))

    def run_external_move_to_xy(
        self,
        x_mm: float,
        y_mm: float,
        *,
        feedrate: float | None = None,
    ) -> None:
        self.events.append(("move", x_mm, y_mm, feedrate))

    def finish_external_task(self) -> None:
        self.events.append(("finish",))


class _FakeSessionLease:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self._events = events
        self.token = "private-token"

    def snapshot(self) -> dict[str, object]:
        return {
            "operation": "lens distortion calibration",
            "policy": {"auto_enabled": True, "engine": "camera"},
            "fixed_exposure_us": 3200.0,
        }

    def close(self) -> dict[str, object]:
        self._events.append(("session_close",))
        return {"accepted": True}


class _FakeSessionManager:
    def __init__(
        self,
        events: list[tuple[object, ...]],
        *,
        open_error: Exception | None = None,
    ) -> None:
        self._events = events
        self._open_error = open_error

    def open(self, operation: str, parent_token=None) -> _FakeSessionLease:
        self._events.append(("session_open", operation, parent_token))
        if self._open_error is not None:
            raise self._open_error
        return _FakeSessionLease(self._events)


class _FakeSettingsManager:
    def __init__(self) -> None:
        self.settings = Settings()
        self.saved_count = 0
        self.replaced: list[Settings] = []

    def replace(self, settings: Settings) -> None:
        self.settings = settings
        self.replaced.append(settings)

    def save(self) -> None:
        self.saved_count += 1

    def replace_and_save(
        self,
        settings: Settings,
        *,
        preserve_exposure_policy: bool = False,
    ) -> None:
        updated = settings.clone()
        if preserve_exposure_policy:
            updated.exposure_policy = self.settings.exposure_policy.clone()
        self.replace(updated)
        self.save()

    def objectives_configuration(self) -> ObjectivesSettings:
        return self.settings.objectives


class _DeadThread:
    def __init__(self) -> None:
        self.joined = False

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


class _DeferredThread:
    def __init__(self, *, target, args, daemon, **_kwargs) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon

    def is_alive(self) -> bool:
        return False

    def start(self) -> None:
        return None


class _FakeDialog:
    def __init__(self) -> None:
        self.running: list[bool] = []
        self.statuses: list[str] = []
        self.objectives: list[ObjectivesSettings] = []

    def set_running(self, running: bool) -> None:
        self.running.append(bool(running))

    def set_status(self, message: str) -> None:
        self.statuses.append(str(message))

    def set_objectives(self, objectives: ObjectivesSettings) -> None:
        self.objectives.append(objectives)


class _FakeFrame:
    def width(self) -> int:
        return 1920

    def height(self) -> int:
        return 1200


class _CalibrationTokenStage:
    def __init__(self, current_token: object, *, busy: bool = True) -> None:
        self.current_token = current_token
        self.busy = busy
        self.applied: list[object] = []
        self.accepted_candidates: list[tuple[object, object]] = []
        self.published_candidates: list[object] = []
        self.rejected_candidates: list[object] = []

    def is_busy(self) -> bool:
        return self.busy

    def is_calibration_task_token_current(self, token: object) -> bool:
        return token is self.current_token

    def apply_objective_configuration(self, objective, _objectives) -> None:
        self.applied.append(objective)

    def accept_objective_calibration_candidate(
        self,
        token: object,
        matrix: object,
    ) -> bool:
        if token is not self.current_token:
            return False
        self.accepted_candidates.append((token, matrix))
        return True

    def publish_objective_calibration_candidate(self, token: object) -> bool:
        if token is not self.current_token:
            return False
        self.published_candidates.append(token)
        self.runtime_matrix = self.accepted_candidates[-1][1]
        return True

    def reject_objective_calibration_candidate(self, token: object) -> bool:
        self.rejected_candidates.append(token)
        return True


def test_lens_start_captures_request_without_gui_thread_serial_read() -> None:
    captured = []
    window = Main.__new__(Main)
    window._optical_calibration_runtime = SimpleNamespace(
        state=lambda: SimpleNamespace(active_run_id=None),
        start_lens=lambda request: (
            captured.append(request)
            or CalibrationStartDecision(True, 202, "started", request.run_id)
        ),
    )
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        current_stage_position=lambda: pytest.fail("GUI launch read serial position"),
    )
    window._stage_serial_ready = lambda: True
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._active_microscope_scale = lambda: SimpleNamespace(
        pixels_to_mm=[[-0.001, 0.0], [0.0, -0.001]],
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    window._optical_calibration_request_adapter = OpticalCalibrationRequestAdapter(
        window._active_objective_metadata,
        window._active_microscope_scale,
    )
    window._coordinate_feedrate_for_axes = lambda _axes: 120.0
    window._current_needle_feedrate = lambda: 70.0
    window._lens_distortion_dialog = None
    window._show_status = lambda *_args: None

    result = Main._start_lens_distortion_calibration(window)

    assert result["accepted"] is True
    assert captured[0].objective_name == "X20"


def test_completed_artifact_is_saved_to_captured_objective_after_restore_warning() -> None:
    payload = {
        "model_version": 1,
        "model_type": "stage_geometry",
        "frame_size": [1920, 1200],
        "pixels_to_mm": [[-0.001, 0.0], [0.0, -0.001]],
        "calibrated_pixels_to_mm": [[-0.001, 0.0], [0.0, -0.001]],
        "center_px": [960.0, 600.0],
        "k1": 0.01,
        "k2": -0.002,
        "p1": 0.0005,
        "p2": -0.0003,
        "baseline_residual_mean_px": 0.8,
        "baseline_residual_max_px": 1.4,
        "residual_mean_px": 0.5,
        "residual_max_px": 1.0,
        "feature_count": 20,
        "observation_count": 40,
        "optimizer_success": True,
    }
    preview = QImage(4, 3, QImage.Format_Grayscale8)
    artifact = LensCalibrationArtifact(payload, preview, preview)
    outcome = OpticalCalibrationOutcome(
        "lens-1", 17, "lens", False, "complete, restore failed", "X20", True,
        lens_artifact=artifact,
        restore_warning="camera restore failed",
    )
    saved = []
    window = Main.__new__(Main)
    window._lens_distortion_dialog = None
    window._optical_calibration_wizard = None
    window._save_objective_distortion = lambda *args, **kwargs: saved.append(
        (args, kwargs)
    )
    window._show_status = lambda *_args: None

    Main._on_lens_distortion_calibration_finished(
        window, outcome, False, outcome.message, artifact
    )

    assert saved[0][0] == (payload, "X20")
    assert saved[0][1]["optical_context"] is outcome

def test_click_calibration_update_keeps_stage_calibrated_distortion_matrix() -> None:
    window = Main.__new__(Main)
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

    accepted = Main._start_click_to_move(window, 4.0, -3.0)

    assert accepted is False
    assert requests == []

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
    window._start_click_to_move = (
        lambda dx_px, dy_px: events.append(("start", dx_px, dy_px)) or True
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
