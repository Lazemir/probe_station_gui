# Coordinate Lifecycle Behavioral Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four remaining behavioural coordinate-lifecycle defects without changing motion, registration, route-control, or hardware semantics.

**Architecture:** Keep the existing adapters and persistence models, but make selection intent explicit, make deletion remove duplicate raw UUID slots, make persistence-worker backend failure a retryable thread retirement, and validate signed FOV dimensions. Each change is independently testable and committed before lifecycle extraction starts.

**Tech Stack:** Python 3.11+, PySide6, dataclasses, pytest, unittest/Qt event pumping, Ruff.

## Global Constraints

- Work only in `codex/software-coordinate-systems` and the existing linked worktree.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python command.
- Do not run hardware-dependent code.
- Do not change route Pause/Resume/Interrupt semantics or API route-control naming.
- Do not add Plan 3 frame-relative motion or Plan 4 B-axis point hold.
- Do not emit Qt signals while holding a non-reentrant lock.
- Preserve malformed/future raw persistence data unless an explicit deletion names its UUID.
- Keep C shallowly hidden; do not add per-axis enable switches.

---

### Task 1: Preserve selected frame through temporary authority loss

**Files:**
- Modify: `probe_station_gui/coordinates/presentation.py:20-330`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py:140-255`
- Test: `tests/coordinates/test_presentation.py`
- Test: `tests/ui/test_stage_position_panel.py`

**Interfaces:**
- Consumes: `RegistrySnapshot`, `PhysicalMachinePose`, selector entries and existing `_frame_selection_reason()`.
- Produces: `CoordinateDisplayPlan.selection_available: bool` and `CoordinateDisplayPlan.selection_reason: str | None`; `build_coordinate_display_plan()` preserves an existing record ID even when temporarily unavailable; explicit selection still rejects disabled entries.

- [ ] **Step 1: Write failing presentation tests**

```python
def test_existing_selection_survives_temporary_b_authority_loss() -> None:
    plan = _plan(
        _snapshot(_ready_design_record()),
        selected_frame_id=DESIGN_ID,
        authority_axes={"X", "Y", "Z", "A"},
    )
    assert plan.selected_frame_id == DESIGN_ID
    assert plan.selection_available is False
    assert plan.selection_reason
    assert all(update.value is None for update in plan.axis_updates[:2])


def test_missing_selected_record_is_permanent_machine_fallback() -> None:
    plan = _plan(_snapshot(), selected_frame_id=DESIGN_ID)
    assert plan.selected_frame_id == MACHINE_FRAME_ID
    assert plan.selection_available is True
```

- [ ] **Step 2: Write failing panel persistence tests**

```python
def test_temporary_b_loss_does_not_persist_machine_selection() -> None:
    owner = _Owner(selected=DESIGN_ID, authority_axes={"X", "Y", "Z", "A"})
    update_software_coordinate_display(owner, owner._latest_physical_machine_pose)
    assert owner._selected_coordinate_frame_id == DESIGN_ID
    assert owner.persisted_selections == []


def test_explicit_unavailable_selection_keeps_previous_selection() -> None:
    owner = _Owner(selected=MACHINE_FRAME_ID, authority_axes={"X", "Y", "Z", "A"})
    select_gui_coordinate_frame(owner, DESIGN_ID)
    assert owner._selected_coordinate_frame_id == MACHINE_FRAME_ID
```

- [ ] **Step 3: Run the focused RED gate**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_presentation.py tests\ui\test_stage_position_panel.py -q --basetemp C:\tmp\coordinate-cleanup-task1-red
```

Expected: the new tests fail because the display plan substitutes and persists Machine.

- [ ] **Step 4: Implement selection intent and availability**

```python
@dataclass(frozen=True)
class CoordinateDisplayPlan:
    selected_frame_id: str
    entries: tuple[CoordinateSelectorEntry, ...]
    axis_updates: tuple[CoordinateAxisDisplay, ...]
    selection_available: bool = True
    selection_reason: str | None = None
```

In `build_coordinate_display_plan()`, fall back to Machine only when the requested record is absent. When the record exists but `_frame_selection_reason()` is non-`None`, retain its ID, return unavailable axis updates, and expose the reason. In `select_gui_coordinate_frame()`, inspect the requested selector entry and leave the previous selection unchanged when it is disabled. Do not persist a selection merely because authority is temporarily missing.

- [ ] **Step 5: Run focused and neighbouring GREEN gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_presentation.py tests\ui\test_stage_position_panel.py tests\app\test_main_stage_coordinate_controls.py -q --basetemp C:\tmp\coordinate-cleanup-task1-green
```

Expected: all collected tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add probe_station_gui/coordinates/presentation.py probe_station_gui/views/main_window_stage_position_panel.py tests/coordinates/test_presentation.py tests/ui/test_stage_position_panel.py
git commit -m "fix: preserve temporarily unavailable frame selection"
```

### Task 2: Make explicit deletion remove every raw duplicate UUID slot

**Files:**
- Modify: `probe_station_gui/coordinates/persistence.py:50-150`
- Modify: `probe_station_gui/settings/software_coordinates.py:278-430`
- Modify: `probe_station_gui/dialogs/settings/coordinate_system.py:200-235`
- Test: `tests/coordinates/test_persistence.py`
- Test: `tests/settings/test_software_coordinates.py`
- Test: `tests/ui/test_settings_software_coordinates.py`

**Interfaces:**
- Consumes: raw accepted/rejected record slots and canonical UUID strings.
- Produces: `CoordinateFrameDocument.without_frame_id(frame_id: str) -> CoordinateFrameDocument`; `SoftwareCoordinateSettings.without_custom_frame(frame_id: str) -> SoftwareCoordinateSettings`.

- [ ] **Step 1: Write failing durable-document deletion tests**

```python
def test_explicit_delete_removes_rejected_duplicate_uuid_slot() -> None:
    payload = CoordinateFrameDocument(records=(_record(),)).to_dict()
    duplicate = deepcopy(payload["records"][0])
    duplicate["future_field"] = {"schema": 2}
    payload["records"].append(duplicate)
    loaded = CoordinateFrameDocument.from_dict(payload)
    deleted = loaded.without_frame_id(_record().frame_id)
    reloaded = CoordinateFrameDocument.from_dict(deleted.to_dict())
    assert reloaded.records == ()
    assert deleted.to_dict()["records"] == []


def test_delete_preserves_unrelated_unidentified_raw_slot() -> None:
    payload = CoordinateFrameDocument(records=(_record(),)).to_dict()
    payload["records"].append({"future_payload": [1, 2, 3]})
    deleted = CoordinateFrameDocument.from_dict(payload).without_frame_id(_record().frame_id)
    assert deleted.to_dict()["records"] == [{"future_payload": [1, 2, 3]}]
```

- [ ] **Step 2: Write failing Custom settings deletion tests**

```python
def test_custom_delete_removes_future_duplicate_uuid_slot() -> None:
    raw = SoftwareCoordinateSettings(custom_frames=(_custom(),)).to_dict()
    duplicate = deepcopy(raw["custom_frames"][0])
    duplicate["future_field"] = True
    raw["custom_frames"].append(duplicate)
    settings = parse_software_coordinate_settings(raw, section_present=True)
    deleted = settings.without_custom_frame(_custom().frame_id)
    assert deleted.to_dict()["custom_frames"] == []
```

- [ ] **Step 3: Run the focused RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_persistence.py tests\settings\test_software_coordinates.py tests\ui\test_settings_software_coordinates.py -q --basetemp C:\tmp\coordinate-cleanup-task2-red
```

Expected: tests fail because rejected duplicate raw slots survive deletion or the new methods do not exist.

- [ ] **Step 4: Implement canonical raw-slot deletion**

```python
def _explicit_raw_frame_id(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("frame_id")
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None
```

Both `without_*()` methods must remove typed records and every raw mapping whose canonical UUID equals the requested UUID, then rebuild accepted-slot indices for remaining records. The Settings editor must call `without_custom_frame()` rather than replacing only the typed tuple.

- [ ] **Step 5: Run focused and materialization GREEN gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_persistence.py tests\settings\test_software_coordinates.py tests\ui\test_settings_software_coordinates.py tests\coordinates\test_custom_frame_materialization.py -q --basetemp C:\tmp\coordinate-cleanup-task2-green
```

Expected: all collected tests pass and unrelated malformed raw slots remain byte-for-byte equivalent.

- [ ] **Step 6: Commit Task 2**

```powershell
git add probe_station_gui/coordinates/persistence.py probe_station_gui/settings/software_coordinates.py probe_station_gui/dialogs/settings/coordinate_system.py tests/coordinates/test_persistence.py tests/settings/test_software_coordinates.py tests/ui/test_settings_software_coordinates.py
git commit -m "fix: prevent deleted coordinate frames from returning"
```

### Task 3: Recover coordinate persistence after backend-factory failure

**Files:**
- Modify: `probe_station_gui/coordinates/persistence.py:390-620`
- Test: `tests/coordinates/test_persistence.py`

**Interfaces:**
- Consumes: `CoordinateFrameStoreWorker._backend_factory`, queued `_StoreOperation` values and creator-thread signals.
- Produces: transient worker-run retirement; every queued request receives `CoordinateFrameStoreFailure`; next submission creates a new thread unless `stop()` was called.

- [ ] **Step 1: Write fail-once/recover tests**

```python
def test_backend_factory_failure_fails_queue_and_next_submit_restarts(qtbot) -> None:
    attempts = 0
    backend = _RecordingBackend(CoordinateFrameDocument())

    def factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("factory failed")
        return backend

    worker = CoordinateFrameStoreWorker(backend_factory=factory)
    failures: list[CoordinateFrameStoreFailure] = []
    loaded: list[CoordinateFrameLoadResult] = []
    worker.failed.connect(failures.append)
    worker.loaded.connect(loaded.append)
    worker.load(1)
    worker.publish(2, CoordinateFrameDocument())
    qtbot.waitUntil(lambda: len(failures) == 2)
    worker.load(3)
    qtbot.waitUntil(lambda: [item.request_id for item in loaded] == [3])
    assert attempts == 2
    worker.stop()
```

- [ ] **Step 2: Run the worker RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_persistence.py -q -k "factory or worker" --basetemp C:\tmp\coordinate-cleanup-task3-red
```

Expected: queued requests receive no failures and the later load cannot start because `_thread` still references the dead thread.

- [ ] **Step 3: Implement retryable thread retirement**

In `_run()`, catch backend creation failure before entering the operation loop. Under `_condition`, drain the current pending queue, set `_active = False`, and clear `_thread` only when it still refers to `threading.current_thread()`. Release the lock, then post one typed failure per drained request. Do not emit `finished` for transient backend failure. `_submit()` may then create a fresh worker thread. Preserve the existing final `finished` path only for `_stopping`.

- [ ] **Step 4: Run focused and shutdown GREEN gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_persistence.py tests\ui\test_main_window_shutdown.py tests\ui\test_main_window_connection_flow.py -q --basetemp C:\tmp\coordinate-cleanup-task3-green
```

Expected: all collected tests pass; no signal is emitted under the condition lock.

- [ ] **Step 5: Commit Task 3**

```powershell
git add probe_station_gui/coordinates/persistence.py tests/coordinates/test_persistence.py
git commit -m "fix: restart coordinate persistence after backend failure"
```

### Task 4: Reject non-positive signed FOV dimensions

**Files:**
- Modify: `probe_station_gui/design/focus_candidate.py:75-90`
- Test: `tests/design/test_focus_candidate.py`

**Interfaces:**
- Consumes: `_required_size(value, label)`.
- Produces: finite `width > 0` and `height > 0`, preserving the original sign.

- [ ] **Step 1: Add negative and zero FOV tests**

```python
@pytest.mark.parametrize("fov", [(-10.0, 10.0), (10.0, -10.0), (0.0, 10.0), (10.0, 0.0)])
def test_candidate_rejects_non_positive_fov(fov: tuple[float, float]) -> None:
    with pytest.raises(ValueError, match="Field of view"):
        select_central_focus_candidate(
            structures=[_structure()],
            design_bounds=(0.0, 0.0, 100.0, 100.0),
            fov_size=fov,
        )
```

- [ ] **Step 2: Run the FOV RED gate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_focus_candidate.py -q --basetemp C:\tmp\coordinate-cleanup-task4-red
```

Expected: negative values pass because `_required_size()` currently applies `abs()`.

- [ ] **Step 3: Implement strict signed validation**

```python
width = float(raw_width)
height = float(raw_height)
if not math.isfinite(width) or not math.isfinite(height) or width <= 0.0 or height <= 0.0:
    raise ValueError(f"{label} dimensions must be finite and greater than zero.")
return width, height
```

- [ ] **Step 4: Run the FOV and Design navigation GREEN gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_focus_candidate.py tests\app\test_main_design_navigation.py -q --basetemp C:\tmp\coordinate-cleanup-task4-green
```

Expected: all collected tests pass.

- [ ] **Step 5: Commit Task 4**

```powershell
git add probe_station_gui/design/focus_candidate.py tests/design/test_focus_candidate.py
git commit -m "fix: reject non-positive design field of view"
```

### Task 5: Behavioral cleanup integration gate

**Files:**
- Modify only if a regression discovered by this gate requires a scoped fix.
- Test: existing coordinate, settings, Design, shutdown, route and API suites.

**Interfaces:**
- Consumes: Tasks 1-4 commits.
- Produces: reviewed behavioral-cleanup checkpoint before module extraction.

- [ ] **Step 1: Run focused aggregate**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates tests\settings tests\design\test_focus_candidate.py tests\ui\test_stage_position_panel.py tests\ui\test_settings_software_coordinates.py tests\ui\test_main_window_connection_flow.py tests\ui\test_main_window_shutdown.py -q --basetemp C:\tmp\coordinate-cleanup-aggregate
```

- [ ] **Step 2: Run static gates**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui/coordinates/presentation.py probe_station_gui/coordinates/persistence.py probe_station_gui/views/main_window_stage_position_panel.py probe_station_gui/settings/software_coordinates.py probe_station_gui/dialogs/settings/coordinate_system.py probe_station_gui/design/focus_candidate.py tests/coordinates/test_presentation.py tests/coordinates/test_persistence.py tests/settings/test_software_coordinates.py tests/ui/test_stage_position_panel.py tests/ui/test_settings_software_coordinates.py tests/design/test_focus_candidate.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q probe_station_gui tests
git diff --check 7bc1f6f..HEAD
```

- [ ] **Step 3: Run the complete suite**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -qq --basetemp C:\tmp\coordinate-cleanup-full
```

- [ ] **Step 4: Request whole-checkpoint review**

Generate a review package from `7bc1f6f` to `HEAD`. The reviewer must check the four behaviours against `docs/superpowers/specs/2026-08-07-coordinate-lifecycle-cleanup-design.md` and triage any new Critical/Important issue before module extraction begins.
