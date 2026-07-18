from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage

import main as main_module
from main import Main
from probe_station_gui.camera import geometry_mask
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)


@pytest.fixture(autouse=True)
def _stable_geometry_mask_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "_load_geometry_mask_backend",
        lambda: geometry_mask,
    )


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


@pytest.mark.parametrize(
    "start_calibration",
    (
        lambda window: Main._start_flat_field_calibration(window),
        lambda window: Main._start_lens_distortion_calibration(window),
        lambda window: Main._start_flat_field_calibration(
            window,
            wizard_run_id=41,
            full_wizard=True,
        ),
        lambda window: Main._start_lens_distortion_calibration(
            window,
            wizard_run_id=41,
            parent_session_token="outer-token",
            full_wizard=True,
        ),
    ),
)
def test_standalone_and_wizard_calibration_launch_do_not_read_serial_position(
    monkeypatch,
    start_calibration,
) -> None:
    window = Main.__new__(Main)
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: False,
        current_stage_position=lambda: pytest.fail(
            "Qt calibration launch slot read the serial stage position"
        ),
    )
    window._stage_serial_ready = lambda: True
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._coordinate_feedrate_for_axes = lambda _axes: 120.0
    window._current_needle_feedrate = lambda: 70.0
    window._lens_distortion_dialog = None
    window._show_status = lambda *_args: None
    monkeypatch.setattr(main_module.threading, "Thread", _DeferredThread)

    assert start_calibration(window) is True


def _lens_output(
    payload: dict[str, object],
    *,
    before_preview: QImage | None = None,
    after_preview: QImage | None = None,
    session_restore_error: str = "",
) -> main_module._LensDistortionCalibrationOutput:
    return main_module._LensDistortionCalibrationOutput(
        payload=payload,
        before_preview=(
            before_preview
            if before_preview is not None
            else QImage(4, 3, QImage.Format_Grayscale8)
        ),
        after_preview=(
            after_preview
            if after_preview is not None
            else QImage(4, 3, QImage.Format_Grayscale8)
        ),
        session_restore_error=session_restore_error,
    )


def _stage_geometry_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
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
        "feature_count": 8,
        "observation_count": 24,
        "optimizer_success": True,
    }
    payload.update(overrides)
    return payload


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
    assert max(abs(x) for x, _y in offsets) == pytest.approx(
        1920 * scale.pixel_size_x_mm * Main.LENS_DISTORTION_FOV_FRACTION
    )
    assert max(abs(y) for _x, y in offsets) == pytest.approx(
        1200 * scale.pixel_size_y_mm * Main.LENS_DISTORTION_FOV_FRACTION
    )


def test_lens_distortion_capture_offsets_follow_affine_click_calibration() -> None:
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0002), (0.0003, -0.0011)),
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )

    offsets = Main._lens_distortion_capture_offsets_mm((1000, 800), scale)

    x_px = 1000 * Main.LENS_DISTORTION_FOV_FRACTION
    y_px = 800 * Main.LENS_DISTORTION_FOV_FRACTION
    expected_corner = Main._lens_distortion_pixel_shift_to_stage_offset_mm(
        scale,
        -x_px,
        -y_px,
    )
    assert expected_corner in offsets


def test_lens_distortion_capture_offsets_allow_structure_at_frame_edge(
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
        lambda _frame: (_ for _ in ()).throw(
            AssertionError("capture offsets inspected structure bounds")
        ),
        raising=False,
    )

    offsets = Main._lens_distortion_capture_offsets_mm(
        (1000, 800),
        scale,
        initial_frame=_FakeFrame(),
    )

    assert len(offsets) == 9
    assert offsets[0] == (0.0, 0.0)
    assert max(abs(x) for x, _y in offsets) == pytest.approx(350 * 0.001)
    assert max(abs(y) for _x, y in offsets) == pytest.approx(280 * 0.001)


def test_run_lens_distortion_calibration_captures_offset_grid(
    monkeypatch,
) -> None:
    window = Main.__new__(Main)
    stage = _FakeStage()
    finished: list[tuple[bool, str, object]] = []
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    expected_offsets = Main._lens_distortion_capture_offsets_mm(
        (1920, 1200),
        scale,
        initial_frame=_FakeFrame(),
    )
    raw_frames = [_FakeFrame() for _ in range(len(expected_offsets) + 1)]
    captured_offsets: list[tuple[float, float]] = []
    segmented_frames: list[object] = []
    masks: list[object] = []
    observations = (object(), object())
    before_preview = QImage(8, 6, QImage.Format_Grayscale8)
    after_preview = QImage(8, 6, QImage.Format_Grayscale8)

    def wait_for_frame(*, after_counter=None, timeout_s=2.0):
        assert after_counter in (None, 0)
        return raw_frames.pop(0), 1

    class _Fit:
        def to_payload(self) -> dict[str, object]:
            return _stage_geometry_payload(
                pixels_to_mm=[[-0.001, -0.0], [0.0, 0.001]],
                calibrated_pixels_to_mm=[[-0.001, -0.0], [0.0, 0.001]],
            )

    def segment(frame):
        stage.events.append(("segment", frame))
        segmented_frames.append(frame)
        mask = SimpleNamespace(frame_size=(1920, 1200), mask=object())
        masks.append(mask)
        return mask

    def build_observations(
        grid_frames,
        geometry_masks,
        *,
        frame_size,
        image_pixels_to_mm,
        match_gate_px,
    ):
        captured_offsets.extend(frame.stage_offset_mm for frame in grid_frames)
        assert tuple(geometry_masks) == tuple(masks)
        assert frame_size == (1920, 1200)
        assert image_pixels_to_mm == ((-0.001, -0.0), (0.0, 0.001))
        assert match_gate_px == 12.0
        return observations

    def fit_geometry(
        fit_observations,
        *,
        frame_size,
        initial_pixels_to_mm,
    ):
        assert tuple(fit_observations) == observations
        assert frame_size == (1920, 1200)
        assert initial_pixels_to_mm == ((-0.001, -0.0), (0.0, 0.001))
        return _Fit()

    def build_previews(raw_grid_frames, geometry_masks, initial_matrix, payload):
        assert tuple(raw_grid_frames[i].frame for i in range(9)) == tuple(
            segmented_frames
        )
        assert tuple(geometry_masks) == tuple(masks)
        assert initial_matrix == ((-0.001, 0.0), (0.0, -0.001))
        assert payload["calibrated_pixels_to_mm"] == [
            [-0.001, 0.0],
            [0.0, -0.001],
        ]
        return before_preview, after_preview

    window.stage_controller = stage
    window._optical_session_manager = _FakeSessionManager(stage.events)
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._flat_field_calibration_store = SimpleNamespace(
        load=lambda _objective: (_ for _ in ()).throw(
            AssertionError("lens calibration loaded flat-field data")
        )
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
    monkeypatch.setattr(
        main_module,
        "apply_flat_field_correction",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("lens calibration applied flat-field correction")
        ),
    )
    monkeypatch.setattr(geometry_mask, "segment_metal_geometry", segment)
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_feature_observations",
        build_observations,
    )
    monkeypatch.setattr(
        main_module,
        "fit_stage_geometry_from_observations",
        fit_geometry,
        raising=False,
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_alignment_previews",
        build_previews,
    )
    monkeypatch.setattr(main_module.time, "sleep", lambda _seconds: None)

    Main._run_lens_distortion_calibration(window)

    assert stage.events[:5] == [
        ("session_open", "lens distortion calibration", None),
        ("begin", "lens distortion calibration"),
        ("position",),
        ("camera_lock", True),
        ("needles", "raise", 71.0),
    ]
    capture_moves = [event for event in stage.events if event[0] == "move"][:-1]
    assert capture_moves[0] == ("move", 10.0, 20.0, 123.0)
    last_dx, last_dy = expected_offsets[-1]
    assert capture_moves[-1] == ("move", 10.0 + last_dx, 20.0 + last_dy, 123.0)
    restore_event = ("move", 10.0, 20.0, 123.0)
    restore_index = max(
        index for index, event in enumerate(stage.events) if event == restore_event
    )
    first_segment_index = next(
        index for index, event in enumerate(stage.events) if event[0] == "segment"
    )
    assert restore_index < first_segment_index
    assert stage.events[-3:] == [
        ("camera_restore", "lens-lock"),
        ("finish",),
        ("session_close",),
    ]
    assert captured_offsets == list(expected_offsets)
    assert len(segmented_frames) == len(expected_offsets) == 9
    assert finished[0][0] is True
    assert "raw geometry" in finished[0][1].lower()
    assert "0.50 px mean" in finished[0][1]
    assert "1.00 px max" in finished[0][1]
    assert "candidate" not in finished[0][1].lower()
    output = finished[0][2]
    assert isinstance(output, main_module._LensDistortionCalibrationOutput)
    assert output.before_preview is before_preview
    assert output.after_preview is after_preview
    assert output.payload["optical_session"]["fixed_exposure_us"] == 3200.0
    assert "token" not in repr(output.payload["optical_session"]).lower()
    assert "calibration_input" not in output.payload
    assert "calibration_candidates" not in output.payload


def test_lens_distortion_calibration_does_not_require_flat_field_before_motion() -> None:
    window = Main.__new__(Main)
    stage = _FakeStage()
    finished: list[tuple[bool, str, object]] = []
    window.stage_controller = stage
    window._optical_session_manager = _FakeSessionManager(stage.events)
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

    Main._run_lens_distortion_calibration(window)

    assert stage.events == [
        ("session_open", "lens distortion calibration", None),
        ("begin", "lens distortion calibration"),
        ("position",),
        ("finish",),
        ("session_close",),
    ]
    assert finished[0][0] is False
    assert "flat-field" not in finished[0][1].lower()


def test_lens_distortion_session_failure_does_not_reserve_or_move() -> None:
    stage = _FakeStage()
    finished: list[tuple[bool, str, object]] = []
    window = Main.__new__(Main)
    window.stage_controller = stage
    window._optical_session_manager = _FakeSessionManager(
        stage.events,
        open_error=RuntimeError("Exposure did not converge."),
    )
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._active_microscope_scale = lambda: SimpleNamespace(
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    window._flat_field_calibration_store = SimpleNamespace(
        load=lambda _objective: (_ for _ in ()).throw(
            AssertionError("lens calibration loaded flat-field data")
        )
    )
    window._emit_lens_distortion_finished = (
        lambda success, message, payload: finished.append((success, message, payload))
    )

    Main._run_lens_distortion_calibration(window, 120.0, 70.0)

    assert stage.events == [
        ("session_open", "lens distortion calibration", None),
    ]
    assert finished == [
        (
            False,
            "Lens distortion calibration failed: Exposure did not converge.",
            None,
        )
    ]


def test_lens_position_failure_releases_stage_and_session_without_camera_work() -> None:
    stage = _FakeStage(position_error=RuntimeError("position unavailable"))
    finished: list[tuple[bool, str, object]] = []
    window = Main.__new__(Main)
    window.stage_controller = stage
    window._optical_session_manager = _FakeSessionManager(stage.events)
    window._active_objective_metadata = lambda: ("X20", 20.0)
    window._active_microscope_scale = lambda: SimpleNamespace(
        pixel_size_x_mm=0.001,
        pixel_size_y_mm=0.001,
    )
    window._wait_for_raw_camera_frame = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("position failure reached camera work")
    )
    window._emit_lens_distortion_finished = (
        lambda success, message, payload: finished.append((success, message, payload))
    )

    Main._run_lens_distortion_calibration(window, 120.0, 70.0)

    assert stage.events == [
        ("session_open", "lens distortion calibration", None),
        ("begin", "lens distortion calibration"),
        ("position",),
        ("finish",),
        ("session_close",),
    ]
    assert finished == [
        (
            False,
            "Lens distortion calibration failed: position unavailable",
            None,
        )
    ]


def test_completed_lens_artifact_is_saved_when_session_restore_fails() -> None:
    window = Main.__new__(Main)
    context = main_module._OpticalCalibrationRunContext(
        operation_id="lens-restore-warning",
        wizard_run_id=61,
        objective_name="X20",
    )
    output = _lens_output(
        _stage_geometry_payload(),
        session_restore_error="native Continuous restore failed",
    )
    saved: list[tuple[dict[str, object], str]] = []
    wizard_results: list[tuple[bool, str, int | None]] = []
    window._lens_distortion_thread = None
    window._lens_distortion_context = context
    window._lens_distortion_dialog = None
    window._optical_calibration_wizard = SimpleNamespace(
        set_lens_distortion_result=lambda success, message, *, run_id=None, **_kwargs: (
            wizard_results.append((bool(success), str(message), run_id))
        )
    )
    window._save_objective_distortion = (
        lambda payload, objective, **_kwargs: saved.append((payload, objective))
    )
    window._lens_distortion_payload_invalidates_click_calibration = lambda _payload: False
    window._show_status = lambda *_args: None

    Main._on_lens_distortion_calibration_finished(
        window,
        context,
        False,
        "Lens distortion calibration complete, but exposure policy restore failed.",
        output,
    )

    assert saved == [(output.payload, "X20")]
    assert wizard_results == [
        (
            False,
            "Lens distortion calibration complete, but exposure policy restore failed.",
            61,
        )
    ]


def test_fit_lens_distortion_output_uses_raw_masks_and_image_coordinate_matrix(
    monkeypatch,
) -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0002), (0.0003, -0.0011)),
    )
    masks = (SimpleNamespace(mask="mask-0"), SimpleNamespace(mask="mask-1"))
    observations = (object(), object())
    before_preview = QImage(5, 4, QImage.Format_Grayscale8)
    after_preview = QImage(5, 4, QImage.Format_Grayscale8)
    calls: list[tuple[object, ...]] = []

    class _Fit:
        def to_payload(self) -> dict[str, object]:
            return _stage_geometry_payload(
                pixels_to_mm=[[-0.0009, -0.0004], [0.0002, 0.0012]],
                calibrated_pixels_to_mm=[
                    [-0.0009, -0.0004],
                    [0.0002, 0.0012],
                ],
            )

    def segment(frame):
        index = len([call for call in calls if call[0] == "segment"])
        calls.append(("segment", frame))
        return masks[index]

    def build_observations(
        grid_frames,
        geometry_masks,
        *,
        frame_size,
        image_pixels_to_mm,
        match_gate_px,
    ):
        calls.append(
            (
                "observations",
                tuple(grid_frames),
                tuple(geometry_masks),
                frame_size,
                image_pixels_to_mm,
                match_gate_px,
            )
        )
        return observations

    def fit_geometry(
        fit_observations,
        *,
        frame_size,
        initial_pixels_to_mm,
    ):
        calls.append(
            ("fit", tuple(fit_observations), frame_size, initial_pixels_to_mm)
        )
        return _Fit()

    def build_previews(raw_frames, geometry_masks, initial_matrix, payload):
        calls.append(
            (
                "previews",
                tuple(raw_frames),
                tuple(geometry_masks),
                initial_matrix,
                payload,
            )
        )
        return before_preview, after_preview

    monkeypatch.setattr(geometry_mask, "segment_metal_geometry", segment)
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_feature_observations",
        build_observations,
    )
    monkeypatch.setattr(
        main_module,
        "fit_stage_geometry_from_observations",
        fit_geometry,
        raising=False,
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_alignment_previews",
        build_previews,
    )

    output = Main._fit_lens_distortion_output(
        frames,
        frame_size=(1920, 1200),
        scale=scale,
    )

    assert output.payload["model_type"] == "stage_geometry"
    assert output.payload["pixels_to_mm"] == [
        [-0.0009, 0.0004],
        [0.0002, -0.0012],
    ]
    assert output.payload["calibrated_pixels_to_mm"] == [
        [-0.0009, 0.0004],
        [0.0002, -0.0012],
    ]
    assert output.payload["feature_count"] == 8
    assert output.payload["observation_count"] == 24
    assert output.before_preview is before_preview
    assert output.after_preview is after_preview
    assert calls[:4] == [
        ("segment", frames[0].frame),
        ("segment", frames[1].frame),
        (
            "observations",
            tuple(frames),
            masks,
            (1920, 1200),
            ((-0.001, -0.0002), (0.0003, 0.0011)),
            12.0,
        ),
        (
            "fit",
            observations,
            (1920, 1200),
            ((-0.001, -0.0002), (0.0003, 0.0011)),
        ),
    ]
    preview_call = calls[4]
    assert preview_call[:4] == (
        "previews",
        tuple(frames),
        masks,
        ((-0.001, 0.0002), (0.0003, -0.0011)),
    )
    assert preview_call[4] is output.payload


def test_fit_lens_distortion_output_fails_when_preview_generation_fails(
    monkeypatch,
) -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    scale = SimpleNamespace(pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)))

    class _Fit:
        def to_payload(self) -> dict[str, object]:
            return _stage_geometry_payload(
                pixels_to_mm=[[-0.001, 0.0], [0.0, 0.001]],
                calibrated_pixels_to_mm=[[-0.001, 0.0], [0.0, 0.001]],
            )

    monkeypatch.setattr(
        geometry_mask,
        "segment_metal_geometry",
        lambda _frame: object(),
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_feature_observations",
        lambda *_args, **_kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        main_module,
        "fit_stage_geometry_from_observations",
        lambda *_args, **_kwargs: _Fit(),
        raising=False,
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_alignment_previews",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("preview canvas is empty")
        ),
    )

    with pytest.raises(ValueError, match="preview canvas is empty"):
        Main._fit_lens_distortion_output(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )


def test_fit_lens_distortion_output_raises_when_stage_geometry_errors(
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

    monkeypatch.setattr(
        geometry_mask, "segment_metal_geometry", lambda _frame: object()
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_feature_observations",
        lambda *_args, **_kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        main_module,
        "fit_stage_geometry_from_observations",
        fit_geometry,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="scipy unavailable"):
        Main._fit_lens_distortion_output(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )


def test_fit_lens_distortion_output_rejects_high_residual(monkeypatch) -> None:
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
                "baseline_residual_mean_px": 0.8,
                "baseline_residual_max_px": 1.4,
                "residual_mean_px": 5.2,
                "residual_max_px": 17.4,
            }

    def fit_geometry(*_args, **_kwargs):
        return _Fit()

    monkeypatch.setattr(
        geometry_mask, "segment_metal_geometry", lambda _frame: object()
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_feature_observations",
        lambda *_args, **_kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        main_module,
        "fit_stage_geometry_from_observations",
        fit_geometry,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="residual is too high"):
        Main._fit_lens_distortion_output(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )


@pytest.mark.parametrize(
    "payload",
    (
        {
            "residual_mean_px": 0.5,
            "residual_max_px": 1.0,
        },
        {
            "model_type": "unsupported",
            "residual_mean_px": 0.5,
            "residual_max_px": 1.0,
        },
    ),
    ids=("missing-model-type", "unsupported-model-type"),
)
def test_fit_lens_distortion_output_rejects_unsupported_model_type_before_preview(
    monkeypatch,
    payload,
) -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
    )
    preview_calls: list[object] = []

    class _Fit:
        def to_payload(self) -> dict[str, object]:
            return dict(payload)

    monkeypatch.setattr(
        geometry_mask, "segment_metal_geometry", lambda _frame: object()
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_feature_observations",
        lambda *_args, **_kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        main_module,
        "fit_stage_geometry_from_observations",
        lambda *_args, **_kwargs: _Fit(),
        raising=False,
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_alignment_previews",
        lambda *_args, **_kwargs: (
            preview_calls.append(True)
            or (
                QImage(2, 2, QImage.Format_Grayscale8),
                QImage(2, 2, QImage.Format_Grayscale8),
            )
        ),
    )

    with pytest.raises(RuntimeError, match="model_type"):
        Main._fit_lens_distortion_output(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )

    assert preview_calls == []


@pytest.mark.parametrize(
    "payload",
    (
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_max_px": 1.0,
        },
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_mean_px": 0.5,
        },
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_mean_px": "not-a-number",
            "residual_max_px": 1.0,
        },
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_mean_px": 0.5,
            "residual_max_px": "not-a-number",
        },
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_mean_px": float("nan"),
            "residual_max_px": 1.0,
        },
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_mean_px": 0.5,
            "residual_max_px": float("inf"),
        },
    ),
    ids=(
        "missing-mean",
        "missing-max",
        "non-numeric-mean",
        "non-numeric-max",
        "nonfinite-mean",
        "nonfinite-max",
    ),
)
def test_fit_lens_distortion_output_rejects_invalid_residual_metrics_before_preview(
    monkeypatch,
    payload,
) -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
        main_module.GridCalibrationFrame(_FakeFrame(), (0.1, 0.0)),
    ]
    scale = SimpleNamespace(
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
    )
    preview_calls: list[object] = []

    class _Fit:
        def to_payload(self) -> dict[str, object]:
            return dict(payload)

    monkeypatch.setattr(
        geometry_mask, "segment_metal_geometry", lambda _frame: object()
    )
    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_feature_observations",
        lambda *_args, **_kwargs: (object(), object()),
    )
    monkeypatch.setattr(
        main_module,
        "fit_stage_geometry_from_observations",
        lambda *_args, **_kwargs: _Fit(),
        raising=False,
    )

    def build_previews(*_args, **_kwargs):
        preview_calls.append(True)
        return QImage(2, 2, QImage.Format_Grayscale8), QImage(
            2, 2, QImage.Format_Grayscale8
        )

    monkeypatch.setattr(
        geometry_mask,
        "build_geometry_alignment_previews",
        build_previews,
    )

    with pytest.raises(RuntimeError, match="residual"):
        Main._fit_lens_distortion_output(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )

    assert preview_calls == []


@pytest.mark.parametrize(
    "payload",
    (
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_max_px": 1.0,
        },
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_mean_px": "not-a-number",
            "residual_max_px": 1.0,
        },
        {
            "model_type": "stage_geometry",
            "baseline_residual_mean_px": 0.8,
            "baseline_residual_max_px": 1.4,
            "residual_mean_px": 0.5,
            "residual_max_px": float("nan"),
        },
    ),
)
def test_lens_distortion_success_message_rejects_invalid_metrics(payload) -> None:
    with pytest.raises(RuntimeError, match="residual"):
        Main._lens_distortion_fit_success_message(payload)


@pytest.mark.parametrize(
    "field, value, missing",
    (
        ("baseline_residual_mean_px", None, True),
        ("baseline_residual_mean_px", True, False),
        ("baseline_residual_mean_px", float("nan"), False),
        ("baseline_residual_mean_px", float("inf"), False),
        ("baseline_residual_max_px", None, True),
        ("baseline_residual_max_px", True, False),
        ("baseline_residual_max_px", float("nan"), False),
        ("baseline_residual_max_px", float("inf"), False),
    ),
    ids=(
        "missing-baseline-mean",
        "boolean-baseline-mean",
        "nan-baseline-mean",
        "infinite-baseline-mean",
        "missing-baseline-max",
        "boolean-baseline-max",
        "nan-baseline-max",
        "infinite-baseline-max",
    ),
)
def test_lens_distortion_completion_rejects_invalid_baseline_metrics_without_save(
    field,
    value,
    missing,
) -> None:
    payload = _stage_geometry_payload()
    if missing:
        payload.pop(field)
    else:
        payload[field] = value

    with pytest.raises(RuntimeError, match=field):
        Main._validate_lens_distortion_fit_payload(payload)

    window = Main.__new__(Main)
    saved: list[object] = []
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_dialog = None
    window._save_active_objective_distortion = lambda *args: saved.append(args)
    window._show_status = lambda *_args: None

    Main._on_lens_distortion_calibration_finished(
        window, True, "done", _lens_output(payload)
    )

    assert saved == []


def test_lens_distortion_validation_allows_high_finite_baseline_metrics() -> None:
    payload = _stage_geometry_payload(
        baseline_residual_mean_px=1e300,
        baseline_residual_max_px=1e300,
        residual_mean_px=0.2,
        residual_max_px=0.4,
    )

    Main._validate_lens_distortion_fit_payload(payload)


@pytest.mark.parametrize(
    "field",
    (
        "baseline_residual_mean_px",
        "baseline_residual_max_px",
        "residual_mean_px",
        "residual_max_px",
    ),
)
def test_lens_distortion_validation_rejects_negative_residual_metrics(
    field: str,
) -> None:
    payload = _stage_geometry_payload(**{field: -0.01})

    with pytest.raises(RuntimeError, match=field):
        Main._validate_lens_distortion_fit_payload(payload)


def test_negative_baseline_metric_does_not_save_or_report_wizard_success() -> None:
    context = main_module._OpticalCalibrationRunContext(
        operation_id="negative-baseline",
        wizard_run_id=52,
        objective_name="X20",
    )
    payload = _stage_geometry_payload(baseline_residual_mean_px=-0.01)
    window = Main.__new__(Main)
    saved: list[object] = []
    wizard_calls: list[tuple[object, ...]] = []
    window._lens_distortion_context = context
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_dialog = None
    window._optical_calibration_context_is_current = (
        lambda kind, candidate: kind == "lens" and candidate is context
    )
    window._save_objective_distortion = lambda *args: saved.append(args)
    window._optical_calibration_wizard = SimpleNamespace(
        set_lens_distortion_result=lambda *args, **kwargs: wizard_calls.append(
            (*args, kwargs)
        )
    )
    window._show_status = lambda *_args: None

    Main._on_lens_distortion_calibration_finished(
        window,
        context,
        True,
        "done",
        _lens_output(payload),
    )

    assert saved == []
    assert wizard_calls[0][0] is False
    assert wizard_calls[0][-1] == {"run_id": 52}


def test_fit_lens_distortion_output_requires_click_calibration() -> None:
    frames = [
        main_module.GridCalibrationFrame(_FakeFrame(), (0.0, 0.0)),
    ]
    scale = SimpleNamespace()

    with pytest.raises(RuntimeError, match="requires click-to-move calibration"):
        Main._fit_lens_distortion_output(
            frames,
            frame_size=(1920, 1200),
            scale=scale,
        )


def test_lens_distortion_finished_rejects_structurally_incomplete_payload() -> None:
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
    payload = {
        "model_version": 1,
        "model_type": "stage_geometry",
        "frame_size": [640, 480],
        "baseline_residual_mean_px": 0.8,
        "baseline_residual_max_px": 1.4,
        "residual_mean_px": 0.2,
        "residual_max_px": 0.4,
    }

    Main._on_lens_distortion_calibration_finished(
        window, True, "done", _lens_output(payload)
    )

    profile = manager.settings.objectives.objectives["X50"]
    assert profile.distortion_correction_configured is False
    assert profile.distortion_correction == {}
    assert profile.xy_calibration_configured is True
    assert profile.pixels_to_mm == [[1.0, 0.0], [0.0, 1.0]]
    assert manager.saved_count == 0
    assert apply_calls == []
    assert refresh_calls == []
    assert dialog.running == [False]
    assert len(statuses) == 1
    assert statuses[0][0].startswith("Lens distortion calibration save failed:")
    assert statuses[0][1] == 8000


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
    payload = _stage_geometry_payload(
        frame_size=[640, 480],
        pixels_to_mm=matrix,
        calibrated_pixels_to_mm=matrix,
        center_px=[320.0, 240.0],
        residual_mean_px=0.2,
        residual_max_px=0.4,
    )

    Main._on_lens_distortion_calibration_finished(
        window, True, "done", _lens_output(payload)
    )

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
        wizard_run_id=27,
        objective_name="X20",
    )
    matrix = [[-0.000117, 0.0], [0.0, -0.000117]]
    payload = _stage_geometry_payload(
        frame_size=[640, 480],
        pixels_to_mm=matrix,
        calibrated_pixels_to_mm=matrix,
        center_px=[320.0, 240.0],
        residual_mean_px=0.2,
        residual_max_px=0.4,
    )
    window.settings_manager = manager
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_context = captured_context
    window._lens_distortion_dialog = None
    wizard_calls: list[tuple[object, ...]] = []
    window._optical_calibration_wizard = SimpleNamespace(
        set_lens_distortion_result=lambda success, message, **kwargs: wizard_calls.append(
            (success, message, kwargs)
        )
    )
    window._apply_objective_settings = lambda: None
    window._refresh_objective_calibration_ui = lambda: None
    window._show_status = lambda *_args: None

    before_preview = QImage(7, 5, QImage.Format_RGB888)
    after_preview = QImage(7, 5, QImage.Format_RGB888)
    output = _lens_output(
        payload,
        before_preview=before_preview,
        after_preview=after_preview,
    )

    Main._on_lens_distortion_calibration_finished(
        window,
        captured_context,
        True,
        "done",
        output,
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
    assert wizard_calls == [
        (
            True,
            "done",
            {
                "run_id": 27,
                "before_preview": before_preview,
                "after_preview": after_preview,
                "without_calibration_metrics": (0.8, 1.4),
                "with_calibration_metrics": (0.2, 0.4),
            },
        )
    ]


def test_stale_lens_distortion_completion_saves_and_displays_nothing() -> None:
    window = Main.__new__(Main)
    stale_context = main_module._OpticalCalibrationRunContext(
        operation_id="stale-lens",
        wizard_run_id=31,
        objective_name="X20",
    )
    current_context = main_module._OpticalCalibrationRunContext(
        operation_id="current-lens",
        wizard_run_id=32,
        objective_name="X50",
    )
    saved: list[object] = []
    displayed: list[object] = []
    statuses: list[object] = []
    window._lens_distortion_context = current_context
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_dialog = None
    window._optical_calibration_wizard = SimpleNamespace(
        set_lens_distortion_result=lambda *args, **kwargs: displayed.append(
            (args, kwargs)
        )
    )
    window._save_objective_distortion = lambda *args: saved.append(args)
    window._show_status = lambda *args: statuses.append(args)

    Main._on_lens_distortion_calibration_finished(
        window,
        stale_context,
        True,
        "stale done",
        _lens_output({"model_version": 1}),
    )

    assert saved == []
    assert displayed == []
    assert statuses == []
    assert window._lens_distortion_context is current_context


@pytest.mark.parametrize(
    "output",
    (
        _lens_output(
            {
                "model_type": "stage_geometry",
                "residual_mean_px": 0.5,
            }
        ),
        {
            "model_type": "stage_geometry",
            "residual_mean_px": 0.5,
            "residual_max_px": 1.0,
        },
        main_module._LensDistortionCalibrationOutput(
            payload={
                "model_type": "stage_geometry",
                "residual_mean_px": 0.5,
                "residual_max_px": 1.0,
            },
            before_preview=None,
            after_preview=QImage(4, 3, QImage.Format_Grayscale8),
        ),
    ),
    ids=("malformed-payload", "bare-dict", "malformed-previews"),
)
def test_malformed_lens_distortion_completion_is_rejected_without_save_or_previews(
    output,
) -> None:
    window = Main.__new__(Main)
    context = main_module._OpticalCalibrationRunContext(
        operation_id="malformed-lens",
        wizard_run_id=44,
        objective_name="X20",
    )
    saved: list[object] = []
    wizard_calls: list[tuple[object, ...]] = []
    statuses: list[tuple[object, ...]] = []
    window._lens_distortion_context = context
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_dialog = None
    window._optical_calibration_wizard = SimpleNamespace(
        set_lens_distortion_result=lambda *args, **kwargs: wizard_calls.append(
            (args, kwargs)
        )
    )
    window._save_objective_distortion = lambda *args: saved.append(args)
    window._show_status = lambda *args: statuses.append(args)

    Main._on_lens_distortion_calibration_finished(
        window,
        context,
        True,
        "done",
        output,
    )

    assert saved == []
    assert len(wizard_calls) == 1
    wizard_args, wizard_kwargs = wizard_calls[0]
    assert wizard_args[0] is False
    assert wizard_kwargs == {"run_id": 44}
    assert statuses[0][1] == 8000
    assert statuses[0][0] == wizard_args[1]


@pytest.mark.parametrize(
    "payload",
    (
        {
            "residual_mean_px": 0.5,
            "residual_max_px": 1.0,
        },
        {
            "model_type": "unsupported",
            "residual_mean_px": 0.5,
            "residual_max_px": 1.0,
        },
    ),
    ids=("missing-model-type", "unsupported-model-type"),
)
def test_successful_lens_distortion_completion_rejects_unsupported_model_payload(
    payload,
) -> None:
    window = Main.__new__(Main)
    context = main_module._OpticalCalibrationRunContext(
        operation_id="unsupported-model-lens",
        wizard_run_id=45,
        objective_name="X20",
    )
    saved: list[object] = []
    wizard_calls: list[tuple[object, ...]] = []
    statuses: list[tuple[object, ...]] = []
    window._lens_distortion_context = context
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_dialog = None
    window._optical_calibration_wizard = SimpleNamespace(
        set_lens_distortion_result=lambda *args, **kwargs: wizard_calls.append(
            (args, kwargs)
        )
    )
    window._save_objective_distortion = lambda *args: saved.append(args)
    window._show_status = lambda *args: statuses.append(args)

    Main._on_lens_distortion_calibration_finished(
        window,
        context,
        True,
        "done",
        _lens_output(payload),
    )

    assert saved == []
    assert len(wizard_calls) == 1
    wizard_args, wizard_kwargs = wizard_calls[0]
    assert wizard_args[0] is False
    assert wizard_kwargs == {"run_id": 45}
    assert statuses[0][1] == 8000
    assert statuses[0][0] == wizard_args[1]


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


def test_late_lens_completion_does_not_mutate_active_correction_during_scan() -> None:
    window = Main.__new__(Main)
    manager = _FakeSettingsManager()
    context = main_module._OpticalCalibrationRunContext(
        operation_id="late-lens",
        wizard_run_id=None,
        objective_name="X20",
    )
    original_payload = {"model_version": 1, "camera_matrix": [[20.0]]}
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                magnification=20.0,
                pixels_to_mm=[[0.01, 0.0], [0.0, 0.01]],
                xy_calibration_configured=True,
                distortion_correction=original_payload,
                distortion_correction_configured=True,
            )
        },
    )
    dialog = _FakeDialog()
    statuses: list[str] = []
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(is_busy=lambda: False)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_context = context
    window._microscope_scan_thread = SimpleNamespace(is_alive=lambda: True)
    window._lens_distortion_dialog = dialog
    window._optical_calibration_wizard = None
    window._apply_objective_settings = lambda: pytest.fail(
        "late lens completion applied active correction"
    )
    window._refresh_objective_calibration_ui = lambda: None
    window._show_status = lambda message, _timeout=0: statuses.append(str(message))

    Main._on_lens_distortion_calibration_finished(
        window,
        context,
        True,
        "Lens distortion calibration saved.",
        _lens_output(_stage_geometry_payload()),
    )

    profile = manager.settings.objectives.objectives["X20"]
    assert manager.saved_count == 0
    assert profile.distortion_correction == original_payload
    assert window._lens_distortion_context is None
    assert "save failed" in dialog.statuses[-1].lower()
    assert "scan" in dialog.statuses[-1].lower()
    assert statuses[-1] == dialog.statuses[-1]


def test_cancelled_wizard_lens_completion_does_not_save_correction() -> None:
    window = Main.__new__(Main)
    manager = _FakeSettingsManager()
    context = main_module._OpticalCalibrationRunContext(
        operation_id="cancelled-lens",
        wizard_run_id=73,
        objective_name="X20",
    )
    original_payload = {"model_version": 1, "camera_matrix": [[20.0]]}
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                distortion_correction=original_payload,
                distortion_correction_configured=True,
            )
        },
    )
    wizard_results: list[tuple[bool, str, int | None]] = []
    dialog = _FakeDialog()
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(is_busy=lambda: False)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = _DeadThread()
    window._lens_distortion_context = context
    window._microscope_scan_thread = None
    window._lens_distortion_dialog = dialog
    window._optical_calibration_wizard = SimpleNamespace(
        set_lens_distortion_result=lambda success, message, *, run_id=None, **_kwargs: (
            wizard_results.append((bool(success), str(message), run_id))
        )
    )
    window._apply_objective_settings = lambda: pytest.fail(
        "cancelled lens completion applied active correction"
    )
    window._refresh_objective_calibration_ui = lambda: None
    window._show_status = lambda *_args: None

    Main._cancel_optical_calibration_wizard(window, 73)
    Main._on_lens_distortion_calibration_finished(
        window,
        context,
        True,
        "Lens distortion calibration saved.",
        _lens_output(_stage_geometry_payload()),
    )

    profile = manager.settings.objectives.objectives["X20"]
    assert manager.saved_count == 0
    assert profile.distortion_correction == original_payload
    assert wizard_results[0][0] is False
    assert wizard_results[0][2] == 73
    assert "save failed" in wizard_results[0][1].lower()
    assert window._lens_distortion_context is None


@pytest.mark.parametrize("blocker", ("scan", "calibration"))
def test_click_calibration_active_update_is_rejected_after_new_operation_starts(
    blocker: str,
) -> None:
    window = Main.__new__(Main)
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
    window.stage_controller = SimpleNamespace(
        is_busy=lambda: True,
        apply_objective_configuration=lambda objective, _objectives: restored.append(
            objective.pixels_to_mm
        ),
    )
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = (
        main_module._OpticalCalibrationRunContext(
            operation_id="new-lens",
            wizard_run_id=None,
            objective_name="X20",
        )
        if blocker == "calibration"
        else None
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
    )

    assert persisted == []
    assert manager.settings.objectives.objectives["X20"].pixels_to_mm == newer_matrix
    assert restored == [newer_matrix]
    assert statuses == [
        "Click-to-move calibration result ignored because a scan or calibration is active."
    ]


def test_click_calibration_active_update_allows_own_stage_task() -> None:
    window = Main.__new__(Main)
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X20",
        objectives={"X20": ObjectiveCalibrationSettings(name="X20")},
    )
    persisted: list[object] = []
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(is_busy=lambda: True)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = None
    window._persist_objective_plan = lambda plan: persisted.append(plan)
    window._show_status = lambda *_args: None

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
    )

    assert len(persisted) == 1


def test_click_calibration_inactive_update_is_allowed_during_scan() -> None:
    window = Main.__new__(Main)
    manager = _FakeSettingsManager()
    manager.settings.objectives = ObjectivesSettings(
        active_name="X5",
        objectives={
            "X5": ObjectiveCalibrationSettings(name="X5"),
            "X20": ObjectiveCalibrationSettings(name="X20"),
        },
    )
    persisted: list[object] = []
    window.settings_manager = manager
    window.stage_controller = SimpleNamespace(is_busy=lambda: True)
    window._flat_field_calibration_thread = None
    window._flat_field_calibration_context = None
    window._lens_distortion_thread = None
    window._lens_distortion_context = None
    window._microscope_scan_thread = SimpleNamespace(is_alive=lambda: True)
    window._persist_objective_plan = lambda plan: persisted.append(plan)
    window._show_status = lambda *_args: None

    Main._on_objective_calibration_updated(
        window,
        "X20",
        [[0.02, 0.0], [0.0, 0.02]],
    )

    assert len(persisted) == 1
    updated = persisted[0].settings.objectives.objectives["X20"]
    assert updated.pixels_to_mm == [[0.02, 0.0], [0.0, 0.02]]


@pytest.mark.parametrize("blocker", ("scan", "calibration"))
def test_api_lens_reset_returns_conflict_during_active_operation(
    blocker: str,
) -> None:
    window = Main.__new__(Main)
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
    window._lens_distortion_context = (
        main_module._OpticalCalibrationRunContext(
            operation_id="starting-lens",
            wizard_run_id=None,
            objective_name="X20",
        )
        if blocker == "calibration"
        else None
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


def test_api_force_click_reset_returns_conflict_during_scan_startup() -> None:
    window = Main.__new__(Main)
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
    window._lens_distortion_context = main_module._OpticalCalibrationRunContext(
        operation_id="starting-lens",
        wizard_run_id=None,
        objective_name="X20",
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
