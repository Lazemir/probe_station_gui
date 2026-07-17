# Optical Calibration Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a GUI wizard that runs flat-field and lens-distortion calibration together or individually for the active objective.

**Architecture:** A focused profile-store module owns durable flat-field artifacts. A `QWizard` owns operator navigation while `Main` owns asynchronous camera and stage work. Lens fitting consumes frames corrected by the same stored flat-field profile used by the live view.

**Tech Stack:** Python 3.11, PySide6, NumPy, OpenCV, pytest

## Global Constraints

- Keep camera I/O, stage movement, image processing, and file writes off the GUI thread.
- In-process GUI flows call controller methods and Qt signals, never the localhost API.
- Full calibration uses a shifted 3x3 flat-field acquisition with 80% overlap, followed by a nine-position center/edge/corner lens acquisition.
- Lens fitting requires a valid active-objective flat field; there is no raw-frame fallback.
- Each acquisition returns the stage to its own starting position and releases the external task.
- Failed flat-field calibration must leave the previously installed `current.json` intact.

---

### Task 1: Flat-Field Profile Store

**Files:**
- Create: `probe_station_gui/camera/flat_field_calibration.py`
- Modify: `probe_station_gui/camera/imaging.py`
- Modify: `probe_station_gui/camera/live_correction.py`
- Test: `tests/camera/test_flat_field_calibration.py`
- Test: `tests/camera/test_live_correction.py`

**Interfaces:**
- Produces: `FlatFieldCalibrationStore(config_dir: str | Path)`
- Produces: `load(objective_name: str) -> StoredFlatFieldCalibration`
- Produces: `install(objective_name: str, frames: Sequence[QImage], *, blur_radius_px: int, max_gain: float, metadata: Mapping[str, object]) -> StoredFlatFieldCalibration`
- Produces: `median_flat_field_reference(frames: Sequence[QImage]) -> QImage`

- [ ] **Step 1: Write failing profile-store tests**

```python
def test_install_writes_versioned_reference_and_current_manifest(tmp_path, flat_frames):
    stored = FlatFieldCalibrationStore(tmp_path).install(
        "X20", flat_frames, blur_radius_px=401, max_gain=4.0, metadata={"tiles": 9}
    )
    assert stored.reference_image.is_file()
    assert json.loads(stored.current_manifest.read_text())["objective"] == "X20"

def test_failed_install_keeps_previous_current_manifest(tmp_path, flat_frames, monkeypatch):
    store = FlatFieldCalibrationStore(tmp_path)
    first = store.install("X20", flat_frames, blur_radius_px=401, max_gain=4.0)
    monkeypatch.setattr(QImage, "save", lambda *_args: False)
    with pytest.raises(RuntimeError):
        store.install("X20", flat_frames, blur_radius_px=401, max_gain=4.0)
    assert store.load("X20").reference_image == first.reference_image
```

- [ ] **Step 2: Run the new test file and confirm imports fail**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_flat_field_calibration.py -q`

Expected: FAIL because `flat_field_calibration` is absent.

- [ ] **Step 3: Implement median-reference generation and atomic profile storage**

```python
@dataclass(frozen=True)
class StoredFlatFieldCalibration:
    objective_name: str
    current_manifest: Path
    reference_image: Path
    profile: FlatFieldProfile
    metadata: dict[str, object]

class FlatFieldCalibrationStore:
    def load(self, objective_name: str) -> StoredFlatFieldCalibration: ...
    def install(self, objective_name: str, frames: Sequence[QImage], *,
                blur_radius_px: int = 401, max_gain: float = 4.0,
                metadata: Mapping[str, object] | None = None
                ) -> StoredFlatFieldCalibration: ...
```

Use an adjacent temporary JSON file plus `Path.replace()` for `current.json`.
Write the timestamped reference and profile before replacing the pointer.

- [ ] **Step 4: Make live correction load through the shared store**

Replace the duplicate manifest/reference parsing in
`LiveCameraCorrectionPipeline._compiled_flat_field_for_objective()` with the
store loader while retaining file-signature cache invalidation.

- [ ] **Step 5: Run profile and live-correction tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_flat_field_calibration.py tests/camera/test_live_correction.py tests/camera/test_imaging.py -q`

Expected: PASS.

### Task 2: Optical Calibration Wizard

**Files:**
- Create: `probe_station_gui/dialogs/optical_calibration_wizard.py`
- Create: `tests/ui/test_optical_calibration_wizard.py`

**Interfaces:**
- Produces: `OpticalCalibrationMode` values `FULL`, `FLAT_FIELD`, `LENS_DISTORTION`
- Produces: `OpticalCalibrationWizard.start_flat_field_requested` signal
- Produces: `OpticalCalibrationWizard.start_lens_distortion_requested` signal
- Produces: `set_flat_field_result(success: bool, message: str)` and `set_lens_distortion_result(success: bool, message: str)`

- [ ] **Step 1: Write failing navigation and close-guard tests**

```python
def test_full_mode_requests_flat_then_lens(qtbot):
    wizard = OpticalCalibrationWizard()
    wizard.set_mode(OpticalCalibrationMode.FULL)
    with qtbot.waitSignal(wizard.start_flat_field_requested):
        wizard.next()
    wizard.set_flat_field_result(True, "saved")
    with qtbot.waitSignal(wizard.start_lens_distortion_requested):
        wizard.next()

def test_running_wizard_rejects_close(qtbot):
    wizard = OpticalCalibrationWizard()
    wizard.set_running(True, "Capturing 1/9")
    wizard.close()
    assert wizard.isVisible()
```

- [ ] **Step 2: Run the wizard test and confirm imports fail**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_optical_calibration_wizard.py -q`

Expected: FAIL because the wizard module is absent.

- [ ] **Step 3: Implement the QWizard pages and state transitions**

Use four pages: mode, blank substrate, test structure, and results. `nextId()`
skips pages excluded by the selected mode. `validatePage()` emits one start
signal and returns `False` while the asynchronous stage is running. A successful
completion advances to the next included page; a failure remains on the page.

- [ ] **Step 4: Run the wizard tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_optical_calibration_wizard.py -q`

Expected: PASS.

### Task 3: Flat-Field Hardware Runner

**Files:**
- Modify: `main.py`
- Test: `tests/app/test_main_flat_field_calibration.py`

**Interfaces:**
- Produces: `Main._start_flat_field_calibration()`
- Produces: `Main._run_flat_field_calibration(start_xy, linear_feedrate, needle_feedrate)`
- Emits: `flat_field_calibration_progress(str)` and `flat_field_calibration_finished(bool, str, object)`

- [ ] **Step 1: Write a failing fake-stage runner test**

```python
def test_flat_field_runner_captures_shifted_three_by_three_and_restores_stage(monkeypatch):
    Main._run_flat_field_calibration(window, (10.0, 20.0), 120.0, 70.0)
    moves = [event for event in stage.events if event[0] == "move"]
    assert len(moves) == 10
    assert moves[-1][1:3] == (10.0, 20.0)
    assert len(installed_frames) == 9
```

Assert one-shot auto-exposure occurs before `begin_external_task`, every frame
comes from `_wait_for_raw_camera_frame`, and the store receives the active
objective plus capture metadata.

- [ ] **Step 2: Run the runner test and verify the method is absent**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_flat_field_calibration.py -q`

Expected: FAIL with missing runner methods/signals.

- [ ] **Step 3: Implement asynchronous preflight and 3x3 capture**

Compute FOV from the active calibrated pixel matrix. The adjacent center spacing
is `0.2 * FOV` on each axis. Capture in serpentine order, with `(0, 0)` first,
using raw frames and the existing settle/camera timeout values. Call
`FlatFieldCalibrationStore.install()` only after all nine frames arrive.

- [ ] **Step 4: Run the flat-field runner tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_flat_field_calibration.py tests/app/test_main_microscope_scan.py -q`

Expected: PASS.

### Task 4: Nine-Position Lens Fit and GUI Integration

**Files:**
- Modify: `main.py`
- Modify: `probe_station_gui/dialogs/lens_distortion_dialog.py`
- Modify: `probe_station_gui/views/main_window_menus.py`
- Modify: `tests/app/test_main_lens_distortion.py`
- Modify: `tests/ui/test_main_window_menus.py`
- Test: `tests/app/test_main_optical_calibration.py`

**Interfaces:**
- Consumes: `FlatFieldCalibrationStore.load(objective_name)`
- Consumes: wizard request/result/progress methods from Task 2
- Produces: `Main._show_optical_calibration_wizard(mode=None)`

- [ ] **Step 1: Change lens runner tests to require nine corrected frames**

```python
assert len(offsets) == 9
assert offsets[0] == (0.0, 0.0)
assert corrected_frame_ids == fitted_frame_ids
assert events.index("load_flat") < events.index("first_move")
```

Add a missing-profile test that asserts failure happens before
`begin_external_task` and no fit function runs.

- [ ] **Step 2: Add failing menu and wizard-integration tests**

Assert the Calibration menu contains `Optical Calibration`, its action opens the
wizard, full mode advances from successful flat completion to the structure
page, and the existing lens dialog Calibrate button opens lens-only mode.

- [ ] **Step 3: Run the focused tests and confirm failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_lens_distortion.py tests/app/test_main_optical_calibration.py tests/ui/test_main_window_menus.py -q`

Expected: FAIL on old 25-position behavior and absent wizard integration.

- [ ] **Step 4: Implement nine-position offsets and flat-field-before-fit**

Set `LENS_DISTORTION_CAPTURE_GRID_SIZE = 3`. Load the active profile before
reserving the stage. Apply `apply_flat_field_correction()` to the initial frame
used for feature bounds and to every captured frame passed into
`GridCalibrationFrame`. Keep raw camera acquisition unchanged.

- [ ] **Step 5: Connect the wizard and menu**

Instantiate the wizard lazily, connect its two request signals to the existing
lens starter and new flat-field starter, forward progress/completion on the GUI
thread, and prevent objective changes while either worker is active.

- [ ] **Step 6: Run focused tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_flat_field_calibration.py tests/camera/test_live_correction.py tests/app/test_main_flat_field_calibration.py tests/app/test_main_lens_distortion.py tests/app/test_main_optical_calibration.py tests/ui/test_optical_calibration_wizard.py tests/ui/test_main_window_menus.py -q`

Expected: PASS.

- [ ] **Step 7: Run the complete suite**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q`

Expected: PASS.

- [ ] **Step 8: Launch the GUI for operator verification**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe main.py`

Expected: the main window opens promptly and `Calibration > Optical Calibration`
shows the three calibration modes without starting movement.
