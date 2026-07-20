# Universal Axis Calibration, Preview, and Exact Step Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the A/Z-specific calibration code with one strict NPZ-backed interpolation model for all six axes, add a persistent live PyQtGraph preview, and make Step a lossless exact physical-target mode.

**Architecture:** A pure calibration layer owns validated snapshots and bidirectional interpolation in machine coordinates; the stage coordinate adapter composes that mapping with work origins and existing safety/precision-approach logic. Settings import NPZ files asynchronously and render cached data only, while a small GUI-owned Step accumulator coalesces physical targets and hands complete targets to the existing coordinate-move lifecycle.

**Tech Stack:** Python 3.11, PySide6, NumPy, PyQtGraph 0.14, pytest, pytest-qt.

## Global Constraints

- Calibration axes are exactly `X`, `Y`, `Z`, `A`, `B`, and `C`.
- Calibration files contain scalar Unicode `axis` plus finite, one-dimensional, equal-length, strictly increasing `controller` and `physical` arrays with at least two points.
- X/Y/Z/A use millimetres; B/C use degrees.
- Interpolation is piecewise linear inside the closed measured domain; no sorting, repair, clamping, fitting, or extrapolation occurs at runtime.
- Runtime movement uses the settings snapshot; the retained absolute file path is provenance and replacement UI state only.
- Precision-approach direction and backlash remain independent from calibration.
- Homing, origin, limit-switch, needle safety, route Pause/Resume/Interrupt, and manual terminal semantics must remain unchanged.
- NPZ I/O stays off the GUI thread; status-marker updates use cached machine state and perform no hardware reads.
- Continuous Jog remains a long relative `$J=G91` move stopped with `0x85`, additionally clipped to an enabled calibration controller domain.
- Step accumulation uses one fixed 80 ms window that is not restarted by later presses.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python and pytest command.
- Do not commit machine-local files under `calibrations`.

---

## File Structure

- `probe_station_gui/settings/axis_calibration_config.py`: immutable universal settings value, six-axis defaults, parsing, normalization, and serialization helpers.
- `probe_station_gui/settings/axis_calibration_npz.py`: strict new-format NPZ loader and validation result; no legacy schema knowledge.
- `probe_station_gui/stage/axis_mapping.py`: pure curve/domain/interpolation primitives.
- `probe_station_gui/stage/axis_calibration.py`: universal mapper composed with machine/work-coordinate origins.
- `probe_station_gui/stage/axis_coordinates.py`: controller-facing calibrated display/target methods and existing A needle adapters.
- `probe_station_gui/stage/exact_step.py`: pure accumulated physical-target state.
- `probe_station_gui/dialogs/settings/axis_calibration_preview.py`: focused PyQtGraph curve and live-marker widget.
- `probe_station_gui/dialogs/settings/axis_settings.py`: per-axis calibration controls, async import, preview lifetime, and live marker binding.
- `scripts/prepare_axis_calibration_npz.py`: explicit one-time converter for the local raw direct-pass measurement.
- Existing controller, settings dialog, main window, joystick, and movement lifecycle files only wire these focused components into existing flows.

---

### Task 1: Universal NPZ Contract and Settings Snapshot

**Files:**
- Replace: `probe_station_gui/settings/axis_calibration_config.py`
- Replace: `probe_station_gui/settings/axis_calibration_npz.py`
- Modify: `probe_station_gui/settings/manager.py`
- Modify: `probe_station_gui/default_settings.json`
- Replace tests: `tests/settings/test_axis_calibration_config.py`
- Replace tests: `tests/settings/test_axis_calibration_npz.py`

**Interfaces:**
- Produces: `CALIBRATION_AXES`, `axis_unit(axis: str) -> str`, `AxisCalibrationSettings`, `default_axis_calibrations()`, `parse_axis_calibrations(raw)`, and `load_axis_calibration_npz(path, expected_axis) -> ImportedAxisCalibration`.
- `ImportedAxisCalibration` exposes `axis`, `calibration_file`, `controller_points`, and `physical_points`.
- `Settings.axis_calibrations` is `dict[str, AxisCalibrationSettings]` and serializes under the sole key `axis_calibrations`.

- [ ] **Step 1: Replace the settings tests with six-axis defaults and strict snapshot normalization**

```python
def test_axis_calibrations_default_to_six_disabled_empty_entries():
    values = default_axis_calibrations()
    assert tuple(values) == ("X", "Y", "Z", "A", "B", "C")
    assert all(value == AxisCalibrationSettings() for value in values.values())

def test_invalid_enabled_snapshot_is_disabled_and_cleared():
    parsed = parse_axis_calibrations({
        "X": {"enabled": True, "calibration_file": "x.npz",
              "controller_points": [0.0, 1.0], "physical_points": [0.0, 0.0]},
    })
    assert parsed["X"] == AxisCalibrationSettings()
```

- [ ] **Step 2: Replace loader tests with the only supported schema and all structural failures**

```python
@pytest.mark.parametrize("axis", ["X", "Y", "Z", "A", "B", "C"])
def test_loads_strict_increasing_curve(tmp_path, axis):
    path = tmp_path / f"{axis}.npz"
    np.savez(path, axis=np.array(axis), controller=[0.0, 1.0], physical=[2.0, 3.0])
    loaded = load_axis_calibration_npz(path, axis)
    assert loaded.axis == axis
    assert loaded.controller_points == (0.0, 1.0)
    assert loaded.physical_points == (2.0, 3.0)

@pytest.mark.parametrize("field,values", [
    ("controller", [0.0, 0.0]),
    ("controller", [1.0, 0.0]),
    ("physical", [0.0, 0.0]),
    ("physical", [1.0, 0.0]),
    ("physical", [0.0, float("nan")]),
])
def test_rejects_non_increasing_or_nonfinite_data(tmp_path, field, values):
    data = {"axis": np.array("X"), "controller": [0.0, 1.0], "physical": [0.0, 1.0]}
    data[field] = values
    path = tmp_path / "bad.npz"
    np.savez(path, **data)
    with pytest.raises(AxisCalibrationFileError):
        load_axis_calibration_npz(path, "X")
```

- [ ] **Step 3: Run the focused tests and verify they fail because old A/Z types and schema are still present**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_axis_calibration_config.py tests/settings/test_axis_calibration_npz.py -q`

Expected: FAIL on missing universal APIs or old `gcode`/`indicator` assumptions.

- [ ] **Step 4: Implement the universal immutable settings value and strict loader**

```python
CALIBRATION_AXES = ("X", "Y", "Z", "A", "B", "C")

@dataclass(frozen=True)
class AxisCalibrationSettings:
    enabled: bool = False
    calibration_file: str = ""
    controller_points: tuple[float, ...] = ()
    physical_points: tuple[float, ...] = ()

def axis_unit(axis: str) -> str:
    return "deg" if axis in {"B", "C"} else "mm"

def _strict_curve(controller, physical) -> bool:
    return (len(controller) >= 2 and len(controller) == len(physical)
            and all(math.isfinite(value) for value in (*controller, *physical))
            and all(right > left for left, right in pairwise(controller))
            and all(right > left for left, right in pairwise(physical)))
```

The NPZ loader reads with `allow_pickle=False`, requires the three named keys, validates scalar Unicode `axis`, validates both arrays without transforming them, and returns tuples plus `str(Path(path).resolve())`. `parse_axis_calibrations` fills all six axes and replaces any invalid enabled persisted value with `AxisCalibrationSettings()`.

- [ ] **Step 5: Replace old manager/default fields and verify clone/round-trip behavior**

```python
@dataclass
class Settings:
    # existing fields remain
    axis_calibrations: dict[str, AxisCalibrationSettings] = field(
        default_factory=default_axis_calibrations
    )

def axis_calibrations_configuration(self) -> dict[str, AxisCalibrationSettings]:
    return {axis: replace(value) for axis, value in self._settings.axis_calibrations.items()}
```

Delete `axis_a_calibration`, `axis_z_calibration`, cosine/polynomial parsers, and their default JSON sections. Persist each universal entry as `enabled`, `calibration_file`, `controller_points`, and `physical_points`.

- [ ] **Step 6: Run focused settings tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add probe_station_gui/settings probe_station_gui/default_settings.json tests/settings
git commit -m "feat: add universal axis calibration settings"
```

---

### Task 2: Pure Universal Coordinate Mapper

**Files:**
- Replace: `probe_station_gui/stage/axis_mapping.py`
- Replace: `probe_station_gui/stage/axis_calibration.py`
- Replace tests: `tests/stage/test_axis_mapping.py`
- Replace tests: `tests/stage/test_axis_calibration.py`

**Interfaces:**
- Consumes: `AxisCalibrationSettings` from Task 1.
- Produces: `CalibrationOutOfDomain`, `AxisCalibrationCurve`, `curve_from_settings`, `controller_to_physical`, `physical_to_controller`, and `StageAxisCalibrationMapper` methods for raw configured and raw machine coordinates.

- [ ] **Step 1: Write direct/inverse/domain tests including endpoints and identity**

```python
def test_interpolation_is_bidirectional_and_strictly_bounded():
    curve = AxisCalibrationCurve((0.0, 1.0, 2.0), (10.0, 12.0, 15.0))
    assert controller_to_physical(curve, 0.5) == pytest.approx(11.0)
    assert physical_to_controller(curve, 13.5) == pytest.approx(1.5)
    assert controller_to_physical(curve, 0.0) == 10.0
    with pytest.raises(CalibrationOutOfDomain):
        controller_to_physical(curve, -1e-9)

def test_disabled_curve_uses_identity_mapping():
    mapper = StageAxisCalibrationMapper({"X": AxisCalibrationSettings()})
    assert mapper.controller_to_physical("X", 1.25) == 1.25
    assert mapper.physical_to_controller("X", 1.25) == 1.25
```

- [ ] **Step 2: Write nonlinear work-origin tests**

```python
def test_work_coordinate_mapping_uses_machine_origin():
    mapper = calibrated_mapper("X", controller=(0.0, 10.0, 20.0), physical=(0.0, 12.0, 30.0))
    mapper.set_position_reporting_mode("work")
    mapper.set_controller_coordinate_offset("G54", "X", 10.0)
    mapper.set_active_work_coordinate_system("G54")
    assert mapper.configured_controller_to_physical("X", 5.0) == pytest.approx(18.0)
    assert mapper.physical_to_configured_controller("X", 18.0) == pytest.approx(5.0)
```

- [ ] **Step 3: Run mapper tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_axis_mapping.py tests/stage/test_axis_calibration.py -q`

Expected: FAIL because existing mapper only exposes A/Z formulas and clamps interpolation.

- [ ] **Step 4: Implement pure curve primitives**

```python
@dataclass(frozen=True)
class AxisCalibrationCurve:
    controller: tuple[float, ...]
    physical: tuple[float, ...]

def controller_to_physical(curve: AxisCalibrationCurve, value: float) -> float:
    if not curve.controller[0] <= value <= curve.controller[-1]:
        raise CalibrationOutOfDomain("controller coordinate is outside calibration range")
    return float(np.interp(value, curve.controller, curve.physical))

def physical_to_controller(curve: AxisCalibrationCurve, value: float) -> float:
    if not curve.physical[0] <= value <= curve.physical[-1]:
        raise CalibrationOutOfDomain("physical coordinate is outside calibration range")
    return float(np.interp(value, curve.physical, curve.controller))
```

- [ ] **Step 5: Implement machine/work-coordinate composition**

For a calibrated work coordinate `w_raw` with controller work origin `o`, return `f(w_raw + o) - f(o)`. For an entered physical work target `w`, return `f_inverse(w + f(o)) - o`. Machine mode uses `o = 0`. A missing active work offset raises `CalibrationCoordinateUnavailable`; identity axes keep the existing raw coordinate even without an offset.

- [ ] **Step 6: Run mapper tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_axis_mapping.py tests/stage/test_axis_calibration.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add probe_station_gui/stage/axis_mapping.py probe_station_gui/stage/axis_calibration.py tests/stage/test_axis_mapping.py tests/stage/test_axis_calibration.py
git commit -m "feat: map every calibrated stage axis"
```

---

### Task 3: Controller and Exact-Motion Integration

**Files:**
- Modify: `probe_station_gui/stage/axis_coordinates.py`
- Modify: `probe_station_gui/stage/controller.py`
- Modify: `probe_station_gui/stage/precision_motion.py`
- Modify: `probe_station_gui/views/main_window_stage_position_panel.py`
- Modify: `main.py`
- Modify tests: `tests/stage/test_controller_axis_needles.py`
- Modify tests: `tests/stage/test_precision_motion.py`
- Modify tests: `tests/app/test_main_stage_coordinate_controls.py`

**Interfaces:**
- Consumes: `StageAxisCalibrationMapper` from Task 2 and settings mapping from Task 1.
- Produces controller methods `apply_axis_calibrations`, `calibrated_axis_display_value`, `calibrated_axis_raw_target_value`, `axis_display_limits`, `axis_raw_limits_for_configured_mode`, `latest_machine_position`, and `axis_calibration_preview_position`.

- [ ] **Step 1: Add parameterized all-axis status and target tests**

```python
@pytest.mark.parametrize("axis,index", zip("XYZABC", range(6)))
def test_all_axis_display_and_exact_targets_use_inverse_curve(controller, axis, index):
    controller.apply_axis_calibrations(curve_settings(axis, [0.0, 10.0], [0.0, 20.0]))
    assert controller.calibrated_axis_display_value(axis, 4.0) == pytest.approx(8.0)
    assert controller.calibrated_axis_raw_target_value(axis, 12.0) == pytest.approx(6.0)
```

Add coverage that multi-axis Apply, Design X/Y click targets, focus Z, needle A, B/C exact targets, and precision-approach pre/final targets receive inverse-mapped raw coordinates, while homing/origin commands remain unchanged.

- [ ] **Step 2: Run focused controller/app tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_axis_needles.py tests/stage/test_precision_motion.py tests/app/test_main_stage_coordinate_controls.py -q`

Expected: FAIL on removed A/Z APIs or missing universal mapping.

- [ ] **Step 3: Replace axis-specific controller state and application wiring**

```python
def apply_axis_calibrations(self, settings: Mapping[str, AxisCalibrationSettings]) -> None:
    self._axis_calibration_mapper.set_calibrations(settings)

def axis_calibration_preview_position(self, axis: str) -> tuple[float, float] | None:
    raw_machine = self._last_machine_position[self._axis_index[axis]]
    return raw_machine, self._axis_calibration_mapper.machine_controller_to_physical(axis, raw_machine)
```

`Main._apply_settings` calls only `stage_controller.apply_axis_calibrations(settings.axis_calibrations)`. Remove all A cosine and Z polynomial setup. Keep existing signed A lowering conversion at its current needle boundary, implemented in terms of universal A physical coordinates.

- [ ] **Step 4: Make precision fingerprints and validation generic**

The precision fingerprint includes the selected axis curve tuples for every axis. An inverse target outside its physical domain returns the existing rejected-target result before commands are queued. `axis_display_limits` maps raw software limits through the current configured-coordinate transform without clamping.

- [ ] **Step 5: Run focused tests and related stage suite**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage tests/app/test_main_stage_coordinate_controls.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add main.py probe_station_gui/stage probe_station_gui/views/main_window_stage_position_panel.py tests/stage tests/app/test_main_stage_coordinate_controls.py
git commit -m "feat: apply calibration to exact stage targets"
```

---

### Task 4: Per-Axis Settings and Live PyQtGraph Preview

**Files:**
- Create: `probe_station_gui/dialogs/settings/axis_calibration_preview.py`
- Modify: `probe_station_gui/dialogs/settings/axis_settings.py`
- Modify: `probe_station_gui/dialogs/settings_dialog.py`
- Modify: `probe_station_gui/views/main_window_auxiliary.py`
- Create: `tests/ui/test_axis_calibration_preview.py`
- Modify: `tests/ui/test_precision_approach_settings.py`

**Interfaces:**
- Consumes: universal settings and loader from Task 1; position source with `stage_position_changed` signal and `axis_calibration_preview_position(axis)` method from Task 3.
- Produces: `AxisCalibrationPreview.set_curve`, `.clear_curve`, and `.set_current_position`; every axis page exposes the same Browse/Reset/Enabled/status/preview behavior.

- [ ] **Step 1: Write preview lifetime and marker tests**

```python
def test_curve_stays_visible_but_marker_hides_when_disabled(qtbot):
    preview = AxisCalibrationPreview("X")
    qtbot.addWidget(preview)
    preview.set_curve((0.0, 1.0), (0.0, 2.0))
    curve_item = preview.curve_item
    preview.set_current_position(0.5, 1.0, visible=True)
    assert preview.marker_item.isVisible()
    preview.set_current_position(None, None, visible=False)
    assert preview.curve_item is curve_item
    assert preview.curve_item.isVisible()
    assert not preview.marker_item.isVisible()
    assert not preview.position_line.isVisible()

def test_position_update_does_not_reset_view_range(qtbot):
    preview = populated_preview(qtbot, "B")
    preview.plot_widget.setXRange(0.2, 0.4, padding=0)
    before = preview.plot_widget.viewRange()
    preview.set_current_position(0.3, 0.6, visible=True)
    assert preview.plot_widget.viewRange() == before
```

- [ ] **Step 2: Write settings tests for all axes, failed-import preservation, Reset, and units**

```python
@pytest.mark.parametrize("axis,unit", [("X", "mm"), ("A", "mm"), ("B", "deg"), ("C", "deg")])
def test_axis_page_uses_matching_curve_and_unit(qtbot, axis, unit):
    widget = axis_widget_with_curve(qtbot, axis)
    widget.select_axis(axis)
    assert widget.calibration_preview.axis == axis
    assert unit in widget.calibration_preview.bottom_label
```

Verify no preview widget exists before a saved valid snapshot, switching axes preserves constructed previews, unchecked Enabled hides only live items, invalid import preserves all previous state, and Reset clears snapshot/file/preview.

- [ ] **Step 3: Run UI tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_axis_calibration_preview.py tests/ui/test_precision_approach_settings.py -q`

Expected: FAIL because the preview and generic calibration controls do not exist.

- [ ] **Step 4: Implement the focused preview widget**

```python
class AxisCalibrationPreview(QWidget):
    def set_curve(self, controller: Sequence[float], physical: Sequence[float]) -> None:
        self.curve_item.setData(controller, physical)
        self.samples_item.setData(controller, physical)
        self.plot_widget.enableAutoRange()
        self.plot_widget.autoRange()

    def set_current_position(self, controller, physical, *, visible: bool) -> None:
        shown = visible and controller is not None and physical is not None
        self.marker_item.setVisible(shown)
        self.position_line.setVisible(shown)
        if shown:
            self.marker_item.setData([controller], [physical])
            self.position_line.setValue(controller)
```

Use a blue curve, small hoverable sample scatter, contrasting diamond, dashed vertical `InfiniteLine`, axis/unit labels, native pan/wheel/rectangular zoom, and a compact outside-range status. Marker updates must not call auto-range.

- [ ] **Step 5: Rewrite axis settings around one six-axis mapping**

The async worker invokes `load_axis_calibration_npz(path, selected_axis)`. Its success handler atomically replaces that axis's snapshot and lazily creates its preview. Its failure handler changes only the concise validation status. `SettingsDialog` receives `axis_position_source` explicitly, and `main_window_auxiliary.open_settings_dialog` passes `owner.stage_controller`.

- [ ] **Step 6: Run UI tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_axis_calibration_preview.py tests/ui/test_precision_approach_settings.py tests/ui/test_key_bindings.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add probe_station_gui/dialogs probe_station_gui/views/main_window_auxiliary.py tests/ui
git commit -m "feat: preview calibration curves in axis settings"
```

---

### Task 5: Continuous Jog Domain and Wheel Semantics

**Files:**
- Modify: `probe_station_gui/stage/jog_queue.py`
- Modify: `probe_station_gui/views/joystick_window.py`
- Modify tests: `tests/stage/test_controller_jog_motion.py`
- Modify tests: `tests/ui/test_joystick_window.py`

**Interfaces:**
- Consumes: `axis_raw_limits_for_configured_mode(axis)` from Task 3.
- Produces unchanged long-relative Jog commands clipped by both software and calibration domains; Step editor focus owns exact 0.001 wheel increments while the panel wheel owns feedrate otherwise.

- [ ] **Step 1: Add calibrated Jog clipping tests**

```python
def test_continuous_jog_is_relative_and_clipped_to_calibration_domain(controller):
    controller.set_cached_position(X=4.0)
    controller.apply_axis_calibrations(curve_settings("X", [0.0, 5.0], [0.0, 6.0]))
    controller.start_jog("X", 1, 2.0)
    assert controller.serial_worker.sent[-1].startswith("$J=G91 G21 X1")

def test_jog_release_still_sends_realtime_stop(joystick, serial_worker):
    joystick.stop_jog("X")
    assert serial_worker.raw_writes[-1] == b"\x85"
```

- [ ] **Step 2: Add wheel routing tests**

```python
def test_panel_wheel_changes_feedrate_in_step_mode(qtbot, joystick):
    joystick.set_control_mode("Step")
    old_speed = joystick.speed_slider.value()
    send_wheel(joystick, +120)
    assert joystick.speed_slider.value() > old_speed

def test_focused_step_editor_wheel_changes_exactly_one_micron(qtbot, joystick):
    joystick.step_size_spin.setFocus()
    joystick.step_size_spin.setValue(0.010)
    send_wheel(joystick.step_size_spin, +120)
    assert joystick.step_size_spin.value() == pytest.approx(0.011)
```

- [ ] **Step 3: Run tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_jog_motion.py tests/ui/test_joystick_window.py -q`

Expected: FAIL because Jog ignores calibration bounds and Step-mode panel wheel changes Step size.

- [ ] **Step 4: Intersect Jog endpoint bounds and preserve its command model**

`constrain_jog_distances` intersects existing software bounds with the enabled curve's configured raw controller domain before calculating the relative distance. It still emits one `$J=G91 G21 ... F...` command and uses the existing release stop path.

- [ ] **Step 5: Route wheel events by editor focus**

Set Step spin-box decimals to 3 and `singleStep` to `0.001`. Its focused wheel consumes one signed `singleStep` per 120-unit notch with no acceleration. The parent panel handler changes feedrate in Jog and Step whenever the editor does not own the event. Typing and spin buttons remain unchanged.

- [ ] **Step 6: Run focused tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_controller_jog_motion.py tests/ui/test_joystick_window.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add probe_station_gui/stage/jog_queue.py probe_station_gui/views/joystick_window.py tests/stage/test_controller_jog_motion.py tests/ui/test_joystick_window.py
git commit -m "fix: bound jog and refine step wheel control"
```

---

### Task 6: Lossless Exact Step Accumulator

**Files:**
- Create: `probe_station_gui/stage/exact_step.py`
- Modify: `probe_station_gui/stage/coordinate_targets.py`
- Modify: `probe_station_gui/stage/move_lifecycle.py`
- Modify: `probe_station_gui/views/joystick_window.py`
- Modify: `main.py`
- Create: `tests/stage/test_exact_step.py`
- Modify: `tests/app/test_main_stage_coordinate_controls.py`

**Interfaces:**
- Produces: `ExactStepAccumulator.add(axis, delta, baseline)`, `.targets`, `.clear`, `.has_targets`; `CoordinateTargetMoveState.display_targets`; Main methods `_queue_exact_step`, `_dispatch_exact_step_targets`, `_clear_exact_step_targets`, and `_on_coordinate_move_finished`.
- Consumes the existing coordinate-target start plan so each coalesced target receives the same calibration, limits, safety, and precision-approach treatment as coordinate Apply.

- [ ] **Step 1: Write pure accumulation tests**

```python
def test_same_and_opposite_presses_accumulate_from_pending_target():
    steps = ExactStepAccumulator()
    assert steps.add("X", +0.001, baseline=1.000) == pytest.approx(1.001)
    assert steps.add("X", +0.001, baseline=99.0) == pytest.approx(1.002)
    assert steps.add("X", -0.001, baseline=99.0) == pytest.approx(1.001)

def test_clear_discards_all_targets():
    steps = ExactStepAccumulator()
    steps.add("Y", 0.01, baseline=2.0)
    steps.clear()
    assert not steps.has_targets
```

- [ ] **Step 2: Write GUI orchestration tests for fixed-window coalescing and active-move deferral**

```python
def test_first_press_starts_non_extending_80ms_window(qtbot, main):
    main._queue_exact_step("X", 0.001)
    remaining = main._exact_step_timer.remainingTime()
    main._queue_exact_step("X", 0.001)
    assert main._exact_step_timer.remainingTime() <= remaining
    qtbot.wait(90)
    assert main.started_coordinate_display_targets[-1]["X"] == pytest.approx(0.002)

def test_active_move_gets_one_latest_followup_after_success(main):
    main.begin_fake_exact_move({"X": 1.0})
    main._queue_exact_step("X", 0.001)
    main._queue_exact_step("X", 0.001)
    assert len(main.started_coordinate_moves) == 1
    main._on_coordinate_move_finished(True)
    assert main.started_coordinate_display_targets[-1]["X"] == pytest.approx(1.002)
```

Add tests for baseline priority (pending, active, latest displayed), multi-axis coalescing, invalid candidate preserving prior target, net-zero suppression, pending field styling, and failure clearing deferred work.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_exact_step.py tests/app/test_main_stage_coordinate_controls.py -q`

Expected: FAIL because Step still immediately sends or rejects moves while busy.

- [ ] **Step 4: Implement the pure accumulator and preserve active display targets**

```python
@dataclass
class ExactStepAccumulator:
    targets: dict[str, float] = field(default_factory=dict)

    def add(self, axis: str, delta: float, *, baseline: float) -> float:
        target = self.targets.get(axis, baseline) + delta
        self.targets[axis] = target
        return target

    @property
    def has_targets(self) -> bool:
        return bool(self.targets)

    def clear(self) -> None:
        self.targets.clear()
```

`CoordinateTargetMoveState` stores both raw `target_position` and physical `display_targets` until completion so new Step input never uses a stale status sample.

- [ ] **Step 5: Implement fixed-window dispatch and active-motion follow-up**

The first accepted input starts a single-shot 80 ms timer only if it is inactive. Each candidate is converted and checked through `_raw_target_from_display_value` plus existing display-limit validation before replacing the pending value. If a coordinate move is active, retain only the latest desired target. On successful completion, dispatch one latest non-equal target; on failure/cancel clear it. Every actual dispatch uses `_start_coordinate_targets_move` so precision approach occurs once per issued target.

- [ ] **Step 6: Wire every coordinate-authority invalidation**

Call `_clear_exact_step_targets()` on leaving Step, disconnect/reconnect, soft reset, every homing/origin operation, manual serial command, coordinate-system/work-origin change, failed/cancelled movement, and `closeEvent`. The method stops the timer, clears pure state, clears deferred display targets, and removes pending coordinate styling without issuing movement.

- [ ] **Step 7: Run focused and route-safety regression tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_exact_step.py tests/app/test_main_stage_coordinate_controls.py tests/measurement/test_route_controls.py -q`

Expected: PASS; if the repository names the route test module differently, run `rg --files tests | rg "route.*control|control.*route"` and include all matches in this command.

- [ ] **Step 8: Commit**

```powershell
git add main.py probe_station_gui/stage/exact_step.py probe_station_gui/stage/coordinate_targets.py probe_station_gui/stage/move_lifecycle.py probe_station_gui/views/joystick_window.py tests/stage/test_exact_step.py tests/app/test_main_stage_coordinate_controls.py
git commit -m "feat: accumulate exact step targets"
```

---

### Task 7: Prepare and Verify the Machine-Local Z Archive

**Files:**
- Create: `scripts/prepare_axis_calibration_npz.py`
- Create: `tests/scripts/test_prepare_axis_calibration_npz.py`
- Produce locally, do not add: `calibrations/axis_z_spm6335_section3_precise_start16p5_top23p4_step0p0025_settle2p0_feed1_transition10_20260505_175825_forward.npz`

**Interfaces:**
- Produces: `longest_strictly_increasing_indices(values) -> np.ndarray` and `prepare_forward_calibration(source, destination, axis="Z")`.
- The application never imports this converter and never learns the raw archive schema.

- [ ] **Step 1: Write converter tests for a direct pass with initial conflicts**

```python
def test_preparer_selects_direct_pass_and_keeps_original_samples(tmp_path):
    source = tmp_path / "raw.npz"
    np.savez(source, gcode=[0., 1., 2., 3., 4.], indicator=[0., 0.2, 0.1, 0.3, 0.4], direction=[-1, 1, 1, 1, 1])
    destination = tmp_path / "forward.npz"
    prepare_forward_calibration(source, destination, axis="Z")
    with np.load(destination, allow_pickle=False) as data:
        assert set(data.files) == {"axis", "controller", "physical"}
        assert data["axis"].item() == "Z"
        assert np.all(np.diff(data["controller"]) > 0)
        assert np.all(np.diff(data["physical"]) > 0)
        assert all(value in {0.2, 0.1, 0.3, 0.4} for value in data["physical"])
```

- [ ] **Step 2: Run the converter test and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/scripts/test_prepare_axis_calibration_npz.py -q`

Expected: FAIL because the converter does not exist.

- [ ] **Step 3: Implement deterministic longest strictly increasing subsequence conversion**

Use predecessor reconstruction with `bisect_left`, filter raw samples by `direction == 1`, retain selected `gcode` and `indicator` values unchanged, assert both output arrays are strictly increasing, refuse to overwrite an existing destination, and save only scalar Unicode `axis`, `controller`, and `physical` with `np.savez_compressed`.

- [ ] **Step 4: Run converter tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/scripts/test_prepare_axis_calibration_npz.py -q`

Expected: PASS.

- [ ] **Step 5: Hash the source, create the local output, and verify both archives**

```powershell
$source = 'C:\Users\Public\code\probe_station_gui\calibrations\axis_z_spm6335_section3_precise_start16p5_top23p4_step0p0025_settle2p0_feed1_transition10_20260505_175825.npz'
$destination = $source -replace '\.npz$', '_forward.npz'
$before = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe scripts\prepare_axis_calibration_npz.py $source $destination --axis Z
$after = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash
if ($before -ne $after) { throw 'Source archive changed' }
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -c "import numpy as np,sys; d=np.load(sys.argv[1],allow_pickle=False); assert set(d.files)=={'axis','controller','physical'}; assert d['axis'].item()=='Z'; assert len(d['controller'])==2758; assert (np.diff(d['controller'])>0).all(); assert (np.diff(d['physical'])>0).all(); print(len(d['controller']))" $destination
```

Expected final output: `2758`; source hashes identical.

- [ ] **Step 6: Commit only converter code and tests**

```powershell
git add scripts/prepare_axis_calibration_npz.py tests/scripts/test_prepare_axis_calibration_npz.py
git commit -m "chore: add calibration archive preparer"
```

---

### Task 8: Remove Legacy Models and Verify the Whole Feature

**Files:**
- Modify all remaining matches under: `probe_station_gui/`, `tests/`, `docs/`
- Modify: `pyproject.toml` only if PyQtGraph is not already declared.

**Interfaces:**
- Consumes all earlier tasks.
- Produces one codebase with no runtime A cosine, Z polynomial, legacy `gcode`/`indicator` loader, direction-aware calibration, or axis-specific calibration setting symbols.

- [ ] **Step 1: Search for and remove every legacy calibration reference**

Run: `rg -n "axis_a_calibration|axis_z_calibration|AxisACalibration|AxisZCalibration|cosine|quintic|branch_direction|final_direction|indicator_points|gcode_points" probe_station_gui tests`

Expected before cleanup: any remaining migration misses are listed. Replace tests and call sites with universal APIs; do not add compatibility aliases.

- [ ] **Step 2: Verify the legacy search is empty**

Run: `rg -n "axis_a_calibration|axis_z_calibration|AxisACalibration|AxisZCalibration|branch_direction|indicator_points|gcode_points" probe_station_gui tests`

Expected: exit code 1 and no output.

- [ ] **Step 3: Run calibration, motion, UI, and settings suites together**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings tests/stage tests/ui tests/app/test_main_stage_coordinate_controls.py tests/scripts/test_prepare_axis_calibration_npz.py -q`

Expected: PASS.

- [ ] **Step 4: Run the complete hardware-free suite**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q`

Expected: all tests PASS with no hardware access.

- [ ] **Step 5: Inspect the final diff and ensure the local NPZ is untracked**

Run: `git status --short; git diff --check; git log --oneline --decorate -10`

Expected: no `calibrations/*.npz` entry, no whitespace errors, and only intentional feature changes.

- [ ] **Step 6: Commit final integration fixes if needed**

```powershell
git add probe_station_gui tests pyproject.toml docs
git commit -m "test: verify universal calibrated motion"
```

Skip this commit only when Step 5 reports no remaining tracked changes.

---

## Self-Review Record

- Spec coverage: Tasks 1–3 cover the strict six-axis snapshot and mapping in machine/work coordinates; Task 4 covers conditional persistent preview and cached live marker; Task 5 covers unchanged continuous Jog plus domain clipping and wheel focus; Task 6 covers every accumulation, precision-approach, validation, and invalidation rule; Task 7 preserves and verifies source measurements; Task 8 removes all legacy models and runs the full suite.
- Placeholder scan: the plan contains no deferred implementation placeholders; every task gives exact files, APIs, tests, commands, expected results, and commit boundary.
- Type consistency: `AxisCalibrationSettings`, `StageAxisCalibrationMapper`, controller `axis_calibration_preview_position`, preview methods, `CoordinateTargetMoveState.display_targets`, and `ExactStepAccumulator` names are identical at their producer and consumer tasks.

