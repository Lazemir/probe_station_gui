# Task 4 Report: Focus and Contact Registration Lifecycle

## Status

Completed.

Commit: `refactor: move focus and contact lifecycle out of main` (this task
commit).

## Implementation

- Extended `DesignRegistrationLifecycle` with an immutable
  `RegistrationContext`, unique focus/contact operation tokens, typed focus
  completions, and focus/contact effects.
- Moved focus-candidate identity, exact-context acceptance, target-bound move
  validation, autofocus completion validation, and exactly-once Z commit
  eligibility out of `Main`.
- Moved first-contact eligibility and first-write-wins A commit policy into the
  lifecycle. Contact capture now requires ready Z, missing A, and the exact
  current session/frame/version token before hardware is read or a registry
  effect is attempted.
- Made every lifecycle cancellation clear pending focus/contact operations and
  candidate/eligibility state. Existing capture rollback baselines are still
  returned exactly once.
- Removed Main's `_pending_registration_focus_token`,
  `_pending_registration_focus_target_xy`, `_focus_candidate`, candidate
  context, active-contact-record guard, and duplicate contact-token policy.
- Kept immutable context construction, Qt overlay/signal work, stage/autofocus
  requests, physical coordinate reads, registry commits, publication, and
  status presentation in `Main` as effect adapters.
- Routed document/design/top-cell/frame/mark-set/Z/route-context changes through
  lifecycle cancellation while retaining Task 3 capture and rollback effects.

## TDD Evidence

1. Interface RED failed collection because `RegistrationContext` did not
   exist. The minimal interface GREEN passed `3` focus/contact tests.
2. Token RED failed collection because `FocusCompletion` did not exist. Token
   GREEN passed `5` focused tests.
3. Readiness RED rejected the new `xyb_ready` context input. GREEN established
   ready-X/Y/B plus missing-Z focus eligibility and ready-Z plus missing-A
   contact eligibility (`7` focused tests).
4. The Main candidate adapter RED proved `_focus_candidate` still existed.
   GREEN made the lifecycle own the candidate and drove the overlay only from
   returned effects.
5. The focus adapter RED rejected explicit current-context acceptance because
   Main still used its removed field. GREEN delegated candidate use, exact move
   target matching, autofocus authorization, stale completion rejection, and Z
   commit eligibility.
6. The contact adapter RED returned a route callback even when a spy lifecycle
   rejected eligibility. GREEN made lifecycle preflight and token validation
   precede the physical A read and registry commit.
7. Self-review RED showed invalid contact completion escaped as `ValueError`.
   GREEN now fails closed before lifecycle or registry effects.

## Verification

Fresh Task 4 lifecycle/Main gate:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_registration_lifecycle.py tests\design\test_frame_references.py tests\app\test_main_design_navigation.py tests\app\test_main_route_measurement_session.py -q -p no:cacheprovider --basetemp .tmp\task4-core-gate-final
```

Result: `170 passed, 5 subtests passed`.

- Lifecycle interface suite: `24 passed`.
- Main navigation adapter suite: `99 passed`.
- Route suite with the unrelated headless Windows Qt clipboard assertion
  deselected: `375 passed, 1 deselected`.
- Full suite with process-local `QLocale.setDefault(QLocale.c())` and the same
  clipboard assertion deselected: `2874 passed, 1 deselected, 1 warning,
  18 subtests passed` in `56.11s`.
- The deselected
  `test_raw_data_dialog_populates_and_copies_rows` was reproduced separately:
  its three sibling view tests pass, but the headless Windows Qt clipboard
  remains empty after `_copy_all()`. No clipboard or result-view code changed.
- Ruff passed all four touched code/test files with the repository's existing
  deferred-import `E402` exclusion for `main.py` and its bootstrap test.
- Focused `py_compile`, `git diff --check`, and obsolete Main-policy searches
  passed.

## Scope and Review

- Task 3 capture batching, stale-callback authorization, exact rollback
  effects, and publication behavior were preserved.
- Route Pause/Resume/Interrupt and API route-control semantics were not changed;
  route regressions remained green apart from the isolated clipboard condition.
- Task 5 duplicate-policy cleanup was not started.
- No hardware-dependent code was run.
- The pre-existing untracked `.tmp/` directory was left untouched.
- An independent review-agent request was attempted but unavailable because the
  thread limit was reached. Self-review found and fixed the invalid-contact
  fail-closed issue described above.
