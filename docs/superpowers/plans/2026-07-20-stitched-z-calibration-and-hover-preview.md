# Stitched Z Calibration and Hover Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce one strict machine-local Z calibration from all three measured forward sections and make the embedded preview light-themed with continuously interpolated pointer coordinates.

**Architecture:** A dedicated offline stitcher owns knowledge of the three raw archive layouts, established seams, and offsets, then emits the same strict universal NPZ schema already consumed by the application. `AxisCalibrationPreview` owns only rendered curve state and pointer presentation; interpolation is a small pure function so hover behavior is deterministic and testable without stage hardware.

**Tech Stack:** Python 3.12, NumPy, PySide6, PyQtGraph, pytest.

## Global Constraints

- Do not modify any source calibration archive.
- Keep the generated stitched NPZ machine-local and ignored by Git.
- Use only the established seams `12.000` and `20.214` mm and offsets `8.661368914604154` and `13.56547962940159` mm.
- Save only scalar Unicode `axis` and strictly increasing finite one-dimensional `controller` and `physical` arrays.
- Apply the preview palette per widget; never change global PyQtGraph configuration.
- Hover inspection remains available when the saved file exists even if its `Enabled` checkbox is off; only the machine-position marker follows `Enabled`.
- Keep all file parsing and large-array processing outside periodic GUI callbacks.

---

### Task 1: Three-Section Z Stitcher

**Files:**
- Create: `scripts/prepare_stitched_z_calibration_npz.py`
- Create: `tests/scripts/test_prepare_stitched_z_calibration_npz.py`

**Interfaces:**
- Consumes: `longest_strictly_increasing_indices(values)` from `scripts.prepare_axis_calibration_npz`.
- Produces: `prepare_stitched_z_calibration(section1, section2, section3, destination, axis="Z") -> int` and a four-positional-argument CLI.

- [ ] **Step 1: Write failing synthetic seam and schema tests**

Create three tiny archives whose controller ranges overlap the two seams. Assert that section 1 contributes only `<12.0`, section 2 contributes `12.0..20.214`, section 3 contributes only `direction == 1` values `>20.214`, offsets are added exactly, the final LIS removes a seam conflict, and the saved keys are exactly `axis/controller/physical`:

```python
count = prepare_stitched_z_calibration(s1, s2, s3, output)
with np.load(output, allow_pickle=False) as data:
    assert set(data.files) == {"axis", "controller", "physical"}
    assert data["axis"].item() == "Z"
    assert np.all(np.diff(data["controller"]) > 0)
    assert np.all(np.diff(data["physical"]) > 0)
    assert 11.9 in data["controller"]
    assert 12.0 in data["controller"]
    assert 20.3 in data["controller"]
```

Add tests for missing raw keys, non-finite values, non-increasing controller data, fewer than two usable output points, and refusal to overwrite an existing destination.

- [ ] **Step 2: Run the new test and verify red**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/scripts/test_prepare_stitched_z_calibration_npz.py -q
```

Expected: collection fails because `scripts.prepare_stitched_z_calibration_npz` does not exist.

- [ ] **Step 3: Implement raw-section validation and stitching**

Use named constants and one loader per raw shape:

```python
SECTION_1_END_MM = 12.0
SECTION_2_END_MM = 20.214
SECTION_2_OFFSET_MM = 8.661368914604154
SECTION_3_OFFSET_MM = 13.56547962940159

controller = np.concatenate((
    s1_gcode[s1_gcode < SECTION_1_END_MM],
    s2_gcode[(s2_gcode >= SECTION_1_END_MM) & (s2_gcode <= SECTION_2_END_MM)],
    s3_gcode[(s3_direction == 1) & (s3_gcode > SECTION_2_END_MM)],
))
physical = np.concatenate((
    s1_indicator[s1_gcode < SECTION_1_END_MM],
    s2_indicator[s2_mask] + SECTION_2_OFFSET_MM,
    s3_indicator[s3_mask] + SECTION_3_OFFSET_MM,
))
selected = longest_strictly_increasing_indices(physical)
controller = controller[selected]
physical = physical[selected]
```

Validate every source array before slicing, validate both final arrays after selection, create the destination parent only after validation, refuse an existing destination, and save with `np.savez_compressed`.

- [ ] **Step 4: Add and test the CLI**

The CLI accepts `section1 section2 section3 destination --axis Z`, prints the final sample count, and exits nonzero on validation failure. Run the focused test and expect all cases to pass.

- [ ] **Step 5: Commit the stitcher**

```powershell
git add -f scripts/prepare_stitched_z_calibration_npz.py tests/scripts/test_prepare_stitched_z_calibration_npz.py
git commit -m "feat: stitch full Z calibration archive"
```

---

### Task 2: Light Preview and Interpolated Hover

**Files:**
- Modify: `probe_station_gui/dialogs/settings/axis_calibration_preview.py`
- Modify: `tests/ui/test_axis_calibration_preview.py`

**Interfaces:**
- Produces: `interpolated_curve_position(controller, physical, x) -> tuple[float, float] | None`, `AxisCalibrationPreview.set_hover_controller_value(x, visible=True)`, and persistent `hover_vertical_line`, `hover_horizontal_line`, and `hover_label` items.
- Preserves: `set_curve`, `clear_curve`, `set_current_position`, `set_outside_range`, `marker_item`, and `position_line` behavior.

- [ ] **Step 1: Write failing pure interpolation tests**

```python
assert interpolated_curve_position((0.0, 2.0), (1.0, 5.0), 0.5) == pytest.approx((0.5, 2.0))
assert interpolated_curve_position((0.0, 2.0), (1.0, 5.0), -0.1) is None
assert interpolated_curve_position((), (), 0.0) is None
```

Also assert both endpoints are accepted and invalid/mismatched arrays return `None`.

- [ ] **Step 2: Write failing widget palette and hover-state tests**

Assert `backgroundBrush().color().name()` is `#ffffff`, axis text pens are dark, and after `set_curve` plus `set_hover_controller_value(0.5)`:

```python
assert preview.hover_position == pytest.approx((0.5, 2.0))
assert preview.hover_vertical_line.isVisible()
assert preview.hover_horizontal_line.isVisible()
assert preview.hover_label.isVisible()
assert "X: 0.5 mm" in preview.hover_label.toPlainText()
assert "Y: 2 mm" in preview.hover_label.toPlainText()
```

Assert out-of-range input, `visible=False`, and `clear_curve()` hide all hover items without hiding the curve or changing `viewRange()`.

- [ ] **Step 3: Run the preview test and verify red**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_axis_calibration_preview.py -q
```

Expected: failure on the missing interpolation function, hover items, and white background.

- [ ] **Step 4: Apply a local light palette and create persistent hover items**

Set `PlotWidget.setBackground("#ffffff")`; configure left and bottom axes with dark pens/text pens, a subtle grid, and a dark view border. Create two non-movable dashed `InfiniteLine` items and a `TextItem` with dark text, translucent white fill, and gray border. Hide them initially.

- [ ] **Step 5: Store curve arrays and implement interpolation**

`set_curve` converts inputs to finite NumPy arrays and stores them without rebuilding on hover. The pure interpolation helper rejects unusable arrays or X outside the inclusive domain and otherwise returns `(x, float(np.interp(x, controller, physical)))`. `set_hover_controller_value` updates both guide values and label text while preserving the current view range.

- [ ] **Step 6: Connect rate-limited scene pointer motion**

Create one `pg.SignalProxy(self.plot_widget.scene().sigMouseMoved, rateLimit=60, slot=self._on_scene_mouse_moved)`. The slot verifies `plotItem.sceneBoundingRect().contains(scene_position)`, maps through `viewBox.mapSceneToView`, updates interpolated hover state, and hides it outside the plot/domain. Clamp label placement against the current `viewRange`; never call `autoRange` from a hover path.

- [ ] **Step 7: Run preview and settings regressions**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_axis_calibration_preview.py tests/ui/test_precision_approach_settings.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit preview behavior**

```powershell
git add probe_station_gui/dialogs/settings/axis_calibration_preview.py tests/ui/test_axis_calibration_preview.py
git commit -m "fix: make calibration preview readable"
```

---

### Task 3: Generate Local Full-Range Archive and Verify

**Files:**
- Produce locally, do not add: `calibrations/axis_z_spm6335_full_stitched_forward_20260505.npz`
- Modify only if regression gaps are found: focused implementation/test files from Tasks 1–2.

**Interfaces:**
- Consumes: Task 1 CLI and the three existing machine-local raw archives.
- Produces: one ignored universal NPZ loadable by the settings GUI.

- [ ] **Step 1: Hash all three source archives**

Record SHA-256 for the section-1 up, section-2 up, and section-3 precise archives before generation.

- [ ] **Step 2: Generate the stitched archive**

Run the new script with the three absolute source paths and destination `C:\Users\Public\code\probe_station_gui\calibrations\axis_z_spm6335_full_stitched_forward_20260505.npz`.

- [ ] **Step 3: Verify output and source preservation**

Reload with `allow_pickle=False`; require exactly `axis/controller/physical`, axis `Z`, 4114 samples for the current source data, controller range `0.020..23.400`, and strictly increasing arrays. Re-hash all source files and require hashes identical to Step 1. Confirm `git status --ignored` reports the generated archive only under ignored `calibrations/`.

- [ ] **Step 4: Run the complete hardware-free suite**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
```

Expected: zero failures.

- [ ] **Step 5: Inspect final state**

Run `git diff --check`, `git status --short`, the legacy-calibration reference scan, and `git log --oneline -6`. Commit only genuine regression fixes; never stage the generated archive.

---

## Self-Review Record

- Spec coverage: Task 1 covers exact seams, offsets, raw-source isolation, strict output schema, and validation; Task 2 covers the local light palette, interpolated hover label/guides, pointer leave behavior, live-marker independence, and constant-time item reuse; Task 3 covers real archive generation, source hashes, ignored status, and full regression.
- Placeholder scan: no deferred implementation markers or unspecified error-handling steps remain.
- Type consistency: the stitcher and hover helper names and signatures are identical at their producer and consumer steps.
