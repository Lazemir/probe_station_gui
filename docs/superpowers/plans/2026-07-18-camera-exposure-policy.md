# Camera Exposure Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one persistent manual/automatic exposure policy with selectable software/camera engine, a `Once` action, and fixed-exposure optical sessions around every autofocus, calibration, and area scan.

**Architecture:** A transport-independent `ExposurePolicyController` owns policy transitions and monitoring, while `OpticalSessionManager` owns exclusive, nestable fixed-exposure leases. A small Qt adapter runs operator commands outside the GUI thread. `Main` composes these services with the existing camera broker and injects an optical-session factory into stage workflows.

**Tech Stack:** Python 3.11, PySide6, NumPy, FastAPI, pytest, existing `CameraApiBroker` and `CameraAutoExposureController`.

## Global Constraints

- Persist `auto_enabled` and `engine` independently; migration default is `Auto + Software`.
- Software monitoring checks a fresh raw frame every 5 seconds, starts adjustment beyond 5% drift, and uses the existing 2% one-shot convergence tolerance.
- GUI camera and stage I/O must never block the Qt thread or call the local HTTP API.
- Optical work must finish the selected `Once` before movement and keep exposure fixed until its outermost lease closes.
- Exposure controls are disabled during an optical session; API commands return HTTP 409 and are never queued.
- Direct generic writes to `ExposureAuto` are rejected; `ExposureTime` writes are rejected while auto or an optical session is active.
- Keep the existing isolated Spinnaker `NEW_BUFFER_DATA [-1011]` suppression and sustained-timeout escalation behavior.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python command.

---

### Task 1: Persisted Exposure Policy Settings

**Files:**
- Modify: `probe_station_gui/settings/sections.py`
- Modify: `probe_station_gui/settings/manager.py`
- Modify: `probe_station_gui/default_settings.json`
- Test: `tests/settings/test_exposure_policy_settings.py`
- Test: `tests/settings/test_default_file.py`

**Interfaces:**
- Produces: `ExposurePolicySettings(auto_enabled: bool = True, engine: str = "software")` with `clone()` and `to_dict()`.
- Produces: `SettingsManager.exposure_policy_configuration() -> ExposurePolicySettings` and `set_exposure_policy_configuration(settings: ExposurePolicySettings) -> None`.

- [ ] **Step 1: Write failing settings tests**

```python
def test_missing_exposure_policy_migrates_to_software_auto(manager_from_raw):
    settings = manager_from_raw({})
    assert settings.exposure_policy.to_dict() == {
        "auto_enabled": True,
        "engine": "software",
    }

def test_invalid_engine_normalizes_to_software(manager_from_raw):
    settings = manager_from_raw(
        {"camera": {"exposure": {"auto_enabled": False, "engine": "invalid"}}}
    )
    assert settings.exposure_policy.engine == "software"
    assert settings.exposure_policy.auto_enabled is False
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_exposure_policy_settings.py tests/settings/test_default_file.py -q`

Expected: FAIL because `ExposurePolicySettings` and the `camera.exposure` section do not exist.

- [ ] **Step 3: Add the settings model and parser**

```python
@dataclass
class ExposurePolicySettings:
    auto_enabled: bool = True
    engine: str = "software"

    def clone(self) -> "ExposurePolicySettings":
        return ExposurePolicySettings(self.auto_enabled, self.engine)

    def to_dict(self) -> dict[str, bool | str]:
        return {"auto_enabled": self.auto_enabled, "engine": self.engine}
```

Store it under `camera.exposure`, normalize the engine to `software|camera`, add it to `Settings.clone()` and `Settings.to_dict()`, and persist changes through a clone/replace/save operation.

- [ ] **Step 4: Run settings tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add probe_station_gui/settings/sections.py probe_station_gui/settings/manager.py probe_station_gui/default_settings.json tests/settings/test_exposure_policy_settings.py tests/settings/test_default_file.py
git commit -m "feat: persist camera exposure policy"
```

### Task 2: Exposure Policy And Optical Session Core

**Files:**
- Create: `probe_station_gui/camera/exposure_policy.py`
- Modify: `probe_station_gui/camera/__init__.py`
- Test: `tests/camera/test_exposure_policy.py`
- Test: `tests/camera/test_optical_session.py`

**Interfaces:**
- Consumes: `CameraAutoExposureController.run()` and broker-style settings/frame callbacks.
- Produces: `ExposureEngine`, `ExposurePolicy`, `ExposurePolicyBusyError`, `ExposurePolicyError`, `ExposurePolicyController`, `OpticalSessionManager`, and `OpticalSessionLease`.
- Produces: `ExposurePolicyController.snapshot()`, `set_policy(auto_enabled: bool, engine: str)`, `run_once()`, `start()`, and `shutdown(timeout_s: float = 2.0)`.
- Produces: `OpticalSessionManager.open(operation: str, parent_token: str | None = None) -> OpticalSessionLease`.

- [ ] **Step 1: Write failing state-machine and session tests**

```python
def test_software_auto_checks_brightness_before_adjusting(fake_policy):
    fake_policy.start()
    fake_policy.clock.advance(5.0)
    fake_policy.wake_monitor()
    assert fake_policy.highlight_reads == 1
    assert fake_policy.software_once_calls == 0

def test_outer_session_adjusts_before_ready_and_nested_session_does_not(fake_policy):
    with fake_policy.sessions.open("scan") as outer:
        assert fake_policy.events[:2] == ["software_once", "exposure_off"]
        with fake_policy.sessions.open("autofocus", parent_token=outer.token):
            pass
        assert fake_policy.software_once_calls == 1
    assert fake_policy.monitor_resumed is True
```

Cover all four policy combinations, 5% drift, 2% convergence reuse, hardware `Once`, transition rollback, busy/no-queue behavior, nested-token validation, pre-failure cleanup, and non-raising final-Once warnings.

- [ ] **Step 2: Run core tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_exposure_policy.py tests/camera/test_optical_session.py -q`

Expected: FAIL because `probe_station_gui.camera.exposure_policy` does not exist.

- [ ] **Step 3: Implement the core services**

```python
class ExposurePolicyController:
    MONITOR_INTERVAL_S = 5.0
    DRIFT_TOLERANCE_FRACTION = 0.05

    def set_policy(self, *, auto_enabled: bool, engine: str) -> dict[str, object]:
        selected = ExposureEngine(engine)
        return self._run_exclusive(
            lambda: self._transition_policy(bool(auto_enabled), selected)
        )

    def run_once(self) -> dict[str, object]:
        return self._run_exclusive(self._run_selected_engine_once)

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return self._state_payload_locked()

class OpticalSessionLease:
    token: str
    def close(self) -> dict[str, object]:
        return self._manager.close(self.token)

    def __enter__(self) -> "OpticalSessionLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False
```

Use one command lock, a state lock, a stop event, and a wake event. Emit copied state only after releasing locks. The monitor reads one fresh raw frame and calls `highlight_level`; it runs the full software one-shot only outside the 5% band. Hardware one-shot must wait for native `Once -> Off` and then for a newer raw frame.

- [ ] **Step 4: Run core and existing one-shot tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_exposure_policy.py tests/camera/test_optical_session.py tests/camera/test_auto_exposure.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add probe_station_gui/camera/exposure_policy.py probe_station_gui/camera/__init__.py tests/camera/test_exposure_policy.py tests/camera/test_optical_session.py
git commit -m "feat: add camera exposure policy controller"
```

### Task 3: Qt Adapter And Camera Settings Controls

**Files:**
- Create: `probe_station_gui/camera/exposure_policy_qt.py`
- Modify: `probe_station_gui/dialogs/camera_settings_dialog.py`
- Modify: `probe_station_gui/dialogs/settings_dialog.py`
- Modify: `probe_station_gui/views/main_window_auxiliary.py`
- Test: `tests/camera/test_exposure_policy_qt.py`
- Create: `tests/ui/test_camera_exposure_controls.py`

**Interfaces:**
- Consumes: `ExposurePolicyController`.
- Produces: `ExposurePolicyQtAdapter.snapshot()`, `request_update(auto_enabled: bool, engine: str)`, `request_once()`, `state_changed`, and `command_finished`.
- `CameraSettingsWidget` gains keyword-only `exposure_policy_source`.

- [ ] **Step 1: Write failing adapter and widget tests**

```python
def test_controls_are_independent_and_once_does_not_change_them(app, source):
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    widget._auto_group.button(0).click()
    widget._camera_engine_button.click()
    widget._once_button.click()
    assert source.updates[-1] == (False, "camera")
    assert source.once_calls == 1

def test_optical_session_disables_entire_exposure_block(app, source):
    widget = CameraSettingsWidget(source.grabber, exposure_policy_source=source)
    source.state_changed.emit({"auto_enabled": True, "engine": "software", "busy": True})
    assert widget._exposure_group.isEnabled() is False
```

Also assert `ExposureAuto` and duplicate `ExposureTime` are absent from `OPERATOR_NODE_NAMES`, manual exposure editing follows `auto_enabled`, and commands are dispatched from a worker rather than the GUI thread.

- [ ] **Step 2: Run focused UI tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_exposure_policy_qt.py tests/ui/test_camera_exposure_controls.py -q`

Expected: FAIL because the adapter and controls do not exist.

- [ ] **Step 3: Implement the adapter and compact controls**

```python
class ExposurePolicyQtAdapter(QObject):
    state_changed = Signal(object)
    command_finished = Signal(object)

    @Slot(bool, str)
    def request_update(self, auto_enabled: bool, engine: str) -> None:
        self._start_command(
            lambda: self._controller.set_policy(
                auto_enabled=auto_enabled,
                engine=engine,
            )
        )

    @Slot()
    def request_once(self) -> None:
        self._start_command(self._controller.run_once)
```

Use two exclusive button groups labelled `Exposure` and `Engine`, a `Once` button, and one numeric `Exposure time` editor backed by the existing camera worker. Disable the complete group while `busy`; disable only exposure-time editing while automatic mode is enabled.

- [ ] **Step 4: Run camera-settings and dialog tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_exposure_policy_qt.py tests/ui/test_camera_exposure_controls.py tests/ui/test_settings_objectives_coordinates.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add probe_station_gui/camera/exposure_policy_qt.py probe_station_gui/dialogs/camera_settings_dialog.py probe_station_gui/dialogs/settings_dialog.py probe_station_gui/views/main_window_auxiliary.py tests/camera/test_exposure_policy_qt.py tests/ui/test_camera_exposure_controls.py
git commit -m "feat: add camera exposure policy controls"
```

### Task 4: Policy API And Python Client

**Files:**
- Modify: `probe_station_gui/camera/api_control.py`
- Modify: `probe_station_gui/api/server.py`
- Modify: `probe_station_client/client.py`
- Modify: `tests/camera/test_camera_api_control.py`
- Modify: `tests/api/test_server.py`
- Modify: `tests/api/test_client.py`

**Interfaces:**
- Consumes: controller callbacks `snapshot`, `set_policy`, and `run_once`.
- Produces: `GET/PUT /api/v1/camera/exposure-policy`, `POST /api/v1/camera/exposure-once`.
- Produces: `ProbeStationCameraClient.exposure_policy()`, `set_exposure_policy(...)`, and `exposure_once(timeout_s=30.0)`.

- [ ] **Step 1: Replace old route/client tests with policy tests**

```python
def test_exposure_policy_endpoints(server_client):
    assert server_client.get("/api/v1/camera/exposure-policy").status_code == 200
    response = server_client.put(
        "/api/v1/camera/exposure-policy",
        json={"auto_enabled": False, "engine": "camera"},
    )
    assert response.json()["engine"] == "camera"

def test_old_auto_exposure_route_is_removed(server_client):
    assert server_client.post("/api/v1/camera/auto-exposure", json={}).status_code == 404
```

Add tests for camera permissions, invalid engine 400, busy 409, managed `ExposureAuto`, conditional `ExposureTime` writes, and exact Python client paths.

- [ ] **Step 2: Run API tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_camera_api_control.py tests/api/test_server.py tests/api/test_client.py -q`

Expected: FAIL on missing policy routes and still-present old route.

- [ ] **Step 3: Implement routes, broker guards, and client methods**

```python
def exposure_policy(self) -> dict[str, Any]:
    return self._client._request("GET", "/api/v1/camera/exposure-policy")

def set_exposure_policy(self, *, auto_enabled: bool, engine: str) -> dict[str, Any]:
    return self._client._request(
        "PUT",
        "/api/v1/camera/exposure-policy",
        {"auto_enabled": bool(auto_enabled), "engine": str(engine)},
    )
```

Decorate native node reads with `managed_by="exposure_policy"`. Reject generic writes before submitting to `Grabber`, using controller state supplied to `CameraApiBroker`.

- [ ] **Step 4: Run API/client tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_camera_api_control.py tests/api/test_server.py tests/api/test_client.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add probe_station_gui/camera/api_control.py probe_station_gui/api/server.py probe_station_client/client.py tests/camera/test_camera_api_control.py tests/api/test_server.py tests/api/test_client.py
git commit -m "feat: expose camera exposure policy API"
```

### Task 5: Main Composition And Lifecycle

**Files:**
- Modify: `main.py`
- Modify: `probe_station_gui/views/main_window_shutdown.py`
- Modify: `tests/app/test_main_camera_api.py`
- Modify: `tests/ui/test_main_window_shutdown.py`

**Interfaces:**
- Consumes: persisted policy, broker, one-shot controller, Qt adapter, and session manager.
- Produces: `Main._exposure_policy_controller`, `Main._optical_session_manager`, and `Main._exposure_policy_adapter`.
- Produces: API callbacks and a session factory injected into `StageController`.

- [ ] **Step 1: Write failing composition and shutdown tests**

```python
def test_main_composes_policy_from_persisted_settings(window):
    assert window._exposure_policy_controller.snapshot()["engine"] == "software"
    assert window._exposure_policy_controller.snapshot()["auto_enabled"] is True

def test_shutdown_stops_policy_before_camera_thread(owner):
    shutdown_ui._close_serial_and_panels(owner)
    assert owner.events[:2] == ["exposure_policy_shutdown", "grabber_stop"]
```

- [ ] **Step 2: Run focused app tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_camera_api.py tests/ui/test_main_window_shutdown.py -q`

Expected: FAIL because `Main` has no policy services.

- [ ] **Step 3: Compose and start services without blocking startup**

```python
self._exposure_policy_controller = ExposurePolicyController(
    initial_policy=self.settings_manager.exposure_policy_configuration(),
    software_once=self._camera_auto_exposure_controller.run,
    settings_read=self._camera_api_broker.read_settings,
    settings_write=self._write_camera_auto_exposure_settings,
    frame_read=self._read_camera_auto_exposure_frame,
    persist=self.settings_manager.set_exposure_policy_configuration,
)
self._optical_session_manager = OpticalSessionManager(self._exposure_policy_controller)
```

Start monitoring only after the camera thread starts, pass policy callbacks to FastAPI, pass the Qt adapter to SettingsDialog, and shut policy down before `Grabber.stop()`.

- [ ] **Step 4: Run app composition tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_camera_api.py tests/ui/test_main_window_shutdown.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add main.py probe_station_gui/views/main_window_shutdown.py tests/app/test_main_camera_api.py tests/ui/test_main_window_shutdown.py
git commit -m "feat: compose camera exposure policy services"
```

### Task 6: Autofocus And Click-To-Move Optical Sessions

**Files:**
- Modify: `probe_station_gui/stage/controller.py`
- Modify: `probe_station_gui/stage/autofocus_flow.py`
- Modify: `probe_station_gui/stage/click_move.py`
- Create: `tests/stage/test_optical_sessions.py`
- Modify: `tests/stage/test_autofocus_flow.py`

**Interfaces:**
- Consumes: `OpticalSessionManager.open` through `StageController.set_optical_session_factory(factory)`.
- Produces: one session boundary for GUI autofocus, external local autofocus, click calibration, and click-calibration verification.

- [ ] **Step 1: Write failing ordering and nesting tests**

```python
def test_autofocus_enters_session_before_serial_motion(controller, session_factory):
    controller.set_optical_session_factory(session_factory)
    controller._run_autofocus()
    assert session_factory.events.index("session_ready") < session_factory.events.index("serial")

def test_verified_click_move_does_not_open_calibration_session(controller):
    controller._objective_calibration_verified[controller._active_objective_name] = True
    controller._ensure_calibration()
    assert controller.session_factory.calls == []
```

Cover pre-adjustment failure before serial movement and nested external autofocus using the active parent token.

- [ ] **Step 2: Run stage tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_optical_sessions.py tests/stage/test_autofocus_flow.py -q`

Expected: FAIL because stage workflows do not enter optical sessions.

- [ ] **Step 3: Add the injected session boundary**

```python
def set_optical_session_factory(self, factory: Callable[..., ContextManager]) -> None:
    self._optical_session_factory = factory

def _optical_session(self, operation: str):
    if self._optical_session_factory is None:
        return nullcontext()
    return self._optical_session_factory(operation)
```

Wrap autofocus before `_serial_session()`. In `_ensure_calibration`, enter the session only after determining calibration or verification is required and before the first calibration frame or movement.

- [ ] **Step 4: Run focused and existing stage tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_optical_sessions.py tests/stage/test_autofocus_flow.py tests/stage/test_controller_click_move.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add probe_station_gui/stage/controller.py probe_station_gui/stage/autofocus_flow.py probe_station_gui/stage/click_move.py tests/stage/test_optical_sessions.py tests/stage/test_autofocus_flow.py
git commit -m "feat: lock exposure during stage optical workflows"
```

### Task 7: Calibration Wizard And Area Scan Sessions

**Files:**
- Modify: `main.py`
- Modify: `probe_station_gui/dialogs/optical_calibration_wizard.py`
- Modify: `probe_station_gui/dialogs/microscope_scan_dialog.py`
- Modify: `probe_station_gui/camera/microscope_scan.py`
- Modify: `tests/app/test_main_flat_field_calibration.py`
- Modify: `tests/app/test_main_lens_distortion.py`
- Modify: `tests/app/test_main_optical_calibration.py`
- Modify: `tests/app/test_main_microscope_scan.py`
- Modify: `tests/ui/test_microscope_scan_dialog.py`
- Modify: `tests/camera/test_microscope_scan.py`

**Interfaces:**
- Consumes: `OpticalSessionManager.open` and optional explicit `parent_token` for wizard nesting.
- Produces: fixed exposure around flat-field, lens distortion, full wizard, and microscope area scans.
- Removes: `MicroscopeScanConfiguration.auto_exposure`, `AutoExposureScanOptions`, `auto_exposure_options_from_payload`, and the scan checkbox.

- [ ] **Step 1: Replace per-run one-shot tests with session-ordering tests**

```python
def test_scan_session_is_ready_before_stage_reservation(window, scan_configuration, plan):
    window._run_microscope_scan(scan_configuration, plan)
    assert window.events[:3] == ["session_open", "session_ready", "stage_begin"]

def test_full_wizard_reuses_outer_session_for_both_calibrations(window, wizard):
    window._start_flat_field_calibration_from_wizard()
    window._start_lens_distortion_calibration_from_wizard()
    assert window.session_manager.outer_open_count == 1
```

Assert one fixed exposure spans every tile, standalone calibration sessions close independently, full-wizard nested stages reuse the stored parent token, and pre-failure prevents stage reservation and movement.

- [ ] **Step 2: Run focused workflow tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_flat_field_calibration.py tests/app/test_main_lens_distortion.py tests/app/test_main_optical_calibration.py tests/app/test_main_microscope_scan.py tests/ui/test_microscope_scan_dialog.py tests/camera/test_microscope_scan.py -q`

Expected: FAIL because runners still invoke `_run_camera_auto_exposure` and expose the scan option.

- [ ] **Step 3: Replace runner-specific exposure code with leases**

```python
with self._optical_session_manager.open("microscope_scan") as exposure_session:
    self.stage_controller.begin_external_task("microscope scan")
    try:
        self._capture_scan_tiles(configuration, plan)
    finally:
        self.stage_controller.end_external_task()
```

Store a wizard outer lease/token from the first full-mode capture through the final result or wizard cancellation. Pass that token into each calibration worker context. Remove `ExposureAuto` from legacy camera-lock batches so the session remains authoritative. Record the session policy and pre-adjustment result in scan/calibration metadata.

- [ ] **Step 4: Run all optical workflow tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_flat_field_calibration.py tests/app/test_main_lens_distortion.py tests/app/test_main_optical_calibration.py tests/app/test_main_microscope_scan.py tests/ui/test_optical_calibration_wizard.py tests/ui/test_microscope_scan_dialog.py tests/camera/test_microscope_scan.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add main.py probe_station_gui/dialogs/optical_calibration_wizard.py probe_station_gui/dialogs/microscope_scan_dialog.py probe_station_gui/camera/microscope_scan.py tests/app/test_main_flat_field_calibration.py tests/app/test_main_lens_distortion.py tests/app/test_main_optical_calibration.py tests/app/test_main_microscope_scan.py tests/ui/test_microscope_scan_dialog.py tests/camera/test_microscope_scan.py
git commit -m "feat: use optical sessions for calibrations and scans"
```

### Task 8: Regression And Hardware Verification

**Files:**
- Modify only if failures reveal a feature regression in files already listed above.

**Interfaces:**
- Verifies all interfaces from Tasks 1-7 and the existing timeout suppression.

- [ ] **Step 1: Run focused exposure, API, UI, and workflow suites**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_exposure_policy_settings.py tests/camera/test_auto_exposure.py tests/camera/test_exposure_policy.py tests/camera/test_optical_session.py tests/camera/test_camera_api_control.py tests/camera/test_camera_worker.py tests/api/test_server.py tests/api/test_client.py tests/ui/test_camera_exposure_controls.py tests/app/test_main_camera_api.py tests/stage/test_optical_sessions.py tests/app/test_main_optical_calibration.py tests/app/test_main_microscope_scan.py -q`

Expected: PASS.

- [ ] **Step 2: Run the full suite**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest -q`

Expected: all tracked-data tests PASS; the two known axis-fit tests may fail only because untracked local `calibrations/*.npz` files are absent from this worktree.

- [ ] **Step 3: Inspect logs before live verification**

```powershell
Get-Content -LiteralPath 'C:\Users\Lazemir\AppData\Local\ProbeStationGUI\Logs\status-history.log' -Tail 100
Get-Content -LiteralPath 'C:\Users\Lazemir\AppData\Local\ProbeStationGUI\Logs\probe-station-gui.log' -Tail 200
```

Expected: no unresolved camera or stage fault that would make movement unsafe.

- [ ] **Step 4: Launch and verify operator behavior**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe main.py`

Verify `Manual/Auto`, `Software/Camera`, and `Once`; then run autofocus and one short calibration only after the station is connected and the operator has confirmed motion is allowed. Confirm exposure is fixed during operation and restored afterward.

- [ ] **Step 5: Commit regression fixes, if any**

```powershell
git add main.py probe_station_gui/camera/exposure_policy.py probe_station_gui/camera/exposure_policy_qt.py probe_station_gui/camera/api_control.py probe_station_gui/api/server.py probe_station_gui/dialogs/camera_settings_dialog.py probe_station_gui/dialogs/microscope_scan_dialog.py probe_station_gui/dialogs/optical_calibration_wizard.py probe_station_gui/stage/controller.py probe_station_gui/stage/autofocus_flow.py probe_station_gui/stage/click_move.py
git commit -m "fix: harden camera exposure policy integration"
```

Do not create an empty commit when no regression fix was required.
