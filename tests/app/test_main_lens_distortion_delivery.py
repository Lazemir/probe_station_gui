from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage

from main import Main
from probe_station_gui.camera.optical_calibration_adapters import (
    OpticalCalibrationRequestAdapter,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    CalibrationStartDecision,
    LensCalibrationArtifact,
    LensDistortionCalibrationRequest,
    OpticalCalibrationOutcome,
    OpticalCalibrationRuntime,
)
from probe_station_gui.settings.manager import Settings, SettingsManager
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)


class _QueuedThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    def join(self, _timeout=None) -> None:
        return None


class _UnusedPort:
    def __getattr__(self, name: str):
        raise AssertionError(f"queued delivery touched port: {name}")


class _QueuedEvents:
    def progress(self, _event) -> None:
        pass

    def complete(self, _outcome) -> None:
        pass

    def warning(self, _message) -> None:
        pass


def _queued_lens_delivery() -> tuple[
    OpticalCalibrationRuntime,
    LensDistortionCalibrationRequest,
    OpticalCalibrationOutcome,
    LensCalibrationArtifact,
]:
    runtime = OpticalCalibrationRuntime(
        stage=_UnusedPort(),
        camera=_UnusedPort(),
        sessions=_UnusedPort(),
        store=_UnusedPort(),
        events=_QueuedEvents(),
        thread_factory=_QueuedThread,
    )
    request = LensDistortionCalibrationRequest(
        run_id="lens-a",
        wizard_run_id=17,
        objective_name="X20",
        magnification=20.0,
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
        pixel_size_mm=(0.001, 0.001),
        linear_feedrate=120.0,
        needle_feedrate=70.0,
    )
    preview = QImage(4, 3, QImage.Format_Grayscale8)
    artifact = LensCalibrationArtifact({"model_type": "stage_geometry"}, preview, preview)
    outcome = OpticalCalibrationOutcome(
        request.run_id,
        request.wizard_run_id,
        "lens",
        True,
        "saved",
        request.objective_name,
        False,
        lens_artifact=artifact,
    )
    runtime.start_lens(request)
    runtime._finish_run(request, outcome)
    return runtime, request, outcome, artifact


@pytest.mark.parametrize(
    "invalidated_by", ("new-run", "cancel", "scan", "new-run-during-scan")
)
def test_queued_lens_outcome_cannot_mutate_after_invalidation(invalidated_by) -> None:
    runtime, request, outcome, artifact = _queued_lens_delivery()
    replacement_outcome = replace(outcome, run_id="lens-b")
    if invalidated_by in {"new-run", "new-run-during-scan"}:
        replacement_request = replace(request, run_id="lens-b")
        runtime.start_lens(replacement_request)
        if invalidated_by == "new-run-during-scan":
            runtime._finish_run(replacement_request, replacement_outcome)
    window = Main.__new__(Main)
    window._optical_calibration_runtime = runtime
    window._microscope_scan_running = lambda: invalidated_by in {
        "scan",
        "new-run-during-scan",
    }
    dialog_updates: list[tuple[str, object]] = []
    if invalidated_by in {"cancel", "scan"}:
        window._lens_distortion_dialog = SimpleNamespace(
            set_running=lambda value: dialog_updates.append(("running", value)),
            set_status=lambda message: dialog_updates.append(("status", message)),
        )
    else:
        window._lens_distortion_dialog = SimpleNamespace(
            set_running=lambda _value: pytest.fail("stale outcome changed dialog"),
            set_status=lambda _message: pytest.fail("stale outcome changed status"),
        )
    window._optical_calibration_wizard = SimpleNamespace(
        active_run_id=lambda: 17,
        set_lens_distortion_result=lambda *_args, **_kwargs: pytest.fail(
            "stale outcome changed wizard"
        )
    )
    window._save_objective_distortion = lambda *_args, **_kwargs: pytest.fail(
        "stale outcome saved correction"
    )
    window._show_status = lambda *_args: pytest.fail("stale outcome was shown")
    if invalidated_by == "cancel":
        Main._cancel_optical_calibration_wizard(window, 17)

    Main._on_lens_distortion_calibration_finished(
        window, outcome, True, outcome.message, artifact
    )

    if invalidated_by in {"cancel", "scan"}:
        assert dialog_updates == [
            ("running", False),
            ("status", "Lens distortion calibration stopped."),
        ]
    if invalidated_by == "new-run-during-scan":
        assert runtime.consume(replacement_outcome) is True


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
    window._optical_calibration_runtime = SimpleNamespace(consume=lambda *_args, **_kwargs: True)
    window._microscope_scan_running = lambda: False
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


def test_completion_persists_to_captured_objective_after_active_objective_changes(
    tmp_path,
    monkeypatch,
) -> None:
    calibrated = [[-0.0012, 0.0001], [-0.0002, -0.0011]]
    payload = {
        "model_version": 1,
        "model_type": "stage_geometry",
        "frame_size": [100, 50],
        "pixels_to_mm": [[-0.001, 0.0], [0.0, -0.001]],
        "calibrated_pixels_to_mm": calibrated,
        "center_px": [50.0, 25.0],
        "k1": 0.0,
        "k2": 0.0,
        "p1": 0.0,
        "p2": 0.0,
        "baseline_residual_mean_px": 1.5,
        "baseline_residual_max_px": 2.5,
        "residual_mean_px": 0.5,
        "residual_max_px": 1.0,
        "feature_count": 20,
        "observation_count": 40,
        "optimizer_success": True,
    }
    preview = QImage(100, 50, QImage.Format_Grayscale8)
    artifact = LensCalibrationArtifact(payload, preview, preview)
    outcome = OpticalCalibrationOutcome(
        "lens-x20", 17, "lens", True, "complete", "X20", True,
        lens_artifact=artifact,
    )
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(
        "probe_station_gui.settings.manager.platform.system",
        lambda: "Windows",
    )
    monkeypatch.setattr(
        "probe_station_gui.settings.manager.configure_logging",
        lambda *_args: None,
    )
    manager = SettingsManager()
    settings = Settings()
    settings.objectives = ObjectivesSettings(
        active_name="X5",
        objectives={
            "X5": ObjectiveCalibrationSettings(
                name="X5",
                pixels_to_mm=[[0.005, 0.0], [0.0, 0.005]],
                xy_calibration_configured=True,
            ),
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                pixels_to_mm=[[-0.001, 0.0], [0.0, -0.001]],
                xy_calibration_configured=True,
            ),
        },
    )
    manager.replace(settings)
    window = Main.__new__(Main)
    window.settings_manager = manager
    window._optical_calibration_runtime = SimpleNamespace(
        consume=lambda received, **_kwargs: received is outcome
    )
    window._microscope_scan_running = lambda: False
    window._lens_distortion_dialog = None
    window._optical_calibration_wizard = None
    window._apply_objective_settings = lambda: None
    window._refresh_objective_calibration_ui = lambda: None
    window._show_status = lambda *_args: None

    Main._on_lens_distortion_calibration_finished(
        window, outcome, True, outcome.message, artifact
    )

    assert manager.settings.objectives.active_name == "X5"
    assert manager.settings.objectives.objectives["X5"].pixels_to_mm == [
        [0.005, 0.0],
        [0.0, 0.005],
    ]
    x20 = manager.settings.objectives.objectives["X20"]
    assert x20.distortion_correction == payload
    assert x20.distortion_correction_configured is True
    assert x20.pixels_to_mm == calibrated
    assert x20.xy_calibration_configured is True
    persisted = json.loads(
        (manager.config_dir() / SettingsManager.CONFIG_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert persisted["objectives"]["active_name"] == "X5"
    assert persisted["objectives"]["objectives"]["X20"][
        "distortion_correction"
    ] == payload
