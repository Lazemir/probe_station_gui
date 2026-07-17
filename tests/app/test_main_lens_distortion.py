from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def test_lens_distortion_capture_offsets_cover_center_edges_and_corners() -> None:
    scale = SimpleNamespace(
        pixel_size_x_mm=0.001174,
        pixel_size_y_mm=0.001169,
    )

    offsets = Main._lens_distortion_capture_offsets_mm((1920, 1200), scale)

    assert len(offsets) == 9
    assert offsets[0] == (0.0, 0.0)
    assert len({round(x, 9) for x, _y in offsets}) == 3
    assert len({round(y, 9) for _x, y in offsets}) == 3
    max_x = max(abs(x) for x, _y in offsets)
    max_y = max(abs(y) for _x, y in offsets)
    assert max_x <= (1920 * scale.pixel_size_x_mm - Main.LENS_DISTORTION_GRID_STEP_MM * Main.LENS_DISTORTION_GRID_CELL_COUNT) * 0.5
    assert max_y <= (1200 * scale.pixel_size_y_mm - Main.LENS_DISTORTION_GRID_STEP_MM * Main.LENS_DISTORTION_GRID_CELL_COUNT) * 0.5


def test_lens_distortion_capture_offsets_clamp_to_safe_edge_margin() -> None:
    grid_span = Main.LENS_DISTORTION_GRID_STEP_MM * Main.LENS_DISTORTION_GRID_CELL_COUNT
    fov_mm = grid_span + Main.LENS_DISTORTION_GRID_STEP_MM * 0.5
    scale = SimpleNamespace(
        pixel_size_x_mm=fov_mm / 1000.0,
        pixel_size_y_mm=fov_mm / 1000.0,
    )

    offsets = Main._lens_distortion_capture_offsets_mm((1000, 1000), scale)

    max_offset = max(abs(value) for offset in offsets for value in offset)
    safe_edge = (fov_mm - grid_span) * 0.5 * Main.LENS_DISTORTION_EDGE_MARGIN_FRACTION
    assert max_offset <= safe_edge


def test_lens_distortion_capture_offsets_use_detected_feature_bounds(monkeypatch) -> None:
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    frame = _FakeFrame()
    monkeypatch.setattr(
        main_module,
        "detect_bright_feature_bounds",
        lambda _frame: SimpleNamespace(left=250.0, top=150.0, right=750.0, bottom=650.0),
    )

    offsets = Main._lens_distortion_capture_offsets_mm(
        (1000, 800),
        scale,
        initial_frame=frame,
    )

    edge_margin = max(
        8.0,
        800 * Main.LENS_DISTORTION_FEATURE_EDGE_MARGIN_FRACTION,
    )
    expected_x_px = (250.0 - edge_margin) * Main.LENS_DISTORTION_EDGE_MARGIN_FRACTION
    expected_y_px = (150.0 - edge_margin) * Main.LENS_DISTORTION_EDGE_MARGIN_FRACTION
    assert len(offsets) == 9
    assert offsets[0] == (0.0, 0.0)
    assert max(abs(x) for x, _y in offsets) == pytest.approx(expected_x_px * 0.001)
    assert max(abs(y) for _x, y in offsets) == pytest.approx(expected_y_px * 0.001)


def test_lens_distortion_capture_offsets_fail_when_feature_too_close_to_edge(
    monkeypatch,
) -> None:
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    monkeypatch.setattr(
        main_module,
        "detect_bright_feature_bounds",
        lambda _frame: SimpleNamespace(left=8.0, top=150.0, right=900.0, bottom=650.0),
    )

    with pytest.raises(RuntimeError, match="too close"):
        Main._lens_distortion_capture_offsets_mm(
            (1000, 800),
            scale,
            initial_frame=_FakeFrame(),
        )


def test_run_lens_distortion_calibration_captures_offset_grid(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    stage = _FakeStage()
    finished: list[tuple[bool, str, object]] = []
    scale = SimpleNamespace(
        pixels_to_mm=((0.1, 0.0), (0.0, 0.1)),
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    monkeypatch.setattr(
        main_module,
        "detect_bright_feature_bounds",
        lambda _frame: SimpleNamespace(left=500.0, top=300.0, right=1420.0, bottom=900.0),
    )
    expected_offsets = Main._lens_distortion_capture_offsets_mm(
        (1920, 1200),
        scale,
        initial_frame=_FakeFrame(),
    )
    raw_frames = [_FakeFrame() for _ in range(len(expected_offsets) + 1)]
    captured_offsets: list[tuple[float, float]] = []
    corrected_frames: list[object] = []

    class _CorrectedFrame(_FakeFrame):
        def __init__(self, raw: object) -> None:
            self.raw = raw

    def wait_for_frame(*, after_counter=None, timeout_s=2.0):
        assert after_counter in (None, 0)
        return raw_frames.pop(0), 1

    class _Fit:
        def __init__(self, grid_frames) -> None:
            self.grid_frames = grid_frames

        def to_payload(self) -> dict[str, object]:
            captured_offsets.extend(
                frame.stage_offset_mm for frame in self.grid_frames
            )
            return {
                "model_version": 1,
                "model_type": "stage_geometry",
                "frame_size": [1920, 1200],
                "pixels_to_mm": [[0.1, 0.0], [0.0, 0.1]],
                "calibrated_pixels_to_mm": [[0.1, 0.0], [0.0, 0.1]],
                "residual_mean_px": 0.5,
                "residual_max_px": 1.0,
            }

    def fit_geometry(
        grid_frames,
        *,
        frame_size,
        pixels_to_mm=None,
        cluster_tolerance_px=None,
    ):
        assert frame_size == (1920, 1200)
        assert pixels_to_mm == ((0.1, -0.0), (0.0, -0.1))
        assert cluster_tolerance_px == 12.0
        return _Fit(grid_frames)

    window.stage_controller = stage
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._flat_field_calibration_store = SimpleNamespace(
        load=lambda objective: (
            stage.events.append(("load_flat", objective))
            or SimpleNamespace(profile="flat-profile")
        )
    )
    window._run_camera_auto_exposure = lambda: (
        stage.events.append(("auto_exposure",))
        or {"accepted": True, "converged": True}
    )
    window._apply_microscope_scan_camera_lock = lambda settings: (
        stage.events.append(("camera_lock", settings.enabled)) or "lens-lock"
    )
    window._restore_microscope_scan_camera_lock = lambda key: (
        stage.events.append(("camera_restore", key)) or ""
    )
    window._current_needle_feedrate = lambda: 71.0
    window._coordinate_feedrate_for_axes = lambda axes: 123.0
    window._latest_raw_camera_counter = lambda: 0
    window._wait_for_raw_camera_frame = wait_for_frame
    window._active_microscope_scale = lambda: scale
    window._show_status = lambda _message, _timeout_ms=0: None
    window._emit_lens_distortion_finished = (
        lambda success, message, payload: finished.append((success, message, payload))
    )
    def apply_flat(frame, profile):
        assert profile == "flat-profile"
        corrected = _CorrectedFrame(frame)
        corrected_frames.append(corrected)
        return corrected

    def detect_corrected(frame):
        assert isinstance(frame, _FakeFrame)
        return SimpleNamespace(left=500.0, top=300.0, right=1420.0, bottom=900.0)

    monkeypatch.setattr(main_module, "apply_flat_field_correction", apply_flat)
    monkeypatch.setattr(main_module, "detect_bright_feature_bounds", detect_corrected)
    monkeypatch.setattr(main_module, "fit_stage_geometry_from_grid_frames", fit_geometry)
    monkeypatch.setattr(main_module.time, "sleep", lambda _seconds: None)

    Main._run_lens_distortion_calibration(window, (10.0, 20.0))

    assert stage.events[:5] == [
        ("load_flat", "X20"),
        ("begin", "lens distortion calibration"),
        ("auto_exposure",),
        ("camera_lock", True),
        ("needles", "raise", 71.0),
    ]
    capture_moves = [event for event in stage.events if event[0] == "move"][:-1]
    assert capture_moves[0] == ("move", 10.0, 20.0, 123.0)
    last_dx, last_dy = expected_offsets[-1]
    assert capture_moves[-1] == ("move", 10.0 + last_dx, 20.0 + last_dy, 123.0)
    assert stage.events[-3:] == [
        ("move", 10.0, 20.0, 123.0),
        ("camera_restore", "lens-lock"),
        ("finish",),
    ]
    assert captured_offsets == list(expected_offsets) * 2
    assert len(corrected_frames) == len(expected_offsets)
    assert finished[0][0] is True
    assert "raw 0.50/1.00 px" in finished[0][1]
    assert "flat field 0.50/1.00 px" in finished[0][1]
    assert finished[0][2]["calibration_input"] == "raw"


def test_lens_distortion_calibration_does_not_require_flat_field_before_motion() -> None:
    window = Main.__new__(Main)
    stage = _FakeStage()
    finished: list[tuple[bool, str, object]] = []
    window.stage_controller = stage
    window._active_microscope_scale = lambda: SimpleNamespace(
        pixels_to_mm=((0.1, 0.0), (0.0, 0.1)),
    )
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._flat_field_calibration_store = SimpleNamespace(
        load=lambda _objective: (_ for _ in ()).throw(FileNotFoundError("missing"))
    )
    window._current_needle_feedrate = lambda: 71.0
    window._coordinate_feedrate_for_axes = lambda _axes: 123.0
    window._show_status = lambda *_args: None
    window._emit_lens_distortion_finished = (
        lambda success, message, payload: finished.append((success, message, payload))
    )

    Main._run_lens_distortion_calibration(window, (10.0, 20.0))

    assert stage.events == [
        ("begin", "lens distortion calibration"),
        ("finish",),
    ]
    assert finished[0][0] is False
    assert "flat-field" not in finished[0][1].lower()


def test_lens_distortion_auto_exposure_failure_releases_stage_without_move() -> None:
    stage = _FakeStage()
    finished: list[tuple[bool, str, object]] = []
    window = Main.__new__(Main)
    window.stage_controller = stage
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._active_microscope_scale = lambda: SimpleNamespace(
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    window._flat_field_calibration_store = SimpleNamespace(
        load=lambda objective: (
            stage.events.append(("load_flat", objective))
            or SimpleNamespace(profile="flat-profile")
        )
    )
    window._run_camera_auto_exposure = lambda: {
        "accepted": False,
        "message": "Exposure did not converge.",
    }
    window._emit_lens_distortion_finished = (
        lambda success, message, payload: finished.append((success, message, payload))
    )

    Main._run_lens_distortion_calibration(window, (10.0, 20.0), 120.0, 70.0)

    assert stage.events == [
        ("load_flat", "X20"),
        ("begin", "lens distortion calibration"),
        ("finish",),
    ]
    assert finished == [
        (
            False,
            "Lens distortion calibration failed: Exposure did not converge.",
            None,
        )
    ]


def test_fit_lens_distortion_payload_uses_image_coordinate_matrix(monkeypatch) -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0002), (0.0003, -0.0011)),
    )
    calls: list[tuple[object, ...]] = []

    class _Fit:
        def to_payload(self) -> dict[str, object]:
            return {
                "model_version": 1,
                "model_type": "stage_geometry",
                "pixels_to_mm": [[-0.0009, -0.0004], [0.0002, 0.0012]],
                "calibrated_pixels_to_mm": [
                    [-0.0009, -0.0004],
                    [0.0002, 0.0012],
                ],
            }

    def fit_geometry(
        grid_frames,
        *,
        frame_size,
        pixels_to_mm,
        cluster_tolerance_px,
    ):
        calls.append(
            (
                "geometry",
                tuple(grid_frames),
                frame_size,
                pixels_to_mm,
                cluster_tolerance_px,
            )
        )
        return _Fit()

    monkeypatch.setattr(main_module, "fit_stage_geometry_from_grid_frames", fit_geometry)

    payload = Main._fit_lens_distortion_payload(
        frames,
        frame_size=(1920, 1200),
        scale=scale,
    )

    assert payload == {
        "model_version": 1,
        "model_type": "stage_geometry",
        "pixels_to_mm": [[-0.0009, 0.0004], [0.0002, -0.0012]],
        "calibrated_pixels_to_mm": [[-0.0009, 0.0004], [0.0002, -0.0012]],
    }
    assert calls == [
        (
            "geometry",
            tuple(frames),
            (1920, 1200),
            ((-0.001, -0.0002), (0.0003, 0.0011)),
            12.0,
        )
    ]


def test_lens_distortion_fit_selection_uses_best_valid_candidate() -> None:
    raw = {
        "model_type": "stage_geometry",
        "residual_mean_px": 1.7,
        "residual_max_px": 4.2,
    }
    flat = {
        "model_type": "stage_geometry",
        "residual_mean_px": 1.4,
        "residual_max_px": 5.6,
    }

    selected = Main._select_lens_distortion_fit_candidate(
        {"raw": raw, "flat_field": flat}
    )

    assert selected["calibration_input"] == "flat_field"
    assert selected["calibration_candidates"] == {
        "raw": {"residual_mean_px": 1.7, "residual_max_px": 4.2},
        "flat_field": {"residual_mean_px": 1.4, "residual_max_px": 5.6},
    }


def test_lens_distortion_fit_selection_rejects_invalid_candidates() -> None:
    raw = {
        "model_type": "stage_geometry",
        "residual_mean_px": 4.5,
        "residual_max_px": 19.7,
    }
    flat = {
        "model_type": "stage_geometry",
        "residual_mean_px": 3.2,
        "residual_max_px": 10.0,
    }

    with pytest.raises(RuntimeError, match="raw.*4.50.*flat field.*3.20"):
        Main._select_lens_distortion_fit_candidate(
            {"raw": raw, "flat_field": flat}
        )


def test_lens_distortion_fit_selection_reports_rejected_candidate_metrics() -> None:
    raw = {
        "model_type": "stage_geometry",
        "residual_mean_px": 4.5,
        "residual_max_px": 19.7,
    }
    flat = {
        "model_type": "stage_geometry",
        "residual_mean_px": 1.4,
        "residual_max_px": 5.6,
    }

    selected = Main._select_lens_distortion_fit_candidate(
        {"raw": raw, "flat_field": flat}
    )

    assert selected["calibration_input"] == "flat_field"
    assert selected["calibration_candidates"]["raw"] == {
        "residual_mean_px": 4.5,
        "residual_max_px": 19.7,
    }


def test_lens_distortion_candidates_compare_same_raw_frames(monkeypatch) -> None:
    raw_frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    corrected: list[object] = []
    calls: list[tuple[object, ...]] = []

    class _CorrectedFrame(_FakeFrame):
        pass

    def apply_flat(frame, profile):
        assert profile == "flat-profile"
        result = _CorrectedFrame()
        corrected.append(result)
        return result

    def fit_payload(frames, *, frame_size, scale, validate=True):
        calls.append(tuple(frame.frame for frame in frames))
        assert frame_size == (1920, 1200)
        assert scale == "scale"
        assert validate is False
        is_flat = isinstance(frames[0].frame, _CorrectedFrame)
        return {
            "model_type": "stage_geometry",
            "residual_mean_px": 1.2 if is_flat else 1.8,
            "residual_max_px": 4.0 if is_flat else 5.0,
        }

    monkeypatch.setattr(main_module, "apply_flat_field_correction", apply_flat)
    monkeypatch.setattr(Main, "_fit_lens_distortion_payload", staticmethod(fit_payload))

    selected = Main._fit_lens_distortion_candidates(
        raw_frames,
        frame_size=(1920, 1200),
        scale="scale",
        flat_field_profile="flat-profile",
    )

    assert len(calls) == 2
    assert calls[0] == tuple(frame.frame for frame in raw_frames)
    assert calls[1] == tuple(corrected)
    assert selected["calibration_input"] == "flat_field"


def test_lens_distortion_candidates_use_raw_when_flat_field_is_unavailable(
    monkeypatch,
) -> None:
    raw_frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    calls: list[tuple[object, ...]] = []

    def fit_payload(frames, *, frame_size, scale, validate=True):
        calls.append(tuple(frame.frame for frame in frames))
        assert validate is False
        return {
            "model_type": "stage_geometry",
            "residual_mean_px": 1.8,
            "residual_max_px": 5.0,
        }

    monkeypatch.setattr(Main, "_fit_lens_distortion_payload", staticmethod(fit_payload))

    selected = Main._fit_lens_distortion_candidates(
        raw_frames,
        frame_size=(1920, 1200),
        scale="scale",
        flat_field_profile=None,
    )

    assert calls == [tuple(frame.frame for frame in raw_frames)]
    assert selected["calibration_input"] == "raw"


def test_lens_distortion_candidates_keep_raw_when_flat_correction_fails(
    monkeypatch,
) -> None:
    raw_frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]

    def fit_payload(frames, *, frame_size, scale, validate=True):
        assert validate is False
        return {
            "model_type": "stage_geometry",
            "residual_mean_px": 1.8,
            "residual_max_px": 5.0,
        }

    monkeypatch.setattr(Main, "_fit_lens_distortion_payload", staticmethod(fit_payload))
    monkeypatch.setattr(
        main_module,
        "apply_flat_field_correction",
        lambda _frame, _profile: (_ for _ in ()).throw(
            ValueError("profile size mismatch")
        ),
    )

    selected = Main._fit_lens_distortion_candidates(
        raw_frames,
        frame_size=(1920, 1200),
        scale="scale",
        flat_field_profile="stale-profile",
    )

    assert selected["calibration_input"] == "raw"
    assert selected["calibration_candidate_failures"] == [
        "flat field: profile size mismatch"
    ]


def test_fit_lens_distortion_payload_raises_when_stage_geometry_errors(
    monkeypatch,
) -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
    )

    def fit_geometry(*_args, **_kwargs):
        raise RuntimeError("scipy unavailable")

    monkeypatch.setattr(main_module, "fit_stage_geometry_from_grid_frames", fit_geometry)

    with pytest.raises(RuntimeError, match="scipy unavailable"):
        Main._fit_lens_distortion_payload(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )


def test_fit_lens_distortion_payload_rejects_high_residual(monkeypatch) -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
    )

    class _Fit:
        def to_payload(self) -> dict[str, object]:
            return {
                "model_version": 1,
                "model_type": "stage_geometry",
                "residual_mean_px": 5.2,
                "residual_max_px": 17.4,
            }

    def fit_geometry(*_args, **_kwargs):
        return _Fit()

    monkeypatch.setattr(main_module, "fit_stage_geometry_from_grid_frames", fit_geometry)

    with pytest.raises(RuntimeError, match="residual is too high"):
        Main._fit_lens_distortion_payload(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )


def test_fit_lens_distortion_payload_requires_click_calibration() -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
    ]
    scale = SimpleNamespace()

    with pytest.raises(RuntimeError, match="requires click-to-move calibration"):
        Main._fit_lens_distortion_payload(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )


def test_lens_distortion_finished_resets_click_calibration_without_stage_matrix() -> None:
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
    assert statuses == [("done Recalibrate click-to-move.", 10000)]


def test_lens_distortion_finished_applies_stage_calibrated_grid_matrix() -> None:
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
    matrix = [[-0.000117, 0.0], [0.0, -0.000117]]
    payload = {
        "model_version": 1,
        "frame_size": [640, 480],
        "calibrated_pixels_to_mm": matrix,
    }

    Main._on_lens_distortion_calibration_finished(window, True, "done", payload)

    profile = manager.settings.objectives.objectives["X50"]
    assert profile.distortion_correction_configured is True
    assert profile.distortion_correction == payload
    assert profile.xy_calibration_configured is True
    assert profile.pixels_to_mm == matrix
    assert manager.saved_count == 1
    assert apply_calls == ["apply"]
    assert refresh_calls == ["refresh"]
    assert dialog.running == [False]
    assert statuses == [("done", 10000)]


def test_lens_distortion_completion_saves_to_captured_objective_after_active_switch() -> None:
    window = Main.__new__(Main)
    manager = _FakeSettingsManager()
    x5_payload = {"model_version": 1, "frame_size": [320, 240]}
    x5_matrix = [[-0.0005, 0.0], [0.0, -0.0005]]
    manager.settings.objectives = ObjectivesSettings(
        active_name="X5",
        objectives={
            "X5": ObjectiveCalibrationSettings(
                name="X5",
                magnification=5.0,
                pixels_to_mm=x5_matrix,
                xy_calibration_configured=True,
                distortion_correction=x5_payload,
                distortion_correction_configured=True,
            ),
            "X20": ObjectiveCalibrationSettings(name="X20", magnification=20.0),
        },
    )
    captured_context = main_module._OpticalCalibrationRunContext(
        operation_id="lens-x20",
        wizard_run_id=None,
        objective_name="X20",
    )
    matrix = [[-0.000117, 0.0], [0.0, -0.000117]]
    payload = {
        "model_version": 1,
        "frame_size": [640, 480],
        "calibrated_pixels_to_mm": matrix,
    }
    window.settings_manager = manager
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_context = captured_context
    window._lens_distortion_dialog = None
    window._optical_calibration_wizard = None
    window._apply_objective_settings = lambda: None
    window._refresh_objective_calibration_ui = lambda: None
    window._show_status = lambda *_args: None

    Main._on_lens_distortion_calibration_finished(
        window,
        captured_context,
        True,
        "done",
        payload,
    )

    x20 = manager.settings.objectives.objectives["X20"]
    x5 = manager.settings.objectives.objectives["X5"]
    assert x20.distortion_correction == payload
    assert x20.distortion_correction_configured is True
    assert x20.pixels_to_mm == matrix
    assert x20.xy_calibration_configured is True
    assert x5.distortion_correction == x5_payload
    assert x5.distortion_correction_configured is True
    assert x5.pixels_to_mm == x5_matrix
    assert x5.xy_calibration_configured is True


def test_click_calibration_update_keeps_stage_calibrated_distortion_matrix() -> None:
    window = Main.__new__(Main)
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
    window.settings_manager = manager
    window._persist_objective_plan = lambda plan: persisted.append(plan)

    Main._on_objective_calibration_updated(
        window,
        "X50",
        [[-0.000119, 0.0], [0.0, -0.000112]],
    )

    profile = persisted[0].settings.objectives.objectives["X50"]
    assert profile.pixels_to_mm == calibrated


def test_reset_lens_distortion_preserves_click_calibration() -> None:
    window = Main.__new__(Main)
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
    events: list[tuple[object, ...]] = []
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
        ("reset", "Click-to-move calibration reset."),
        ("start", 1.5, -2.0),
    ]


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
