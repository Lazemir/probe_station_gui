# Task 5 Report: Complete Coordinate Lifecycle Module Extraction

## Status

Completed.

Commit: `refactor: complete coordinate lifecycle module extraction` (this task
commit).

## Implementation

- Removed Main's coordinate-selection and pending-restore shadow fields. The
  `CoordinateFrameLifecycle` now owns both active GUI selection and persisted
  restore intent and exposes only its selected-frame result to adapters.
- Replaced the stage-position adapter's pending-restore branch and lazy
  lifecycle helper with one immutable `FrameSelectionContext`, one lifecycle
  call, and returned-decision/effect application.
- Removed `presentation.decide_pending_frame_restore`; selector eligibility and
  display fallback continue to delegate to the lifecycle module rather than
  reproduce its policy.
- Removed Main's `_registration_capture_lifecycle` and
  `_cancel_registration_capture` pass-through helpers. Capture, focus, contact,
  and cancellation paths call the installed `DesignRegistrationLifecycle`
  directly and apply the returned immutable effects.
- Made rejected focus moves issue exactly one lifecycle cancellation and apply
  both target-binding and cancellation effects. Autofocus request rejection now
  also applies the returned cancellation effect.
- Updated integration fixtures that intentionally bypass `Main.__init__` to
  install the lifecycle instances they consume. No hardware, route-control,
  camera, clipboard, or unrelated provenance behavior was changed.
- `probe_station_gui/design/registration_lifecycle.py` required no production
  change: the Task 3/4 interface already represented every Task 5 adapter
  operation and effect.

## TDD Evidence

1. Delegation RED: the brief's focused gate failed only because Main still
   exposed `_registration_capture_lifecycle`; the initial local-basetemp result
   was `1 failed, 7 passed, 110 deselected`.
2. Pending-restore ownership RED rejected the new `restore_frame_id` lifecycle
   input. GREEN moved wait/accept/discard state into the lifecycle and preserved
   ready-through-Z/missing-A restore behavior.
3. Stage-adapter absence RED found `_selected_coordinate_frame_id`; GREEN
   removed both shadow fields and the presentation restore helper.
4. Exact parity RED showed an implicit Machine fallback did not update the
   lifecycle's active selection. GREEN updates active intent without persisting
   an implicit fallback (`27` focused lifecycle tests passed at that checkpoint).
5. Main delegation GREEN passed `8` lifecycle/delegation tests; the complete
   Main design-navigation adapter suite passed `102` tests.
6. Final seam-audit RED showed a rejected focus move issued two cancellation
   completions and applied neither returned effect. GREEN applies the binding
   effect and exactly one cancellation effect; the focused gate passed `3`
   tests.

## Verification

- Stage/selection parity gate: `120 passed, 4 subtests passed`.
- Route fixture regression gate: `40 passed, 5 subtests passed`.
- Main design-navigation adapter suite: `102 passed`.
- Full repository suite with process-local `QLocale.c()` and only the
  previously documented headless Windows Qt clipboard assertion deselected:
  `2881 passed, 1 deselected, 1 warning, 18 subtests passed` in `58.68s`.
- The task brief's aggregate argument order produced `1011 passed, 5 subtests
  passed` plus two pre-existing provenance-worker failures. The failures were
  reduced to `test_provenance.py + test_main_design_navigation.py`: collection
  clears/reimports `probe_station_gui`, leaving the already-collected test's
  old `FrameKind` enum incompatible with the worker's lazy import identity
  check. `tests/coordinates/test_provenance.py` passes `6/6` in isolation and
  passes in the normal full-suite collection order. No provenance code was
  changed for Task 5.
- Ruff passed every touched code/test file with the repository's documented
  `E402,F401` deferred-import exclusions. The raw brief command reported only
  the established `main.py` `E402` baseline.
- `compileall -q main.py probe_station_gui tests`, `git diff --check`, and
  production searches for the removed fields/helpers passed.

## Scope and Review

- Production searches contain no `_selected_coordinate_frame_id`,
  `_pending_coordinate_frame_restore_id`, `decide_pending_frame_restore`,
  `_registration_capture_lifecycle`, or `_cancel_registration_capture`.
- Main and the stage adapter retain context construction, Qt/hardware I/O, and
  effect execution; selection, restoration, capture, focus, contact, and
  cancellation decisions remain in the two deep lifecycle modules.
- Route Pause/Resume/Interrupt and API route-control semantics were not changed.
- No hardware-dependent code was run.
- The pre-existing untracked `.tmp/` directory was left untouched.
- Task 6 final range-wide verification remains separate.

## Fix Round 1: Authoritative Selection and Registration Effects

Two review findings were reproduced and corrected with new interface-level
REDs.

### Selection adapter

- RED proved that one explicit Design selection called `plan_selection` twice;
  with an invalid rotation pivot the lifecycle and UI ended on Machine while
  persistence incorrectly recorded the originally requested Design frame.
- `update_software_coordinate_display` now owns the complete adapter operation:
  it collects the pivot, registry, authority and homing context, requests one
  final lifecycle decision, applies one display plan, and persists only that
  decision's effect. `select_gui_coordinate_frame` supplies explicit intent to
  this operation instead of planning first and refreshing afterward.
- GREEN asserts one lifecycle call, one UI-plan application and one persistence
  application. Invalid-pivot fallback now leaves lifecycle, UI and persistence
  consistently on Machine.

### Registration adapter

- Three REDs showed that rejected snapshot submission left the lifecycle token
  active, while current failed callbacks and conversion failures did not apply
  accepted lifecycle effects.
- Rejected submission now uses the existing failed
  `RegistrationCaptureOutcome`, applies the returned effect once, reports the
  reason, and closes the active token. Current callback authorization/failure
  and conversion rejection effects are each applied exactly once.
- Tests exercise the real `DesignRegistrationLifecycle` interface and prove a
  second outcome for each completed token is stale. Baseline marks,
  registration/status and focus-overlay presentation remain unchanged.

### Fix-round verification

- Selection/pivot gate: `106 passed, 4 subtests passed`.
- Main design-navigation suite: `105 passed`.
- Combined Task 3/5 gate: `242 passed, 4 subtests passed`.
- Task 5 aggregate with the two already-documented argument-order provenance
  cases separated: `1016 passed, 2 deselected, 5 subtests passed`; isolated
  provenance remained `6 passed`.
- Full C-locale rerun with only the documented headless clipboard assertion
  deselected: `2886 passed, 1 deselected, 1 warning, 18 subtests passed` in
  `59.53s`.
- The first full run observed one camera monitor timing failure (`2` reads
  instead of `1`). The unchanged camera test passed three consecutive isolated
  reruns, and the fresh full rerun passed. No camera code was changed.
- Ruff on every fix-round file, `compileall`, `git diff --check`, and obsolete
  policy/static searches passed.
