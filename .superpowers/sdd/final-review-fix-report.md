# Final Review Fix Report

## Scope and commit

- Worktree: `C:\Users\Lazemir\.codex\worktrees\alignment-backlash-approach\probe_station_gui`
- Branch: `codex/alignment-backlash-approach`
- Starting HEAD: `19b2e73c55f312a06a7fdcd295977b7bc85e7ca9`
- Commit subject: `fix: harden precision approach edge cases`
- Final commit hash: reported in the handoff because a commit cannot embed its own final hash without changing that hash.
- Hardware-dependent code was not run.

## Finding-by-finding result

1. **Approximate axes already at the numeric target** — confirmed and fixed.
   - RED: `test_external_route_move_at_target_executes_approximate_precision_axis` failed with `assert [] == [{"X": 1.0, "Y": 2.0}]`.
   - RED: `test_saved_xyz_at_target_executes_approximate_xy_precision` failed with the same skipped-executor symptom.
   - RED from final self-review: `test_saved_xyz_exact_precision_z_preserves_at_target_noop` failed because exact Z still sent a redundant segment.
   - GREEN: approximate effective profiles execute the shared planner even at the numeric target; exact or effectively disabled profiles retain the no-op path. The public external-route path and saved XYZ path are covered.

2. **Calibrated preparation clamp before validation** — confirmed and fixed.
   - RED: both `test_calibrated_z_boundary_rejects_clamped_preparation_before_send` and `test_calibrated_a_boundary_rejects_clamped_preparation_before_send` failed with `DID NOT RAISE`.
   - GREEN: every calibrated precision target now requires a tight display-to-raw-to-display round trip before raw limit validation. Both boundary tests raise before sending and assert `sent == []`.

3. **B registration invalidated before command acceptance** — confirmed and fixed.
   - RED: with the old pre-send wiring temporarily restored, the strengthened regressions failed because both callback snapshots preceded the serial write and a real software-limit rejection cleared the existing registration.
   - GREEN: the narrow `motion_started_callback` seam runs immediately after the actual jog/G1 write is accepted. The regressions exercise `_run_rotate_b`, the production precision executor and sender, and `_LineFakeSerial`: direct B and two-segment B fire once after the first accepted jog write; a genuine production limit rejection performs zero writes and fires zero callbacks.
   - Registration preservation is covered jointly by the new rejection seam regression and the existing `test_multipoint_capture_keeps_old_registration_until_rotation_starts` lifecycle regression.

4. **Cancelled autofocus restore near a limit** — confirmed and fixed.
   - RED: the restore path propagated the impossible precision preparation and never attempted the direct start-Z return; the direct-failure regression saw the wrong earlier error.
   - GREEN: precision restore failure falls back to a separately validated direct move to the known starting Z. A direct failure propagates. The integrated near-limit cancellation regression proves restoration occurs and the autofocus flow stops with cancellation still asserted.

5. **`enabled=True, backlash=0` effective state** — confirmed and fixed.
   - RED: effective UI axes/persistence still included X, live confidence still changed, and saved XYZ issued an unnecessary Z precision call (three focused failures).
   - GREEN: the shared `precision_profile_is_effective` predicate is used by planning, execution, confidence invalidation/update/restore/export, UI enabled-axis presentation, autofocus preparation, and needle final-approach selection. Zero backlash remains serializable but is mechanically and visually disabled.

6. **Axis confidence tooltip meaning** — confirmed and fixed.
   - RED: the presenter regression could not find `Accuracy: Exact` in an exact field tooltip.
   - GREEN: fields with confidence roles explain Exact as confirmed by the configured backlash approach and Approximate as not yet completed; fields without a role retain the prior tooltip.

7. **Legend geometry assertions** — confirmed as a test-coverage gap; production geometry was already correct.
   - Coverage RED: the old test asserted only one common size per group and did not assert pixel dimensions or spacing, so incorrect uniform geometry would pass.
   - GREEN: the strengthened test asserts exact state swatches `(10, 10)`, accuracy swatches `(14, 4)`, item spacing `4`, group layout spacing `10`, labels, and semantic tooltip. It passed without a production change.

8. **Route interrupt after precision preparation** — confirmed as a missing route-level regression; existing runtime propagation was correct.
   - Coverage RED: no prior route test named or asserted a precision preparation/final boundary.
   - Sensitivity RED: with cancellation checks temporarily disabled, the strengthened regression observed both real serial segments, preparation and final.
   - GREEN: `test_interrupt_after_precision_preparation_skips_final_and_contact_work` runs a real `StageController` through the production route controller and real two-segment precision sender. It cancels only after the preparation jog is accepted, then proves the actual final jog, needle lowering/contact work, and LCR measurement do not run. The route waits for the normal correction/confirmation checkpoint and completes after Skip.

## Changed files

Production:

- `probe_station_gui/settings/precision_approach.py`
- `probe_station_gui/stage/autofocus_flow.py`
- `probe_station_gui/stage/click_move.py`
- `probe_station_gui/stage/motion_commands.py`
- `probe_station_gui/stage/needle_actions.py`
- `probe_station_gui/stage/position_presenter.py`
- `probe_station_gui/stage/precision_approach.py`
- `probe_station_gui/stage/precision_motion.py`

Tests:

- `tests/route/test_measurement.py`
- `tests/settings/test_precision_approach_settings_model.py`
- `tests/stage/test_controller_autofocus.py`
- `tests/stage/test_controller_click_move.py`
- `tests/stage/test_position_presenter.py`
- `tests/stage/test_precision_motion.py`
- `tests/ui/test_stage_position_panel.py`

Report:

- `.superpowers/sdd/final-review-fix-report.md`

## Verification

All Python commands used the required shared interpreter:
`C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.

- Focused aggregate:
  - Command: `python.exe -m pytest tests/stage/test_precision_approach.py tests/stage/test_precision_motion.py tests/stage/test_controller_click_move.py tests/stage/test_controller_autofocus.py tests/stage/test_position_presenter.py tests/settings/test_precision_approach_settings_model.py tests/ui/test_precision_approach_settings.py tests/ui/test_stage_position_panel.py tests/app/test_main_objective_alignment.py tests/route/test_measurement.py -q`
  - Result after final change: `134 passed in 2.56s`.
- Complete suite:
  - Command: `python.exe -m pytest tests -q`
  - Initial result: `1692 passed, 10 subtests passed, 2 failed in 36.94s`; both failures were missing ignored calibration `.npz` fixtures in the isolated worktree.
  - The seven ignored fixtures (262 KB) were copied read-only from the main checkout into this worktree only; they do not appear in Git status.
  - Final fresh result after the last production change: `1695 passed, 10 subtests passed in 29.39s`.
- Reviewer follow-up, meaningful seam regressions (tests only; no production diff):
  - Old B pre-send wiring sensitivity: `python.exe -m pytest tests/stage/test_precision_motion.py -q -k "real_accepted_jog_write or real_prewrite_b_limit_rejection"` -> `3 failed, 16 deselected`; after restoring production wiring -> `3 passed, 16 deselected in 0.25s`.
  - Disabled cancellation sensitivity: `python.exe -m pytest tests/route/test_measurement.py -q -k interrupt_after_precision_preparation` -> `1 failed, 27 deselected`; after restoring cancellation -> `1 passed, 27 deselected in 0.33s`.
  - Focused aggregate: four strengthened regressions -> `4 passed in 0.34s`.
  - Covering files: `python.exe -m pytest tests/stage/test_precision_motion.py tests/route/test_measurement.py -q` -> `47 passed in 1.29s`.
- `git diff --check`: exit `0`; only Git's existing LF-to-CRLF working-copy warnings were printed.
- Branch and starting HEAD were rechecked before commit.

## Self-review

- No settings schema, serialized profile fields, public controller entry points, or API route-control actions changed.
- Route Pause/Resume/Interrupt production code was not modified. The new route regression verifies interrupt persistence through the precision segment boundary and blocks later contact work.
- Existing needle safety checks remain in place; the autofocus fallback uses the normal absolute-move safety path and validates the known start target before sending.
- B invalidation is neither pre-write nor delayed until whole-motion completion: it occurs once at the accepted first motion command.
- Calibrated range rejection occurs before `_precision_motion_in_flight` and before the first segment send, so a clamped preparation cannot be marked exact.
- No Qt signals were introduced under locks.

## Remaining concerns

- The calibration-fit tests require ignored `.npz` fixtures that are present in the main checkout but not materialized automatically in a new Git worktree. The final suite is green after copying those fixtures locally; repository changes do not depend on or include them.
- No remaining behavioral concern was found in the eight reviewed items.

---

# Design Move and Bounded Viewport Final Review Fix Report

## Scope and commit

- Worktree: `C:\Users\Lazemir\.codex\worktrees\design-move-bounded-viewport\probe_station_gui`
- Branch: `codex/design-move-bounded-viewport`
- Starting HEAD: `74a12ee1a97c44599e5771d26f3991fa8980ad54`
- Commit subject: `fix: preserve design interaction ordering`
- Final commit hash: added after the commit because a commit cannot contain its own hash.
- Hardware-dependent code and `main.py` were not run.

## Finding-by-finding result

1. **Legacy calibration fallback leaked into explicit tools** — confirmed and fixed.
   - RED command: `python.exe -m pytest tests/design/test_click_navigation.py::test_unhandled_explicit_tool_click_does_not_calibrate_or_move tests/design/test_click_navigation.py::test_explicit_legacy_click_keeps_calibration_fallback -q`
   - RED result: exit `1`; Array, disabled Point, and disabled Guide each failed because they emitted `(0, 11.0, 22.0)` through `calibration_point_selected`; explicit legacy LMB/RMB already passed (`3 failed, 2 passed in 1.38s`).
   - GREEN: click dispatch now makes one action decision. Only the explicit `legacy` tool reaches LMB/RMB calibration; unhandled explicit-tool clicks return. The duplicated Move assignment and unreachable `slot is None` Move fallback were removed.

2. **Identity-equivalent layer updates refocused the GDS** — confirmed and fixed.
   - RED command: `python.exe -m pytest tests/ui/test_design_plot_klayout.py::test_file_backed_layer_toggle_preserves_view_and_cached_navigation -q`
   - RED result: exit `1`; the zoomed/panned range was replaced by the GDS-focused range (`1 failed in 1.04s`).
   - GREEN: `set_document()` compares the old and new `_design_content_key()` values before replacement. An unchanged content key preserves the valid ViewBox range and the exact cached navigation content/frame while still reconfiguring raster and snap layers. New path/top-cell/rotation/bounds/source-load identities retain full recompute and GDS focus.

3. **Preflight-skipped clicks overtook accepted FIFO clicks and retained stale hover correlation** — confirmed and fixed.
   - RED command: `python.exe -m pytest tests/ui/test_design_plot_klayout.py::test_preflight_skipped_click_waits_for_older_accepted_click tests/ui/test_design_plot_klayout.py::test_preflight_skipped_click_invalidates_inflight_hover -q`
   - RED result: exit `1`; skipped click B emitted before A and `cancel_hover()` was never called (`2 failed in 1.05s`).
   - GREEN: a pane-local FIFO records every accepted action, stores worker or preflight-fallback completions by request id, and drains only from the head. Skipped work submits no worker/native traversal. An isolated skipped action completes immediately. Before a skipped fallback is queued, hover request identity and pending markup are cleared and `cancel_hover()` is called, so late hover responses are ignored.
   - Self-review RED: `test_cancelled_ordered_click_ignores_late_failure` initially failed because a late failure for an already tool-cancelled queued click showed a snap-failure state (`1 failed in 1.06s`). GREEN ignores failures for terminal queue entries and later emits only the surviving earlier action (`2 passed in 0.87s` with the failure-release regression).

4. **Collector cleanup regression** — coverage added; production already correct.
   - `test_backend_budget_abort_closes_shape_stream` uses a close-aware generator with a `finally` marker and a deterministic shape-budget abort.
   - It passed immediately (`1 passed in 0.22s`), proving `_KLayoutSnapBackend.snap()` closes the contour stream on early collector return.

## Changed tracked files

Production:

- `probe_station_gui/views/design_plot_pane.py`

Tests:

- `tests/design/test_click_navigation.py`
- `tests/design/test_klayout_snap_backend.py`
- `tests/ui/test_design_plot_klayout.py`

Ignored report:

- `.superpowers/sdd/final-review-fix-report.md`

## Final verification

All Python commands used `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.

- New-regression GREEN: `9 passed in 1.49s`, exit `0`.
- Required focused command:
  - `python.exe -m pytest tests/design/test_click_navigation.py tests/design/test_klayout_snap_backend.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_plot_move.py tests/ui/test_design_plot_selection.py tests/ui/test_design_navigator_panel.py -q`
  - Fresh final result: `137 passed in 7.77s`, exit `0`.
- Task 5 targeted command:
  - `python.exe -m pytest tests/design tests/ui tests/app/test_main_design_navigation.py tests/ui/test_main_window_auxiliary.py tests/stage/test_controller_click_move.py -q`
  - Fresh final result: `551 passed in 11.91s`, exit `0`.
- Full repository suite:
  - `python.exe -m pytest tests -q`
  - Fresh final result: `1817 passed, 30 subtests passed in 30.86s`, exit `0`.
- `git diff --check`: exit `0`; Git printed only existing LF-to-CRLF working-copy warnings.

## Ordered-completion state audit

- Success stores the correlated result and drains completed actions strictly from the FIFO head.
- Failure stores a terminal no-action completion, advances the FIFO, and does not fall back to raw or markup movement.
- Escape/tool cancellation marks matching queued actions terminal, including results that arrived early, then drains them when older actions finish. Late responses and failures cannot revive a cancelled action.
- Document replacement, content/config generation invalidation, snap-mode changes, unload, and shutdown call one reset helper that clears pending records, order, and completion buffers together. Worker `cancel_pending()` remains in the existing invalidation paths.
- A skipped click invalidates hover correlation and pending hover markup before `cancel_hover()` and fallback selection; late hover responses cannot overwrite click feedback.
- Markup generation is checked again at FIFO drain time, so a delayed skipped completion cannot publish stale markup or movement after a markup/snap change.
- Signal-driven re-entry is safe: each head entry is removed before its action signal is emitted, and a synchronous unload/reset leaves the drain loop with an empty queue.

## Scope protection and concerns

- Stage movement/safety, registration mathematics, raster LOD, selection rectangles, and route Pause/Resume/Interrupt code were not changed.
- Skipped preflight work never submits a `SnapRequest`; markup-aware fallback and exact raw Move behavior remain covered by the focused suite.
- The requested remaining Minor in `navigation_bounds` was intentionally left unchanged: the first-two `NeedleOffset.apply_to()` loop was not replaced with `MeasurementRoute.needle_hits_for_point()`.
- No behavioral concern remains from the automated and state-path review. Manual hardware/GUI operation was outside scope and was not performed.
