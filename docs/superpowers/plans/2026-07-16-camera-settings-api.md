# Camera Settings API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authenticated operator camera controls and fresh-frame access to the local API, then provide a repeatable native-versus-custom auto-exposure diagnostic.

**Architecture:** FastAPI calls a thread-safe `CameraApiBroker` rather than the GUI request bridge. The broker correlates API requests with results emitted by the existing `Grabber` command queue, while frame waits use the existing main-window frame condition. The diagnostic uses the public Python client and never imports GUI internals or moves the stage.

**Tech Stack:** Python 3.9+, PySide6 `QImage`, rotpy/GenICam through `Grabber`, FastAPI, NumPy, OpenCV, unittest/pytest.

## Global Constraints

- API access is limited to `ExposureAuto`, `ExposureTime`, `GainAuto`, `Gain`, `BlackLevel`, `BalanceWhiteAuto`, `BalanceRatioSelector`, and `BalanceRatio`.
- Camera acquisition must not stop or restart for settings reads or writes.
- Invalid or unavailable settings fail explicitly; there is no alias, alternate-node, alternate-mode, or automatic-retry fallback.
- Camera I/O stays out of the GUI and FastAPI threads.
- All Python commands use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.
- The diagnostic does not move the stage and restores settings unless `--apply-winner` selects an unambiguous winner.

---

### Task 1: Camera API Permissions

**Files:**
- Modify: `probe_station_gui/api/keys.py`
- Modify: `probe_station_gui/dialogs/settings_dialog.py`
- Modify: `tests/api/test_keys.py`

**Interfaces:**
- Produces: `API_PERMISSION_CAMERA_READ`, `API_PERMISSION_CAMERA_WRITE`, and legacy permission migration in `ApiKeyRecord.from_dict()`.
- Produces: camera permission columns in `ApiSettingsWidget.PERMISSION_COLUMNS`.

- [ ] **Step 1: Write failing permission tests**

Add tests proving new keys default to camera read only, explicit camera values persist, and legacy records inherit `stage_read`/`stage_write` only when camera fields are absent:

```python
legacy = ApiKeyRecord.from_dict({
    "id": "legacy",
    "permissions": {"stage_read": True, "stage_write": False},
})
assert legacy.permissions[API_PERMISSION_CAMERA_READ] is True
assert legacy.permissions[API_PERMISSION_CAMERA_WRITE] is False
```

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/api/test_keys.py -q`

Expected: import or assertion failures because camera permissions do not exist.

- [ ] **Step 3: Implement permission constants, defaults, and migration**

Add both constants to `API_KEY_PERMISSIONS`; set new-key defaults to read `True`, write `False`. In `ApiKeyRecord.from_dict`, inspect the raw permission mapping before normalization and seed absent camera values from the corresponding stored stage values:

```python
raw_permissions = data.get("permissions")
legacy_defaults = {}
if isinstance(raw_permissions, Mapping):
    if API_PERMISSION_CAMERA_READ not in raw_permissions:
        legacy_defaults[API_PERMISSION_CAMERA_READ] = bool(
            raw_permissions.get(API_PERMISSION_STAGE_READ, False)
        )
    if API_PERMISSION_CAMERA_WRITE not in raw_permissions:
        legacy_defaults[API_PERMISSION_CAMERA_WRITE] = bool(
            raw_permissions.get(API_PERMISSION_STAGE_WRITE, False)
        )
```

Normalize with these defaults, while preserving explicit camera values.

- [ ] **Step 4: Extend the API-key table**

Add `Camera read` and `Camera write` columns before timestamps, update column count, item payload width, and permission-column indices.

- [ ] **Step 5: Run focused tests and commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/api/test_keys.py -q`

Expected: PASS.

Commit: `feat: add camera API permissions`

### Task 2: Atomic Grabber Operations And Request Correlation

**Files:**
- Modify: `probe_station_gui/camera/worker.py`
- Modify: `tests/camera/test_camera_worker.py`

**Interfaces:**
- Produces: `Grabber.camera_settings_batch_changed` signal.
- Produces: `request_camera_settings_snapshot(..., request_id: str | None = None)`.
- Produces: `request_camera_settings_batch(settings, *, request_id: str, map_key: str = "camera")`.
- Result payloads preserve `request_id` and contain `ok`, `nodes`/`maps`, and rollback diagnostics.

- [ ] **Step 1: Write failing worker tests**

Test that snapshot commands preserve request IDs, ordered batches read back values, duplicate names fail, all nodes are validated before the first write, later write failure restores changed values in reverse order, and rollback errors are reported.

```python
grabber.request_camera_settings_batch(
    [("ExposureAuto", "Off"), ("ExposureTime", 1800.0)],
    request_id="req-1",
)
command = grabber._camera_commands.get_nowait()
assert command.action == "batch_set"
assert command.payload["request_id"] == "req-1"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_camera_worker.py -q`

Expected: missing batch API/signal failures.

- [ ] **Step 3: Implement request-ID propagation**

Include `request_id` in queued payloads, immediate camera-not-ready results, worker task exceptions, snapshot results, and setting results. `_emit_camera_task_result` must merge the submitted payload's request ID into exception results rather than losing correlation.

- [ ] **Step 4: Implement two-phase atomic batch write**

`_apply_camera_settings_batch(payload)` first rejects duplicates and reads/validates every node into `saved_settings`. Only after all nodes pass does it apply values in order. On failure, restore changed entries in reverse order with `_restore_camera_setting_values`.

Return:

```python
{
    "ok": not errors,
    "request_id": request_id,
    "nodes": updated_nodes,
    "rollback_errors": rollback_errors,
    "streaming": self._acquiring,
}
```

- [ ] **Step 5: Route `batch_set` through the existing settings executor**

Add a `_process_camera_commands` branch using `camera_settings_batch_changed`; do not stop acquisition or call camera methods from the caller thread.

- [ ] **Step 6: Run focused tests and commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_camera_worker.py -q`

Expected: PASS.

Commit: `feat: add atomic camera settings batches`

### Task 3: Thread-Safe Camera API Broker

**Files:**
- Create: `probe_station_gui/camera/api_control.py`
- Create: `tests/camera/test_camera_api_control.py`

**Interfaces:**
- Produces: `OPERATOR_CAMERA_NODE_NAMES: tuple[str, ...]`.
- Produces: `CameraApiBroker(snapshot_submit, batch_submit, frame_counter, timeout_s=5.0)`.
- Produces: `read_settings(names) -> dict[str, Any]`, `write_settings(settings) -> dict[str, Any]`, and `complete(result) -> None`.
- Produces: `encode_camera_frame_png(frame, counter, space) -> dict[str, Any]`.

- [ ] **Step 1: Write failing broker tests**

Cover allowlist ordering, correlation of concurrent requests, bounded timeout cleanup, ignored late completion, failure normalization, and frame-counter watermarking:

```python
result_holder = {}
thread = Thread(target=lambda: result_holder.update(broker.read_settings(["Gain"])))
thread.start()
request_id = submitted[0][0]
broker.complete({"ok": True, "request_id": request_id, "maps": []})
thread.join()
assert result_holder["accepted"] is True
assert result_holder["frame_counter_at_completion"] == 17
```

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_camera_api_control.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement broker pending operations**

Use `uuid.uuid4().hex`, `threading.Event`, and an `RLock`. Register under lock, invoke submit outside the lock, wait with a deadline, then remove under lock. `complete` copies the result and sets the event after releasing the lock. Timeout returns HTTP-oriented status 504 and does not retry.

- [ ] **Step 4: Implement allowlist validation and result normalization**

Reject unknown or duplicate names before submission. Convert worker `ok` to API `accepted`, preserving worker detail and assigning 503 for not-ready, 409 for camera rejection, and 504 for timeout.

- [ ] **Step 5: Implement PNG encoding**

Copy the supplied `QImage`, encode through `QBuffer` as PNG, and return bytes plus width, height, counter, and space. Null images and encoding failure return explicit rejected results.

- [ ] **Step 6: Run focused tests and commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_camera_api_control.py -q`

Expected: PASS.

Commit: `feat: add camera API operation broker`

### Task 4: HTTP Endpoints, Main Wiring, And Python Client

**Files:**
- Modify: `probe_station_gui/api/server.py`
- Modify: `probe_station_gui/api/keys.py`
- Modify: `main.py`
- Modify: `probe_station_client/client.py`
- Modify: `probe_station_client/__init__.py`
- Modify: `tests/api/test_server.py`
- Modify: `tests/api/test_client.py`
- Create: `tests/app/test_main_camera_api.py`

**Interfaces:**
- `ProbeStationApiServer(..., camera_settings_read_callback=None, camera_settings_write_callback=None, camera_frame_callback=None)`.
- `GET /api/v1/camera/settings?name=ExposureTime&name=Gain`.
- `PATCH /api/v1/camera/settings` with `{"settings": [{"name": str, "value": object}]}`.
- `GET /api/v1/camera/frame?space=raw&after_counter=17&timeout_ms=2000`.
- `ProbeStationClient.camera: ProbeStationCameraClient` with `settings()`, `update_settings()`, and `frame()`.

- [ ] **Step 1: Write failing HTTP and client tests**

Assert read/write permissions independently, unknown node HTTP 400, ordered body preservation, rejected callbacks mapped to status, PNG bytes/headers, frame timeout, and client query/header parsing.

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/api/test_server.py tests/api/test_client.py -q`

Expected: missing constructor arguments/routes/client namespace.

- [ ] **Step 3: Add dedicated server callbacks and routes**

Validate query/body shape in FastAPI, authorize with `camera_read` or `camera_write`, call the dedicated callback in the FastAPI worker, and pass rejected results through `_raise_for_rejected`. The frame route returns `Response` with PNG headers from callback metadata.

- [ ] **Step 4: Wire broker and frames in `Main`**

Create the broker after `Grabber`, connect snapshot and batch result signals to `broker.complete`, pass broker methods into `ProbeStationApiServer`, and implement a thread-safe frame callback selecting `_wait_for_raw_camera_frame` or `_wait_for_camera_frame`. Do not use `ApiRequestBridge` for camera operations.

- [ ] **Step 5: Add main integration tests**

Use a minimal `Main` instance created through `__new__`, fake grabber submissions, real frame condition, and `QImage` frames to verify settings correlation and raw/corrected selection without constructing hardware or the full UI.

- [ ] **Step 6: Add the camera client namespace**

Implement `ProbeStationCameraClient`. Add a private request method returning body plus response headers so `frame()` can return:

```python
CameraFrame(
    data=body,
    counter=int(headers["X-Camera-Frame-Counter"]),
    space=headers["X-Camera-Frame-Space"],
    width=int(headers["X-Camera-Frame-Width"]),
    height=int(headers["X-Camera-Frame-Height"]),
)
```

- [ ] **Step 7: Run focused and surrounding tests, then commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/api tests/camera/test_camera_api_control.py tests/app/test_main_camera_api.py -q`

Expected: PASS.

Commit: `feat: expose camera settings and frames over API`

### Task 5: Repeatable Auto-Exposure Diagnostic

**Files:**
- Create: `probe_station_gui/camera/exposure_diagnostic.py`
- Create: `tests/camera/test_exposure_diagnostic.py`

**Interfaces:**
- Produces: `ExposureDiagnosticConfig` dataclass with target percentile/value, step ratio, clipping limits, settling count, convergence tolerances, frame count, and time limits.
- Produces: `next_exposure_us(current_us, measured_level, limits, config) -> float`.
- Produces: `frame_metrics(frames) -> dict[str, float]`.
- Produces: `choose_preferred_mode(mode_reports, config) -> str | None`.
- Produces: module CLI callable with `python -m probe_station_gui.camera.exposure_diagnostic`.

- [ ] **Step 1: Write failing controller and metric tests**

Use synthetic RGB arrays to prove multiplicative convergence, min/max and step clamps, immediate clipping reduction, saturation/near-clip fractions, temporal drift/noise, SNR ordering, and no winner when every mode violates clipping limits.

- [ ] **Step 2: Run tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_exposure_diagnostic.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement pure controller and metrics**

Decode PNGs with OpenCV, convert BGR to RGB, calculate per-pixel maximum RGB percentiles, and derive a stable silicon mask from low-gradient pixels below the temporal-median luminance percentile. Keep every threshold in `ExposureDiagnosticConfig` and serialize it into report metadata.

- [ ] **Step 4: Implement API-driven mode runner**

Snapshot operator settings and per-selector balance ratios. Fix white balance and gain consistently, run native full auto, native exposure-only, and custom exposure-only modes. After every batch, use `frame_counter_at_completion`, discard settling frames, and record every fresh settings/frame sample.

- [ ] **Step 5: Implement restoration, reports, and CLI**

Restore original settings in `finally`, including per-selector white balance ratios and original selector. Write `report.json`, `summary.md`, traces, and final PNGs under `.scratch/camera-auto-exposure-<timestamp>`. `--apply-winner` reapplies only a non-ambiguous winner after successful restoration.

- [ ] **Step 6: Test interruption and restoration**

Use a fake `ProbeStationCameraClient` that raises during mode two. Assert the original ordered settings are restored exactly and report metadata records the failure.

- [ ] **Step 7: Run focused tests and commit**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_exposure_diagnostic.py tests/api/test_client.py -q`

Expected: PASS.

Commit: `feat: add camera exposure diagnostic`

### Task 6: Regression And Hardware Verification

**Files:**
- Modify only if failures reveal defects in files owned by Tasks 1-5.
- Generate ignored artifacts under `.scratch/`.

**Interfaces:**
- Consumes the complete API and diagnostic CLI.
- Produces a real report comparing the three exposure modes on the current aluminium-on-silicon structure.

- [ ] **Step 1: Run formatting/static sanity checks**

Run: `git diff --check`

Expected: no output.

- [ ] **Step 2: Run the complete automated suite**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q`

Expected: PASS.

- [ ] **Step 3: Restart the GUI from the project environment**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe main.py`

Verify the local API starts and live camera acquisition remains continuous.

- [ ] **Step 4: Read runtime logs before diagnosing hardware behavior**

Inspect:

- `C:\Users\Lazemir\AppData\Local\ProbeStationGUI\Logs\status-history.log`
- `C:\Users\Lazemir\AppData\Local\ProbeStationGUI\Logs\probe-station-gui.log`

Confirm no camera frame-gap or settings-operation errors were introduced.

- [ ] **Step 5: Run the hardware diagnostic**

Run with the existing Codex API key exposed through `PROBE_STATION_API_KEY`:

`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m probe_station_gui.camera.exposure_diagnostic --base-url http://127.0.0.1:8765`

Expected: three mode traces, final frames, `report.json`, and `summary.md`; original camera settings restored.

- [ ] **Step 6: Review real metrics and optionally apply winner**

Only if one mode satisfies clipping and wins the declared ordering, rerun with `--apply-winner`. Record final exposure/gain/mode values and show the three final frames to the operator.

- [ ] **Step 7: Commit verification fixes**

If verification required code changes, rerun the affected RED/GREEN tests and commit them as `fix: harden camera exposure diagnostic`.
