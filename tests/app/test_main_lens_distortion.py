from __future__ import annotations

from types import SimpleNamespace

import main as main_module
from main import Main
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)


class _FakeStage:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def begin_external_task(self, label: str) -> None:
        self.events.append(("begin", label))

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

    def objectives_configuration(self) -> ObjectivesSettings:
        return self.settings.objectives


class _DeadThread:
    def __init__(self) -> None:
        self.joined = False

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


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


def test_run_lens_distortion_calibration_captures_offset_grid(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    stage = _FakeStage()
    finished: list[tuple[bool, str, object]] = []
    frames = [_FakeFrame() for _ in Main.LENS_DISTORTION_CAPTURE_OFFSETS_MM]
    captured_offsets: list[tuple[float, float]] = []

    def wait_for_frame(*, after_counter=None, timeout_s=2.0):
        assert after_counter == 0
        return frames.pop(0), 1

    def fit_grid(grid_frames, *, frame_size):
        assert frame_size == (1920, 1200)
        captured_offsets.extend(frame.stage_offset_mm for frame in grid_frames)
        return {"model_version": 1, "frame_size": [640, 480]}

    window.stage_controller = stage
    window._current_needle_feedrate = lambda: 71.0
    window._current_linear_feedrate = lambda: 123.0
    window._latest_raw_camera_counter = lambda: 0
    window._wait_for_raw_camera_frame = wait_for_frame
    window._show_status = lambda _message, _timeout_ms=0: None
    window._emit_lens_distortion_finished = (
        lambda success, message, payload: finished.append((success, message, payload))
    )
    monkeypatch.setattr(main_module, "fit_distortion_from_grid_frames", fit_grid)
    monkeypatch.setattr(main_module.time, "sleep", lambda _seconds: None)

    Main._run_lens_distortion_calibration(window, (10.0, 20.0))

    assert stage.events[0] == ("begin", "lens distortion calibration")
    assert stage.events[1] == ("needles", "raise", 71.0)
    capture_moves = stage.events[2 : 2 + len(Main.LENS_DISTORTION_CAPTURE_OFFSETS_MM)]
    assert capture_moves[0] == ("move", 10.0, 20.0, 123.0)
    assert capture_moves[-1] == ("move", 10.05, 20.05, 123.0)
    assert stage.events[-2:] == [("move", 10.0, 20.0, 123.0), ("finish",)]
    assert captured_offsets == list(Main.LENS_DISTORTION_CAPTURE_OFFSETS_MM)
    assert finished == [
        (
            True,
            "Lens distortion calibration saved. Recalibrate click-to-move.",
            {"model_version": 1, "frame_size": [640, 480]},
        )
    ]


def test_lens_distortion_finished_saves_payload_and_resets_click_calibration() -> None:
    window = Main.__new__(Main)
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X50",
        objectives={
            "X50": ObjectiveCalibrationSettings(
                name="X50",
                magnification=50.0,
                pixels_to_mm=[[1.0, 0.0], [0.0, 1.0]],
                xy_calibration_configured=True,
            )
        },
    )
    dialog = _FakeDialog()
    apply_calls: list[str] = []
    refresh_calls: list[str] = []
    statuses: list[tuple[str, int]] = []
    window.settings_manager = manager
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_dialog = dialog
    window._apply_objective_settings = lambda: apply_calls.append("apply")
    window._refresh_objective_calibration_ui = lambda: refresh_calls.append("refresh")
    window._show_status = (
        lambda message, timeout_ms=0: statuses.append((str(message), int(timeout_ms)))
    )
    payload = {"model_version": 1, "frame_size": [640, 480], "residual_mean_px": 0.2}

    Main._on_lens_distortion_calibration_finished(window, True, "done", payload)

    profile = manager.settings.objectives.objectives["X50"]
    assert profile.distortion_correction_configured is True
    assert profile.distortion_correction == payload
    assert profile.xy_calibration_configured is False
    assert profile.pixels_to_mm == []
    assert manager.saved_count == 1
    assert apply_calls == ["apply"]
    assert refresh_calls == ["refresh"]
    assert dialog.running == [False]
    assert statuses == [("done", 10000)]


def test_camera_frame_distortion_correction_reuses_compiled_payload(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
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
