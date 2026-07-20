# NPZ Axis Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator select A/Z NPZ measurement files in the Axes settings and use their normalized points for persistent forward and inverse linear coordinate interpolation.

**Architecture:** A pure NPZ importer validates and snapshots measurement points away from the GUI thread. Settings persist the external file path plus normalized points, while stage mapping consumes only the snapshot at startup. The Axes widget owns file selection and reset state; runtime mapping remains independent of Qt and NumPy file I/O.

**Tech Stack:** Python 3.11+, NumPy, PySide6 `QThreadPool`/`QRunnable`, pytest, existing settings and stage-coordinate modules.

## Global Constraints

- The external NPZ path remains visible and replaceable through the GUI.
- Remove only the unrelated `Path` preview from **Precision approach**.
- Remove `Curve source` and `Fit error`; a PNG is never a calibration file.
- Accepted NPZ files contain finite one-dimensional `gcode` and `indicator` arrays with equal length and at least two usable points.
- If `direction` is present, select the branch matching the configured precision final direction.
- Use monotonic piecewise-linear interpolation in both directions; do not fit, smooth, or extrapolate.
- Parse NPZ files in a worker, never synchronously during startup or on the GUI thread.
- Persist normalized point snapshots so startup performs no NPZ file parsing.
- Preserve legacy cosine/polynomial settings compatibility, public controller APIs, motion safety, and route Pause/Resume/Interrupt semantics.
- Do not run hardware-dependent code.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python command.

---

### Task 1: Pure NPZ import and persistent interpolation settings

**Files:**
- Create: `probe_station_gui/settings/axis_calibration_npz.py`
- Modify: `probe_station_gui/settings/axis_calibration_config.py`
- Modify: `probe_station_gui/default_settings.json`
- Test: `tests/settings/test_axis_calibration_npz.py`
- Test: `tests/settings/test_axis_calibration_config.py`

**Interfaces:**
- Produces: `LINEAR_INTERPOLATION_MODEL = "linear_interpolation"`.
- Produces: `AxisCalibrationImportError(ValueError)`.
- Produces: `ImportedAxisCalibration(calibration_file, gcode_points_mm, display_points_mm, branch_direction)`.
- Produces: `load_axis_calibration_npz(path, *, axis, final_direction) -> ImportedAxisCalibration`.
- Produces settings fields on A and Z: `calibration_file`, `interpolation_gcode_mm`, `interpolation_display_mm`, and `interpolation_direction`.

- [ ] **Step 1: Write failing importer tests with real temporary NPZ files**

Create `tests/settings/test_axis_calibration_npz.py` with these concrete cases:

```python
def test_import_z_selects_requested_direction_and_sorts_points(tmp_path) -> None:
    path = tmp_path / "z.npz"
    np.savez(
        path,
        gcode=np.array([2.0, 0.0, 1.0, 2.0, 0.0, 1.0]),
        indicator=np.array([2.1, 0.1, 1.1, 2.0, 0.0, 1.0]),
        direction=np.array([-1, -1, -1, 1, 1, 1]),
    )
    imported = load_axis_calibration_npz(path, axis="Z", final_direction=1)
    assert imported.calibration_file == str(path.resolve())
    assert imported.gcode_points_mm == (0.0, 1.0, 2.0)
    assert imported.display_points_mm == (0.0, 1.0, 2.0)
    assert imported.branch_direction == 1
```

```python
def test_import_a_uses_negative_indicator_display_convention(tmp_path) -> None:
    path = tmp_path / "a.npz"
    np.savez(path, gcode=[-2.0, -1.0, 0.0], indicator=[2.0, 1.0, 0.0])
    imported = load_axis_calibration_npz(path, axis="A", final_direction=-1)
    assert imported.gcode_points_mm == (-2.0, -1.0, 0.0)
    assert imported.display_points_mm == (-2.0, -1.0, -0.0)
    assert imported.branch_direction is None
```

Add separate tests that reject missing arrays, mismatched shapes, non-finite
values, a missing requested branch, fewer than two unique G-code points, and a
non-monotonic display curve. Add a duplicate-G-code test asserting median
indicator aggregation.

- [ ] **Step 2: Run importer tests and verify RED**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_axis_calibration_npz.py -q
```

Expected: collection fails because `axis_calibration_npz` does not exist.

- [ ] **Step 3: Implement deterministic NPZ normalization**

Create the module with the exact public records:

```python
LINEAR_INTERPOLATION_MODEL = "linear_interpolation"

class AxisCalibrationImportError(ValueError):
    pass

@dataclass(frozen=True)
class ImportedAxisCalibration:
    calibration_file: str
    gcode_points_mm: tuple[float, ...]
    display_points_mm: tuple[float, ...]
    branch_direction: int | None
```

`load_axis_calibration_npz` must use `np.load(path, allow_pickle=False)`, select
`direction == final_direction` when the array exists, sort with
`np.argsort`, group identical G-code values with `np.median`, transform A to
`-indicator`, collapse adjacent equal display plateaus, and then require
`np.diff(display) > 0`. Wrap malformed data in
`AxisCalibrationImportError` with operator-facing text.

- [ ] **Step 4: Add snapshot fields and parsing tests**

Extend both calibration dataclasses with:

```python
calibration_file: str = ""
interpolation_gcode_mm: list[float] = field(default_factory=list)
interpolation_display_mm: list[float] = field(default_factory=list)
interpolation_direction: int | None = None
```

Update `to_dict()` and `clone()` so both point lists are copied. Add parser
tests proving a valid `linear_interpolation` model stays configured, invalid or
non-monotonic snapshots become disabled, and legacy cosine/quintic models still
round-trip. Normalize legacy `.png` `source` values to an empty string.

- [ ] **Step 5: Implement interpolation-aware settings parsing**

Add a private helper returning normalized copied lists only when both lists
are finite, equal-length, at least two points, and strictly increasing. In
`parse_axis_a_calibration` and `parse_axis_z_calibration`, preserve
`linear_interpolation` instead of forcing the legacy model and validate it
using this helper; run the existing parametric validation only for the legacy
model. Set `source=""` when the persisted value ends in `.png`.

Update both calibration sections in `probe_station_gui/default_settings.json`
with empty `calibration_file`, empty interpolation arrays, null direction, and
an empty legacy `source`.

- [ ] **Step 6: Run settings tests and commit**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_axis_calibration_npz.py tests/settings/test_axis_calibration_config.py tests/settings/test_default_file.py -q
```

Expected: all selected tests pass.

```powershell
git add probe_station_gui/settings/axis_calibration_npz.py probe_station_gui/settings/axis_calibration_config.py probe_station_gui/default_settings.json tests/settings/test_axis_calibration_npz.py tests/settings/test_axis_calibration_config.py
git commit -m "feat: import NPZ axis calibration curves"
```

---

### Task 2: Runtime linear interpolation mapping

**Files:**
- Modify: `probe_station_gui/stage/axis_mapping.py`
- Modify: `probe_station_gui/stage/axis_coordinates.py`
- Test: `tests/stage/test_axis_calibration.py`
- Test: `tests/stage/test_controller_axis_needles.py`
- Test: `tests/stage/test_precision_motion.py`

**Interfaces:**
- Consumes Task 1 fields and `LINEAR_INTERPOLATION_MODEL`.
- Produces: `interpolate_calibration_curve(x_points, y_points, x) -> float`.
- Preserves public `calibrated_axis_display_value` and `calibrated_axis_raw_value` signatures.

- [ ] **Step 1: Write failing forward/inverse mapping tests**

Add controller tests applying this snapshot:

```python
AxisZCalibrationSettings(
    configured=True,
    model="linear_interpolation",
    interpolation_gcode_mm=[0.0, 1.0, 3.0],
    interpolation_display_mm=[0.0, 2.0, 5.0],
)
```

Assert Z raw `0.5 -> 1.0`, display `3.5 -> raw 2.0`, and both directions
round-trip at interior points. Add the equivalent A curve
`gcode=[-3,-1,0]`, `display=[-5,-2,0]`. Assert endpoint clamping and retain the
existing precision round-trip rejection for an out-of-domain requested target.

- [ ] **Step 2: Run stage mapping tests and verify RED**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_axis_calibration.py tests/stage/test_controller_axis_needles.py -q
```

Expected: interpolation models are currently rejected by `apply_axis_*_calibration`.

- [ ] **Step 3: Implement dependency-free piecewise-linear mapping**

In `axis_mapping.py`, add a `bisect_right` based helper:

```python
def interpolate_calibration_curve(
    x_points: tuple[float, ...],
    y_points: tuple[float, ...],
    x_value: float,
) -> float:
    if x_value <= x_points[0]:
        return y_points[0]
    if x_value >= x_points[-1]:
        return y_points[-1]
    upper = bisect_right(x_points, x_value)
    lower = upper - 1
    fraction = (x_value - x_points[lower]) / (x_points[upper] - x_points[lower])
    return y_points[lower] + fraction * (y_points[upper] - y_points[lower])
```

Branch the A and Z forward/inverse functions on
`calibration["model"] == LINEAR_INTERPOLATION_MODEL`. Forward uses
`gcode_points -> display_points`; inverse uses
`display_points -> gcode_points`. Preserve all legacy branches unchanged.

- [ ] **Step 4: Accept interpolation snapshots in the controller mixin**

In both `apply_axis_*_calibration` methods, recognize the interpolation model,
convert the persisted lists to finite tuples, validate equal length and strict
ordering, then store:

```python
{
    "model": LINEAR_INTERPOLATION_MODEL,
    "steps_per_mm": float(calibration.steps_per_mm),
    "min": gcode_points[0],
    "max": gcode_points[-1],
    "gcode_points": gcode_points,
    "display_points": display_points,
}
```

- [ ] **Step 5: Run mapping, precision, and needle regressions and commit**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_axis_calibration.py tests/stage/test_controller_axis_needles.py tests/stage/test_precision_motion.py -q
```

Expected: all selected tests pass, including calibrated-limit rejection before
the first send.

```powershell
git add probe_station_gui/stage/axis_mapping.py probe_station_gui/stage/axis_coordinates.py tests/stage/test_axis_calibration.py tests/stage/test_controller_axis_needles.py tests/stage/test_precision_motion.py
git commit -m "feat: map axis coordinates by linear interpolation"
```

---

### Task 3: Asynchronous calibration file controls in the Axes tab

**Files:**
- Modify: `probe_station_gui/dialogs/settings/axis_settings.py`
- Test: `tests/ui/test_precision_approach_settings.py`

**Interfaces:**
- Consumes `load_axis_calibration_npz` and `ImportedAxisCalibration` from Task 1.
- Produces unchanged `AxisSettingsWidget.to_settings(settings)` public behavior with replace/reset support.

- [ ] **Step 1: Write failing UI layout and persistence tests**

Replace the read-only metadata test with assertions that A/Z expose a read-only
file edit, Browse, Reset, and status. Assert no form label has text `Path`,
`Curve source`, or `Fit error`.

Add a successful import test using a temporary real NPZ and Qt event polling:

```python
widget._start_calibration_import("Z", str(path))
deadline = time.monotonic() + 5.0
while widget._calibration_tasks_running and time.monotonic() < deadline:
    QApplication.processEvents()
    time.sleep(0.01)
assert widget._calibration_tasks_running == 0
widget.to_settings(settings)
assert settings.axis_z_calibration.model == "linear_interpolation"
assert settings.axis_z_calibration.calibration_file == str(path.resolve())
assert settings.axis_z_calibration.interpolation_gcode_mm == [0.0, 1.0, 2.0]
assert settings.axis_z_calibration.configured is True
```

Add invalid-import rollback, cancelled-dialog, and Reset tests. Add a direction
mismatch test proving calibration is unchecked and not persisted as configured.

- [ ] **Step 2: Run the UI tests and verify RED**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_precision_approach_settings.py -q
```

Expected: current widget has source/error labels, no file controls, and still
contains the precision preview row.

- [ ] **Step 3: Remove the precision preview row**

Remove `preview_label` from `_AxisPrecisionControls`, remove
`layout.addRow("Path", preview_label)`, `_update_preview`, and its signal
connections. `_set_precision_enabled` then enables only backlash and direction.

- [ ] **Step 4: Add non-blocking file import controls**

Use a `QRunnable` with a signal carrier:

```python
class _CalibrationImportSignals(QObject):
    finished = Signal(str, object, str)

class _CalibrationImportTask(QRunnable):
    @Slot()
    def run(self) -> None:
        try:
            imported = load_axis_calibration_npz(
                self._path,
                axis=self._axis,
                final_direction=self._final_direction,
            )
        except AxisCalibrationImportError as exc:
            self.signals.finished.emit(self._axis, None, str(exc))
        else:
            self.signals.finished.emit(self._axis, imported, "")
```

`Browse` calls `QFileDialog.getOpenFileName` with filter
`NumPy calibration (*.npz)`, then starts the task on
`QThreadPool.globalInstance()`. Disable Browse/Reset while loading. On success,
replace only the chosen axis clone, set model/path/points/direction/source,
check Enabled, and show `<N> points · <min>-<max> mm`. On failure, restore the
controls and show the error without changing the clone.

- [ ] **Step 5: Implement Reset and direction compatibility**

Reset sets `configured=False`, clears `calibration_file`, both point lists,
`interpolation_direction`, and `source`, then clears File/status. When a
direction combo changes, compare its data with a non-null imported direction;
on mismatch uncheck Enabled and show `Choose a curve for this direction.`

`to_settings` persists the updated clones and always clears legacy PNG source
metadata. A checkbox cannot enable an interpolation model without valid points.

- [ ] **Step 6: Run UI/settings integration tests and commit**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_precision_approach_settings.py tests/settings/test_axis_calibration_npz.py tests/settings/test_axis_calibration_config.py -q
```

Expected: all selected tests pass and the worker leaves no running tasks.

```powershell
git add probe_station_gui/dialogs/settings/axis_settings.py tests/ui/test_precision_approach_settings.py
git commit -m "feat: choose NPZ axis calibration in settings"
```

- [ ] **Step 7: Run full integration verification**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_axis_calibration_npz.py tests/settings/test_axis_calibration_config.py tests/stage/test_axis_calibration.py tests/stage/test_controller_axis_needles.py tests/stage/test_precision_approach.py tests/stage/test_precision_motion.py tests/stage/test_controller_click_move.py tests/ui/test_precision_approach_settings.py tests/route/test_measurement.py -q
```

Expected: all affected suites pass.

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
git diff --check
git status --short --branch
```

Expected: zero test failures, no whitespace errors, and only intentional
tracked changes.
