# Design Registration and Coordinate UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn every loaded/registered design into a persistent software coordinate frame, complete it through X/Y/B marks, central-structure focus Z, and contact A, and expose Machine/Designs/Custom selection beside the position readout.

**Architecture:** Keep `DesignSession` responsible for the one active Design Window document and route state, but move long-lived coordinate state into `CoordinateFrameRegistry`. A narrow registration service converts existing design interactions into immutable frame-record updates. The position panel renders a pure presentation plan derived from calibrated `PhysicalMachinePose`, selected frame, pivot, and controller authority.

**Tech Stack:** Existing KLayout/NumPy design model, SciPy/NumPy rigid fit, PySide6 widgets/signals, coordinate core from Plan 1, existing autofocus/contact workflows, pytest/pytest-qt.

## Global Constraints

- Complete `2026-07-22-coordinate-frame-core-and-persistence.md` first.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for Python and pytest.
- Captured stage marks are calibrated physical Machine XY, never raw MPos or WCO values.
- Registration applies rigid translation+rotation only; observed scale is diagnostic and never applied.
- Design readiness is `X/Y/B -> Z -> A`; every Z change invalidates A without exception.
- A design frame cannot become active before X/Y homing; B is never homed.
- Ordinary autofocus must not change a design Z reference.
- A failed/interrupted autofocus or contact cannot establish a reference.
- Known physical B motion recomputes frames and must not stale registration.
- C is hidden shallowly from ordinary GUI controls, not deleted from backend/settings/terminal support.
- Do not alter route Pause/Resume/Interrupt semantics; add route regression coverage for every contact/autofocus hook.
- GUI text must be concise and must not expose internal request/pending/runner terminology.

---

## File Structure

- Create `probe_station_gui/design/rigid_registration.py`: unit conversion, rigid fit, residual/scale diagnostics.
- Create `probe_station_gui/design/frame_registration.py`: draft state, frame-record construction, migration, readiness updates.
- Modify `probe_station_gui/design/session.py`: link the active document to `active_frame_id`; stop owning durable coordinate validity.
- Modify `probe_station_gui/design/model.py`: route/navigation compatibility with the rigid transform.
- Modify `probe_station_gui/design/navigation_adapter.py`: load/select/new-registration behavior and legacy migration.
- Create `probe_station_gui/design/focus_candidate.py`: pure central FOV candidate selection.
- Modify `probe_station_gui/views/design_navigator_panel.py`: registration instance and focus-reference controls.
- Modify `probe_station_gui/views/design_plot_pane.py`: focus candidate/selected-point overlay and event.
- Create `probe_station_gui/coordinates/presentation.py`: selector entries and frame-aware position/readiness plan.
- Modify `probe_station_gui/views/stage_position_panel.py`: selector beside Position and axis reason tooltips.
- Modify `probe_station_gui/views/main_window_stage_position_panel.py`: calibrated Machine pose -> selected frame presentation.
- Modify `probe_station_gui/dialogs/settings/coordinate_system.py`: custom-frame CRUD editor.
- Modify `probe_station_gui/dialogs/settings_dialog.py`: pass current physical pose and save software coordinate settings.
- Modify `probe_station_gui/views/joystick_window.py` and its adapters: use visible axes without deleting C backend support.
- Modify `main.py` only for top-level object ownership and narrow signal/callback wiring.

### Task 1: Rigid Design Registration in Physical Millimetres

**Files:**
- Create: `probe_station_gui/design/rigid_registration.py`
- Modify: `probe_station_gui/design/model.py:108-280`
- Test: `tests/design/test_rigid_registration.py`
- Modify test: `tests/design/test_workflow.py:42-55,338-370`

**Interfaces:**
- Consumes: design mark points, `design_unit_mm`, calibrated physical Machine stage marks, optional check marks.
- Produces: `RigidRegistrationFit`, `fit_rigid_registration()`, `design_mm_to_machine_xy()`, `machine_xy_to_design_mm()`.

- [ ] **Step 1: Write failing tests proving scale is diagnostic only**

```python
import pytest

from probe_station_gui.design.rigid_registration import fit_rigid_registration


def test_rigid_fit_does_not_apply_measured_scale_difference() -> None:
    fit = fit_rigid_registration(
        design_points=((0.0, 0.0), (10.0, 0.0)),
        machine_points=((1.0, 2.0), (21.0, 2.0)),
        design_unit_mm=1.0,
    )
    assert fit.rotation_deg == pytest.approx(0.0)
    assert fit.distance_scale_ratio == pytest.approx(2.0)
    assert fit.design_mm_to_machine_xy((5.0, 5.0)) == pytest.approx((6.0, 7.0))


def test_rigid_fit_recovers_rotation_translation_and_check_residuals() -> None:
    fit = fit_rigid_registration(
        design_points=((0.0, 0.0), (2.0, 0.0), (0.0, 2.0)),
        machine_points=((5.0, 7.0), (5.0, 9.0), (3.0, 7.0)),
        design_unit_mm=1.0,
        check_design_points=((1.0, 1.0),),
        check_machine_points=((4.0, 8.1),),
    )
    assert fit.rotation_deg == pytest.approx(90.0)
    assert fit.source_residual.rms_mm == pytest.approx(0.0, abs=1e-10)
    assert fit.check_residual.max_mm == pytest.approx(0.1)
    assert fit.machine_xy_to_design_mm((4.0, 8.0)) == pytest.approx((1.0, 1.0))


def test_design_unit_conversion_happens_before_fit() -> None:
    fit = fit_rigid_registration(
        design_points=((0.0, 0.0), (1000.0, 0.0)),
        machine_points=((0.0, 0.0), (1.0, 0.0)),
        design_unit_mm=0.001,
    )
    assert fit.distance_scale_ratio == pytest.approx(1.0)
```

- [ ] **Step 2: Run tests and confirm the rigid module is missing**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_rigid_registration.py -v
```

Expected: FAIL importing `probe_station_gui.design.rigid_registration`.

- [ ] **Step 3: Implement one SVD rigid fit and adapt DesignRegistration**

Define these immutable results:

```python
@dataclass(frozen=True)
class ResidualMetrics:
    count: int = 0
    rms_mm: float = 0.0
    max_mm: float = 0.0


@dataclass(frozen=True)
class RigidRegistrationFit:
    rotation: np.ndarray
    offset_machine_mm: np.ndarray
    rotation_deg: float
    distance_scale_ratio: float
    source_residual: ResidualMetrics
    check_residual: ResidualMetrics
```

Convert input design points to millimetres, subtract centroids, calculate
`covariance = machine_centered.T @ design_centered`, and obtain a proper
rotation using SVD plus determinant correction. Set
`offset = machine_center - rotation @ design_center`. Compute scale ratio only
as the least-squares similarity scale diagnostic; never multiply the rotation
by it.

Change `DesignRegistration.from_marks()` to delegate to the rigid fit with an
explicit `design_unit_mm` argument. Retain its `matrix`, `offset`, conversion,
valid/stale interface for routes, but make `matrix` a proper rotation and
return diagnostic ratio through a new `distance_scale_ratio` field. Update old
similarity assertions to rigid expectations.

- [ ] **Step 4: Run registration/workflow tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_rigid_registration.py tests\design\test_workflow.py tests\design\test_registration.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit rigid fitting**

```powershell
git add probe_station_gui/design/rigid_registration.py probe_station_gui/design/model.py tests/design/test_rigid_registration.py tests/design/test_workflow.py tests/design/test_registration.py
git commit -m "feat: fit design registration as rigid transform"
```

### Task 2: Multiple Persistent Design Frame Instances and Migration

**Files:**
- Create: `probe_station_gui/design/frame_registration.py`
- Modify: `probe_station_gui/design/session.py:41-160,205-380`
- Modify: `probe_station_gui/design/navigation_adapter.py:211-353`
- Modify: `probe_station_gui/views/main_window_connection_flow.py:241-372`
- Modify: `main.py:780-950,6680-6860,8690-8970`
- Test: `tests/design/test_frame_registration.py`
- Modify test: `tests/design/test_navigation_adapter.py`
- Modify test: `tests/design/test_workflow.py`
- Modify test: `tests/route/test_session_start.py`

**Interfaces:**
- Consumes: `CoordinateFrameRegistry`, `CoordinateFrameStoreWorker`, `RigidRegistrationFit`, active `DesignDocument`, calibrated physical B and Machine XY marks.
- Produces: `DesignFrameDraft`, `DesignFrameMetadata`, `new_design_frame_draft()`, `commit_xyb_registration()`, `migrate_legacy_design_state()`, `DesignSession.active_frame_id`.

- [ ] **Step 1: Write failing multiple-instance and migration tests**

```python
from probe_station_gui.coordinates.model import ReadinessStatus
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.design.frame_registration import (
    commit_xyb_registration,
    migrate_legacy_design_state,
    new_design_frame_draft,
)


def test_same_design_can_have_two_independent_registration_ids(design_document) -> None:
    registry = CoordinateFrameRegistry()
    first = registry.add(new_design_frame_draft(design_document, existing_names=()))
    second = registry.add(new_design_frame_draft(design_document, existing_names=(first.name,)))
    assert first.frame_id != second.frame_id
    assert first.name == design_document.path.stem
    assert second.name == f"{design_document.path.stem} (2)"


def test_mark_commit_makes_only_xyb_ready(design_document) -> None:
    draft = new_design_frame_draft(design_document, existing_names=())
    registered = commit_xyb_registration(
        draft,
        design_points=((0.0, 0.0), (1000.0, 0.0)),
        physical_machine_points=((3.0, 4.0), (4.0, 4.0)),
        physical_b_deg=12.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    assert all(registered.readiness[axis].available for axis in ("X", "Y", "B"))
    assert registered.readiness["Z"].status is ReadinessStatus.MISSING
    assert registered.readiness["A"].status is ReadinessStatus.MISSING


def test_legacy_registration_migrates_without_z_or_a(legacy_design_state, design_document) -> None:
    migrated = migrate_legacy_design_state(
        legacy_design_state,
        design_document=design_document,
        physical_b_deg=0.0,
    )
    assert migrated is not None
    assert migrated.readiness["X"].available
    assert not migrated.readiness["Z"].available
    assert not migrated.readiness["A"].available


def test_changed_design_file_marks_frame_stale_instead_of_deleting_it(frame_loader) -> None:
    result = frame_loader.load_with_changed_fingerprint()
    assert result.frame is not None
    assert not result.frame.readiness["X"].available
    assert "changed" in result.frame.readiness["X"].reason.lower()


def test_route_start_snapshots_active_design_frame(route_start_context) -> None:
    result = route_start_context.start(frame_id="design-a", frame_version=4)
    assert result.frame_id == "design-a"
    assert result.frame_version == 4
```

- [ ] **Step 2: Run and verify registration service is absent**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_frame_registration.py -v
```

Expected: FAIL importing `probe_station_gui.design.frame_registration`.

- [ ] **Step 3: Implement draft/commit/migration and lifecycle wiring**

Use a `DesignFrameMetadata` serializer with these exact durable fields:

```python
@dataclass(frozen=True)
class DesignFrameMetadata:
    source_path: str
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    top_cell_name: str
    design_unit_mm: float
    source_design_marks: tuple[tuple[float, float], ...] = ()
    source_machine_marks: tuple[tuple[float, float], ...] = ()
    check_design_marks: tuple[tuple[float, float], ...] = ()
    check_machine_marks: tuple[tuple[float, float], ...] = ()
    scale_ratio: float | None = None
    rms_residual_mm: float | None = None
    max_residual_mm: float | None = None
    machine_profile_id: str = "default"
    calibration_fingerprints: tuple[tuple[str, str], ...] = ()
```

`new_design_frame_draft()` creates yellow X/Y/Z/A/B readiness and a UUID.
`commit_xyb_registration()` stores the rigid transform anchored at the supplied
physical B, sets `origin_xy_at_reference_b` to the rigid-fit offset,
`xy_angle_at_reference_b_deg` to the fitted rotation, and derives
`b_zero_machine_deg = physical_b_deg - fitted_rotation_deg`. It marks X/Y/B
ready and leaves Z/A missing. It must not move hardware.

Add `active_frame_id` to `DesignSession`; document unload clears the active
link but does not delete registry records. Loading an existing frame links it;
`New registration` creates another draft. The connection flow loads
`coordinate-frames.json` independently from `controller-state.json` and then
applies temporary authority blocks.

On first successful legacy migration, publish the new frame document before
removing the legacy `design` entry from controller state. File hashing remains
in the existing design load worker. Main owns the registry/store and stops the
worker during shutdown.

File fingerprint mismatch marks the stored record stale without deleting it.
Route start captures the active design frame ID/version alongside the existing
registration snapshot so switching the Design Window cannot retarget an active
measurement. Physical alignment remains optional and disabled in this stage;
Task 2 of the point-hold plan enables it only after compensated X/Y/B execution
exists.

- [ ] **Step 4: Run design persistence and connection regressions**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_frame_registration.py tests\design\test_navigation_adapter.py tests\design\test_workflow.py tests\ui\test_main_window_connection_flow.py tests\route\test_session_start.py -v
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit multiple design frames**

```powershell
git add probe_station_gui/design probe_station_gui/views/main_window_connection_flow.py main.py tests/design tests/ui/test_main_window_connection_flow.py tests/route/test_session_start.py
git commit -m "feat: persist multiple design coordinate frames"
```

### Task 3: Central FOV Focus Reference and Contact Reference

**Files:**
- Create: `probe_station_gui/design/focus_candidate.py`
- Modify: `probe_station_gui/views/design_plot_pane.py`
- Modify: `probe_station_gui/views/design_navigator_panel.py:213-245,979-1010`
- Modify: `probe_station_gui/stage/autofocus_flow.py`
- Modify: `probe_station_gui/route/contact_lifecycle.py:181-360`
- Modify: `main.py:1000-1060,4310-4360,8760-8835`
- Test: `tests/design/test_focus_candidate.py`
- Test: `tests/design/test_frame_references.py`
- Modify test: `tests/stage/test_controller_autofocus.py`
- Modify test: `tests/route/test_contact_lifecycle_interface.py`

**Interfaces:**
- Consumes: active design draft/record, current objective FOV in design units, visible design geometry, successful autofocus/contact callbacks, current `PhysicalMachinePose`.
- Produces: `FocusCandidate`, `select_central_focus_candidate()`, `set_focus_reference()`, `set_contact_reference()`, `reset_focus_reference()`.

- [ ] **Step 1: Write failing focus/contact dependency tests**

```python
from probe_station_gui.design.focus_candidate import select_central_focus_candidate
from probe_station_gui.design.frame_registration import (
    reset_focus_reference,
    set_contact_reference,
    set_focus_reference,
)


def test_candidate_is_near_design_center_and_fits_current_fov() -> None:
    candidate = select_central_focus_candidate(
        design_bounds=(0.0, 0.0, 100.0, 100.0),
        structure_bounds=((48.0, 48.0, 52.0, 52.0), (10.0, 10.0, 30.0, 30.0)),
        fov_size=(10.0, 10.0),
    )
    assert candidate.center == (50.0, 50.0)
    assert candidate.bounds == (48.0, 48.0, 52.0, 52.0)


def test_focus_completes_registration_and_reset_always_clears_contact(registered_xyb) -> None:
    focused = set_focus_reference(registered_xyb, physical_machine_z_mm=6.0)
    contacted = set_contact_reference(focused, physical_machine_a_mm=8.0)
    reset = reset_focus_reference(contacted, reason="Focus reference changed.")
    assert not reset.readiness["Z"].available
    assert not reset.readiness["A"].available
    assert reset.transform.z_zero_machine_mm is None
    assert reset.transform.a_zero_machine_mm is None


def test_failed_operations_do_not_capture_references(reference_controller) -> None:
    reference_controller.autofocus_finished(success=False, physical_z_mm=2.0)
    reference_controller.contact_finished(success=False, physical_a_mm=3.0)
    assert reference_controller.record.transform.z_zero_machine_mm is None
    assert reference_controller.record.transform.a_zero_machine_mm is None
```

- [ ] **Step 2: Run and confirm focus module/references are missing**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_focus_candidate.py tests\design\test_frame_references.py -v
```

Expected: FAIL importing the new functions.

- [ ] **Step 3: Implement candidate selection and explicit workflow tokens**

Define:

```python
@dataclass(frozen=True)
class FocusCandidate:
    center: tuple[float, float]
    bounds: tuple[float, float, float, float]
    distance_from_design_center: float
```

`select_central_focus_candidate()` rejects non-finite/empty structures, keeps
only bounds whose width and height fit the current FOV, and sorts by distance
from structure center to design center, then smaller area for deterministic
ties. Geometry extraction runs in the existing design worker. The operator may
replace the result by clicking another Design Window point.

Add a registration-focus operation token carrying `frame_id` and record
version. Only autofocus started with that token may call
`set_focus_reference()` after success. Ordinary autofocus never calls it.
After setting Z, publish the frame document and mark registration complete.

Add a narrow post-success contact callback to `RouteContactFlow` and manual
contact completion. It captures A only when an active design frame has valid Z,
the contact completed successfully, and the frame version still matches. Do
not move the callback before the existing interrupt and quality checkpoints.
Reset focus calls the registry cascade and never accepts A in the same update.

Add candidate and selected-focus overlays plus concise `Find focus reference`,
`Use selected point`, and `Reset focus reference` controls to Design Window.

- [ ] **Step 4: Run focus, contact, route-control, and autofocus tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_focus_candidate.py tests\design\test_frame_references.py tests\stage\test_controller_autofocus.py tests\route\test_contact_lifecycle_interface.py tests\route\test_measurement_api_route_control.py -v
```

Expected: all tests PASS, including Interrupt preventing later contact steps.

- [ ] **Step 5: Commit design Z/A workflow**

```powershell
git add probe_station_gui/design probe_station_gui/views/design_plot_pane.py probe_station_gui/views/design_navigator_panel.py probe_station_gui/stage/autofocus_flow.py probe_station_gui/route/contact_lifecycle.py main.py tests/design tests/stage/test_controller_autofocus.py tests/route
git commit -m "feat: complete design frames with focus and contact"
```

### Task 4: Coordinate Selector and Frame-Aware Position Presentation

**Files:**
- Create: `probe_station_gui/coordinates/presentation.py`
- Modify: `probe_station_gui/views/stage_position_panel.py:53-220`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py:16-258`
- Modify: `probe_station_gui/stage/position_update.py:190-315`
- Modify: `main.py:780-950,1070-1110`
- Test: `tests/coordinates/test_presentation.py`
- Modify test: `tests/ui/test_stage_position_panel.py`
- Modify test: `tests/stage/test_position_update.py`

**Interfaces:**
- Consumes: registry snapshot, selected/pending frame ID, calibrated `PhysicalMachinePose`, pivot settings, Machine homed/authority state.
- Produces: `CoordinateSelectorEntry`, `CoordinateDisplayPlan`, `build_coordinate_display_plan()`, `StagePositionPanel.coordinate_system_changed`.

- [ ] **Step 1: Write failing selector/display/restore tests**

```python
from probe_station_gui.coordinates.presentation import build_coordinate_display_plan


def test_selector_groups_machine_designs_and_customs(frame_snapshot, physical_pose) -> None:
    plan = build_coordinate_display_plan(
        frame_snapshot,
        selected_frame_id="machine",
        physical_pose=physical_pose,
        pivot_machine_xy=(0.0, 0.0),
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A", "B"},
    )
    assert [(entry.group, entry.name) for entry in plan.selector_entries] == [
        ("Machine", "Machine"),
        ("Designs", "chip-a"),
        ("Custom", "fixture"),
    ]


def test_design_draft_starts_all_visible_axes_yellow(frame_snapshot, physical_pose) -> None:
    plan = build_coordinate_display_plan(
        frame_snapshot,
        selected_frame_id="draft-frame",
        physical_pose=physical_pose,
        pivot_machine_xy=(0.0, 0.0),
        homed_axes={"X", "Y"},
        authority_axes={"X", "Y", "Z", "A", "B"},
        allow_unavailable_selection_for_preview=True,
    )
    assert all(update.color_role == "unavailable" for update in plan.axis_updates)


def test_last_design_selection_restores_with_yellow_a_but_not_yellow_z(restore_decider) -> None:
    assert restore_decider(frame_id="complete_except_a", homed_axes={"X", "Y"}) == "complete_except_a"
    assert restore_decider(frame_id="missing_z", homed_axes={"X", "Y"}) == "machine"
```

- [ ] **Step 2: Run presentation/UI tests and confirm missing API**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_presentation.py tests\ui\test_stage_position_panel.py -v
```

Expected: FAIL importing `build_coordinate_display_plan` or finding the selector.

- [ ] **Step 3: Implement pure presentation and selector widget**

Define immutable presentation values:

```python
@dataclass(frozen=True)
class CoordinateSelectorEntry:
    frame_id: str
    name: str
    group: str
    enabled: bool
    reason: str = ""


@dataclass(frozen=True)
class CoordinateAxisDisplay:
    axis: str
    value: float | None
    color_role: str
    tooltip: str


@dataclass(frozen=True)
class CoordinateDisplayPlan:
    selected_frame_id: str
    selector_entries: tuple[CoordinateSelectorEntry, ...]
    axis_updates: tuple[CoordinateAxisDisplay, ...]
```

Machine display uses `PhysicalMachinePose` directly. B-attached display uses
the frame transform and refuses a dependent value when authority/readiness is
missing. The presentation layer does not read raw MPos or call hardware.

Insert a grouped `QComboBox` immediately after the `Position:` label in
`StagePositionPanel`; group headings are disabled model rows. Emit only stable
frame IDs. Add `set_coordinate_display_plan()` that updates values, blue/yellow
base colors, and exact reason tooltips without rebuilding fields.

In `main_window_stage_position_panel`, convert the full raw status tuple through
`StageAxisCalibrationMapper` into `PhysicalMachinePose` first, then call the
presentation function. Keep a pending last-selection restore; after X/Y homing,
restore only a current frame with ready X/Y/B/Z. Cancel pending restore after
any explicit user selection. Persist successful selections through
`SettingsManager.update_and_save()`.

- [ ] **Step 4: Run position UI and presenter regressions**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_presentation.py tests\ui\test_stage_position_panel.py tests\stage\test_position_update.py tests\stage\test_position_presenter.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit selector and display**

```powershell
git add probe_station_gui/coordinates/presentation.py probe_station_gui/views/stage_position_panel.py probe_station_gui/views/main_window_stage_position_panel.py probe_station_gui/stage/position_update.py main.py tests/coordinates/test_presentation.py tests/ui/test_stage_position_panel.py tests/stage
git commit -m "feat: select and display software coordinate frames"
```

### Task 5: Custom Frame Settings UI and Shallow C Hiding

**Files:**
- Replace implementation: `probe_station_gui/dialogs/settings/coordinate_system.py`
- Modify: `probe_station_gui/dialogs/settings_dialog.py:871-1050`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py:40-54`
- Modify: `probe_station_gui/views/joystick_window.py:320-730`
- Modify: `probe_station_gui/settings/manager.py:250-310`
- Modify: `main.py:3310-3470`
- Test: `tests/ui/test_settings_software_coordinates.py`
- Modify test: `tests/ui/test_settings_objectives_coordinates.py`
- Modify test: `tests/ui/test_stage_position_panel.py`

**Interfaces:**
- Consumes: `SoftwareCoordinateSettings`, optional cached `PhysicalMachinePose`, stage-idle callback.
- Produces: `CoordinateSystemSettingsWidget.to_settings()`, custom-frame Add/Duplicate/Rename/Delete/edit operations; ordinary controls based on `VISIBLE_STAGE_AXES`.

- [ ] **Step 1: Write failing custom UI and C visibility tests**

```python
def test_coordinates_tab_adds_renames_and_deletes_custom_frame(qtbot, settings_dialog) -> None:
    tab = settings_dialog.coordinate_system_tab
    tab.add_custom_frame()
    tab.set_current_name("fixture")
    tab.set_current_origin(x_mm=1.0, y_mm=2.0)
    saved = settings_dialog.result_settings().software_coordinates
    assert saved.custom_frames[0].name == "fixture"
    frame_id = saved.custom_frames[0].frame_id
    tab.delete_current_frame()
    assert all(frame.frame_id != frame_id for frame in tab.settings().custom_frames)


def test_use_current_position_fills_physical_machine_values(settings_coordinate_tab) -> None:
    settings_coordinate_tab.use_current_position()
    frame = settings_coordinate_tab.current_frame()
    assert (frame.origin_x_mm, frame.origin_y_mm, frame.reference_b_deg) == (1.0, 2.0, 3.0)


def test_ordinary_position_and_jog_controls_hide_only_c(main_window) -> None:
    assert set(main_window._stage_axis_fields) == {"X", "Y", "Z", "A", "B"}
    assert "C" not in main_window.joystick_panel.visible_axis_names()
    assert "C" in main_window.settings_manager.settings.axis_calibrations


def test_axis_calibration_change_invalidates_only_dependent_frame_axes(main_window) -> None:
    main_window.apply_axis_calibration_change_for_test("Z")
    frame = main_window.design_frame_for_test()
    assert frame.readiness["X"].available
    assert not frame.readiness["Z"].available
    assert not frame.readiness["A"].available


def test_custom_settings_are_materialized_in_registry(settings_dialog, main_window) -> None:
    settings_dialog.coordinate_system_tab.add_custom_frame()
    settings_dialog.apply_for_test()
    records = main_window.coordinate_registry.snapshot().records
    assert any(record.kind.value == "custom" for record in records)
```

- [ ] **Step 2: Run and confirm old WCO widget fails the new expectations**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui\test_settings_software_coordinates.py tests\ui\test_stage_position_panel.py -v
```

Expected: FAIL because custom CRUD does not exist and the position panel still
receives all backend axes.

- [ ] **Step 3: Replace Coordinates tab content and use visible-axis constant**

Build a list/detail editor with laconic Add, Duplicate, Rename, Delete actions
and numeric rows for Machine X/Y origin, reference B, XY angle, B zero, Z zero,
and A zero. Each row uses the axis unit. `Use current position` reads only the
cached physical pose callback; it never polls hardware. Disable Apply/Save when
the stage-idle callback is false or a draft contains invalid values.

The editor applies changes through `CustomFrameSettings.apply_geometry_edit()`
so upstream edits clear downstream references and every Z edit clears A. Do not
show the legacy G54-G59 controls, but leave their parser/serialized values
untouched.

On Settings Apply, materialize custom settings as versioned registry records.
Compare saved per-axis calibration fingerprints with each design record:
X/Y/B changes cascade through Z/A, Z changes cascade through A, and A changes
only A. Apply pivot changes only while the stage is idle and refresh transforms
without deleting registration marks.

Expose these concrete methods to `SettingsDialog` and tests:

```python
class CoordinateSystemSettingsWidget(QWidget):
    def __init__(
        self,
        settings: SoftwareCoordinateSettings,
        parent: QWidget | None = None,
        *,
        physical_pose_source: Callable[[], PhysicalMachinePose | None] | None = None,
        stage_idle_source: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings.clone()
        self._physical_pose_source = physical_pose_source
        self._stage_idle_source = stage_idle_source

    def settings(self) -> SoftwareCoordinateSettings:
        return self._settings.clone()

    def to_settings(self, settings: Settings) -> None:
        settings.software_coordinates = self.settings()
```

Construct the position panel and ordinary joystick/manual axis selectors with
`VISIBLE_STAGE_AXES`. Keep `Main.STAGE_AXIS_NAMES`, controller parsing,
`axis_calibrations`, precision approach data, and terminal behavior on all six
`STAGE_AXES`.

- [ ] **Step 4: Run Settings, panel, joystick, and full-suite tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui\test_settings_software_coordinates.py tests\ui\test_settings_objectives_coordinates.py tests\ui\test_stage_position_panel.py tests\stage\test_joystick_feedrate_targets.py -v
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

Expected: focused tests and complete suite PASS.

- [ ] **Step 5: Commit Settings UI and C visibility**

```powershell
git add probe_station_gui/dialogs/settings/coordinate_system.py probe_station_gui/dialogs/settings_dialog.py probe_station_gui/views probe_station_gui/settings/manager.py main.py tests/ui tests/stage/test_joystick_feedrate_targets.py
git commit -m "feat: configure custom frames and hide c controls"
```
