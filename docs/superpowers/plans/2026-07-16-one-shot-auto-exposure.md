# One-Shot Camera Auto-Exposure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run application-controlled one-shot exposure before every microscope scan, expose it through the operator camera API, and suppress isolated recoverable Spinnaker `NEW_BUFFER_DATA` timeouts.

**Architecture:** A transport-independent controller owns camera state transitions, convergence, rollback, and serialization. `Main` adapts the existing camera broker and raw-frame condition to that controller for both scan workers and the external API. Acquisition timeout classification remains inside `Grabber`, where the actual Spinnaker exception and frame recovery are visible.

**Tech Stack:** Python 3.11, PySide6, NumPy, OpenCV, FastAPI, rotpy/Spinnaker, pytest.

## Global Constraints

- GUI code calls the controller directly and never calls the app's localhost API.
- Camera acquisition remains running while settings are read or written.
- One-shot exposure runs before stage task acquisition, needle movement, or tile movement.
- Success leaves manual exposure active; failure restores the original camera state.
- Gain is fixed at `0 dB`; existing white-balance ratios are preserved with automatic white balance disabled.
- `auto_exposure=false` disables the pre-scan adjustment for API scans.
- Only Spinnaker code `-1011` containing `NEW_BUFFER_DATA` receives transient treatment; every other acquisition exception keeps current error behavior.
- All Python commands use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.

---

### Task 1: Shared One-Shot Exposure Controller

**Files:**
- Create: `probe_station_gui/camera/auto_exposure.py`
- Modify: `probe_station_gui/camera/exposure_diagnostic.py`
- Create: `tests/camera/test_auto_exposure.py`
- Modify: `tests/camera/test_exposure_diagnostic.py`

**Interfaces:**
- Produces: `AutoExposureConfig`, `AutoExposureFrame`, `AutoExposureBusyError`, `CameraAutoExposureController.run(config=None) -> dict[str, Any]`.
- Consumes callbacks: `settings_read(names)`, `settings_write(ordered_settings)`, and `frame_read(after_counter, timeout_s)`.
- Produces shared `next_exposure_us(...)` and `highlight_level(...)` used by the diagnostic.

- [ ] **Step 1: Write failing controller tests**

Cover bounded convergence, fresh-frame watermarks, ordered manual-mode transition, success leaving the result active, failure rollback, and non-blocking busy rejection. The success assertion must include:

```python
result = controller.run(AutoExposureConfig(settling_frames=0, convergence_window=2))
assert result["accepted"] is True
assert result["converged"] is True
assert camera.state["ExposureAuto"] == "Off"
assert camera.state["GainAuto"] == "Off"
assert camera.state["Gain"] == "0.0"
assert result["brightness_trace"][-2:] == pytest.approx([235.0, 235.0])
```

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_auto_exposure.py -q`

Expected: import failure because `probe_station_gui.camera.auto_exposure` does not exist.

- [ ] **Step 3: Implement the minimal controller**

Add immutable config/frame dataclasses, validate all numeric config fields, acquire a non-blocking operation lock, snapshot the five operator nodes, disable automatic modes in one batch, write gain/exposure in a second batch, consume only fresh raw frames, and restore the snapshot on every unsuccessful exit. Return a JSON-safe result containing configuration, final settings, frame counter, iteration count, and brightness trace.

- [ ] **Step 4: Share controller math with the diagnostic**

Import `AutoExposureConfig`, `next_exposure_us`, and `highlight_level` into `exposure_diagnostic.py`. Keep `ExposureDiagnosticConfig` as a compatible extension for diagnostic-only SNR fields so existing CLI/report behavior remains stable.

- [ ] **Step 5: Run focused tests and commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_auto_exposure.py tests/camera/test_exposure_diagnostic.py -q`

Expected: PASS.

Commit: `feat: add reusable camera auto exposure`

### Task 2: Scan Configuration And Direct Integration

**Files:**
- Modify: `probe_station_gui/camera/microscope_scan.py`
- Modify: `probe_station_gui/dialogs/microscope_scan_dialog.py`
- Modify: `main.py`
- Modify: `tests/camera/test_microscope_scan.py`
- Modify: `tests/app/test_main_microscope_scan.py`
- Modify: `tests/dialogs/test_microscope_scan_dialog.py` if present, otherwise create it under `tests/ui/`.

**Interfaces:**
- Produces: `AutoExposureScanOptions(enabled: bool = True)` and `auto_exposure_options_from_payload(payload, default_enabled=True)`.
- `MicroscopeScanConfiguration.auto_exposure: bool` defaults to `True`.
- `Main._run_camera_auto_exposure() -> dict[str, Any]` adapts broker settings and raw `QImage` frames to the shared controller.

- [ ] **Step 1: Write failing scan-option tests**

Assert missing API payload defaults to enabled, `auto_exposure=false` disables it, object form accepts `enabled`, and scan metadata serializes the applied result.

- [ ] **Step 2: Write failing scan-order tests**

Use a minimal `Main` and ordered event list. Assert `auto_exposure` occurs before `begin_external_task`, `raise`, and all moves. Assert rejected auto-exposure produces no stage events and emits a failed scan result. Assert disabled auto-exposure starts with the existing camera lock.

- [ ] **Step 3: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_microscope_scan.py tests/app/test_main_microscope_scan.py -q`

Expected: failures for missing options and pre-scan call.

- [ ] **Step 4: Implement GUI and API scan configuration**

Add an `Auto exposure` checkbox to the scan dialog, checked by default, and include the boolean in `MicroscopeScanConfiguration`. Parse the same option in `_api_microscope_area_scan` and attach it to the worker configuration.

- [ ] **Step 5: Run the controller before motion**

In `_run_microscope_scan`, invoke the direct controller before `stage_controller.begin_external_task`. On rejection raise a scan failure before stage ownership or movement. On success add `auto_exposure` result metadata to `corrections`; then apply the existing camera lock and continue unchanged.

- [ ] **Step 6: Run focused tests and commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_microscope_scan.py tests/app/test_main_microscope_scan.py tests/ui -q`

Expected: PASS.

Commit: `feat: run auto exposure before microscope scans`

### Task 3: Operator Camera API And Client

**Files:**
- Modify: `probe_station_gui/api/server.py`
- Modify: `probe_station_client/client.py`
- Modify: `main.py`
- Modify: `tests/api/test_server.py`
- Modify: `tests/api/test_client.py`
- Modify: `tests/app/test_main_camera_api.py`

**Interfaces:**
- `ProbeStationApiServer(..., camera_auto_exposure_callback=None)`.
- `POST /api/v1/camera/auto-exposure` requires `camera_write` and accepts `{}` or `{"config": {...}}`.
- `ProbeStationCameraClient.auto_exposure(config=None, timeout_s=30.0) -> dict[str, Any]`.

- [ ] **Step 1: Write failing server and client tests**

Assert missing permission returns 403, missing callback returns 501, ordered callback result passes through, invalid non-object config returns 400, rejected results preserve 409, and the Python client sends POST with an extended request timeout.

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/api/test_server.py tests/api/test_client.py tests/app/test_main_camera_api.py -q`

Expected: constructor/route/client method failures.

- [ ] **Step 3: Implement endpoint and client**

Add the dedicated callback and route beside camera settings routes. Authorize with `API_PERMISSION_CAMERA_WRITE`, validate config shape, call the callback synchronously in the FastAPI worker, and map rejected status through `_raise_for_rejected`. Add the client method without changing default timeout behavior for other requests.

- [ ] **Step 4: Wire Main busy guards**

Pass `self._api_camera_auto_exposure` into the server. Reject when a microscope scan is active; otherwise parse configuration and run the shared controller. A concurrent controller operation returns 409 without waiting.

- [ ] **Step 5: Run focused tests and commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/api/test_server.py tests/api/test_client.py tests/app/test_main_camera_api.py -q`

Expected: PASS.

Commit: `feat: expose camera auto exposure API`

### Task 4: Recoverable Spinnaker Timeout Classification

**Files:**
- Modify: `probe_station_gui/camera/worker.py`
- Modify: `tests/camera/test_camera_worker.py`

**Interfaces:**
- Produces: `Grabber.NEW_BUFFER_TIMEOUT_CODE = -1011` and `Grabber.NEW_BUFFER_TIMEOUT_ALERT_COUNT = 5`.
- Produces private `_handle_acquisition_exception(exc) -> bool` and recovery state consumed by `_emit_frame`.

- [ ] **Step 1: Write failing timeout tests**

Use a fake exception with `spin_error_code=-1011`. Assert one matching timeout emits no `error`, five consecutive matches emit exactly one error, a valid-frame recovery resets escalation, the recovered frame suppresses the expected gap warning, and mismatched code/message emits immediately.

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_camera_worker.py -q`

Expected: missing classifier/state failures.

- [ ] **Step 3: Implement classification and escalation**

Route acquisition exceptions through the classifier. Ignore isolated matching timeouts, escalate once at the named threshold, and reset on a valid image. Suppress only the first frame-gap warning caused by a non-escalated timeout. Preserve current handling for all unrelated exceptions.

- [ ] **Step 4: Run focused tests and commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_camera_worker.py -q`

Expected: PASS.

Commit: `fix: suppress transient camera buffer timeouts`

### Task 5: Regression And Hardware Verification

**Files:**
- Modify only files owned by Tasks 1-4 if verification finds a defect.
- Generate ignored reports under `.scratch/`.

- [ ] **Step 1: Run static sanity checks**

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 2: Run the complete automated suite**

Run: `$env:QT_QPA_PLATFORM='offscreen'; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q`

Expected: all tests pass without a native Qt application lifecycle failure.

- [ ] **Step 3: Restart the GUI normally**

Restart from `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe main.py`; verify camera streaming and API health before calling the new endpoint.

- [ ] **Step 4: Invoke real one-shot exposure through the API**

Call `ProbeStationClient.camera.auto_exposure()` using the existing Codex operator key. Verify the response converges, leaves `ExposureAuto=Off`, `GainAuto=Off`, `Gain=0`, and returns a fresh-frame watermark.

- [ ] **Step 5: Verify scan integration without unnecessary motion**

Use a one-tile area scan centered at the current point. Confirm auto-exposure status precedes camera lock and stage task status, the manifest contains the auto-exposure result, and the camera retains the final exposure after scan completion.

- [ ] **Step 6: Verify timeout logging behavior**

Inspect both runtime logs. Confirm isolated `-1011` no longer produces `ERROR` or Telegram-alert dispatch while unrelated camera errors remain eligible for the existing path.

- [ ] **Step 7: Commit verification fixes**

If hardware verification requires changes, repeat the relevant RED/GREEN test and commit as `fix: harden automatic camera exposure`.
