# B-Axis Point-Hold and Pivot Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an experimental B rotation that keeps the microscope view-center point under the crosshair with segmented X/Y compensation, then calibrate the real Machine XY rotation pivot from camera observations.

**Architecture:** A pure planner snapshots physical Machine pose, objective optical offset, universal calibration generation, assumed/calibrated pivot, limits, and pinned feedrate. It builds and preflights every physical and raw G53 X/Y/B segment before one operation lease executes them. A separate camera workflow recenters one feature at several B angles, fits its Machine-XY circle with `circle-fit` plus robust SciPy refinement, validates held-out samples, and saves only a user-confirmed machine pivot.

**Tech Stack:** Coordinate/motion foundation from Plans 1-3, NumPy, SciPy `least_squares`, MIT-licensed `circle-fit`, OpenCV ECC, existing objective pixel-to-mm calibration, `StageOperationLifecycle`, `StageMotionExecution`, PySide6, pytest.

## Global Constraints

- Complete the first three software-coordinate plans before this plan.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for Python and pytest.
- The initial pivot is physical Machine XY `(0, 0)` with provenance `assumed`.
- Universal B calibration exclusively owns angle scale/sign; pivot fitting must never modify it.
- Rotation is an explicit `Rotate around view center` operation; ordinary B jog/entry remains uncompensated.
- Snapshot the current microscope view center in physical Machine coordinates; selected GUI/API frame is irrelevant.
- Preflight every intermediate X/Y/B physical limit and calibration domain before the first serial write.
- One operation lease, cancellation token, and pinned feedrate cover the complete trajectory; the speed slider cannot restart it.
- On cancel/failure, stop and query actual MPos; never blindly return to the start.
- Known/partial physical B motion recomputes attached frames and does not delete registrations.
- Camera/file/fit work stays outside the GUI thread; Qt updates stay in the GUI thread.
- Calibration acquisition requires X/Y homing, trusted B, raised needles, active objective, and valid objective pixel-to-mm calibration.
- Manual pivot entry is advanced fallback with provenance `manual`; normal persisted values come from camera calibration.

---

## File Structure

- Modify `pyproject.toml`: add `circle-fit` direct dependency.
- Create `probe_station_gui/coordinates/point_hold.py`: segment geometry, sagitta sizing, immutable path and preflight errors.
- Create `probe_station_gui/stage/point_hold_rotation.py`: operation lifecycle/execution service and final-position recovery.
- Create `probe_station_gui/dialogs/point_hold_rotation_dialog.py`: experimental angle/speed UI.
- Modify `probe_station_gui/views/joystick_window.py`: concise launch/stop controls in the B area.
- Create `probe_station_gui/calibration/rotation_pivot.py`: observation model, Taubin initialization, robust fit, held-out validation.
- Create `probe_station_gui/calibration/rotation_pivot_workflow.py`: camera acquisition/recentering worker and result candidate.
- Create `probe_station_gui/dialogs/rotation_pivot_calibration_dialog.py`: prerequisites, progress, review/apply.
- Modify `probe_station_gui/settings/software_coordinates.py`: calibration acquisition settings and confirmed pivot metadata.
- Modify `main.py`: narrow service ownership, signal wiring, and shutdown.
- Add structured operation logs through existing logging configuration.

### Task 1: Point-Hold Arc Geometry and Segment Count

**Files:**
- Create: `probe_station_gui/coordinates/point_hold.py`
- Modify: `probe_station_gui/coordinates/__init__.py`
- Test: `tests/coordinates/test_point_hold.py`

**Interfaces:**
- Consumes: start physical Machine pose, view-center physical Machine XY, pivot, physical B target/delta, optical-axis/raw-stage conversion callbacks, universal mapper, physical limits, segment settings.
- Produces: `PointHoldRequest`, `PointHoldSegment`, `PointHoldPath`, `PointHoldPlanningError`, `plan_point_hold_path()`.

- [ ] **Step 1: Write failing geometry, segmentation, and whole-path tests**

```python
import math

import pytest

from probe_station_gui.coordinates.point_hold import (
    PointHoldPlanningError,
    PointHoldRequest,
    plan_point_hold_path,
)


def test_view_center_follows_rotation_about_machine_pivot(point_hold_context) -> None:
    path = plan_point_hold_path(
        PointHoldRequest(target_b_deg=90.0, feedrate=3.0),
        point_hold_context.with_start(x_mm=10.0, y_mm=0.0, b_deg=0.0),
    )
    final = path.segments[-1]
    assert final.physical_machine_targets == pytest.approx(
        {"X": 0.0, "Y": 10.0, "B": 90.0}, abs=1e-9
    )


def test_segment_count_honors_angle_step_and_sagitta(point_hold_context) -> None:
    context = point_hold_context.with_limits(
        max_segment_angle_deg=10.0,
        max_chord_error_mm=0.01,
    ).with_start(x_mm=100.0, y_mm=0.0, b_deg=0.0)
    path = plan_point_hold_path(PointHoldRequest(target_b_deg=30.0, feedrate=3.0), context)
    assert len(path.segments) >= 3
    step_rad = math.radians(path.segments[0].physical_machine_targets["B"])
    assert 100.0 * (1.0 - math.cos(step_rad / 2.0)) <= 0.01 + 1e-12


def test_invalid_intermediate_xy_rejects_entire_path_without_writes(point_hold_context) -> None:
    context = point_hold_context.with_machine_limits({"X": (-5.0, 5.0), "Y": (-5.0, 5.0)})
    with pytest.raises(PointHoldPlanningError) as raised:
        plan_point_hold_path(
            PointHoldRequest(target_b_deg=90.0, feedrate=3.0),
            context.with_start(x_mm=4.0, y_mm=0.0, b_deg=0.0),
        )
    assert raised.value.code == "target_out_of_bounds"
    assert context.serial_writes == []


def test_inverse_calibration_domain_is_checked_for_every_node(point_hold_context) -> None:
    with pytest.raises(PointHoldPlanningError) as raised:
        plan_point_hold_path(
            PointHoldRequest(target_b_deg=45.0, feedrate=3.0),
            point_hold_context.with_physical_x_domain((-1.0, 1.0)),
        )
    assert raised.value.code == "calibration_out_of_domain"
```

- [ ] **Step 2: Run and verify planner module is absent**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_point_hold.py -v
```

Expected: FAIL importing `probe_station_gui.coordinates.point_hold`.

- [ ] **Step 3: Implement node math and deterministic segment sizing**

Define:

```python
@dataclass(frozen=True)
class PointHoldRequest:
    target_b_deg: float
    feedrate: float


@dataclass(frozen=True)
class PointHoldSegment:
    index: int
    physical_machine_targets: Mapping[str, float]
    controller_machine_targets: Mapping[str, float]


@dataclass(frozen=True)
class PointHoldPath:
    start_physical_pose: PhysicalMachinePose
    held_point_machine_xy: tuple[float, float]
    pivot_machine_xy: tuple[float, float]
    pivot_source: str
    calibration_generation: int
    feedrate: float
    segments: tuple[PointHoldSegment, ...]
```

For radius `r`, chord-error limit `e`, and total angle magnitude `a`, calculate
`angle_nodes = ceil(a / max_segment_angle)` and, when `0 < e < r`,
`sagitta_nodes = ceil(a / (2*acos(1-e/r)))`; use the larger count and at least
one. For node delta `d`, rotate the captured view-center point as
`pivot + R(d) * (held - pivot)`. Convert optical-axis target back to physical
raw-stage X/Y with the captured objective adapter, inverse map X/Y/B, and check
all configured physical/controller domains and limits. Return no partial path
on any error.

- [ ] **Step 4: Run point-hold math tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_point_hold.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit point-hold planner**

```powershell
git add probe_station_gui/coordinates/point_hold.py probe_station_gui/coordinates/__init__.py tests/coordinates/test_point_hold.py
git commit -m "feat: plan b-axis point-hold trajectories"
```

### Task 2: Preflighted Lifecycle Execution and Actual-Position Recovery

**Files:**
- Create: `probe_station_gui/stage/point_hold_rotation.py`
- Modify: `probe_station_gui/stage/motion_execution.py`
- Modify: `probe_station_gui/stage/motion_commands.py:23-120`
- Modify: `probe_station_gui/stage/controller.py:200-380`
- Modify: `probe_station_gui/design/frame_registration.py`
- Modify: `main.py:5780-5865`
- Test: `tests/stage/test_point_hold_rotation.py`
- Modify test: `tests/stage/test_operation_lifecycle.py`
- Modify test: `tests/stage/test_motion_execution.py`
- Modify test: `tests/design/test_objective_alignment.py`

**Interfaces:**
- Consumes: fully planned `PointHoldPath`, `StageOperationLifecycle`, `StageMotionExecution`, status/stop callbacks.
- Produces: `PointHoldRotationService.start()`, `PointHoldRotationResult`, progress/finished callbacks.

- [ ] **Step 1: Write failing no-partial-preflight, pinned-speed, and cancel tests**

```python
from probe_station_gui.stage.point_hold_rotation import PointHoldRotationResult


def test_service_validates_all_segments_before_first_write(rotation_service, path) -> None:
    rotation_service.execution.reject_validation_at_index = 2
    result = rotation_service.run_for_test(path)
    assert not result.success
    assert rotation_service.serial.writes == []


def test_one_lease_and_one_pinned_feedrate_cover_all_segments(rotation_service, path) -> None:
    result = rotation_service.run_for_test(path)
    assert result.success
    assert rotation_service.lease_count == 1
    assert {plan.feedrate for plan in rotation_service.executed_plans} == {path.feedrate}


def test_cancel_stops_and_reports_actual_pose_without_blind_return(rotation_service, path) -> None:
    rotation_service.cancel_after_segment = 1
    rotation_service.actual_controller_pose = {"X": 1.5, "Y": 2.5, "B": 3.5}
    result = rotation_service.run_for_test(path)
    assert result.cancelled
    assert rotation_service.stop_count >= 1
    assert result.actual_controller_pose == {"X": 1.5, "Y": 2.5, "B": 3.5}
    assert rotation_service.return_to_start_count == 0


def test_missing_final_status_blocks_b_authority_but_keeps_frame_transform(rotation_service, path) -> None:
    original = rotation_service.design_frame.transform
    rotation_service.final_status = None
    result = rotation_service.run_for_test(path)
    assert not result.position_confirmed
    assert rotation_service.design_frame.transform == original
    assert not rotation_service.design_frame.readiness["B"].available


def test_optional_design_alignment_commits_only_after_complete_point_hold(alignment_controller) -> None:
    alignment_controller.point_hold_result = PointHoldRotationResult(
        success=False,
        cancelled=True,
        position_confirmed=True,
        completed_segments=1,
        actual_controller_pose={"X": 1.0, "Y": 2.0, "B": 3.0},
        message="Cancelled.",
    )
    alignment_controller.run_optional_alignment()
    assert alignment_controller.registration_commit_count == 0
```

- [ ] **Step 2: Run and verify service is absent**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_point_hold_rotation.py -v
```

Expected: FAIL importing the service.

- [ ] **Step 3: Implement one background operation with a preflight pass**

Define:

```python
@dataclass(frozen=True)
class PointHoldRotationResult:
    success: bool
    cancelled: bool
    position_confirmed: bool
    completed_segments: int
    actual_controller_pose: Mapping[str, float] | None
    message: str
```

`start(path)` calls `StageOperationLifecycle.start_background("Rotate around view center", run)`.
Inside `run`, first call a side-effect-free validation method for every
`AbsoluteMotionPlan(targets=segment.controller_machine_targets,
feedrate=path.feedrate, machine_position_mode=True, wait_for_completion=True)`.
Only after all validate, execute in order with cancellation checks between
segments. Emit progress after each completed segment outside locks.

On cancel/error, invoke the existing realtime stop once plus its normal resend,
then read actual Machine status and map it through the universal calibration.
Publish actual pose when confirmed; otherwise block B authority while retaining
frame records. Do not generate a return path. Rotation requests are rejected
when needles are not known raised or X/Y homing/B authority is missing.

Replace the currently B-only optional design-alignment execution with this
service. Keep alignment opt-in. Commit the prepared X/Y/B registration only
after `PointHoldRotationResult.success` and confirmed final pose; cancel/error
leaves the registration draft uncommitted.

- [ ] **Step 4: Run service/lifecycle/execution tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_point_hold_rotation.py tests\stage\test_operation_lifecycle.py tests\stage\test_motion_execution.py tests\design\test_objective_alignment.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit lifecycle execution**

```powershell
git add probe_station_gui/stage probe_station_gui/design/frame_registration.py main.py tests/stage/test_point_hold_rotation.py tests/stage/test_operation_lifecycle.py tests/stage/test_motion_execution.py tests/design/test_objective_alignment.py
git commit -m "feat: execute point-hold rotation as one operation"
```

### Task 3: Experimental Rotation GUI and Logging

**Files:**
- Create: `probe_station_gui/dialogs/point_hold_rotation_dialog.py`
- Modify: `probe_station_gui/views/joystick_window.py:560-730`
- Modify: `probe_station_gui/views/main_window_docks.py:280-370`
- Modify: `main.py:760-950,1070-1140`
- Test: `tests/ui/test_point_hold_rotation_dialog.py`
- Modify test: `tests/stage/test_joystick_feedrate_targets.py`

**Interfaces:**
- Consumes: current physical pose, pivot source, feasible B/XY limits, shared speed as initial value, service start/cancel/progress/result.
- Produces: dedicated `Rotate around view center` action/dialog; pinned request; structured logs.

- [ ] **Step 1: Write failing dedicated-mode and slider-isolation tests**

```python
def test_dialog_identifies_assumed_pivot_and_builds_pinned_request(qtbot, dialog) -> None:
    assert "Assumed pivot: X 0 mm, Y 0 mm" in dialog.pivot_text()
    dialog.set_target_b_deg(5.0)
    dialog.set_feedrate(3.0)
    request = dialog.request()
    assert request.target_b_deg == 5.0
    assert request.feedrate == 3.0


def test_ordinary_b_jog_remains_uncompensated(joystick) -> None:
    joystick.start_jog("B", 1)
    assert joystick.last_jog_axes_for_test() == ("B",)


def test_speed_slider_does_not_restart_active_point_hold(main_window) -> None:
    main_window.start_point_hold_for_test(target_b_deg=5.0, feedrate=3.0)
    main_window.set_linear_feedrate_for_test(30.0)
    assert main_window.point_hold_restart_count_for_test() == 0
    assert main_window.point_hold_active_feedrate_for_test() == 3.0
```

- [ ] **Step 2: Run and observe missing dialog/action**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui\test_point_hold_rotation_dialog.py tests\stage\test_joystick_feedrate_targets.py -v
```

Expected: FAIL importing the dialog or finding the dedicated control.

- [ ] **Step 3: Implement concise experimental UI and audit logging**

Add one `Rotate around view center` control beside the B controls. It opens a
non-blocking dialog with target/delta B choice, angle value, pinned feedrate,
pivot source/coordinates, calculated segment count, feasible range, Start, and
Stop. Disable Start until the complete path preview succeeds. Do not add
ellipses to the menu/button label and do not overload ordinary B controls.

At start, log one structured summary containing operation ID, physical start,
held point, pivot/provenance, objective offset, calibration generation,
feedrate, and all planned physical/raw nodes at debug level. Log actual pose and
image-stability residual per checkpoint when available, plus final/cancel/error
result. Slider callbacks explicitly ignore the active pinned point-hold service.

```python
class PointHoldRotationDialog(QDialog):
    start_requested = Signal(object)
    stop_requested = Signal()

    def request(self) -> PointHoldRequest:
        return PointHoldRequest(
            target_b_deg=float(self._target_b_spin.value()),
            feedrate=float(self._feedrate_spin.value()),
        )

    def set_path_preview(self, path: PointHoldPath | None, message: str = "") -> None:
        self._start_button.setEnabled(path is not None)
        count = "" if path is None else str(len(path.segments))
        self._segment_count_label.setText(count)
        self._status_label.setText(str(message))
```

- [ ] **Step 4: Run dialog/joystick/app regressions**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\ui\test_point_hold_rotation_dialog.py tests\stage\test_joystick_feedrate_targets.py tests\app\test_main_coordinate_feedrate.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit experimental UI**

```powershell
git add probe_station_gui/dialogs/point_hold_rotation_dialog.py probe_station_gui/views/joystick_window.py probe_station_gui/views/main_window_docks.py main.py tests/ui/test_point_hold_rotation_dialog.py tests/stage/test_joystick_feedrate_targets.py tests/app/test_main_coordinate_feedrate.py
git commit -m "feat: add experimental point-hold rotation ui"
```

### Task 4: Robust Camera Pivot Fit

**Files:**
- Modify: `pyproject.toml`
- Create: `probe_station_gui/calibration/__init__.py`
- Create: `probe_station_gui/calibration/rotation_pivot.py`
- Test: `tests/calibration/test_rotation_pivot.py`

**Interfaces:**
- Consumes: finite `PivotObservation` values in calibrated physical Machine XY/B and explicit validation indices.
- Produces: `PivotObservation`, `RotationPivotFit`, `fit_rotation_pivot()` using `circle_fit.taubinSVD` and `scipy.optimize.least_squares`.

- [ ] **Step 1: Add dependency and write failing synthetic robust-fit tests**

Add `"circle-fit",` to `[project].dependencies`, then add:

```python
import math

import pytest

from probe_station_gui.calibration.rotation_pivot import (
    PivotObservation,
    fit_rotation_pivot,
)


def _observation(angle_deg: float, *, noise_x: float = 0.0, noise_y: float = 0.0) -> PivotObservation:
    angle = math.radians(angle_deg)
    return PivotObservation(
        physical_b_deg=angle_deg,
        feature_machine_xy=(
            2.0 + 10.0 * math.cos(angle) + noise_x,
            -3.0 + 10.0 * math.sin(angle) + noise_y,
        ),
    )


def test_fit_recovers_pivot_with_noise_and_one_outlier() -> None:
    observations = [_observation(angle) for angle in (-30, -15, 0, 15, 30, 45)]
    observations[2] = _observation(0, noise_x=1.5, noise_y=-1.0)
    fit = fit_rotation_pivot(tuple(observations), validation_indices=(1, 5))
    assert fit.pivot_machine_xy == pytest.approx((2.0, -3.0), abs=0.2)
    assert fit.radius_mm == pytest.approx(10.0, abs=0.2)
    assert fit.fit_method == "taubin+least_squares_soft_l1"


def test_fit_reports_held_out_errors_separately() -> None:
    fit = fit_rotation_pivot(
        tuple(_observation(angle) for angle in (-40, -20, 0, 20, 40, 60)),
        validation_indices=(0, 5),
    )
    assert fit.fit_count == 4
    assert fit.validation_count == 2
    assert fit.validation_max_error_mm < 1e-9


def test_fit_does_not_return_b_scale_or_sign_adjustment() -> None:
    fit = fit_rotation_pivot(
        tuple(_observation(angle) for angle in (-45, -20, 0, 20, 45)),
        validation_indices=(0,),
    )
    assert not hasattr(fit, "b_scale")
    assert not hasattr(fit, "b_sign")
```

- [ ] **Step 2: Install project dependency and run failing test**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pip install -e .
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\calibration\test_rotation_pivot.py -v
```

Expected: editable install succeeds; test FAILS importing the new module.

- [ ] **Step 3: Implement Taubin initialization and robust radial refinement**

Define:

```python
@dataclass(frozen=True)
class PivotObservation:
    physical_b_deg: float
    feature_machine_xy: tuple[float, float]


@dataclass(frozen=True)
class RotationPivotFit:
    pivot_machine_xy: tuple[float, float]
    radius_mm: float
    fit_rms_error_mm: float
    fit_max_error_mm: float
    validation_rms_error_mm: float
    validation_max_error_mm: float
    fit_count: int
    validation_count: int
    sampled_b_range_deg: tuple[float, float]
    fit_method: str = "taubin+least_squares_soft_l1"
```

Validate at least three non-collinear fit observations and at least one held-out
observation. Call `circle_fit.taubinSVD(fit_xy)` for `(xc, yc, radius, sigma)`.
Refine `(xc, yc, radius)` with `least_squares` radial residuals and
`loss="soft_l1"`; require positive finite radius. Compute fit and held-out
metrics independently. Compare observed angular ordering only for diagnostics;
never return or apply a B scale/sign correction.

- [ ] **Step 4: Run fit and complete non-hardware tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\calibration\test_rotation_pivot.py tests\coordinates\test_point_hold.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit calibration math and dependency**

```powershell
git add pyproject.toml probe_station_gui/calibration tests/calibration/test_rotation_pivot.py
git commit -m "feat: fit b-axis pivot from camera observations"
```

### Task 5: Camera Acquisition, Review, and Confirmed Pivot Persistence

**Files:**
- Create: `probe_station_gui/calibration/rotation_pivot_workflow.py`
- Create: `probe_station_gui/dialogs/rotation_pivot_calibration_dialog.py`
- Modify: `probe_station_gui/settings/software_coordinates.py`
- Modify: `probe_station_gui/dialogs/settings/coordinate_system.py`
- Modify: `main.py:760-950,1070-1140`
- Test: `tests/calibration/test_rotation_pivot_workflow.py`
- Test: `tests/ui/test_rotation_pivot_calibration_dialog.py`
- Modify test: `tests/settings/test_software_coordinates.py`
- Modify test: `tests/camera/test_optical_calibration_runtime.py`

**Interfaces:**
- Consumes: camera frames, OpenCV registration, objective pixel-to-mm transform, stage point-hold/B movement service, physical status, fit service, user decision.
- Produces: `RotationPivotCalibrationRequest`, `RotationPivotCalibrationCandidate`, cancellable worker, review dialog, confirmed `RotationPivotSettings(source="camera_calibrated")`.

- [ ] **Step 1: Write failing workflow, cancellation, and confirmation tests**

```python
def test_workflow_requires_homing_b_authority_raised_needles_and_optical_calibration(workflow) -> None:
    result = workflow.check_prerequisites(
        homed_axes={"X"},
        authority_axes={"X", "Y", "B"},
        needles_raised=True,
        objective_calibrated=True,
    )
    assert result.code == "xy_homing_required"


def test_successful_acquisition_returns_candidate_without_saving(workflow) -> None:
    candidate = workflow.run_for_test()
    assert candidate.fit.pivot_machine_xy == (2.0, -3.0)
    assert workflow.settings_save_count == 0


def test_user_accept_saves_camera_provenance_and_diagnostics(dialog, candidate) -> None:
    dialog.set_candidate(candidate)
    settings = dialog.accept_candidate_for_test()
    assert settings.source == "camera_calibrated"
    assert settings.x_mm == 2.0
    assert settings.y_mm == -3.0
    assert settings.rms_error_mm == candidate.fit.validation_rms_error_mm


def test_cancel_restores_known_safe_pose_or_reports_actual_pose(workflow) -> None:
    workflow.cancel_after_observation = 2
    result = workflow.run_for_test()
    assert result.cancelled
    assert result.position_confirmed
    assert result.actual_physical_pose is not None
```

- [ ] **Step 2: Run and verify workflow/dialog are absent**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\calibration\test_rotation_pivot_workflow.py tests\ui\test_rotation_pivot_calibration_dialog.py -v
```

Expected: FAIL importing the workflow/dialog.

- [ ] **Step 3: Implement cancellable camera sampling and explicit review**

Use request/candidate shapes:

```python
@dataclass(frozen=True)
class RotationPivotCalibrationRequest:
    physical_b_angles_deg: tuple[float, ...]
    validation_indices: tuple[int, ...]
    objective_name: str
    feature_roi: tuple[int, int, int, int]


@dataclass(frozen=True)
class RotationPivotCalibrationCandidate:
    fit: RotationPivotFit
    observations: tuple[PivotObservation, ...]
    objective_name: str
    started_at_utc: str
```

The dialog lets the operator choose a safe physical B span and odd sample
count, then center/confirm a high-contrast ROI. The worker captures the start
frame, moves to each requested B under one calibration operation, predicts the
feature location from the current pivot, and uses OpenCV ECC translation to
reacquire/recenter it. Convert pixel displacement through the existing active
objective calibration and record the physical Machine XY at which the feature
is centered. Reject low ECC confidence without publishing partial calibration.

Reserve deterministic end samples for validation, fit in the worker, and
return a candidate. The review dialog shows pivot, sampled range, fit RMS/max,
held-out RMS/max, objective, and observation plot. Only Apply writes
`RotationPivotSettings` with incremented version and source
`camera_calibrated`; Cancel leaves settings unchanged. Advanced manual edits set
source `manual`.

On cancellation or error, use a separately planned/preflighted path toward the
starting pose only when it is known safe. If that cannot be proven, stop at and
report confirmed actual pose. Never claim a return that status did not confirm.

- [ ] **Step 4: Run calibration/settings/camera tests and full suite**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\calibration tests\ui\test_rotation_pivot_calibration_dialog.py tests\settings\test_software_coordinates.py tests\camera\test_optical_calibration_runtime.py -v
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

Expected: focused and complete suites PASS without opening hardware.

- [ ] **Step 5: Commit calibration workflow and document hardware acceptance**

```powershell
git add probe_station_gui/calibration probe_station_gui/dialogs/rotation_pivot_calibration_dialog.py probe_station_gui/settings/software_coordinates.py probe_station_gui/dialogs/settings/coordinate_system.py main.py tests/calibration tests/ui/test_rotation_pivot_calibration_dialog.py tests/settings/test_software_coordinates.py tests/camera/test_optical_calibration_runtime.py
git commit -m "feat: calibrate and review b-axis rotation pivot"
```

After the commit, execute the seven-step hardware acceptance checklist from
`docs/superpowers/specs/2026-07-22-software-coordinate-systems-design.md` with
the operator. Do not mark the experimental mode hardware-validated until its
planned/actual logs and image stability have been reviewed.
