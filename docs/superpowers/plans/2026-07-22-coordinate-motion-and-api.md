# Coordinate Motion and API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve GUI, Design Window, WASD, Step, and API movement from explicit software frames into immutable calibrated Machine-coordinate plans without changing GUI selection or active targets mid-motion.

**Architecture:** Introduce a pure `MotionIntent -> ResolvedMotionPlan` resolver above `StageMotionExecution`. It snapshots frame/version/calibration/current physical pose/feedrate ownership, applies readiness and transforms, then inverse-calibrates raw Machine targets. Execution remains unaware of software frames and sends exact targets with G53; the GUI and API each provide their own frame context.

**Tech Stack:** Coordinate core and UI from Plans 1-2, existing `StageAxisCalibrationMapper`, `StageMotionExecution`, FluidNC G53/jog syntax, FastAPI/request bridge, PySide6 jog controls, pytest.

## Global Constraints

- Complete the core/persistence and design/UI plans first.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for Python and pytest.
- Exact target pipeline is frame -> calibrated physical Machine -> inverse universal calibration -> raw Machine -> G53.
- Never resolve a missing frame axis through a hidden Machine fallback.
- Mixed-axis intents are all-or-nothing; no serial write occurs after a failed component.
- X/Y relative movement is forbidden until the selected frame basis is known; physical relative B/Z/A remains allowed under the approved readiness table.
- Changing GUI selection never mutates an active plan; changing while WASD is held stops jog and requires a new press.
- GUI and API contexts are independent; API defaults to Machine and never selects a GUI frame.
- API omitted feedrate follows shared GUI/default speed and may be reissued; explicit API feedrate is pinned.
- Segmented/owned operations are pinned and never restarted by the speed slider.
- Do not change manual terminal or API route control semantics.

---

## File Structure

- Create `probe_station_gui/coordinates/motion.py`: intent, feedrate policy, failures, immutable resolved plan.
- Create `probe_station_gui/coordinates/motion_resolver.py`: readiness, frame transform, calibration inversion, atomic resolution.
- Create `probe_station_gui/coordinates/jog.py`: frozen-basis keyboard direction planning.
- Modify `probe_station_gui/stage/motion_command_planning.py`: exact G53 G1 builder.
- Modify `probe_station_gui/stage/motion_execution.py`: honor Machine mode for non-jog absolute plans.
- Modify `probe_station_gui/stage/coordinate_targets.py`: hold resolved-plan snapshots, not display/raw pairs.
- Modify `probe_station_gui/stage/api_moves.py`: explicit frame and feedrate policy.
- Modify `probe_station_gui/api/command_dispatch.py`, `request_bridge.py`, and `server.py`: pass frame ID and expose list/status.
- Modify `probe_station_gui/views/joystick_window.py`: emit logical directions and stop on frame-generation change.
- Modify `probe_station_gui/views/main_window_stage_position_panel.py` and `main.py`: resolver ownership and GUI intent submission.
- Remove B-motion-driven registration invalidation from position/update and coordinate move paths while retaining real authority invalidation.

### Task 1: Pure Motion Intent and Atomic Resolver

**Files:**
- Create: `probe_station_gui/coordinates/motion.py`
- Create: `probe_station_gui/coordinates/motion_resolver.py`
- Modify: `probe_station_gui/coordinates/__init__.py`
- Test: `tests/coordinates/test_motion_resolver.py`

**Interfaces:**
- Consumes: registry snapshot, selected frame ID/version, current `PhysicalMachinePose`, pivot, authority/homing, `StageAxisCalibrationMapper`, physical limits.
- Produces: `MotionIntent`, `MotionMode`, `FeedratePolicy`, `ResolvedMotionPlan`, `MotionResolutionError`, `resolve_motion_intent()`.

- [ ] **Step 1: Write failing eligibility, transform, and snapshot tests**

```python
import pytest

from probe_station_gui.coordinates.motion import (
    FeedratePolicy,
    MotionIntent,
    MotionMode,
    MotionResolutionError,
)
from probe_station_gui.coordinates.motion_resolver import resolve_motion_intent


def test_absolute_design_target_resolves_to_raw_machine_through_inverse_curve(resolver_context) -> None:
    plan = resolve_motion_intent(
        MotionIntent(
            source="gui",
            frame_id="rotated-design",
            mode=MotionMode.ABSOLUTE,
            coordinates={"X": 1.0, "Y": 0.0},
            feedrate_policy=FeedratePolicy.SHARED,
        ),
        resolver_context,
    )
    assert plan.physical_machine_targets == pytest.approx({"X": 10.0, "Y": 6.0})
    assert plan.controller_machine_targets == pytest.approx({"X": 5.0, "Y": 3.0})
    assert plan.frame_version == resolver_context.registry.require("rotated-design").version


@pytest.mark.parametrize("axis", ["Z", "A", "B"])
def test_relative_physical_axis_is_allowed_before_frame_origin(axis, incomplete_context) -> None:
    plan = resolve_motion_intent(
        MotionIntent(
            source="gui",
            frame_id="incomplete-design",
            mode=MotionMode.RELATIVE,
            coordinates={axis: 0.1},
            feedrate_policy=FeedratePolicy.SHARED,
        ),
        incomplete_context,
    )
    assert axis in plan.physical_machine_targets


def test_relative_xy_is_rejected_before_basis_is_registered(incomplete_context) -> None:
    with pytest.raises(MotionResolutionError) as raised:
        resolve_motion_intent(
            MotionIntent(
                source="gui",
                frame_id="incomplete-design",
                mode=MotionMode.RELATIVE,
                coordinates={"X": 1.0},
                feedrate_policy=FeedratePolicy.SHARED,
            ),
            incomplete_context,
        )
    assert raised.value.code == "axis_origin_unregistered"


def test_one_invalid_mixed_component_rejects_whole_intent(incomplete_context) -> None:
    with pytest.raises(MotionResolutionError):
        resolve_motion_intent(
            MotionIntent(
                source="api",
                frame_id="incomplete-design",
                mode=MotionMode.ABSOLUTE,
                coordinates={"Z": 1.0, "A": 2.0},
                feedrate_policy=FeedratePolicy.PINNED,
                feedrate=3.0,
            ),
            incomplete_context,
        )
    assert incomplete_context.serial_writes == []


def test_b_attached_frame_rejects_ordinary_simultaneous_xy_b(resolver_context) -> None:
    with pytest.raises(MotionResolutionError) as raised:
        resolve_motion_intent(
            MotionIntent(
                source="gui",
                frame_id="rotated-design",
                mode=MotionMode.ABSOLUTE,
                coordinates={"X": 1.0, "B": 2.0},
                feedrate_policy=FeedratePolicy.SHARED,
            ),
            resolver_context,
        )
    assert raised.value.code == "coordinated_rotation_required"
```

- [ ] **Step 2: Run and verify resolver modules are absent**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_motion_resolver.py -v
```

Expected: FAIL importing `probe_station_gui.coordinates.motion`.

- [ ] **Step 3: Implement immutable intent/result and complete eligibility table**

Use these exact shapes:

```python
class MotionMode(str, Enum):
    ABSOLUTE = "absolute"
    RELATIVE = "relative"


class FeedratePolicy(str, Enum):
    SHARED = "shared"
    PINNED = "pinned"


@dataclass(frozen=True)
class MotionIntent:
    source: str
    frame_id: str
    mode: MotionMode
    coordinates: Mapping[str, float]
    feedrate_policy: FeedratePolicy
    feedrate: float | None = None


@dataclass(frozen=True)
class ResolvedMotionPlan:
    source: str
    frame_id: str
    frame_version: int
    calibration_generation: int
    mode: MotionMode
    original_coordinates: Mapping[str, float]
    physical_machine_targets: Mapping[str, float]
    controller_machine_targets: Mapping[str, float]
    feedrate_policy: FeedratePolicy
    feedrate: float


class MotionResolutionError(ValueError):
    def __init__(self, code: str, message: str, *, frame_id: str, axis: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.frame_id = frame_id
        self.axis = axis
```

Create an immutable `MotionResolverContext` containing snapshot/pose/pivot,
homed and authority axes, mapper, calibration generation, physical limits, and
shared feedrate. Validate the entire intent first. For partial absolute XY,
obtain the unrequested frame component from the current physical pose before
inverse transform. For relative X/Y, rotate the vector by the captured frame
basis. For relative Z/A/B, add the physical delta to current Machine pose even
when the frame origin is missing. Convert all endpoint physical targets through
`physical_to_controller()` only after readiness and physical-limit checks.
Reject every ordinary design/custom intent containing both XY and B; the
point-hold planner is the only path allowed to evaluate a continuously rotating
basis. Machine-frame mixed XY+B retains its existing behavior.

- [ ] **Step 4: Run resolver tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_motion_resolver.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the resolver**

```powershell
git add probe_station_gui/coordinates tests/coordinates/test_motion_resolver.py
git commit -m "feat: resolve software frame motion intents"
```

### Task 2: G53 Execution and Coordinate-Field Integration

**Files:**
- Modify: `probe_station_gui/stage/motion_command_planning.py`
- Modify: `probe_station_gui/stage/motion_execution.py:34-60,285-390`
- Modify: `probe_station_gui/stage/coordinate_targets.py`
- Modify: `probe_station_gui/stage/move_lifecycle.py`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py:57-100`
- Modify: `probe_station_gui/design/navigation_adapter.py:752-807`
- Modify: `main.py:9040-9205`
- Test: `tests/stage/test_motion_command_planning.py`
- Modify test: `tests/stage/test_motion_execution.py`
- Modify test: `tests/stage/test_coordinate_targets.py`
- Modify test: `tests/app/test_main_stage_coordinate_controls.py`

**Interfaces:**
- Consumes: `ResolvedMotionPlan.controller_machine_targets` and pinned/shared feedrate.
- Produces: `machine_absolute_axis_g1_command()`, execution of `AbsoluteMotionPlan(machine_position_mode=True)`, resolved-plan tracking in coordinate fields.

- [ ] **Step 1: Write failing G53 and immutable-target tests**

```python
def test_machine_absolute_g1_command_uses_g53() -> None:
    assert machine_absolute_axis_g1_command(
        {"X": 1.0, "Y": 2.0}, 30.0
    ) == "G53 G1 X1 Y2 F30"


def test_non_jog_machine_absolute_plan_writes_g53(execution, serial) -> None:
    execution.run_absolute(
        AbsoluteMotionPlan(
            targets={"X": 1.0},
            feedrate=3.0,
            machine_position_mode=True,
        )
    )
    assert "G53 G1 X1 F3" in serial.writes


def test_gui_frame_switch_does_not_recompute_active_coordinate_target(coordinate_target_state, resolved_plan) -> None:
    coordinate_target_state.start(resolved_plan)
    coordinate_target_state.set_display_frame("another-frame")
    assert coordinate_target_state.active_plan is resolved_plan
    assert coordinate_target_state.active_plan.controller_machine_targets == resolved_plan.controller_machine_targets
```

- [ ] **Step 2: Run and observe missing G53 behavior**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_motion_command_planning.py tests\stage\test_motion_execution.py tests\stage\test_coordinate_targets.py -v
```

Expected: the new G53 assertion FAILS because non-jog absolute execution
currently writes ordinary `G1`.

- [ ] **Step 3: Implement G53 builder and replace display/raw target planning**

Add:

```python
def machine_absolute_axis_g1_command(
    targets: Mapping[str, float],
    feedrate: float,
    *,
    axis_order: Sequence[str] = DEFAULT_AXIS_ORDER,
) -> str:
    parts = ordered_absolute_axis_targets(targets, axis_order=axis_order)
    words = " ".join(f"{axis}{format_gcode_value(value)}" for axis, value in parts.items())
    return f"G53 G1 {words} F{format_gcode_value(feedrate)}"
```

When `AbsoluteMotionPlan.machine_position_mode` is true and `as_jog` is false,
`StageMotionExecution.run_absolute()` writes this builder after `G21`/`G90`.
Keep the existing `$J=G90 G21 G53` builder for absolute jog.

Change coordinate field Apply/Enter to create `MotionIntent` with the selected
GUI frame and submit its resolved snapshot. Store original frame/display target
only for UI, and raw/physical targets only from the resolver. Feedrate reissue
may create a new execution request from the stored raw endpoint, but cannot
resolve against the current selector again.

Design Window move creates the same intent with
`frame_id=DesignSession.active_frame_id`, because its clicked point belongs to
that design instance regardless of the GUI display selector. Reject the click
if that frame/version is unavailable instead of falling back to GUI or Machine.

- [ ] **Step 4: Run execution and real main-coordinate tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_motion_command_planning.py tests\stage\test_motion_execution.py tests\stage\test_coordinate_targets.py tests\app\test_main_stage_coordinate_controls.py tests\app\test_main_coordinate_feedrate.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit G53 field movement**

```powershell
git add probe_station_gui/stage probe_station_gui/views/main_window_stage_position_panel.py probe_station_gui/design/navigation_adapter.py main.py tests/stage tests/app/test_main_stage_coordinate_controls.py tests/app/test_main_coordinate_feedrate.py tests/design/test_click_navigation.py
git commit -m "feat: execute resolved coordinate targets with g53"
```

### Task 3: Frame-Relative WASD and Selection-Change Stop

**Files:**
- Create: `probe_station_gui/coordinates/jog.py`
- Modify: `probe_station_gui/stage/exact_step.py`
- Modify: `probe_station_gui/stage/manual_jog_prediction.py`
- Modify: `probe_station_gui/views/joystick_window.py:836-1075,1586-1625,1870-2020`
- Modify: `probe_station_gui/views/main_window_docks.py:280-370`
- Modify: `main.py:1080-1140`
- Test: `tests/coordinates/test_jog.py`
- Modify test: `tests/stage/test_exact_step.py`
- Modify test: `tests/stage/test_manual_jog_prediction.py`
- Modify test: `tests/stage/test_controller_jog_motion.py`
- Modify test: `tests/ui/test_stage_position_panel.py`

**Interfaces:**
- Consumes: selected GUI frame snapshot, current physical pose, pressed direction keys, current shared feedrate.
- Produces: `FrameJogSnapshot`, `plan_frame_jog()`, `JoystickWindow.stop_for_coordinate_frame_change()`.

- [ ] **Step 1: Write failing rotated-WASD and stop tests**

```python
import pytest

from probe_station_gui.coordinates.jog import plan_frame_jog


def test_w_in_ninety_degree_frame_moves_machine_negative_x(ready_rotated_context) -> None:
    jog = plan_frame_jog(
        frame_id="rotated-design",
        direction_xy=(0.0, 1.0),
        context=ready_rotated_context,
    )
    assert jog.machine_direction_xy == pytest.approx((-1.0, 0.0))


def test_switching_frame_while_key_held_stops_and_requires_new_press(joystick, qtbot) -> None:
    joystick.press_direction_for_test("W")
    assert joystick.active_jog_axes()
    joystick.stop_for_coordinate_frame_change("other-frame")
    assert joystick.active_jog_axes() == ()
    assert joystick.jog_stop_write_count_for_test() >= 1
    joystick.sync_held_keys_for_test()
    assert joystick.active_jog_axes() == ()


def test_rotating_frame_rejects_ordinary_simultaneous_xy_b(ready_rotated_context) -> None:
    with pytest.raises(ValueError, match="Rotate around view center"):
        plan_frame_jog(
            frame_id="rotated-design",
            direction_xy=(1.0, 0.0),
            b_direction=1,
            context=ready_rotated_context,
        )


def test_step_accumulates_exact_targets_in_selected_frame(step_controller) -> None:
    step_controller.select_frame("rotated-design")
    step_controller.enqueue_frame_step("X", 0.001)
    step_controller.enqueue_frame_step("X", 0.001)
    plan = step_controller.flush_for_test()
    assert plan.original_coordinates["X"] == pytest.approx(0.002)
    assert plan.mode.value == "relative"
```

- [ ] **Step 2: Run and verify frame jog planner is missing**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_jog.py tests\stage\test_controller_jog_motion.py -v
```

Expected: FAIL importing `probe_station_gui.coordinates.jog`.

- [ ] **Step 3: Implement frozen-basis jog and wire selector generation**

Define:

```python
@dataclass(frozen=True)
class FrameJogSnapshot:
    frame_id: str
    frame_version: int
    machine_direction_xy: tuple[float, float]
    controller_endpoint: Mapping[str, float]
```

`plan_frame_jog()` normalizes the frame XY direction, rotates it once at press
time, chooses an endpoint clipped by physical limits and enabled calibration
domains, inverse maps that endpoint, and returns an absolute `$J=G90 G21 G53`
target for non-Machine rotated XY jog. Keep the existing Machine mixed-axis jog
path. Reject ordinary XY+B in B-attached frames.

Connect `StagePositionPanel.coordinate_system_changed` to
`JoystickWindow.stop_for_coordinate_frame_change()` before updating the active
GUI frame. That method sends the existing 0x85 stop plus scheduled resend,
clears active/pending axes and key stack, and records the new generation so
physically held keys cannot restart until release and a fresh press.

Route Step mode through a relative `MotionIntent` in the captured GUI frame.
Keep the existing exact-step debounce/accumulation, but accumulate frame-space
physical values and resolve one calibrated Machine endpoint at flush time.
Changing frame stops/flush-cancels the old generation; no queued step crosses
into the new frame.

- [ ] **Step 4: Run jog and keyboard regression tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_jog.py tests\stage\test_exact_step.py tests\stage\test_manual_jog_prediction.py tests\stage\test_controller_jog_motion.py tests\ui\test_stage_position_panel.py tests\stage\test_joystick_feedrate_targets.py -v
```

Expected: all tests PASS, including stop resend behavior.

- [ ] **Step 5: Commit frame-relative jog**

```powershell
git add probe_station_gui/coordinates/jog.py probe_station_gui/stage/exact_step.py probe_station_gui/stage/manual_jog_prediction.py probe_station_gui/views/joystick_window.py probe_station_gui/views/main_window_docks.py main.py tests/coordinates/test_jog.py tests/stage tests/ui/test_stage_position_panel.py
git commit -m "feat: jog relative to selected software frame"
```

### Task 4: Independent API Frame Context and Read-Only Frame Endpoint

**Files:**
- Modify: `probe_station_gui/stage/api_moves.py`
- Modify: `probe_station_gui/api/command_dispatch.py:33-120,202-230`
- Modify: `probe_station_gui/api/request_bridge.py`
- Modify: `probe_station_gui/api/server.py:27-66,520-585`
- Modify: `main.py:1830-1945`
- Modify: `probe_station_client/client.py:590-625`
- Test: `tests/api/test_coordinate_systems.py`
- Modify test: `tests/api/test_server.py`
- Modify test: `tests/api/test_command_dispatch.py`
- Modify test: `tests/stage/test_api_moves.py`

**Interfaces:**
- Consumes: optional request `coordinate_system`, optional feedrate, Machine-default API context, registry snapshot and physical pose.
- Produces: `GET /api/v1/coordinate-systems`, frame-aware move payload/response, structured resolver errors.

- [ ] **Step 1: Write failing API separation and metadata tests**

```python
def test_api_move_defaults_to_machine_without_changing_gui_selection(api_client, main_window) -> None:
    main_window.select_coordinate_frame_for_test("design-a")
    response = api_client.post("/api/v1/stage/move", json={"coordinates": {"X": 1.0}})
    assert response.status_code == 200
    assert response.json()["coordinate_system"] == "machine"
    assert main_window.selected_coordinate_frame_id() == "design-a"


def test_api_explicit_frame_echoes_resolution(api_client) -> None:
    response = api_client.post(
        "/api/v1/stage/move",
        json={
            "coordinate_system": "design-a",
            "coordinates": {"X": 1.0, "Y": 2.0},
            "feedrate": 3.0,
        },
    )
    payload = response.json()
    assert payload["coordinate_system"] == "design-a"
    assert payload["feedrate_policy"] == "pinned"
    assert payload["resolved_physical_machine_target"] == {"X": 10.0, "Y": 20.0}
    assert "resolved_controller_machine_target" in payload


def test_coordinate_system_list_reports_axis_readiness(api_client) -> None:
    response = api_client.get("/api/v1/coordinate-systems")
    assert response.status_code == 200
    systems = response.json()["coordinate_systems"]
    design = next(item for item in systems if item["id"] == "design-a")
    assert design["axes"]["A"]["status"] == "missing"


def test_existing_but_unavailable_frame_is_not_reported_as_not_found(api_client) -> None:
    response = api_client.post(
        "/api/v1/stage/move",
        json={"coordinate_system": "blocked-design", "coordinates": {"X": 1.0}},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "coordinate_system_unavailable"
```

- [ ] **Step 2: Run and observe payload/endpoint failures**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\api\test_coordinate_systems.py tests\stage\test_api_moves.py -v
```

Expected: FAIL because `/api/v1/coordinate-systems` and explicit frame handling do not
exist.

- [ ] **Step 3: Pass frame ID through API without GUI mutation**

Extend `MoveToCoordinatesHandler` and request bridge with keyword-only
`coordinate_system: object = "machine"`. Parse blank/missing as `machine` and
otherwise preserve the UUID/string for registry lookup. Build API
`MotionIntent` directly; never read `StagePositionPanel.currentData()`.

Add keyword-only `coordinate_system: str = "machine"` to
`ProbeStationClient.move_stage()`, `move_to()`, and `move_by()`, and include it
in the JSON payload only when it is not `machine`. Add
`ProbeStationClient.coordinate_systems()` for the new read-only endpoint.

```python
def move_stage(
    self,
    coordinates: Mapping[str, float] | None = None,
    *,
    mode: str = "absolute",
    feedrate: float | None = None,
    relative: bool | None = None,
    coordinate_system: str = "machine",
    **axes: float,
) -> dict[str, Any]:
    targets = {
        str(axis).upper(): float(value)
        for axis, value in (coordinates or {}).items()
    }
    targets.update(
        {
            str(axis).upper(): float(value)
            for axis, value in axes.items()
            if value is not None
        }
    )
    payload: dict[str, Any] = {"coordinates": targets}
    if relative is None:
        payload["mode"] = mode
    else:
        payload["relative"] = bool(relative)
    if feedrate is not None:
        payload["feedrate"] = float(feedrate)
    if coordinate_system != "machine":
        payload["coordinate_system"] = str(coordinate_system)
    return self._request("POST", "/api/v1/stage/move", payload)


def coordinate_systems(self) -> dict[str, Any]:
    return self._request("GET", "/api/v1/coordinate-systems")
```

Add `GET /api/v1/coordinate-systems` whose handler receives one immutable registry and
physical-position snapshot and returns ID/name/kind/version/availability,
per-axis status/reason, and current values when resolvable. Map unknown frame to
404, existing unavailable/conflict to 409, invalid coordinates/domain/limits to
422.

Successful movement responses include `coordinate_system`, `frame_version`,
`requested_coordinates`, `resolved_physical_machine_target`,
`resolved_controller_machine_target`, and `feedrate_policy`. Existing clients omitting the
new key continue to move in Machine.

- [ ] **Step 4: Run API, bridge, client, and OpenAPI tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\api tests\stage\test_api_moves.py -v
```

Expected: all API tests PASS and OpenAPI generation remains valid.

- [ ] **Step 5: Commit independent API coordinates**

```powershell
git add probe_station_gui/api probe_station_gui/stage/api_moves.py probe_station_client main.py tests/api tests/stage/test_api_moves.py
git commit -m "feat: expose independent api coordinate frames"
```

### Task 5: Feedrate Ownership and B-Registration Regression Cleanup

**Files:**
- Modify: `probe_station_gui/stage/coordinate_targets.py`
- Modify: `probe_station_gui/stage/api_moves.py`
- Modify: `probe_station_gui/stage/position_presenter.py:154-180`
- Modify: `probe_station_gui/stage/position_update.py:264-315`
- Modify: `main.py:5535-6005,9090-9205`
- Modify test: `tests/app/test_main_coordinate_feedrate.py`
- Modify test: `tests/stage/test_position_presenter.py`
- Modify test: `tests/stage/test_position_update.py`
- Modify test: `tests/design/test_objective_alignment.py`

**Interfaces:**
- Consumes: `ResolvedMotionPlan.feedrate_policy`, actual physical B status updates, explicit authority invalidation events.
- Produces: deterministic slider reissue policy and no registration invalidation from known B movement.

- [ ] **Step 1: Add failing ownership and B-motion regression tests**

```python
def test_slider_reissues_shared_api_move_from_same_resolved_target(main_window, shared_api_plan) -> None:
    main_window.start_resolved_plan_for_test(shared_api_plan)
    original_target = dict(shared_api_plan.controller_machine_targets)
    main_window.set_coordinate_frame_for_test("other-frame")
    main_window.apply_feedrate_for_test(30.0)
    assert main_window.last_reissued_controller_target_for_test() == original_target


def test_slider_does_not_reissue_explicit_api_feedrate(main_window, pinned_api_plan) -> None:
    main_window.start_resolved_plan_for_test(pinned_api_plan)
    main_window.apply_feedrate_for_test(30.0)
    assert main_window.coordinate_reissue_count_for_test() == 0


def test_known_b_position_change_does_not_invalidate_design_registration(position_owner) -> None:
    position_owner.apply_physical_b_for_test(10.0, authority_known=True)
    assert position_owner.design_frame_for_test().readiness["B"].available


def test_unknown_b_after_reset_blocks_without_deleting_transform(position_owner) -> None:
    original = position_owner.design_frame_for_test().transform
    position_owner.invalidate_machine_authority_for_test({"B"}, "Controller reset.")
    frame = position_owner.design_frame_for_test()
    assert not frame.readiness["B"].available
    assert frame.transform == original
```

- [ ] **Step 2: Run and observe current invalidation/reissue failures**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py tests\stage\test_position_presenter.py tests\stage\test_position_update.py tests\design\test_objective_alignment.py -v
```

Expected: new assertions FAIL because existing code invalidates registration on
ordinary B changes and feedrate has no explicit owner.

- [ ] **Step 3: Remove motion-driven invalidation and enforce policy enum**

Delete calls that clear/invalidate design registration merely because a known
B movement started/completed or a B coordinate target was accepted. Replace
them with registry presentation refresh using actual physical B. Keep real
invalidation for controller reset, manual terminal uncertainty, calibration
change, and missing final status.

Allow slider reissue only when `active_plan.feedrate_policy is SHARED`; build
the replacement execution request from the stored controller Machine target.
For `PINNED`, update only future GUI/default speed and leave the active motion
untouched.

```python
def plan_feedrate_reissue(
    active_plan: ResolvedMotionPlan | None,
    requested_feedrate: float,
) -> tuple[Mapping[str, float], float] | None:
    if active_plan is None or active_plan.feedrate_policy is FeedratePolicy.PINNED:
        return None
    return (active_plan.controller_machine_targets, float(requested_feedrate))
```

- [ ] **Step 4: Run focused and full regression suites**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py tests\stage\test_position_presenter.py tests\stage\test_position_update.py tests\design\test_objective_alignment.py -v
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

Expected: focused and complete suites PASS.

- [ ] **Step 5: Commit policy and B behavior**

```powershell
git add probe_station_gui/stage main.py tests/app/test_main_coordinate_feedrate.py tests/stage/test_position_presenter.py tests/stage/test_position_update.py tests/design/test_objective_alignment.py
git commit -m "fix: preserve frame snapshots across motion changes"
```
