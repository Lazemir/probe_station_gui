# Axis Calibration Current Position Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the calibration preview's diamond and vertical position line with a small red circular marker and a compact two-column readout for raw and calibrated coordinates.

**Architecture:** `AxisCalibrationPreview` owns the presentation and formats nullable controller/physical values without changing the plot range. `AxisSettingsWidget` continues to obtain the cached machine coordinate and now passes the raw value through even when calibration is disabled or the value lies outside the curve.

**Tech Stack:** Python, PySide6 widgets, PyQtGraph, pytest.

## Global Constraints

- The current marker is a filled red circle with an 8-pixel diameter.
- The old orange current-position vertical line does not exist; pointer-hover guides remain unchanged.
- The readout is a stable two-column card immediately above the plot with `Controller <axis>` and `Physical <axis>` headers.
- Missing values render as an em dash; disabling calibration keeps the raw controller value but hides the marker and calibrated value.
- No hardware polling, auto-ranging, or plot rebuild is introduced by position updates.

---

### Task 1: Position card, marker, and settings propagation

**Files:**
- Modify: `probe_station_gui/dialogs/settings/axis_calibration_preview.py`
- Modify: `probe_station_gui/dialogs/settings/axis_settings.py`
- Test: `tests/ui/test_axis_calibration_preview.py`
- Test: `tests/ui/test_precision_approach_settings.py`

**Interfaces:**
- Consumes: `AxisSettingsWidget._position_source.latest_machine_position() -> tuple[float, ...] | None` and the existing `controller_to_physical(curve, controller_value) -> float` mapping.
- Produces: `AxisCalibrationPreview.set_current_position(controller: float | None, physical: float | None, *, visible: bool) -> None`, with readouts updated independently of marker visibility; `AxisCalibrationPreview.set_outside_range(outside: bool, *, controller: float | None = None) -> None`, preserving the raw value when available.

- [ ] **Step 1: Write failing widget tests for the card and marker**

Add focused assertions that construct the real widget and require the new public presentation attributes:

```python
def test_current_position_card_formats_raw_and_calibrated_values(qtbot) -> None:
    preview = AxisCalibrationPreview("Z")
    qtbot.addWidget(preview)

    preview.set_current_position(5.441, 5.439, visible=True)

    assert preview.controller_heading.text() == "Controller Z"
    assert preview.physical_heading.text() == "Physical Z"
    assert preview.controller_value.text() == "5.441 mm"
    assert preview.physical_value.text() == "5.439 mm"
    assert preview.marker_item.opts["symbol"] == "o"
    assert preview.marker_item.opts["size"] == 8
    assert preview.marker_item.opts["brush"].color().name() == "#e53935"
    assert not hasattr(preview, "position_line")


def test_hidden_marker_keeps_raw_value_and_clears_calibrated_value(qtbot) -> None:
    preview = AxisCalibrationPreview("Z")
    qtbot.addWidget(preview)

    preview.set_current_position(5.441, None, visible=False)

    assert preview.controller_value.text() == "5.441 mm"
    assert preview.physical_value.text() == "—"
    assert not preview.marker_item.isVisible()
```

Update the existing marker, clear, leave-hover, and settings tests to assert the absence of `position_line`. Add settings-level assertions that disabled and out-of-range calibration preserve the raw value and show `Physical <axis>` as `—`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_axis_calibration_preview.py tests/ui/test_precision_approach_settings.py -q
```

Expected: FAIL because the card labels do not exist, the marker is still a yellow diamond, and `position_line` still exists.

- [ ] **Step 3: Implement the card and small red marker**

In `AxisCalibrationPreview.__init__`, insert a stable `QFrame`/`QGridLayout` card before `plot_widget`. Create `controller_heading`, `physical_heading`, `controller_value`, and `physical_value` labels, initialize both values to `—`, and style the card locally with subdued headers, stronger values, and a quiet border. Configure the marker as:

```python
self.marker_item = pg.ScatterPlotItem(
    [],
    [],
    symbol="o",
    size=8,
    pen=pg.mkPen("#b71c1c", width=1),
    brush=pg.mkBrush("#e53935"),
)
```

Delete creation, addition, hiding, positioning, and visibility updates for `position_line`. Add a private formatter:

```python
def _format_position_value(self, value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "—"
    return f"{value:.6g} {self.unit}"
```

At the start of `set_current_position`, update both value labels. Keep `current_position` and the marker populated only when `visible` is true and both finite coordinates are present. Extend `set_outside_range` with `controller` and pass it to `set_current_position(controller, None, visible=False)`.

- [ ] **Step 4: Propagate raw coordinates in every settings state**

In `AxisSettingsWidget._refresh_current_position`, read and validate the cached machine-position tuple before checking the calibration checkbox. Pass the raw coordinate to the preview in disabled and out-of-domain paths:

```python
if not isinstance(machine_position, (tuple, list)) or index >= len(machine_position):
    preview.set_outside_range(False)
    return
controller_value = float(machine_position[index])
if not self._calibration_checkboxes[axis].isChecked():
    preview.set_outside_range(False, controller=controller_value)
    return
```

For `CalibrationOutOfDomain`, call `preview.set_outside_range(True, controller=controller_value)`. The successful path remains `preview.set_current_position(controller_value, physical_value, visible=True)`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_axis_calibration_preview.py tests/ui/test_precision_approach_settings.py -q
```

Expected: all tests pass with no warnings or errors.

- [ ] **Step 6: Run lint and complete hardware-free regression suite**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui/dialogs/settings/axis_calibration_preview.py probe_station_gui/dialogs/settings/axis_settings.py tests/ui/test_axis_calibration_preview.py tests/ui/test_precision_approach_settings.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
git diff --check
```

Expected: Ruff exits 0, the complete suite passes, and `git diff --check` prints no errors.

- [ ] **Step 7: Commit the implementation**

```powershell
git add probe_station_gui/dialogs/settings/axis_calibration_preview.py probe_station_gui/dialogs/settings/axis_settings.py tests/ui/test_axis_calibration_preview.py tests/ui/test_precision_approach_settings.py docs/superpowers/plans/2026-07-20-axis-calibration-current-position-card.md
git commit -m "feat: refine calibration position preview"
```
