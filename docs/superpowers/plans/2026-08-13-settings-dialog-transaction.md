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
