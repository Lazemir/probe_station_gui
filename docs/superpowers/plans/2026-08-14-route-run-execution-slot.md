# Route Run Execution Slot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development to implement this plan task-by-task
> with an independent review after every task.

**Goal:** Replace distributed Main-owned route runner/thread/waiting fields with
one application execution slot while preserving all Route Measurement safety
and external workflow semantics.

**Architecture:** Add a standard-library-only
`_RouteRunExecutionSlot` under `probe_station_gui.application`. The slot
owns active runner identity, thread identity, explicit run kind, and published
safe-waiting state. Existing route-domain mailbox/coordinator, external-result
session state, API control state, durable session metadata, and UI policy
remain separate.

**Tech Stack:** Python 3.9+, dataclasses, enum, threading-compatible structural
objects, pytest, Import Linter, Radon, Ruff

## Global Constraints

- Do not change Pause Request, Pause Ack, Interrupt, confirmation, autofocus
  safe-Z, contact cleanup, or external-result behavior.
- `_RouteRunControlMailbox` remains the sole internal owner of stop,
  one-shot Pause, persistent Interrupt, pending confirmation, and waiting
  synchronization.
- `RouteExternalMeasurementSessionRunner` retains its external-result
  condition, request IDs, decisions, history, and status state.
- `ApiRouteControlState` remains a separate control plane. Its paused state
  must not be written into the physical-run execution slot.
- The slot is standard-library-only and must not import Qt, `main`, route
  policy, views, dialogs, or consuming application owners.
- No Main compatibility properties, forwarding wrappers, aliases,
  `__getattr__`, package re-export, or capability-based run-kind inference.
- Thread start ordering, stop-before-join ordering, takeover timeout retention,
  stale-finish rejection, and final-status-before-metadata-cleanup stay exact.
- Use the shared project environment at
  `C:\Users\Public\code\probe_station_gui\.venv` for every Python command.
- Do not run hardware, network, or visible GUI code.
- Import Linter runs with `--no-cache`; no task cache or basetemp remains.
- Every changed Python file has positive normal and
  comments/docstrings-stripped MI.

## Canonical Files

- Add: `probe_station_gui/application/route_run_execution.py`
- Add: `tests/app/test_route_run_execution.py`
- Add: `tests/app/test_route_run_execution_architecture.py`
- Modify: `main.py`
- Modify: `pyproject.toml`
- Modify: `tests/app/test_repository_maintainability.py`
- Modify route application owners and direct stage/view/dialog consumers named
  in Task 2.
- Modify only tests that construct the deleted raw execution fields.
- Modify this plan only for factual evidence.

---

## Task 1: Add the canonical execution-slot owner

**Files:**
- Add: `probe_station_gui/application/route_run_execution.py`
- Add: `tests/app/test_route_run_execution.py`
- Modify: `pyproject.toml`
- Modify: `tests/app/test_repository_maintainability.py`

### Step 1: Record the safety and architecture baseline

Run the focused current suites before production edits:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/route/test_run_control_mailbox.py tests/route/test_measurement_interrupt_checkpoint.py tests/route/test_measurement_api_route_control.py tests/app/test_main_route_interrupt_safety.py -q -p no:cacheprovider --basetemp=.pytest-route-slot-baseline
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_repository_maintainability.py -q -p no:cacheprovider --basetemp=.pytest-route-slot-import-baseline
```

Record exact counts. No production code may change before the baseline.

### Step 2: Write strict absent-owner RED tests

Add direct tests that first require the canonical module/class and then specify:

- an idle snapshot;
- activation with explicit `GUI` or `EXTERNAL_RESULT_SESSION` kind;
- rejection of a second active run;
- waiting publication and reason derivation;
- stale waiting publication after release cannot reactivate the slot;
- running Interrupt requests runner correction and returns cancel-stage true;
- waiting Interrupt requests runner correction and returns cancel-stage false;
- successful GUI takeover stops then joins and clears;
- timed-out GUI takeover retains the complete snapshot;
- failed-start cleanup preserves its cause-specific current behavior;
- current finish returns the prior snapshot and clears;
- stale finish is ignored;
- slot callbacks are not invoked while a non-reentrant lock is held.

Run the absent-module selection and record RED before implementation.

### Step 3: Implement the standard-library-only owner

Implement exactly the five operations from the design:

- `snapshot`
- `activate`
- `publish_waiting`
- `request_interrupt`
- `release`

Use frozen DTOs and typed enums for run kind, release cause, interrupt
directive, request, and outcome. Do not import concrete runners, application
owners, Qt, or route modules. Do not start threads in `activate`.

Run the direct owner suite to GREEN.

### Step 4: Add the leaf dependency contract

Add the exact contract:

```toml
[[tool.importlinter.contracts]]
id = "route-run-execution-slot-leaf"
name = "Route run execution slot stays independent of callers and route policy"
type = "forbidden"
source_modules = ["probe_station_gui.application.route_run_execution"]
forbidden_modules = [
    "main",
    "probe_station_gui.route",
    "probe_station_gui.dialogs",
    "probe_station_gui.views",
    "probe_station_gui.application.route_launch_setup",
    "probe_station_gui.application.route_capture_run",
    "probe_station_gui.application.route_control",
    "probe_station_gui.application.route_results",
    "probe_station_gui.application.api_stage_contact",
    "probe_station_gui.application.api_route_scan",
]
```

Update the permanent maintainability test's exact summary from four to five
contracts. Run the new contract directly and the complete maintainability
module. Add no weakening option.

### Step 5: Verify and commit Task 1

Run direct tests, the four unchanged safety files, Import Linter, Ruff, format,
compile, diff check, and normal/stripped MI for both new Python files. Remove
only the exact Task 1 basetemps and verify no Import Linter cache.

Commit:

```text
refactor: add route run execution slot
```

Obtain an independent task review before Task 2.

---

## Task 2: Migrate application execution ownership atomically

**Production files:**
- Modify: `main.py`
- Modify:
  `probe_station_gui/application/route_launch_setup.py`,
  `route_capture_run.py`, `route_control.py`, `route_results.py`,
  `api_stage_contact.py`, `api_route_scan.py`, `api_meter_visa.py`,
  `bootstrap_api.py`, `design_edit_dialog.py`, `design_markup.py`,
  and `registration_focus.py`
- Modify: `probe_station_gui/route/dialog_adapter.py`
- Modify: `probe_station_gui/stage/move_lifecycle.py`
- Modify:
  `probe_station_gui/views/main_window_shutdown.py` and
  `main_window_needle_calibration.py`
- Add: `tests/app/test_route_run_execution_architecture.py`
- Modify only existing tests that directly create or inspect the four deleted
  Main fields.

### Step 1: Write the integration/deletion RED

The architecture test must fail on the current code and then assert:

- `Main` composes one `_route_run_execution`;
- the four raw execution fields are absent from `Main.__init__` and all
  production callers;
- no compatibility property, alias, forwarding method, re-export, or reverse
  import exists;
- all production runner/thread/waiting reads use `snapshot()` or a semantic
  slot operation;
- run kind is explicit and is not inferred with `isinstance` or
  `hasattr(..., "submit_external_result")`;
- the four obsolete helpers named in the design are absent;
- the new leaf Import Linter contract remains present.

Record the strict RED before migration.

### Step 2: Compose the slot and migrate launch paths

Replace the four Main initializers with one slot. GUI launch activates kind
`GUI`; API external-result launch activates kind
`EXTERNAL_RESULT_SESSION`. Preserve all existing UI/presenter/signal setup
before the exact existing `thread.start()` call.

Migrate availability checks to immutable snapshots. Do not move durable
session state or metadata into the slot.

### Step 3: Migrate takeover, failed start, finish, and restart

Use typed `release` causes:

- GUI takeover captures route offset, stops, joins up to 2 seconds, retains the
  entire slot on timeout, and clears only on success.
- Failed external initial pause captures status before release and preserves
  current response/cleanup ordering.
- Finish ignores stale runner signals, joins the dead finished thread as
  before, returns the prior runner, stores final external status, then clears
  unrelated metadata.

Delete `_join_finished_route_measurement_thread`,
`_clear_waiting_route_measurement_state`, and callback-heavy
`restart_waiting_route_measurement`.

### Step 4: Migrate waiting, Interrupt, and all read-only consumers

The route runner waiting callback calls `publish_waiting`. Pause Request alone
does not. Migrate Interrupt through `request_interrupt`, leaving stage
cancellation/presentation in the application caller according to the returned
directive. Delete `_current_route_measurement_waiting_reason`.

Do not publish `ApiRouteControlState.paused` into the slot. Consumers needing
combined availability explicitly combine the slot snapshot with the API state.

Migrate shutdown, stage cancel, design guards, dialog adapters, Telegram/API
snapshots, needle calibration, and meter guards without a Main facade.

### Step 5: Migrate tests without compatibility seams

Tests construct a real slot and activate fake runner/thread objects. A test-only
fixture helper may reduce setup repetition, but it must call the public slot
interface and must not recreate the deleted Main attribute surface.

Keep the mailbox/coordinator safety test files byte-identical. Preserve all
existing assertions for:

- pending Pause remains Interrupt;
- Resume only after Pause Ack;
- running versus waiting Interrupt stage cancellation;
- initial-pause takeover and timeout retention;
- stale finish signals;
- final API status capture;
- shutdown stop/join;
- autofocus safe-Z and downstream contact suppression.

### Step 6: Focused GREEN and static gates

Run:

- direct slot and architecture tests;
- all `tests/route`;
- affected App route-control/session/interrupt/stage-coordinate tests;
- affected UI dialog/shutdown/needle/design safety tests;
- affected stage autofocus/move-lifecycle tests;
- all five Import Linter contracts;
- Ruff, format, compile, diff, AST/deletion/DAG checks;
- normal and stripped MI for every changed Python file.

No full suite yet. Remove exact Task 2 basetemps.

### Step 7: Commit Task 2

Commit:

```text
refactor: centralize route run execution
```

Obtain an independent review with explicit attention to stop/join timeout
retention, stale queued signals, Pause Ack distinction, and safe-Z/Interrupt
semantics. Fix every Critical or Important finding and re-review.

---

## Task 3: Final safety matrix and evidence

### Step 1: Run the deterministic hardware-free matrix

Collect the exact suite, then run deterministic directory partitions using the
established offscreen `QLocale.c()`, no-cache strategy. Reproduce the known
raw App native crash only as a diagnostic if it still exists; count the
established split and isolated node. Account for every collected node and
subtest exactly.

### Step 2: Run final architecture/quality gates

Run:

- all five Import Linter contracts with `--no-cache`;
- permanent repository maintainability tests;
- tracked-plus-new normal MI and changed-file stripped MI, all positive;
- configured whole Ruff, changed-file format, compileall, diff check;
- owner-first/reverse import smoke with no QApplication/thread start;
- Lizard warnings/duplicate comparison;
- protected mailbox/coordinator/safety file hash audit;
- exact temp/cache/status/index freeze.

### Step 3: Independent final review

The reviewer must independently inspect:

- the five-operation slot depth and deletion test;
- no raw execution-field compatibility surface;
- exact launch/start, takeover timeout, finish identity, and cleanup ordering;
- Pause Request versus Pause Ack and API-control separation;
- Interrupt persistence, safe-Z restoration, contact/lift/CSV suppression;
- external-result condition and mailbox ownership unchanged;
- fifth Import Linter contract precision;
- all changed Python MI positive.

Require READY with no Critical or Important finding before staging the factual
plan update.

### Step 4: Record evidence

Update this plan with exact counts, metrics, hashes, review verdict, and commit
IDs. Remove task basetemps and caches. Commit only the factual plan update:

```text
docs: record route run execution evidence
```

Leave the worktree/index clean and hand off the next architecture candidate.
