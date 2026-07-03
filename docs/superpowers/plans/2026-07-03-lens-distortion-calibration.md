# Lens Distortion Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-objective lens distortion calibration that straightens live microscope frames and all downstream image consumers.

**Architecture:** Keep the calibration math in a pure `probe_station_gui.camera.distortion` module, persist validated correction payloads on objective profiles, and apply correction once at the main camera-frame boundary. The GUI dialog starts a background capture runner that uses direct stage-controller methods and latest raw frames.

**Tech Stack:** Python 3.11, PySide6, numpy, OpenCV (`cv2`), pytest.

## Global Constraints

- Use the shared project virtual environment: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest ...`.
- Do not route in-process GUI features through the app's own localhost API.
- Keep startup lightweight; distortion maps must be built lazily.
- Keep hardware I/O and image processing out of the GUI thread.
- Use direct controller methods, Qt signals, or narrow callbacks inside the process.
- If no correction is configured, current camera behavior must remain unchanged.
- Reset or save of distortion correction must invalidate the objective's click-to-move calibration.

---

## File Structure

- Create `probe_station_gui/camera/distortion.py`: pure dataclasses, settings payload validation, synthetic-friendly grid detection, correction map building, and QImage correction helpers.
- Create `probe_station_gui/dialogs/lens_distortion_dialog.py`: small objective-aware dialog with status and Calibrate/Reset/Close actions.
- Modify `probe_station_gui/settings/objective_config.py`: add correction payload fields, clone/to_dict/parse validation.
- Modify `probe_station_gui/dialogs/settings/objectives.py`: show distortion status near click calibration status.
- Modify `main.py`: own active correction state/cache, run calibration worker, apply corrected frames before view/stage/latest storage, persist/reset correction.
- Modify `probe_station_gui/views/main_window_menus.py`: add menu action.
- Add tests in `tests/camera/test_distortion.py`, `tests/settings/test_objective_config.py`, `tests/ui/test_settings_objectives_coordinates.py`, and a focused main pipeline test.

### Task 1: Persist Distortion Payloads

**Files:**
- Modify: `probe_station_gui/settings/objective_config.py`
- Test: `tests/settings/test_objective_config.py`

**Interfaces:**
- Produces: `parse_distortion_correction_payload(raw: object) -> dict[str, object]`
- Produces fields on `ObjectiveCalibrationSettings`: `distortion_correction: dict[str, object]`, `distortion_correction_configured: bool`

- [ ] **Step 1: Write the failing tests**

```python
def test_objective_profile_clone_copies_distortion_payload() -> None:
    profile = ObjectiveCalibrationSettings(
        name="X50",
        distortion_correction={"model_version": 1, "frame_size": [640, 480]},
        distortion_correction_configured=True,
    )
    restored = profile.clone()
    restored.distortion_correction["frame_size"][0] = 320
    assert profile.distortion_correction["frame_size"] == [640, 480]
    assert restored.distortion_correction["frame_size"] == [320, 480]


def test_parse_objectives_preserves_valid_distortion_payload() -> None:
    parsed = parse_objectives_settings({
        "active_name": "X50",
        "objectives": {
            "X50": {
                "distortion_correction_configured": True,
                "distortion_correction": {
                    "model_version": 1,
                    "frame_size": [640, 480],
                    "points": [[[10.0, 20.0], [12.0, 22.0]]],
                    "residual_mean_px": 0.2,
                },
            }
        },
    })
    profile = parsed.objectives["X50"]
    assert profile.distortion_correction_configured
    assert profile.distortion_correction["frame_size"] == [640, 480]
```

- [ ] **Step 2: Verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_objective_config.py -q`
Expected: FAIL because the settings dataclass has no distortion fields.

- [ ] **Step 3: Implement settings parsing**

Add the fields to `ObjectiveCalibrationSettings`, deep-copy them in `clone`, include them in `to_dict`, and parse only dict payloads with positive integer `frame_size` and integer `model_version`. Invalid payloads return `{}` and force configured false.

- [ ] **Step 4: Verify GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_objective_config.py -q`
Expected: PASS.

### Task 2: Pure Distortion Model And Remap

**Files:**
- Create: `probe_station_gui/camera/distortion.py`
- Test: `tests/camera/test_distortion.py`

**Interfaces:**
- Consumes: validated payload dict from Task 1.
- Produces: `DistortionCorrection`, `distortion_payload_from_points`, `correction_from_payload`, `apply_distortion_correction`

- [ ] **Step 1: Write failing tests**

```python
def test_apply_distortion_correction_keeps_image_size(qt_app) -> None:
    image = QImage(80, 60, QImage.Format_RGB32)
    image.fill(QColor("black"))
    payload = distortion_payload_from_points(
        frame_size=(80, 60),
        source_points=[(10.0, 10.0), (70.0, 10.0), (10.0, 50.0), (70.0, 50.0)],
        target_points=[(12.0, 11.0), (68.0, 10.0), (11.0, 48.0), (69.0, 49.0)],
        grid_spacing_um=50.0,
    )
    corrected = apply_distortion_correction(image, correction_from_payload(payload))
    assert corrected.width() == 80
    assert corrected.height() == 60
```

- [ ] **Step 2: Verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_distortion.py -q`
Expected: FAIL with import error for `probe_station_gui.camera.distortion`.

- [ ] **Step 3: Implement minimal remap**

Use OpenCV `findHomography` plus `warpPerspective` for the first pass. Keep the public payload shape compatible with a later denser remap by storing point pairs and `model_version`.

- [ ] **Step 4: Verify GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_distortion.py -q`
Expected: PASS.

### Task 3: Bright Grid Detection And Multi-Frame Fit

**Files:**
- Modify: `probe_station_gui/camera/distortion.py`
- Test: `tests/camera/test_distortion.py`

**Interfaces:**
- Produces: `detect_bright_grid(frame: QImage | np.ndarray) -> GridDetection`
- Produces: `fit_distortion_from_grid_frames(frames: Sequence[GridCalibrationFrame], frame_size: tuple[int, int]) -> dict[str, object]`

- [ ] **Step 1: Write failing synthetic tests**

```python
def test_partial_grid_frames_fit_distortion_payload() -> None:
    frames = synthetic_partial_grid_frames(
        frame_size=(320, 240),
        grid_spacing_px=70,
        stage_offsets_mm=[(0.0, 0.0), (0.05, 0.0), (-0.05, 0.0), (0.0, 0.05), (0.0, -0.05)],
    )
    payload = fit_distortion_from_grid_frames(frames, frame_size=(320, 240))
    assert payload["model_version"] == 1
    assert payload["frame_size"] == [320, 240]
    assert payload["residual_max_px"] < 3.0
    assert len(payload["source_points"]) >= 8
```

- [ ] **Step 2: Verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_distortion.py::test_partial_grid_frames_fit_distortion_payload -q`
Expected: FAIL because detection/fit functions do not exist.

- [ ] **Step 3: Implement detector and fitter**

Detect bright grid lines by converting to grayscale, applying background normalization, thresholding bright pixels, summing projections along X/Y, finding peak centers, and pairing visible vertical/horizontal line centers. Convert stage offsets to expected straight-grid coordinates using 0.05 mm spacing. Reject fewer than 8 source/target pairs or degenerate homography.

- [ ] **Step 4: Verify GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_distortion.py -q`
Expected: PASS.

### Task 4: Apply Corrected Frames In Main Pipeline

**Files:**
- Modify: `main.py`
- Test: a focused test in `tests/app/test_main_camera_distortion.py`

**Interfaces:**
- Consumes: `correction_from_payload`, `apply_distortion_correction`
- Produces: `Main._correct_camera_frame_for_active_objective(qimg: QImage) -> QImage`

- [ ] **Step 1: Write failing pipeline test**

```python
def test_camera_frame_pipeline_sends_corrected_frame_to_view_and_stage(qt_app) -> None:
    window = object.__new__(Main)
    window._last_camera_frame_ui_timestamp = None
    window.CAMERA_UI_FRAME_GAP_WARNING_S = 999.0
    window._latest_camera_frame_condition = threading.Condition()
    window._latest_camera_frame = None
    window._latest_camera_frame_counter = 0
    window._latest_camera_frame_for_notifications = None
    window._correct_camera_frame_for_active_objective = lambda frame: tagged_copy(frame, "corrected")
    window.view = FakeView()
    window.stage_controller = FakeStageController()
    Main._on_camera_frame(window, source_image("raw"))
    assert window._latest_camera_frame.text("tag") == "corrected"
    assert window._latest_camera_frame_for_notifications.text("tag") == "corrected"
    assert window.view.frames[-1].text("tag") == "corrected"
    assert window.stage_controller.frames[-1].text("tag") == "corrected"
```

- [ ] **Step 2: Verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_camera_distortion.py -q`
Expected: FAIL because `_on_camera_frame` does not call the correction hook or stage controller.

- [ ] **Step 3: Implement pipeline change**

Remove the direct `self.grabber.frame_ready.connect(self.stage_controller.on_frame_ready)` connection. In `_on_camera_frame`, compute `frame = self._correct_camera_frame_for_active_objective(qimg)`, store/copy that corrected frame, call `self.stage_controller.on_frame_ready(frame)`, and pass it to the view.

- [ ] **Step 4: Verify GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_camera_distortion.py -q`
Expected: PASS.

### Task 5: Calibration Dialog And Menu Wiring

**Files:**
- Create: `probe_station_gui/dialogs/lens_distortion_dialog.py`
- Modify: `probe_station_gui/views/main_window_menus.py`
- Modify: `main.py`
- Test: `tests/ui/test_main_window_menus.py`, `tests/ui/test_settings_objectives_coordinates.py`

**Interfaces:**
- Produces signals: `calibrate_requested`, `reset_requested`
- Produces main methods: `_show_lens_distortion_dialog`, `_start_lens_distortion_calibration`, `_reset_lens_distortion_calibration`

- [ ] **Step 1: Write failing UI tests**

Assert the Calibration menu includes `Lens Distortion Calibration`, and objective settings shows `Lens correction` as `Configured` when the profile payload is configured.

- [ ] **Step 2: Verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window_menus.py tests/ui/test_settings_objectives_coordinates.py -q`
Expected: FAIL on missing action/status.

- [ ] **Step 3: Implement dialog and wiring**

Build a compact dialog following `ClickCalibrationDialog` patterns. Keep labels laconic: `Objective`, `Lens correction`, `Mean error`, `Max error`, `Calibrate`, `Reset`, `Close`.

- [ ] **Step 4: Verify GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window_menus.py tests/ui/test_settings_objectives_coordinates.py -q`
Expected: PASS.

### Task 6: Automatic Capture Runner

**Files:**
- Modify: `main.py`
- Modify: `probe_station_gui/camera/distortion.py`
- Test: `tests/app/test_main_camera_distortion.py`

**Interfaces:**
- Consumes: `fit_distortion_from_grid_frames`
- Produces: `Main._run_lens_distortion_calibration() -> None`

- [ ] **Step 1: Write failing runner test**

Use a fake stage controller with `begin_external_task`, `run_external_move_to_xy`, `finish_external_task`, and a fake `_wait_for_camera_frame`. Assert the runner captures the center and eight offsets, persists a payload, refreshes objective settings, and calls click-calibration reset for the active objective.

- [ ] **Step 2: Verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_camera_distortion.py -q`
Expected: FAIL because runner is missing.

- [ ] **Step 3: Implement runner**

Read the current XY with `stage_controller.current_stage_position()`, run the 3x3 offset pattern with 0.05 mm step, capture fresh frames after each move, fit the payload, persist it into the active objective, save settings, reapply objective settings, and return to start XY in `finally` when possible.

- [ ] **Step 4: Verify GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_camera_distortion.py tests/camera/test_distortion.py tests/settings/test_objective_config.py -q`
Expected: PASS.

### Task 7: Real Sample Calibration Pass

**Files:**
- No production edits unless detector tuning is needed.

**Interfaces:**
- Consumes: local GUI, local FastAPI stage status/move only for operator verification.

- [ ] **Step 1: Start the GUI when implementation tests pass**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe main.py`
Expected: GUI starts; no startup stalls.

- [ ] **Step 2: Inspect runtime logs before diagnosing any app behavior**

Read:
`C:\Users\Lazemir\AppData\Local\ProbeStationGUI\Logs\status-history.log`
`C:\Users\Lazemir\AppData\Local\ProbeStationGUI\Logs\probe-station-gui.log`

- [ ] **Step 3: Run calibration on the installed sample**

Use the new dialog with the active objective. Allow the automatic 0.05 mm XY scan to complete. Capture residual metrics and verify the live grid lines look straight.

- [ ] **Step 4: Final verification**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_distortion.py tests/settings/test_objective_config.py tests/app/test_main_camera_distortion.py tests/ui/test_main_window_menus.py tests/ui/test_settings_objectives_coordinates.py -q`
Expected: PASS.

## Self-Review

- Spec coverage: persistence, detector, multi-frame fit, lazy correction, main frame pipeline, UI, reset invalidation, tests, and real sample pass are covered.
- Placeholder scan: no banned placeholder tokens are intentionally left.
- Type consistency: payload field names are `distortion_correction` and `distortion_correction_configured`; main hook is `_correct_camera_frame_for_active_objective`; pure correction APIs live in `probe_station_gui.camera.distortion`.
