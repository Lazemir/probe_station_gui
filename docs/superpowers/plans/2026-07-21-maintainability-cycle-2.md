# Maintainability Deepening Cycle 2

> **For Codex:** Execute with `subagent-driven-development`. Use one implementer
> and one read-only reviewer per task. Fix every actionable finding before moving
> to the next domain.

**Goal:** Continue the round-robin architecture deepening until `main.py` is a
composition root with positive Maintainability Index. Cycle 2 removes the largest
remaining Camera/Main transaction, then deepens Microscope UI interaction, Stage
operation ownership, and one-point Route execution.

**Base:** `e0941c4`

**Tech stack:** Python 3.11, PySide6, pytest 9, Coverage.py 7, Ruff, Wily 1.25,
Radon 5.1, Lizard 1.23, Vulture 2.16.

## Global invariants and gates

- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for all
  Python commands. Do not run hardware-dependent code.
- Preserve GUI-thread affinity: Qt widgets, painters, timers, and UI signals stay
  on the GUI thread. Stage I/O, camera waits, fitting, file writes, and joins stay
  in workers.
- Keep startup lightweight and lazy. New runtimes may allocate locks and inert
  state only; no camera scan, serial access, file parsing, or heavy import at
  startup.
- Preserve Stage serial/status ordering and terminal coordination.
- Preserve API route control names and exact Pause Request/Pause Ack/Interrupt
  semantics. Interrupt must survive until the runner checkpoint; autofocus must
  return to a known safe Z before control continues.
- New modules use explicit named ports. Do not pass `Main`, a runner/controller as
  `owner: Any`, unrestricted `Any`, `getattr` state bags, or mega-ports.
- No metric gaming by comments, line compression, or forwarding wrappers.
- Per task: capture RED, run focused GREEN, then one exact full Coverage gate,
  configured `ruff check --ignore E402,F401 .`, Wily from a disposable UTF-8
  squash/clone, Radon raw/CC/MI, Lizard warning and duplicate checks, Vulture
  candidate audit, and `git diff --check`.
- Every extracted production module must have MI greater than `0.00`; a target
  monolith may remain at zero only when LOC/CC fall materially and the next seam is
  recorded.

## Task 1 — Camera/Main: deepen optical-calibration runtime

**Current evidence:** `main.py:5076-6570` spans about 1,495 lines and aggregate CC
332. The flat-field and lens worker transactions, nested optical-session ownership,
cancel state, fit validation, stale-result rejection, and shutdown joining are
distributed across `Main` and `main_window_shutdown.py`.

**Files:**

- Create `probe_station_gui/camera/optical_calibration_runtime.py`
- Create `probe_station_gui/camera/optical_calibration_adapters.py`
- Optionally create `probe_station_gui/camera/optical_calibration_geometry.py` when
  required to keep runtime functions below warning thresholds
- Modify `main.py`
- Modify `probe_station_gui/views/main_window_shutdown.py`
- Create `tests/camera/test_optical_calibration_runtime.py`
- Modify only the necessary optical calibration app/controller/shutdown tests

**Deep interface:**

```python
runtime.start_flat(request) -> CalibrationStartDecision
runtime.start_lens(request) -> CalibrationStartDecision
runtime.cancel(run_id=None) -> None
runtime.state() -> OpticalCalibrationState
runtime.shutdown(timeout_s: float) -> bool
```

Use immutable requests/results and named Stage, Camera, OpticalSession, Store, and
Event ports. Geometry fitting remains an implementation, not a port. The real
Stage adapter may bridge the current controller API; Task 3 will deepen ownership
behind that adapter without changing this runtime contract.

`Main` keeps wizard/dialog creation, immutable request capture, settings
persistence, objective UI refresh, API formatting, and short queued result slots.
Move session/worker ownership, cancellation, capture grids, raw-frame acquisition,
fit/validation, cleanup ordering, stale-run rejection, and shutdown internals into
the runtime.

**Binding parity:**

- Open the parent/nested optical session before Stage reservation. Session failure
  performs no Stage or Camera work.
- A full wizard reuses one parent session across flat-field then lens calibration.
- Every reserved exit restores Stage after movement, restores Camera settings,
  releases Stage, then closes the child/parent session according to current mode.
- Cancel between phases, exposure-policy contention retry, thread-start failure,
  frame-size rejection, serpentine offsets/Y convention, raw frames, and stale
  outcomes retain current behavior.
- A valid lens artifact remains available when fitting succeeded but Camera/session
  restoration later reports an error.
- Never emit a Qt-facing callback while holding the runtime lock.

**TDD:** First add a direct failing test proving session-open failure causes zero
Stage/Camera calls. Then cover cleanup ordering on every failure, full-wizard lease
reuse, cancel between phases, shutdown timeout/retry, stale outcomes, thread-start
failure, raw grid order, affine offset conversion, constant frame size, and
successful-artifact-plus-restore-warning behavior. Move transaction assertions out
of `Main.__new__` fakes; retain app tests for presentation/wiring.

**Focused command:**

```powershell
$py = 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
& $py -m pytest tests/camera/test_optical_calibration_runtime.py tests/camera/test_flat_field_calibration.py tests/camera/test_optical_session.py tests/stage/test_controller_optical_sessions.py tests/app/test_main_optical_calibration.py tests/app/test_main_flat_field_calibration.py tests/app/test_main_lens_distortion.py tests/ui/test_main_window_shutdown.py -q
```

**Targets:** Remove 1,150–1,300 physical lines and 230–280 CC from `main.py`;
target `main.py <= 9,950 LOC`, CC `<= 1,790`; every new file MI `> 5`; no function
CC `>= 15` or length `>= 100`.

**Commit:** `refactor: deepen optical calibration runtime`

## Task 2 — MicroscopeView/UI: deepen interaction and click lifecycle

**Current evidence:** `microscope_view.py:284-599` owns 316 lines/CC 82 of pointer,
measurement, target, and alignment state; its measurement painting at `755-1035`
adds 281 lines. The same click lifecycle spills into `main.py:3718-3829` and
`4196-4207` for roughly 125 lines/CC 27, while `stage/move_lifecycle.py` reaches
raw pending fields and view methods.

**Files:**

- Create `probe_station_gui/views/microscope_interaction.py`
- Create `probe_station_gui/views/microscope_overlay_rendering.py` if needed
- Modify `probe_station_gui/views/microscope_view.py`
- Modify `probe_station_gui/stage/move_lifecycle.py`
- Modify `main.py`
- Create `tests/ui/test_microscope_interaction.py`
- Modify only relevant view/move-lifecycle/app tests

**Deep interface:** Own pointer mapping, press/drag/release state, measurement and
alignment overlays, target animation, pending-click retry/timeout, and cached hover
presentation. Expose narrow event handlers, `draw`, mode/alignment setters,
`try_start_move`, pending/cancel/finish methods, and `shutdown`. Construction takes
explicit `ClickMoveBindings`; it never receives `Main` or a raw widget/controller
owner.

The QWidget retains the actual frame/minimap/crosshair paint and thin Qt event
entrypoints. `Main` retains wiring, manual-alignment capture, coordinate display,
and API formatting. Stage move lifecycle depends only on the new pending/cancel/
finish interface.

**Binding parity:** Keep minimap event precedence, click-on-release behavior,
image Y-up conversion, drag/outside cancellation, measurement/alignment motion
suppression, Ctrl ruler locking, cached-only hover, 150 ms latest-click retry,
configured timeout, API no-queue behavior, X/Y mutation, target duration
`distance/feed * 60 + 0.03` with 50 ms minimum, global Cancel semantics, and GUI
thread affinity. No status read or serial work enters the widget/runtime.

**TDD:** First RED proves a busy Stage queues one click, then starts exactly once
when ready and clears pending without clearing the target. Add timeout, press/
release/drag boundaries, minimap precedence, Y conversion, measurement/alignment,
Ctrl lock, hover/leave, target timing, and global move cancellation tests. Keep
real Qt event/painting integrations while moving state assertions to the direct
interface.

**Focused command:**

```powershell
$py = 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
& $py -m pytest tests/ui/test_microscope_interaction.py tests/ui/test_microscope_view_minimap.py tests/stage/test_move_lifecycle.py tests/app/test_main_lens_distortion.py tests/app/test_main_planned_move_prediction.py -q
```

**Targets:** Remove 100–140 lines and 25–30 CC from `main.py`; reduce
`microscope_view.py` by 500–600 lines and 95–110 CC to `<= 525 LOC`, CC `<= 50`,
MI `> 15`; every new file MI `> 5` with no Lizard warning.

**Commit:** `refactor: deepen microscope interaction`

## Task 3 — Stage: deepen operation lifecycle

**Current evidence:** `stage/controller.py` is 1,281 SLOC/CC 278/MI 0. Its
operation ownership cluster at `917-1209`, initialization, terminal idle gating,
and calibration candidate handoff carry about CC 77 and leak into autofocus,
click-move, and connection mixins. The serial terminal itself is positive MI and
is not the extraction target.

**Files:**

- Create `probe_station_gui/stage/operation_lifecycle.py`
- Modify `probe_station_gui/stage/controller.py`
- Modify only the Stage mixins that directly access task ownership state
- Modify `main.py` external-operation blocks when an exception-safe lease deletes
  real `try/finally` plumbing
- Create `tests/stage/test_operation_lifecycle.py`
- Modify relevant controller/terminal/app tests

**Deep interface:** `start_background`, `reserve_external`, `cancel`, and
`snapshot`; return a thread-owned exception-safe `StageOperationLease`. Keep a
private token-bound calibration candidate mailbox. `StageController` retains Qt
signals, needle/coordinate invalidation decisions, jog-stop/reset semantics,
serial session/read/write, motion safety, and compatibility methods.

**Binding parity:** Cancellation invalidates a generation before late publish and
wakes waiters; only the owning thread releases a lease; failed begin never finishes;
cancel-active-task/motion/reset retain distinct safety effects; terminal reads
remain empty while automation owns Stage or the serial lock is unavailable;
ordinary/Ctrl+X commands retain Main invalidation; emit no signal under the
lifecycle lock.

**TDD:** First RED proves cancellation of an accepted calibration candidate wakes
the waiter and prevents publish. Add contention, exception release, stale token,
thread ownership, and controller integration showing an active lease blocks
terminal reads without touching serial. Preserve manual command invalidation and
safe-Z tests.

**Focused command:**

```powershell
$py = 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
& $py -m pytest tests/stage/test_operation_lifecycle.py tests/stage/test_calibration_callback_tokens.py tests/stage/test_controller_optical_sessions.py tests/stage/test_controller_axis_needles.py tests/stage/test_controller_status_session.py tests/ui/test_serial_terminal_window.py tests/app/test_main_stage_coordinate_controls.py tests/app/test_main_flat_field_calibration.py tests/app/test_main_lens_distortion.py -q
```

**Targets:** Remove at least 200 SLOC and 55 CC from `controller.py`; new module
MI `> 10`; remove 25–40 SLOC and 5–10 CC from `main.py` through leases; no new
Lizard warning.

**Commit:** `refactor: deepen stage operation lifecycle`

## Task 4 — Route: deepen one-point execution

**Current evidence:** `route/measurement.py` is 2,148 SLOC/CC 375/MI 0. One-point
preparation/measurement/cleanup, autofocus/photo/contact positioning, hardware,
meter, interrupt, and recording helpers span `769-2109` and carry about 175 CC.
`contact_measurement.py` and recording helpers still receive arbitrary owner state.

**Files:**

- Create `probe_station_gui/route/point_execution.py`
- Optionally create `point_acquisition.py` and `point_execution_adapters.py` to keep
  each module positive-MI and below warning thresholds
- Modify `probe_station_gui/route/measurement.py`
- Modify touched owner-coupled contact acquisition/recording helpers
- Modify `main.py` callback wiring when a typed event adapter deletes plumbing
- Create `tests/route/test_point_execution.py`
- Modify relevant route/API/app tests

**Deep interface:** `RoutePointExecution.execute(RoutePointRequest) ->
RoutePointResult`. The immutable request snapshots the point, offsets, modes,
feedrates, photo/focus flags, settle/count settings, and named quality/seek config.
Use separate narrow Motion, Acquisition, Photo, Control, and Event ports. The result
reports records, saved/emitted flags, photos, stop/interrupt/seek state, and pending
cleanup. Never pass a runner/Main owner or one aggregate mega-port.

The runner retains whole-route validation, meter lifetime, outer loop/final status,
Pause Request consumption and Pause Ack at the post-point safe checkpoint,
confirmation/skip/jump/resume, interrupt clearing at the existing next checkpoint,
and runtime-setting snapshots between points.

**Binding parity:** Interrupt after autofocus skips all later photo/contact/lower/
check/external measurement work; autofocus restores safe Z; interrupt persists to
the runner checkpoint; lift starts before result/photo callbacks with fallback
cleanup; Pause Request never equals Pause Ack and remains an Interrupt path while
pending; API status/waiting/retry semantics, CSV/event order, meter-output lifetime,
contact-seek metadata, and tunable criteria remain unchanged.

**TDD:** First RED interrupts after autofocus and proves safe-Z restoration plus no
capture/contact/lower/read/CSV/result. Add interruption during lower/read,
lift-before-result, lift failure fallback, photo-before-lower, seek exhaustion, and
runner integration for pause-during-contact acknowledged only after lift/saved
point. Preserve full external API route-control regressions.

**Focused command:**

```powershell
$py = 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe'
& $py -m pytest tests/route/test_point_execution.py tests/route/test_contact_lifecycle_interface.py tests/route/test_measurement.py tests/route/test_measurement_contact_seek.py tests/route/test_measurement_readout_quality.py tests/route/test_measurement_api_route_control.py tests/app/test_main_route_control.py -q
```

**Targets:** Remove at least 700 SLOC and 110 CC from `measurement.py`; reduce its
constructor below 30 parameters; every new implementation file MI `> 0`, no Lizard
CC > 10 or length > 80 warning; delete owner/private reach-through from touched
acquisition/recording paths; remove 40–60 SLOC and 5–10 CC from `main.py`.

**Commit:** `refactor: deepen route point execution`

## Cycle completion

After Task 4 is independently approved:

1. Run the full branch Metrics Gate against `e0941c4` and the original goal base
   `e9832f5`.
2. Record exact Wily/Radon LOC, CC, and MI for `main.py`, `microscope_view.py`,
   `stage/controller.py`, and `route/measurement.py` in the progress ledger.
3. Dispatch a fresh whole-branch reviewer and fix all actionable findings.
4. If `radon mi -s main.py` remains `0.00`, measure and begin Cycle 3 in the same
   Camera/Main → MicroscopeView → Stage → Route order without asking to continue.
