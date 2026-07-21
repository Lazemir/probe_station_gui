# Maintainability Deepening Cycle 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete one behaviour-preserving Refactor Pass for each selected architecture direction, reducing MI=0 hotspots before the next measured cycle toward `main.py` Radon MI greater than `0.00`.

**Architecture:** Preserve current public Qt and application-facing interfaces. Move orchestration behind deep modules whose interfaces are the new test surfaces; production objects and in-memory fakes are adapters only at real seams. Each task is committed, reviewed, and measured before the next task starts.

**Tech Stack:** Python 3.11, PySide6, pytest 9, Coverage.py 7, Ruff, Wily 1.25, Radon 5.1, Lizard 1.23, Vulture 2.16.

## Global Constraints

- Preserve Behaviour Parity; do not change GUI copy, public local-API routes, settings keys, hardware protocol, or calibration payload formats.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python command; do not run hardware-dependent code.
- Keep startup lightweight and keep hardware I/O, camera work, serial polling, file parsing, image processing, and long work off the GUI thread.
- Do not route in-process GUI work through the application's local API.
- Do not emit Qt signals while holding a non-reentrant lock.
- Every production change follows TDD: add a test at the new interface, observe the expected failure, then move/refactor implementation.
- Preserve Route Measurement Pause Request, Pause Ack, and Interrupt semantics. Interrupt must propagate through autofocus, photo, contact placement, lower/contact-check/external measurement, and restore a known safe Z when autofocus is aborted.
- Any Stage serial change requires coordinated inspection of `StageController` and `SerialTerminalWindow`.
- Per-pass Metrics Gate: focused pytest, full `pytest tests`, configured `ruff check --ignore E402,F401 .`, Coverage with no unexplained regression, Wily commit diff from a disposable UTF-8 clone/cache, Radon raw/CC/MI, Lizard warnings plus `-Eduplicate`, and Vulture candidate audit.
- Lower LOC alone is insufficient. Each pass must improve CC, MI, locality, interface depth, or test protection without an unexplained regression metric.

---

### Task 1: Deepen Microscope Scan runtime

**Files:**
- Create: `probe_station_gui/camera/microscope_scan_runtime.py`
- Create: `tests/camera/test_microscope_scan_runtime.py`
- Modify: `main.py:10671-11450`
- Modify: `tests/app/test_main_microscope_scan.py`

**Interfaces:**
- Consumes: existing `MicroscopeScanPlan`, `MicroscopeScanTile`, `FlatFieldScanOptions`, `CameraLockSettings`, and the existing optical-session, Stage, Camera, artifact, and event adapters.
- Produces: `MicroscopeScanRunRequest`, `MicroscopeScanRuntime`, and `MicroscopeScanRuntime.run(request) -> None`.
- `Main` remains the Qt adapter. It owns thread creation and Qt signals; it does not own scan-loop ordering, cleanup, correction, or artifact sequencing.
- The runtime must not accept `Main`, `owner: Any`, or unrestricted attribute access. Group dependencies into explicit Stage, Camera, artifact, and event ports; production and in-memory adapters justify those seams.

- [ ] **Step 1: Add the failing runtime-interface test**

  Add `tests/camera/test_microscope_scan_runtime.py` with a recording in-memory adapter and this first observable contract:

  ```python
  def test_capture_failure_returns_stage_before_camera_restore_and_session_release() -> None:
      events: list[str] = []
      runtime = microscope_scan_runtime.MicroscopeScanRuntime(
          stage=_StageAdapter(events),
          camera=_FailingCameraAdapter(events),
          artifacts=_ArtifactAdapter(events),
          event_sink=_EventSink(events),
          session=_SessionAdapter(events),
      )

      runtime.run(_single_tile_request())

      assert events.index("stage:return") < events.index("camera:restore")
      assert events.index("camera:restore") < events.index("session:release")
      assert events[-1].startswith("finished:false:")
  ```

  Fakes must implement only their named port. Reuse concrete plan/options constructors from `tests/camera/test_microscope_scan.py`; do not fake `Main`.

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  & 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/camera/test_microscope_scan_runtime.py -q
  ```

  Expected: collection fails because `probe_station_gui.camera.microscope_scan_runtime` does not exist.

- [ ] **Step 3: Move the scan transaction behind the new interface**

  Move scan-loop and cleanup behaviour currently implemented by `Main._run_microscope_scan` plus its scan-only helpers into `MicroscopeScanRuntime`. Keep these orderings exact:

  ```text
  acquire optical session
  reserve Stage operation
  apply Camera lock
  capture/move/save each tile
  save mosaics and manifest
  return Stage to starting position on every accepted run exit
  restore Camera lock
  release Stage and optical session
  emit exactly one finished outcome
  ```

  Split the current CC38 loop into private runtime methods for acquire, tile capture, artifact finalization, and cleanup. `Main._run_microscope_scan` becomes a short adapter call or is removed when no caller requires its old private name.

- [ ] **Step 4: Replace tests past the old interface**

  Move the cleanup/session/camera-order assertions from `tests/app/test_main_microscope_scan.py` to `tests/camera/test_microscope_scan_runtime.py`. Keep app tests only for Qt start/stop/thread wiring and signal presentation. Preserve direct cases for session rejection, capture failure, stop request, return failure, restore failure, fixed exposure, and successful artifacts.

- [ ] **Step 5: Verify GREEN and focused parity**

  Run:

  ```powershell
  $py = 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
  & $py -m pytest tests/camera/test_microscope_scan_runtime.py tests/camera/test_microscope_scan.py tests/app/test_main_microscope_scan.py -q
  ```

  Expected: all selected tests pass with no warnings.

- [ ] **Step 6: Run full gate, record metrics, commit**

  Targets: remove at least 600 physical lines and 100 aggregate CC from `main.py`; new runtime MI greater than `0.00`; no new Lizard warning-level function; full suite and Ruff pass; Coverage does not regress unexplained.

  Commit:

  ```powershell
  git add main.py probe_station_gui/camera/microscope_scan_runtime.py tests/camera/test_microscope_scan_runtime.py tests/app/test_main_microscope_scan.py
  git commit -m "refactor: deepen microscope scan runtime"
  ```

### Task 2: Deepen MicroscopeView minimap module

**Files:**
- Create: `probe_station_gui/views/microscope_minimap.py`
- Create: `tests/ui/test_microscope_minimap.py`
- Modify: `probe_station_gui/views/microscope_view.py:64-366,1197-1996`
- Modify: `tests/ui/test_microscope_view_minimap.py`
- Modify: `tests/ui/test_microscope_view_klayout_minimap.py`

**Interfaces:**
- Consumes: immutable design-document/minimap inputs, size and FOV inputs, existing real KLayout/background workers.
- Produces: `MicroscopeMinimap`, `configure(...)`, `draw(painter, rect)`, `map_click(point, rect)`, and `shutdown()`.
- `MicroscopeView` remains QWidget adapter and owns Qt events. The minimap module owns request generations, worker retirement, caches, static/background rendering, route/mark/position drawing, coordinate mapping, and stale-result rejection.

- [ ] **Step 1: Add the failing stale-generation test**

  Add a direct interface test:

  ```python
  def test_stale_background_result_cannot_replace_latest_generation() -> None:
      minimap = MicroscopeMinimap(renderer=_DeferredRenderer())
      first = minimap.configure(_document("first"), _bounds())
      second = minimap.configure(_document("second"), _bounds())

      minimap.accept_background(first, _pixmap("old"))
      minimap.accept_background(second, _pixmap("new"))

      assert minimap.background_cache_key.document_id == "second"
      assert minimap.background_pixel(0, 0) == QColor("new")
  ```

  Use a real `QImage`/`QPixmap` under the repository's existing offscreen Qt test setup; `_DeferredRenderer` is the in-memory adapter for the existing worker seam.

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  & 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/ui/test_microscope_minimap.py -q
  ```

  Expected: collection fails because `MicroscopeMinimap` is absent.

- [ ] **Step 3: Move minimap state and implementation**

  Move minimap configuration, invalidation, worker lifecycle, cache, drawing, coordinate mapping, and click-delay state from `MicroscopeView` into the new module. Keep `MicroscopeView.paintEvent` and mouse events as adapters calling `draw`/`map_click`. Preserve current visual geometry, route arrowheads, layer colours, request-generation rejection, bounded worker shutdown, and click delay.

- [ ] **Step 4: Replace private-widget tests with interface tests**

  Move cache, stale-generation, document replacement, resize, mapping, and shutdown assertions to `tests/ui/test_microscope_minimap.py`. Keep widget tests for event-to-interface delegation and rendered integration only.

- [ ] **Step 5: Verify GREEN and focused parity**

  Run:

  ```powershell
  $py = 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
  & $py -m pytest tests/ui/test_microscope_minimap.py tests/ui/test_microscope_view_minimap.py tests/ui/test_microscope_view_klayout_minimap.py -q
  ```

  Expected: all selected tests pass with no warnings.

- [ ] **Step 6: Run full gate, record metrics, commit**

  Targets: remove at least 450 physical lines and 70 aggregate CC from `microscope_view.py`; new minimap module MI greater than `0.00`; no new worker leak or Lizard warning; full suite/Ruff/Coverage gate passes.

  Commit:

  ```powershell
  git add probe_station_gui/views/microscope_minimap.py probe_station_gui/views/microscope_view.py tests/ui/test_microscope_minimap.py tests/ui/test_microscope_view_minimap.py tests/ui/test_microscope_view_klayout_minimap.py
  git commit -m "refactor: deepen microscope minimap module"
  ```

### Task 3: Deepen Stage motion execution

**Files:**
- Create: `probe_station_gui/stage/motion_execution.py`
- Create: `tests/stage/test_motion_execution.py`
- Modify: `probe_station_gui/stage/motion_commands.py:505-875`
- Modify: `probe_station_gui/stage/controller.py`
- Modify: `tests/stage/test_motion_command_planning.py`
- Modify: `tests/stage/test_controller_jog_motion.py`
- Inspect without incidental semantics change: `probe_station_gui/views/serial_terminal_window.py`

**Interfaces:**
- Consumes: existing motion plans, safety state, coordinate/limit state, status queries, serial write queue, motion timing, and cancellation token.
- Produces: `StageMotionExecution`, `run_relative(plan)`, and `run_absolute(plan)`; request/thread/Qt methods stay on `StageController`.
- Real FluidNC serial/session and in-memory recording adapters are the two adapters at the seam. The execution module must not accept `StageController`, `owner: Any`, or unrestricted `__getattr__`.

- [ ] **Step 1: Add the failing transaction-order test**

  Add:

  ```python
  def test_relative_execution_checks_safety_and_limits_before_serial_write() -> None:
      events: list[str] = []
      execution = StageMotionExecution(
          safety=_SafetyAdapter(events, allowed=True),
          limits=_LimitAdapter(events, allowed=True),
          status=_StatusAdapter(events),
          serial=_SerialAdapter(events),
          cancellation=_CancellationAdapter(events),
      )

      execution.run_relative(_x_move_plan(delta_mm=0.25, feedrate=120.0))

      assert events[:4] == ["cancel:check", "safety:check", "status:read", "limits:check"]
      assert events[-1].startswith("serial:G91 G21 G1 X0.25 F120")
  ```

  Test rejected safety/limit paths separately and assert zero serial writes.

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  & 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/stage/test_motion_execution.py -q
  ```

  Expected: collection fails because `StageMotionExecution` is absent.

- [ ] **Step 3: Move motion transaction implementation**

  Move relative/absolute send transaction, current-position requirement, limit checks, G-code emission, timeout calculation, and B-axis reference handling from the unrestricted mixin implementation into `StageMotionExecution`. Keep request acceptance, QThread task start, Qt signals, and the existing public controller method names in `StageController`/`MotionCommandsMixin`.

- [ ] **Step 4: Replace tests past the mixin seam**

  Move direct lock/write/status ordering assertions to `tests/stage/test_motion_execution.py`. Keep controller tests for accepted task, signal, cancellation, and GUI-facing behaviour. Verify `SerialTerminalWindow` coordination remains unchanged.

- [ ] **Step 5: Verify GREEN and focused parity**

  Run:

  ```powershell
  $py = 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
  & $py -m pytest tests/stage/test_motion_execution.py tests/stage/test_motion_command_planning.py tests/stage/test_controller_jog_motion.py tests/stage/test_jog_commands.py tests/ui/test_serial_terminal_window.py -q
  ```

  Expected: all selected tests pass with no warnings.

- [ ] **Step 6: Run full gate, record metrics, commit**

  Targets: remove unrestricted controller-state access from moved execution code; reduce `motion_commands.py` by at least 250 physical lines and 50 aggregate CC; new execution module MI greater than `0.00`; serial output and safety ordering unchanged.

  Commit:

  ```powershell
  git add probe_station_gui/stage/motion_execution.py probe_station_gui/stage/motion_commands.py probe_station_gui/stage/controller.py tests/stage/test_motion_execution.py tests/stage/test_motion_command_planning.py tests/stage/test_controller_jog_motion.py
  git commit -m "refactor: deepen stage motion execution"
  ```

### Task 4: Deepen Route Contact Flow

**Files:**
- Modify: `probe_station_gui/route/contact_lifecycle.py`
- Modify: `probe_station_gui/route/measurement.py:469-650`
- Create: `tests/route/test_contact_lifecycle_interface.py`
- Modify: `tests/route/test_measurement_contact_seek.py`
- Modify: `tests/route/test_measurement_api_route_control.py`

**Interfaces:**
- Consumes: named Stage, autofocus/photo, meter, contact-quality, interrupt, and event ports already represented by runner dependencies.
- Produces: `RouteContactFlow` with `place_contact`, `prepare_external_contact`, `lift_after_external_measurement`, `check_contact`, `seek_contact`, and `measure_current_contact`.
- `RouteMeasurementRunner` remains route coordinator. The contact module must not accept `owner: Any` or reach into arbitrary runner private state.

- [ ] **Step 1: Add the failing Interrupt test at the new interface**

  Add:

  ```python
  def test_interrupt_after_autofocus_skips_lower_check_and_external_measurement() -> None:
      events: list[str] = []
      interrupt = _InterruptAdapter()
      flow = _contact_flow(events, interrupt=interrupt)
      autofocus = _AutofocusAdapter(events, after=lambda: interrupt.request())

      result = flow.place_contact(_contact_request(autofocus=autofocus))

      assert result.interrupted is True
      assert "autofocus:restore-safe-z" in events
      assert "needles:lower" not in events
      assert "contact:check" not in events
      assert "meter:external" not in events
  ```

  Add a second direct test proving Pause Request does not become Pause Ack until the flow reaches its safe waiting point.

- [ ] **Step 2: Verify RED**

  Run:

  ```powershell
  & 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/route/test_contact_lifecycle_interface.py -q
  ```

  Expected: collection fails because `RouteContactFlow` is absent.

- [ ] **Step 3: Move contact invariants behind the new interface**

  Convert current `contact_lifecycle.py` functions into the `RouteContactFlow` implementation with explicit named ports. Preserve all existing ordering, interruption, lift, safe-Z, contact-quality, retry, external-contact, and result-record behaviour. Runner methods become short calls needed for compatibility; no new behaviour or UI copy.

- [ ] **Step 4: Replace tests past the runner interface**

  Move contact-only ordering and interrupt assertions into the direct interface test. Keep runner tests for route checkpoint propagation, Pause Request/Pause Ack GUI state, and end-to-end route flow.

- [ ] **Step 5: Verify GREEN and focused safety parity**

  Run:

  ```powershell
  $py = 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
  & $py -m pytest tests/route/test_contact_lifecycle_interface.py tests/route/test_measurement_contact_seek.py tests/route/test_measurement_api_route_control.py tests/route/test_measurement.py tests/app/test_main_route_control.py -q
  ```

  Expected: all selected tests pass with no warnings, including Interrupt/autofocus/photo/contact placement and Pause Request/Pause Ack cases.

- [ ] **Step 6: Run full gate, record metrics, commit**

  Targets: eliminate `owner: Any` and arbitrary runner-private access from `contact_lifecycle.py`; reduce `measurement.py` interface/implementation leakage and aggregate CC; new/changed contact module MI greater than `0.00`; no Route safety regression.

  Commit:

  ```powershell
  git add probe_station_gui/route/contact_lifecycle.py probe_station_gui/route/measurement.py tests/route/test_contact_lifecycle_interface.py tests/route/test_measurement_contact_seek.py tests/route/test_measurement_api_route_control.py
  git commit -m "refactor: deepen route contact flow"
  ```

## Cycle Completion

After Task 4 review is approved:

1. Run the whole-branch Metrics Gate against base `e9832f5`.
2. Record current MI/LOC/CC for `main.py`, `microscope_view.py`, `motion_commands.py`, and `route/measurement.py` in `.superpowers/sdd/progress.md`.
3. Dispatch a whole-branch reviewer.
4. If `main.py` MI remains `0.00`, create Cycle 2 from current hotspots and continue automatically in the same order: Camera/Main, MicroscopeView, Stage, Route.
