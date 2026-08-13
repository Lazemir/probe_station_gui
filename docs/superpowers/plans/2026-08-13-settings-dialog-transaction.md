# Settings Dialog Transaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Move settings-dialog validation, rollback, persistence, and coordinate reconciliation behind one Qt-free transaction interface.

**Architecture:** SettingsDialogTransaction owns the one SettingsManager and CoordinateSystemCoordinator and exposes one apply operation with immutable context/outcome values. Main remains the Qt composition adapter and retains the broad _apply_settings fan-out.

**Tech Stack:** Python 3.11, dataclasses, PySide6 Main adapter, pytest, Ruff, Radon, Lizard.

## Global Constraints

- Use the shared repository virtual environment.
- No hardware, network, visible GUI, push, merge, or unified order-sensitive full run.
- Qt tests use process-level QLocale.c(), offscreen Qt, no cache provider, and unique workspace basetemps.
- The transaction uses one supplied cached Machine-coordinate snapshot and never initiates Stage I/O.
- Preserve exact text, timeouts, exposure-policy preservation, rollback scope, and save/transition/apply/authority ordering.
- Do not move _apply_settings, pass Main, create a mega-port, alias, re-export, __getattr__, compatibility facade, or reverse import.
- Production scope is the focused owner plus direct Main composition/adapter edits.

---

### Task 1: Canonical owner and vertical path

**Files:**
- Create: probe_station_gui/settings/dialog_transaction.py
- Create: tests/settings/test_dialog_transaction.py

**Interfaces:**
- Produces frozen SettingsDialogContext, SettingsDialogNotice, SettingsDialogOutcome.
- Produces SettingsDialogTransaction.apply(submitted, context).

- [ ] Write an absent-module/class test and run it before production exists. Expected: missing module failure.
- [ ] Add the frozen types. Context contains stage_busy, objective_mutation_busy, and machine_snapshot. Outcome contains accepted, apply_objective_runtime, refresh_coordinate_frame_display, observe_coordinate_authority, and ordered post_apply_notices.
- [ ] Construct the owner with SettingsManager, CoordinateSystemCoordinator, publish_notice, and publish_transition. Construction must have no side effects.
- [ ] Add REDs proving non-Settings input and busy Stage are side-effect free. Busy Stage publishes exactly Stage is busy; settings not changed. for 4000 ms.
- [ ] Add a RED proving an accepted no-op saves once with preserve_exposure_policy=True.
- [ ] Implement only validation, busy rejection, clone, and save. Run the direct tests to GREEN.

### Task 2: Full transaction policy

**Files:**
- Modify: probe_station_gui/settings/dialog_transaction.py
- Modify: tests/settings/test_dialog_transaction.py

**Interfaces:**
- Keeps the Task 1 interface unchanged.
- Completes ordered rollback, reconciliation, and outcome policy.

- [ ] Add custom-frame REDs. Valid loaded-frame changes save first and then publish the synchronized transition. TypeError/ValueError restores the complete previous software-coordinate section, publishes the exact exception at 6000 ms, and still commits unrelated top-level settings.
- [ ] Implement custom-system synchronization and delayed transition publication.
- [ ] Add pivot REDs. Unchanged pivot or no active Design frame needs no B validation. An active frame accepts a cached B snapshot. Missing snapshot/B or invalid geometry restores only the pivot, publishes the exact error at 6000 ms, and performs no live Stage read.
- [ ] Implement cache-only pivot validation with rotation_geometry_snapshot and machine_snapshot.physical_machine_pose.require(B).
- [ ] Add busy-objective REDs. Preserve the current active name/profile exactly while retaining inactive-profile and unrelated changes. Return apply_objective_runtime=False and the exact final 4000 ms notice.
- [ ] Implement active-objective preservation and objective/pivot/axis authority flags.
- [ ] Add an ordering RED: save, optional custom transition, calibration observation, calibration transition. Fingerprints come from saved axis calibrations.
- [ ] Implement calibration reconciliation and immutable refresh/authority decisions. Run the complete direct suite.

### Task 3: Main composition and thin Qt adapter

**Files:**
- Modify: main.py
- Create: tests/app/test_main_settings_dialog_transaction_architecture.py
- Modify: tests/app/test_main_objective_alignment.py
- Modify: tests/app/test_main_software_coordinate_pivot.py
- Modify: tests/app/test_main_camera_api.py
- Modify: tests/app/test_main_coordinate_system_adapter.py
- Modify only other tests that directly invoke the old private policy.

**Interfaces:**
- Main owns one _settings_dialog_transaction.
- Main _apply_settings_from_dialog becomes the Qt adapter for SettingsDialogOutcome.

- [ ] Write architecture REDs: exact one-method public surface, no Qt/Main/views imports, no package re-export/reverse import/alias, one Main composition, and physical deletion of dialog policy from Main.
- [ ] Construct the transaction after the coordinate coordinator with _show_status and coordinate_flow.apply_coordinate_transition adapters.
- [ ] Replace the Main policy body. Gather stage busy, objective busy, and one latest_machine_coordinate_snapshot value; call apply; return on rejection; refresh coordinates; call _apply_settings with the outcome flag; then observe authority; then publish final notices.
- [ ] Delete _reconcile_design_calibration_fingerprints if no caller remains and remove only truly stale imports.
- [ ] Move private policy assertions to the direct owner suite. Keep one Main ordering/context test and the real settings-dialog objective-restoration integration.
- [ ] Run direct owner, architecture, objective alignment, software pivot, camera transaction, coordinate adapter, auxiliary, and settings integration tests under offscreen Qt.

### Task 4: Verification, review, and commit

**Files:**
- Modify: this plan with factual evidence.
- Create ignored: .scratch/settings-dialog-transaction-report.md

- [ ] Run five fresh owner/architecture processes and remove exact basetemps.
- [ ] Run affected settings/objective/coordinate suites, then exact collect-only and fresh top-level partitions. Isolate only the baseline-proven App Qt-order crash node.
- [ ] Run configured whole Ruff, scoped format, compileall, diff-check, owner-first and manager/coordinator-first imports, AST/deletion/DAG/protected hashes, Radon/Lizard/MI.
- [ ] Record exact RED/GREEN evidence, all-node accounting, metrics, scope, and safety constraints in the report and this plan.
- [ ] Freeze HEAD, exact path allowlist, empty index, task-temp absence, and SHA-256 values.
- [ ] Obtain independent read-only review. On any Critical/Important, add a strict RED, fix, refreeze, and re-review.
- [ ] Stage only reviewed paths, audit cached scope/diff, and commit with subject: refactor: isolate settings dialog transaction.
- [ ] Verify commit hash, path count, clean status, empty index, and no task temp. Do not push or merge.

## Execution evidence (2026-08-13)

- [x] Canonical owner RED/GREEN: absent module failed exactly; frozen interface,
  invalid submission, Stage-busy rejection, exposure-safe save, custom-system
  success/rollback, cache-only pivot validation/rollback, busy-objective
  preservation, and calibration reconciliation are covered directly.
- [x] Owner/architecture stress: five fresh processes each passed 16/16.
  Final focused owner/architecture/App policy selection passed 60/60;
  expanded settings/coordinates/App/UI selection passed 444/444.
- [x] Main composition is direct and singular. The old 127-line policy and
  reconcile helper are absent; the adapter gathers one cached snapshot and
  preserves refresh -> runtime apply -> authority -> notice ordering.
- [x] Exact collect-only count is 3307. Deterministic partitions executed all
  3307/3307 items: App 349 + isolated 1 with 5 App subtests; API 146; camera
  331; coordinates 201; design 381; instruments 149; notifications 49;
  packaging 3; route 419; scripts 31; settings 179; shared 4; stage 490; UI
  574. No failures occurred.
- [x] Configured whole Ruff, scoped format, whole compileall, diff-check,
  owner-first and manager/coordinator-first imports, AST/deletion/DAG, and
  five-process stress pass. No hardware, network, visible GUI, unified
  order-sensitive full run, push, or merge was used.
- [x] Metrics: Main 10836/5775/10242, CC 1839/506, max 25, MI 0.00 -> current
  10753/5719/10162, CC 1823/505, max 22, MI 0.00. New owner 246/137/223,
  CC 36/13, max method 5, public apply CC 3, MI 31.33. All-tracked MI-zero
  remains exactly one file (main.py).
- [x] Independent review found one Important startup-load regression: deleting
  the old Main reconciliation method left coordinate-frame load's dynamic
  lookup inert. A strict real-path RED skipped calibration observation before
  Design activation. The corrected coordinate-flow now owns that load-specific
  observation directly, publishes its transition, and only then activates the
  current Design; both stale test injections of the deleted Main method are
  gone. RED 1 failed exactly; corrected nodes 2/2, the complete coordinate-flow
  file 24/24, and transaction/architecture/affected App tests 50/50 passed.
- [x] Corrected-snapshot independent review: READY, Critical 0 / Important 0 /
  Minor 0. The reviewer explicitly closed the startup-load finding and freshly
  passed 18 focused tests. Final pre-commit verification passed 84/84 plus
  configured whole Ruff, scoped format, whole compileall, diff-check, exact
  scope, empty index, and zero task basetemps.
