# Coordinate Lifecycle Module Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace distributed coordinate lifecycle policy in `Main` with two deep, pure Python modules while preserving all behaviour from the cleanup checkpoint.

**Architecture:** `CoordinateFrameLifecycle` owns selection, load/usability, authority and persistence-publication state. `DesignRegistrationLifecycle` owns capture tokens, evidence batches, focus/contact eligibility and cancellation. `Main` remains the Qt/hardware adapter and executes immutable effects returned by the modules.

**Tech Stack:** Python 3.11+, frozen dataclasses, enums, PySide6 adapters, pytest/unittest, Ruff.

## Global Constraints

- Execute only after `2026-08-07-coordinate-lifecycle-behavioral-cleanup.md` is complete and reviewed.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python command.
- Do not run hardware-dependent code.
- Do not change route Pause/Resume/Interrupt semantics or API route-control naming.
- Do not add Plan 3 frame-relative motion or Plan 4 B-axis point hold.
- Lifecycle modules perform no serial/file I/O, Qt signal emission, widget access, or SettingsManager access.
- Expected stale/invalid events return typed no-op/blocked effects; they do not partially mutate state.
- Do not emit Qt signals while holding a non-reentrant lock.
- Do not retain duplicate policy in `Main` compatibility methods.

---

### Task 1: Introduce CoordinateFrameLifecycle selection and usability interface

**Files:**
- Create: `probe_station_gui/coordinates/lifecycle.py`
- Modify: `probe_station_gui/coordinates/__init__.py`
- Modify: `probe_station_gui/coordinates/presentation.py`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py`
- Modify: `main.py:4470-4740`
- Create: `tests/coordinates/test_lifecycle.py`
- Modify: `tests/coordinates/test_presentation.py`
- Modify: `tests/app/test_main_design_navigation.py`

**Interfaces:**
- Consumes: immutable registry records, homed/authority axes, current physical pose, rotation pivot and runtime provenance metadata.
- Produces: `FrameSelectionContext`, `FrameSelectionDecision`, `DesignUsabilityContext`, and the moved `DesignFrameUsabilitySnapshot` through `CoordinateFrameLifecycle.plan_selection()` and `.design_usability()`.

- [ ] **Step 1: Write pure-interface RED tests**

```python
def test_selection_decision_separates_intent_from_temporary_usability() -> None:
    lifecycle = CoordinateFrameLifecycle(selected_frame_id=DESIGN_ID)
    decision = lifecycle.plan_selection(
        FrameSelectionContext(
            records=(_ready_design_record(),),
            requested_frame_id=DESIGN_ID,
            explicit=False,
            homed_axes=frozenset({"X", "Y"}),
            authority_axes=frozenset({"X", "Y", "Z", "A"}),
        )
    )
    assert decision.selected_frame_id == DESIGN_ID
    assert decision.available is False
    assert decision.persist_selection is False


def test_usability_snapshot_requires_ready_current_durable_record() -> None:
    result = CoordinateFrameLifecycle().design_usability(
        DesignUsabilityContext(
            frames_loaded=True,
            record=_draft_design_record(),
            selected_frame_id=DESIGN_ID,
            authority_blocked_axes=frozenset(),
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    assert result.usable is False
    assert "registration" in result.reason.lower()
```

- [ ] **Step 2: Run the interface RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_lifecycle.py -q --basetemp C:\tmp\frame-lifecycle-task1-red
```

Expected: collection fails because `coordinates.lifecycle` does not exist.

- [ ] **Step 3: Create immutable types and the two operations**

```python
@dataclass(frozen=True)
class FrameSelectionContext:
    records: tuple[CoordinateFrameRecord, ...]
    requested_frame_id: str
    explicit: bool
    homed_axes: frozenset[str]
    authority_axes: frozenset[str]


@dataclass(frozen=True)
class FrameSelectionDecision:
    selected_frame_id: str
    available: bool
    reason: str | None
    persist_selection: bool


class CoordinateFrameLifecycle:
    def __init__(self, *, selected_frame_id: str = MACHINE_FRAME_ID) -> None:
        self._selected_frame_id = selected_frame_id
```

Add these exact public methods to the class:

```python
def plan_selection(self, context: FrameSelectionContext) -> FrameSelectionDecision
def design_usability(self, context: DesignUsabilityContext) -> DesignFrameUsabilitySnapshot
```

`plan_selection()` computes the decision from the record set and updates
`_selected_frame_id` only for an accepted explicit selection or permanent
fallback. `design_usability()` is side-effect free. Move the existing frozen
usability snapshot type from `main.py` and reuse the existing exact
readiness/provenance rules rather than copying a simplified version.

- [ ] **Step 4: Make presentation and Main thin adapters**

`build_coordinate_display_plan()` receives a `FrameSelectionDecision` or calls the module once. `Main._design_frame_usability_snapshot()` only constructs `DesignUsabilityContext` and returns the module result. Remove the old conditional policy after all callers delegate.

- [ ] **Step 5: Run focused and motion-consumer GREEN gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_lifecycle.py tests\coordinates\test_presentation.py tests\ui\test_stage_position_panel.py tests\app\test_main_design_navigation.py tests\app\test_main_route_measurement_session.py tests\app\test_main_microscope_scan.py -q --basetemp C:\tmp\frame-lifecycle-task1-green
```

Expected: all collected tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add probe_station_gui/coordinates/lifecycle.py probe_station_gui/coordinates/__init__.py probe_station_gui/coordinates/presentation.py probe_station_gui/views/main_window_stage_position_panel.py main.py tests/coordinates/test_lifecycle.py tests/coordinates/test_presentation.py tests/app/test_main_design_navigation.py
git commit -m "refactor: centralize coordinate frame selection and usability"
```

### Task 2: Move load generation and publication rollback into CoordinateFrameLifecycle

**Files:**
- Modify: `probe_station_gui/coordinates/lifecycle.py`
- Modify: `probe_station_gui/views/main_window_connection_flow.py`
- Modify: `main.py:7880-8230`
- Modify: `tests/coordinates/test_lifecycle.py`
- Modify: `tests/ui/test_main_window_connection_flow.py`
- Modify: `tests/app/test_main_design_navigation.py`

**Interfaces:**
- Consumes: monotonically increasing request IDs, immutable documents/records and success/failure results.
- Produces: `FrameLifecycleEffects`, `FrameLoadResult`, `FramePublication`, and `FramePublicationResult`; owns stale-load filtering and the proven coalesced/interleaved registration rollback algorithm.

- [ ] **Step 1: Write load-generation and stale-result RED tests**

```python
def test_only_current_load_result_produces_registry_replacement() -> None:
    lifecycle = CoordinateFrameLifecycle()
    lifecycle.begin_load(10)
    lifecycle.begin_load(11)
    stale = lifecycle.accept_load(FrameLoadResult(request_id=10, records=(_ready_design_record(),)))
    current = lifecycle.accept_load(FrameLoadResult(request_id=11, records=(_ready_design_record(),)))
    assert stale == FrameLifecycleEffects()
    assert current.replace_records == (_ready_design_record(),)
```

- [ ] **Step 2: Write complete publication-boundary RED tests**

```python
@pytest.mark.parametrize("intervening", ["none", "focus", "trailing_focus"])
def test_failed_coalesced_registration_chain_rolls_to_earliest_predecessor(intervening: str) -> None:
    lifecycle = _lifecycle_with_durable_v0()
    _track_registration_and_intervening_publications(lifecycle, intervening)
    effects = lifecycle.finish_publication(FramePublicationResult(request_id=43, succeeded=False))
    assert effects.rollback_record.version == 0


def test_older_failure_defers_while_newer_publication_is_pending() -> None:
    lifecycle = _lifecycle_with_durable_v0()
    lifecycle.track_publication(_registration_publication(41, before=0, after=1))
    lifecycle.track_publication(_ordinary_publication(42, version=2))
    assert lifecycle.finish_publication(FramePublicationResult(41, False)).rollback_record is None
    assert lifecycle.finish_publication(FramePublicationResult(42, True)).rollback_record is None
```

- [ ] **Step 3: Run Task 2 RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_lifecycle.py -q -k "load or publication or rollback" --basetemp C:\tmp\frame-lifecycle-task2-red
```

Expected: the new interface methods/types are missing.

- [ ] **Step 4: Implement load and publication state**

```python
@dataclass(frozen=True)
class FrameLifecycleEffects:
    replace_records: tuple[CoordinateFrameRecord, ...] | None = None
    rollback_record: CoordinateFrameRecord | None = None
    invalidate_session_reason: str | None = None
    refresh_display: bool = False


```

Add these exact methods to `CoordinateFrameLifecycle`:

```python
def begin_load(self, request_id: int) -> FrameLifecycleEffects
def accept_load(self, result: FrameLoadResult) -> FrameLifecycleEffects
def track_publication(self, publication: FramePublication) -> None
def finish_publication(self, result: FramePublicationResult) -> FrameLifecycleEffects
```

Move the existing request generation, latest submitted save ID, registration
transaction chains and rollback grouping into this module. `begin_load()`
records the current generation and returns a session-invalidating pending
effect. `accept_load()` returns an empty effect for every non-current request.
`finish_publication()` defers a failed superseded request and collapses every
failed current registration chain to its earliest durable predecessor. `Main`
executes returned registry/session/publish effects but contains no version-chain
policy.

- [ ] **Step 5: Run focused and persistence integration GREEN gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_lifecycle.py tests\coordinates\test_persistence.py tests\ui\test_main_window_connection_flow.py tests\app\test_main_design_navigation.py -q --basetemp C:\tmp\frame-lifecycle-task2-green
```

Expected: all collected tests pass, including every coalesced/interleaved rollback regression.

- [ ] **Step 6: Commit Task 2**

```powershell
git add probe_station_gui/coordinates/lifecycle.py probe_station_gui/views/main_window_connection_flow.py main.py tests/coordinates/test_lifecycle.py tests/ui/test_main_window_connection_flow.py tests/app/test_main_design_navigation.py
git commit -m "refactor: move coordinate persistence lifecycle out of main"
```

### Task 3: Introduce DesignRegistrationLifecycle capture batches

**Files:**
- Create: `probe_station_gui/design/registration_lifecycle.py`
- Modify: `probe_station_gui/design/__init__.py`
- Modify: `main.py:650-780,10180-10520`
- Create: `tests/design/test_registration_lifecycle.py`
- Modify: `tests/app/test_main_design_navigation.py`

**Interfaces:**
- Consumes: immutable design/document/frame context, synchronized physical Machine X/Y/B sample and configured pivot.
- Produces: `RegistrationCaptureToken`, `RegistrationSample`, `RegistrationEffects`; owns pending evidence, baselines and context cancellation.

- [ ] **Step 1: Write capture-token RED tests**

```python
def test_stale_sample_after_context_change_is_rejected_and_baseline_restored() -> None:
    lifecycle = DesignRegistrationLifecycle()
    token = lifecycle.begin_capture(_context(frame_version=1, top_cell="TOP"))
    cancellation = lifecycle.cancel(RegistrationCancellation.TOP_CELL_CHANGED)
    stale = lifecycle.accept_sample(token, _sample())
    assert cancellation.restore_baseline is True
    assert stale.accepted is False


def test_mixed_b_samples_are_normalized_about_captured_pivot() -> None:
    lifecycle = DesignRegistrationLifecycle()
    first = lifecycle.begin_capture(_context(frame_version=1, top_cell="TOP"))
    lifecycle.accept_sample(first, _sample(x=10.0, y=0.0, b=0.0))
    second = lifecycle.begin_capture(_context(frame_version=1, top_cell="TOP"))
    result = lifecycle.accept_sample(second, _sample(x=0.0, y=10.0, b=90.0))
    assert result.normalized_samples[0].reference_b_deg == result.normalized_samples[1].reference_b_deg
```

- [ ] **Step 2: Run capture lifecycle RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_registration_lifecycle.py -q --basetemp C:\tmp\registration-lifecycle-task3-red
```

Expected: collection fails because the module does not exist.

- [ ] **Step 3: Implement immutable capture types and state owner**

```python
@dataclass(frozen=True)
class RegistrationCaptureToken:
    request_id: str
    operation_id: str
    session_identity: int
    frame_id: str | None
    frame_version: int | None
    source_identity: tuple[str, str]
    top_cell_name: str
    rotation_quarter_turns: int
    pivot_machine_xy: tuple[float, float]
    source_design_marks: tuple[tuple[float, float], ...]
    check_design_marks: tuple[tuple[float, float], ...]


```

Add these exact methods to `DesignRegistrationLifecycle`:

```python
def begin_capture(self, context: RegistrationCaptureContext) -> RegistrationCaptureToken
def accept_sample(self, token: RegistrationCaptureToken, sample: RegistrationSample) -> RegistrationEffects
def cancel(self, reason: RegistrationCancellation) -> RegistrationEffects
```

`begin_capture()` reuses the operation ID only when every immutable context
field matches; otherwise it returns a cancellation/rollback effect before
starting a new operation. `accept_sample()` rejects a stale token, preserves
the original B/pivot per sample, and returns a commit effect only for a complete
valid batch. `cancel()` clears all pending evidence and returns the saved
baseline exactly once. Move `_RegistrationMarkCaptureContext`,
`_CapturedRegistrationSample`, `_PendingRegistrationEvidence`, pending
dictionaries and baseline restoration policy out of `Main`. `Main` captures
Machine snapshots and executes commit/rollback effects only.

- [ ] **Step 4: Run capture and navigation GREEN gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_registration_lifecycle.py tests\app\test_main_design_navigation.py tests\design\test_workflow.py -q --basetemp C:\tmp\registration-lifecycle-task3-green
```

Expected: all collected tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add probe_station_gui/design/registration_lifecycle.py probe_station_gui/design/__init__.py main.py tests/design/test_registration_lifecycle.py tests/app/test_main_design_navigation.py
git commit -m "refactor: centralize design registration evidence lifecycle"
```

### Task 4: Move focus candidate and first-contact eligibility into DesignRegistrationLifecycle

**Files:**
- Modify: `probe_station_gui/design/registration_lifecycle.py`
- Modify: `main.py:1160-1185,10300-10650`
- Modify: `tests/design/test_registration_lifecycle.py`
- Modify: `tests/app/test_main_design_navigation.py`
- Modify: `tests/design/test_frame_references.py`

**Interfaces:**
- Consumes: `FocusCandidate`, immutable `RegistrationContext`, focus completion and contact completion.
- Produces: `set_focus_candidate()`, `accept_focus()`, `accept_first_contact()` effects; owns candidate/token validity and the rule that A is first-write-wins only after ready Z.

- [ ] **Step 1: Write focus/contact RED tests**

```python
def test_context_change_invalidates_focus_candidate_before_acceptance() -> None:
    lifecycle = DesignRegistrationLifecycle()
    lifecycle.set_focus_candidate(_candidate(), _registration_context(objective="5x"))
    result = lifecycle.accept_focus(_registration_context(objective="10x"))
    assert result.accepted is False
    assert result.clear_focus_candidate is True


def test_contact_is_first_write_wins_and_requires_ready_z() -> None:
    lifecycle = DesignRegistrationLifecycle()
    assert lifecycle.accept_first_contact(_registration_context(z_ready=False), 4.0).accepted is False
    first = lifecycle.accept_first_contact(_registration_context(z_ready=True), 4.0)
    second = lifecycle.accept_first_contact(_registration_context(z_ready=True, a_ready=True), 5.0)
    assert first.commit_a_mm == 4.0
    assert second.commit_a_mm is None
```

- [ ] **Step 2: Run focus/contact RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_registration_lifecycle.py -q -k "focus or contact" --basetemp C:\tmp\registration-lifecycle-task4-red
```

Expected: the lifecycle lacks these operations.

- [ ] **Step 3: Implement focus/contact state transitions**

Store the candidate and its immutable context inside `DesignRegistrationLifecycle`. `accept_focus()` compares exact context identity before returning a Z commit effect. `accept_first_contact()` returns an A commit only when Z is ready and A is missing. `cancel()` clears candidate and eligibility for every context-changing reason named in the spec.

- [ ] **Step 4: Replace Main-owned fields with module calls**

Remove `_pending_registration_focus_token`, `_pending_registration_focus_target_xy`, `_focus_candidate`, and duplicated A callback construction guards after all call sites delegate. Keep Qt signals and autofocus/controller calls in `Main`.

- [ ] **Step 5: Run focus/contact/route GREEN gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_registration_lifecycle.py tests\design\test_frame_references.py tests\app\test_main_design_navigation.py tests\route tests\app\test_main_route_measurement_session.py -q --basetemp C:\tmp\registration-lifecycle-task4-green
```

Expected: all collected tests and route-control subtests pass.

- [ ] **Step 6: Commit Task 4**

```powershell
git add probe_station_gui/design/registration_lifecycle.py main.py tests/design/test_registration_lifecycle.py tests/design/test_frame_references.py tests/app/test_main_design_navigation.py
git commit -m "refactor: move focus and contact lifecycle out of main"
```

### Task 5: Remove duplicate Main policy and verify both deep module seams

**Files:**
- Modify: `main.py`
- Modify: `probe_station_gui/coordinates/lifecycle.py`
- Modify: `probe_station_gui/design/registration_lifecycle.py`
- Modify: `tests/coordinates/test_lifecycle.py`
- Modify: `tests/design/test_registration_lifecycle.py`
- Modify: integration tests only where delegation assertions are required.

**Interfaces:**
- Consumes: Tasks 1-4 modules.
- Produces: `Main` methods that only collect immutable context, call one module operation and execute returned effects.

- [ ] **Step 1: Add delegation characterization tests**

```python
def test_main_coordinate_callback_executes_lifecycle_effects_once() -> None:
    owner = _main_owner_with_spy_lifecycle()
    Main._on_coordinate_frames_saved(owner, _success(12))
    assert owner.coordinate_lifecycle.calls == [("finish_publication", 12, True)]
    assert owner.applied_effects == 1


def test_main_registration_context_change_cancels_lifecycle_once() -> None:
    owner = _main_owner_with_spy_registration_lifecycle()
    Main._unload_design(owner)
    assert owner.registration_lifecycle.cancellations == [RegistrationCancellation.DOCUMENT_UNLOADED]
```

- [ ] **Step 2: Run delegation RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\app\test_main_design_navigation.py tests\ui\test_main_window_connection_flow.py -q -k "lifecycle or delegation" --basetemp C:\tmp\coordinate-lifecycle-task5-red
```

Expected: `Main` still mutates policy fields directly or invokes duplicate helpers.

- [ ] **Step 3: Delete superseded fields and pass-through policy**

Remove Main-owned selection/persistence/capture/focus state transferred by Tasks 1-4. Retain only module instances, Qt/hardware adapters and small `_apply_*_effects()` methods. `rg` must show no production references to removed private fields or old rollback/capture-context helpers.

- [ ] **Step 4: Run module, integration and static gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_lifecycle.py tests\design\test_registration_lifecycle.py tests\coordinates tests\design tests\ui\test_main_window_connection_flow.py tests\ui\test_stage_position_panel.py tests\app\test_main_design_navigation.py tests\app\test_main_route_measurement_session.py tests\app\test_main_microscope_scan.py tests\route -q --basetemp C:\tmp\coordinate-lifecycle-task5-green
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check main.py probe_station_gui/coordinates/lifecycle.py probe_station_gui/design/registration_lifecycle.py tests/coordinates/test_lifecycle.py tests/design/test_registration_lifecycle.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q main.py probe_station_gui tests
```

Expected: all tests pass and no new Ruff finding remains. Use only the repository's documented exclusions for unchanged import-order/re-export baselines.

- [ ] **Step 5: Commit Task 5**

```powershell
git add main.py probe_station_gui/coordinates/lifecycle.py probe_station_gui/design/registration_lifecycle.py tests
git commit -m "refactor: complete coordinate lifecycle module extraction"
```

### Task 6: Full verification and final whole-branch review

**Files:**
- Modify only for scoped fixes required by review.
- Test: complete repository suite.

**Interfaces:**
- Consumes: all cleanup and extraction tasks.
- Produces: clean, reviewed checkpoint before Plan 3.

- [ ] **Step 1: Run fresh complete suite**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -qq --basetemp C:\tmp\coordinate-lifecycle-full
```

- [ ] **Step 2: Run range-wide static gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 main.py probe_station_gui tests
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q main.py probe_station_gui tests
git diff --check 7bc1f6f..HEAD
git status --short
```

- [ ] **Step 3: Request final whole-branch review**

Generate one review package from the merge base with `main` to `HEAD`. The reviewer must verify the approved spec, both module interfaces, absence of duplicated policy in `Main`, all four behavioural fixes, route-control safety, GUI-thread constraints and persistence losslessness. Plans 3/4 are future scope and must not be reported missing.

- [ ] **Step 4: Apply the review protocol**

Fix every Critical/Important finding through strict RED/GREEN and one scoped re-review. Record Minor findings for later triage. Finish only when the reviewer reports no remaining Critical/Important issue and the fresh complete suite exits zero.
