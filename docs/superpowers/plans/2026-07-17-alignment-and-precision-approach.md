# Alignment and Precision Approach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore unambiguous design alignment with an arbitrary number of point pairs, add reusable backlash-aware final approach and persisted coordinate confidence for every stage axis, expose that confidence in the coordinate panel, and move the serial terminal into a lazy standalone Tools window.

**Architecture:** Keep geometry fitting and motion planning pure, then let `StageController` own execution, confidence transitions, and persistence. Existing GUI/API/route entry points continue to call the controller, but absolute target workflows pass through one precision planner. Design alignment remains a `DesignSession` concern while the plot and alignment panel only manage draft/capture interaction. The terminal remains the existing widget, created lazily and hosted by a modeless window.

**Tech Stack:** Python 3.12, PySide6, NumPy, pytest, JSON settings/state, FluidNC G-code.

## Global Constraints

- Work only in `C:\Users\Lazemir\.codex\worktrees\alignment-backlash-approach\probe_station_gui` on `codex/alignment-backlash-approach`.
- Run Python through `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.
- Write the failing regression test before each production change and observe its intended failure.
- Never perform hardware I/O in automated tests; use the existing fake serial/controller fixtures.
- Preserve route Pause/Resume/Interrupt checkpoint semantics and test interruption between precision preparation and final motion.
- Do not emit Qt signals while holding a non-reentrant lock.
- Keep heavy/optional terminal construction out of startup and keep in-process movement on direct controller calls.
- Preserve coordinate field fill colors; the new confidence stripe is an independent visual channel.

---

## Task 1: Add precision-approach settings and editor

**Files:**

- Create: `probe_station_gui/settings/precision_approach.py`
- Create: `probe_station_gui/dialogs/settings/precision_approach.py`
- Modify: `probe_station_gui/settings/manager.py`
- Modify: `probe_station_gui/settings/section_parsing.py`
- Modify: `probe_station_gui/settings/default_file.py`
- Modify: `probe_station_gui/default_settings.json`
- Modify: `probe_station_gui/dialogs/settings_dialog.py`
- Create: `tests/settings/test_precision_approach_settings_model.py`
- Test: `tests/settings/test_section_parsing.py`
- Test: `tests/settings/test_default_file.py`
- Create: `tests/ui/test_precision_approach_settings.py`

- [x] Add failing model/parser tests for a per-axis profile with `enabled`, non-negative `backlash`, and `final_direction` constrained to `+1` or `-1`; verify missing user settings merge defaults for all available axes.
- [x] Add failing default tests for A=`disabled, 0, -1`, Z=`enabled, 0.03 mm, +1`, and X/Y/B/C=`disabled, 0, +1`.
- [x] Implement immutable `PrecisionApproachProfile` and a `PrecisionApproachSettings` mapping with normalization and a deterministic fingerprint payload.

```python
@dataclass(frozen=True)
class PrecisionApproachProfile:
    enabled: bool = False
    backlash: float = 0.0
    final_direction: int = 1

    def __post_init__(self) -> None:
        if not math.isfinite(self.backlash) or self.backlash < 0:
            raise ValueError("backlash must be finite and non-negative")
        if self.final_direction not in (-1, 1):
            raise ValueError("final_direction must be -1 or +1")
```

- [x] Wire the section into `Settings`, JSON parsing, normalized defaults, save/load, and the shipped default file without changing existing settings keys.
- [x] Add failing Qt tests for a “Precision approach” settings section with one row per axis: enabled checkbox, backlash spinbox using the axis display unit, final-side selector, and a concise approach preview.
- [x] Implement the editor and connect it to `SettingsDialog` apply/reset flows. Disable the remaining row controls when the profile is disabled while preserving their values.
- [x] Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_precision_approach_settings_model.py tests/settings/test_section_parsing.py tests/settings/test_default_file.py tests/ui/test_precision_approach_settings.py -q
```

- [x] Commit: `feat: add precision approach settings`

## Task 2: Implement the pure approach planner and confidence state

**Files:**

- Create: `probe_station_gui/stage/precision_approach.py`
- Create: `probe_station_gui/stage/coordinate_confidence.py`
- Create: `tests/stage/test_precision_approach.py`
- Create: `tests/stage/test_coordinate_confidence.py`

- [x] Add table-driven failing tests for disabled/zero-backlash direct moves, already-loaded direct moves, approximate coordinates, insufficient same-direction travel, reversals, mixed-axis preparation, relative-target resolution, and an out-of-limit preparation that rejects the entire operation.
- [x] Implement pure plan value objects. Planning occurs in calibrated display coordinates; callers supply converted current/target values and validate the returned complete target maps before G-code conversion.

```python
@dataclass(frozen=True)
class PrecisionMovePlan:
    preparation_target: Mapping[str, float] | None
    final_target: Mapping[str, float]
    prepared_axes: frozenset[str]
```

- [x] For direction `+1`, prepare at `target - backlash`; for `-1`, prepare at `target + backlash`. A preparation map holds every non-prepared axis at the current coordinate, then the final map contains the complete requested target.
- [x] Add failing state-machine tests: exact after a completed final approach; exact survives same-direction motion; reversal immediately makes approximate; confirmed final-direction travel restores exact only after accumulated travel reaches backlash; homing/reset/failure/profile change makes approximate.
- [x] Implement `AxisCoordinateConfidence` with `exact`, loaded direction, last confirmed machine coordinate, and accumulated take-up travel. Keep transitions pure and event-driven.
- [x] Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_precision_approach.py tests/stage/test_coordinate_confidence.py -q
```

- [x] Commit: `feat: add precision motion planner`

## Task 3: Execute precision plans in StageController and persist confidence

**Files:**

- Create: `probe_station_gui/stage/precision_motion.py`
- Modify: `probe_station_gui/stage/controller.py`
- Modify: `probe_station_gui/stage/motion_commands.py`
- Modify: `probe_station_gui/stage/connection_state.py`
- Modify: `main.py`
- Modify: `probe_station_gui/settings/manager.py`
- Create: `tests/stage/test_precision_motion.py`
- Test: `tests/stage/test_controller_cache.py`
- Test: `tests/stage/test_controller_status_session.py`

- [x] Add failing controller tests proving one accepted logical operation may send preparation and final absolute segments, reports busy/finished once, checks cancellation and safety between segments, and marks affected axes approximate after send failure or cancellation.
- [x] Add `PrecisionMotionMixin` that resolves relative requests to absolute display-space targets, obtains one plan, validates all segments before the first send, executes them in the existing background operation, and updates confidence only from confirmed status/motion outcomes.
- [x] Route `request_absolute_axis_targets_move`, `run_external_absolute_axis_targets_move`, and relative coordinate commands through the mixin. Add an explicit bypass used only by jog, homing, autofocus search/oscillation, and planner-internal segment sending.
- [x] Add failing persistence tests for versioned confidence records in `controller-state.json`: exact restoration requires the same volatile controller session marker, a fresh matching position within the named tolerance, and the same profile/calibration fingerprint. Verify one-axis position mismatch invalidates only that axis and marker mismatch invalidates all enabled axes.
- [x] Extend the existing controller-state export/restore schema without adding a second state file. Never serialize an in-flight prediction as exact.
- [x] Invalidate confidence on reset/session change, homing, manual-command notification, profile/calibration changes, live position mismatch, and failed/partial motion. Keep signal emission outside locks.
- [x] Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_precision_motion.py tests/stage/test_controller_cache.py tests/stage/test_controller_status_session.py tests/stage/test_motion_command_planning.py -q
```

- [x] Commit: `feat: persist precise coordinate confidence`

## Task 4: Integrate target workflows and remove the autofocus special case

**Files:**

- Modify: `probe_station_gui/stage/click_move.py`
- Modify: `probe_station_gui/stage/autofocus_flow.py`
- Modify: `probe_station_gui/stage/controller.py`
- Modify: `main.py`
- Modify: `probe_station_gui/api/server.py`
- Modify: `probe_station_gui/stage/api_moves.py`
- Modify: `probe_station_gui/route/contact_lifecycle.py`
- Modify: `probe_station_gui/route/external_session.py`
- Create: `tests/stage/test_controller_click_move.py`
- Create: `tests/stage/test_controller_autofocus.py`
- Test: `tests/app/test_main_stage_coordinate_controls.py`
- Test: `tests/stage/test_api_moves.py`
- Test: `tests/api/test_server.py`
- Test: `tests/app/test_main_route_control.py`

- [x] Add failing tests that GUI G90/G91, local API G90/G91, click-to-move, route XY travel, autofocus final restoration, exact needle targets, and B rotation all reach the shared planner; assert jog, homing, and autofocus sampling bypass it.
- [x] Resolve click-to-move pixel displacement to a complete absolute target before planning, retaining the direct in-process controller call.
- [x] Replace `AUTOFOCUS_BACKLASH_MM` and `_approach_z_from_below_locked` planning with the Z precision profile. Autofocus sampling keeps raw moves; final restore uses the shared approach and returns to the starting or another known-safe Z when interrupted.
- [x] Make route travel use the shared absolute target executor without changing the public API route-control state machine.
- [ ] Add regression tests for Pause/Resume/Interrupt, especially interruption after the preparation segment: no final movement and no lower/contact-check/external-measurement step may run afterward.
- [ ] Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_click_move.py tests/stage/test_controller_autofocus.py tests/app/test_main_stage_coordinate_controls.py tests/stage/test_api_moves.py tests/api/test_server.py tests/app/test_main_route_control.py -q
```

- [x] Commit: `feat: apply precision approach to target moves`

## Task 5: Show coordinate confidence stripes and legend

**Files:**

- Modify: `probe_station_gui/stage/position_presenter.py`
- Modify: `probe_station_gui/views/stage_position_panel.py`
- Modify: `main.py`
- Test: `tests/stage/test_position_presenter.py`
- Test: `tests/ui/test_stage_position_panel.py`

- [x] Add failing presenter tests for stripe visibility and color: green exact, red approximate, hidden for disabled profile, unavailable coordinate, or active limit. Verify purple edited-target fill and the stripe coexist.
- [x] Extend the presentation model with an optional confidence role; do not overload the existing homed/unhomed/limit/edited fill role.
- [x] Implement a 4 px bottom stripe inside each coordinate field and update it only when its axis presentation changes.
- [x] Add a compact legend under the coordinate grid covering existing fill semantics and both confidence stripes. Add a tooltip explaining exact versus approximate coordinates.
- [x] Connect controller confidence signals event-wise; do not add polling or full widget rebuilds.
- [ ] Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_position_presenter.py tests/ui/test_stage_position_panel.py -q
```

- [x] Commit: `feat: display coordinate confidence`

## Task 6: Fit and persist arbitrary multi-point registrations

**Files:**

- Modify: `probe_station_gui/design/model.py`
- Modify: `probe_station_gui/design/session.py`
- Modify: `probe_station_gui/design/navigation_adapter.py`
- Create: `tests/design/test_registration.py`
- Test: `tests/design/test_workflow.py`
- Test: `tests/design/test_navigation_adapter.py`

- [x] Add failing numerical tests for an all-point least-squares 2D similarity transform with two, three, and noisy point pairs. Verify proper rotation only, uniform scale, translation, RMS residual, maximum source residual, and no hidden pass/fail residual threshold.
- [x] Add rejection tests for fewer than two pairs, length mismatch, non-finite coordinates, and coincident/degenerate source geometry. Reflection-shaped input must still return the best proper-rotation fit and its visible residuals.
- [x] Replace first-two-point fitting with centered SVD/Procrustes fitting and expose residual statistics on `DesignRegistration`.

```python
source_centered = source - source.mean(axis=0)
stage_centered = stage - stage.mean(axis=0)
u, singular, vt = np.linalg.svd(source_centered.T @ stage_centered)
rotation = u @ np.diag([1.0, np.linalg.det(u @ vt)]) @ vt
scale = np.sum(singular * np.array([1.0, np.linalg.det(u @ vt)])) / np.sum(source_centered**2)
```

- [x] Convert session source/stage marks and `AlignmentPreparation` to variable-length immutable tuples. Derive B correction from the fitted angular component, rotate every captured stage point around the existing `(0, 0)` B pivot after successful B motion, then rebuild the all-point registration.
- [x] Version the design state payload and migrate legacy version-1 two-slot mark lists.
- [ ] Preserve existing registration while a new draft is incomplete.
- [ ] Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_registration.py tests/design/test_workflow.py tests/design/test_navigation_adapter.py -q
```

- [x] Commit: `feat: support multipoint design registration`

## Task 7: Add the dedicated Align interaction and dynamic capture panel

**Files:**

- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Modify: `probe_station_gui/views/design_plot_pane.py`
- Modify: `probe_station_gui/views/alignment_panel.py`
- Modify: `probe_station_gui/design/objective_alignment.py`
- Modify: `main.py`
- Test: `tests/ui/test_design_navigator_panel.py`
- Test: `tests/ui/test_design_plot_selection.py`
- Test: `tests/design/test_objective_alignment.py`
- Test: `tests/app/test_main_objective_alignment.py`
- Test: `tests/app/test_main_design_navigation.py`

- [ ] Add failing UI tests for an `Align` toolbar action. In Align mode, snapped LMB appends numbered D1, D2, … points; RMB keeps ordinary context behavior; Point mode still creates route points; normal selection/crossing selection stays unchanged.
- [ ] Add draft commands: Undo removes the newest design point, Clear removes the draft, Done and Enter accept at least two distinct points, Esc discards the draft and exits Align. Test Esc both before and after the first point.
- [ ] Keep the prior registration active throughout design draft and stage capture. Mark it stale only when actual B rotation begins.
- [ ] Replace the fixed two-row alignment panel with dynamic Dn/Sn rows, each with Capture/Replace. Automatically select the next incomplete stage row and enable fitting only when every row is captured.
- [ ] On final capture, fit all pairs, show RMS and max residual, request B correction through the shared precision executor, and apply the registration immediately if the correction is negligible. On successful rotation, rotate all captured stage marks and refit.
- [ ] Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_navigator_panel.py tests/ui/test_design_plot_selection.py tests/design/test_objective_alignment.py tests/app/test_main_objective_alignment.py tests/app/test_main_design_navigation.py -q
```

- [ ] Commit: `feat: add multipoint align tool`

## Task 8: Move Terminal to a lazy Tools window

**Files:**

- Modify: `probe_station_gui/views/main_window_docks.py`
- Modify: `probe_station_gui/views/main_window_menus.py`
- Modify: `probe_station_gui/views/main_window_connection_flow.py`
- Modify: `probe_station_gui/views/main_window_shutdown.py`
- Modify: `main.py`
- Modify: `probe_station_gui/views/serial_terminal_window.py`
- Test: `tests/ui/test_main_window_docks.py`
- Test: `tests/ui/test_main_window_menus.py`
- Test: `tests/ui/test_serial_terminal_window.py`
- Test: `tests/ui/test_main_window_connection_flow.py`
- Test: `tests/ui/test_main_window_shutdown.py`

- [ ] Add failing startup tests proving the Connection dock has no Terminal tab and `SerialTerminalWindow` is not constructed before activation.
- [ ] Add a `Terminal` action to Tools without an ellipsis. On first activation create one standalone modeless window, bind the current serial connection, connect history/send/live polling/Ctrl+X/manual-command signals, then show it. Subsequent activations reuse, raise, and focus it.
- [ ] Make close hide the window without disconnecting. Connection/disconnection callbacks synchronize it only when it exists; application shutdown still closes it cleanly.
- [ ] Preserve manual command behavior: invalidate needle state and mark all enabled precision axes approximate.
- [ ] Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window_docks.py tests/ui/test_main_window_menus.py tests/ui/test_serial_terminal_window.py tests/ui/test_main_window_connection_flow.py tests/ui/test_main_window_shutdown.py -q
```

- [ ] Commit: `refactor: open terminal lazily from tools`

## Task 9: Cross-feature verification and cleanup

**Files:**

- Modify only files required by failures found below.
- Test: `tests/`

- [ ] Run focused safety regressions:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_route_control.py tests/stage/test_controller_axis_needles.py tests/stage/test_controller_jog_motion.py tests/stage/test_controller_status_session.py -q
```

- [ ] Run all design, stage, UI, API, and app tests:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design tests/stage tests/ui tests/api tests/app -q
```

- [ ] Run the complete suite:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

- [ ] Inspect `git diff --check`, `git status --short`, and the final diff for accidental startup work, localhost self-calls, ellipses in menu labels, unrelated edits, and generated files.
- [ ] Commit any verification-only corrections with a narrowly scoped message.
