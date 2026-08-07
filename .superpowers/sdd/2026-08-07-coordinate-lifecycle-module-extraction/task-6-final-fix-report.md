# Task 6 final fix report

Date: 2026-08-07

Base HEAD: `cb7277c296e28b49d1249de47e7565125a807b45`

Implementation commit: `b4a28bac4347932d41b4a717a85dd2ae37be33f5`

## Scope and outcome

The three Important whole-branch review findings were fixed as one scoped TDD package:

1. An implicit display refresh now retains a temporarily unavailable Design frame, applies its unavailable/yellow display plan, clears stale values, and does not persist Machine. Only a rejected explicit selection returns before applying a plan.
2. Operator Align now submits immutable indexed capture tokens to `DesignRegistrationLifecycle`. The lifecycle owns the active request and evidence batch, preserves each sample's physical B, pivot, and objective context, normalizes in deterministic mark-slot order, and returns the existing exact rollback effects. `Main` only collects hardware/context data and executes the common fit/publication adapter. The duplicate operator capture dataclasses, pending/evidence/operation fields, context validator, fit/rollback method, and discard method were removed. The prior “capture already running” UI guard is represented by the lifecycle token, and no direct B move was added.
3. Both successful-contact paths check Interrupt immediately after the post-contact callback. An interrupt requested while that callback blocks now returns the normal interrupted result before contact photo, later status, or success return.

No camera, clipboard, provenance, persistence schema, Plan 3, or Plan 4 source was changed. No hardware-dependent command was run.

## TDD evidence

### Temporary Design-frame unavailability

RED:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui\test_stage_position_panel.py::test_temporary_b_loss_applies_unavailable_display_plan -q
```

Failed because no display plan was applied (`len(plans) == 0`).

GREEN: the exact test passed. The focused presentation/UI/control gate then passed `118` tests and `4` subtests.

### Operator Align lifecycle delegation

RED: the real operator callback test captured slot 0 at B=0/pivot=(0, 0) and slot 1 at B=90/pivot=(10, 20). It failed because the frame stayed at version 0 instead of committing version 1.

GREEN: the exact test passed after lifecycle delegation. Two additional lifecycle regressions were driven RED-to-GREEN:

- out-of-order indexed capture initially chose B=90 rather than deterministic slot-0 B=0;
- a second operator request initially superseded the active hardware request after the duplicate `Main` pending field was removed.

The final operator/lifecycle focused gate passed `28` tests. It verifies mixed B/pivots, per-sample objective evidence, slot ordering, active-request preservation, real callback fit/publication, exact fit-failure baseline restoration, and absence of the three former `Main` state fields.

### Post-contact callback Interrupt

RED: the deterministic threaded test blocked inside the callback, requested Interrupt, released the callback, and received `interrupted=False`.

GREEN: the exact test passed, and the full contact lifecycle interface passed `14` tests.

## Integration and full-suite verification

Combined coordinate/design/route integration gate:

```text
684 passed, 9 subtests passed
```

Final changed-area self-review gate:

```text
43 passed
```

The first native-locale full run exposed one transient API thread-start timeout plus the two existing locale-sensitive camera exposure failures. The API test passed immediately in isolation. A fresh native-locale full rerun had exactly the two camera failures and no API failure:

- `test_exposure_time_is_only_manual_and_uses_the_policy_adapter`
- `test_exposure_time_latest_pending_write_replaces_stale_completion`

Both fail because `apply_pending_settings()` returns false under the process locale. They also reproduce together in isolation, and `git diff cb7277c -- probe_station_gui/dialogs/camera_settings_dialog.py tests/ui/test_camera_exposure_controls.py` is empty. Per the explicit scope restriction, no camera edit was made.

A complete process-local C-locale run, with no tests deselected, was then executed:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -c "from PySide6.QtCore import QLocale; QLocale.setDefault(QLocale.c()); import pytest; raise SystemExit(pytest.main(['tests', '-q', '--basetemp', r'.tmp\coordinate-lifecycle-final-fix-full-c-locale-20260807b']))"
```

Final result: `2891 passed, 18 subtests passed`, exit code 0, in 59.74 seconds. The first C-locale attempt had only the same transient API timing failure (`2890 passed`); the complete rerun was clean.

## Static, diff, and architecture gates

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 main.py probe_station_gui tests
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q main.py probe_station_gui tests
git diff --check
```

All exited 0. Ruff reported `All checks passed!`.

A production-tree search found no remaining `_OperatorAlignmentCaptureContext`, `_RegistrationEvidenceSample`, `_pending_operator_alignment_capture`, `_alignment_physical_draft`, `_alignment_operation_id`, `_complete_operator_alignment_machine_capture`, `_operator_alignment_capture_context_is_current`, `_commit_operator_alignment`, or `_discard_operator_alignment_evidence` implementation.

Self-review found no further in-scope Critical or Important defect. The pre-existing untracked `.tmp/` test-artifact directory was preserved.
