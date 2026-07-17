# Axis Settings Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace separate axis-calibration and precision-approach tabs with one `Axes` tab and rebuild the stage-coordinate legend as two aligned semantic groups.

**Architecture:** Add a focused `AxisSettingsWidget` containing a left-hand axis selector and one persistent right-hand page per axis. The widget edits existing settings value objects through the current collection path. Separately, replace the coordinate panel's rich-text legend with small real widgets arranged by field state and accuracy; neither change alters persisted configuration or stage behavior.

**Tech Stack:** Python 3.12, PySide6, pytest, existing Settings value objects.

## Global Constraints

- Work directly in `C:\Users\Public\code\probe_station_gui` on `codex/alignment-backlash-approach`.
- Run Python through `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.
- Keep `Coordinates` as a separate settings tab with no behavior changes.
- Do not change persisted settings keys, defaults, stage-controller behavior, or calibration coefficients.
- Use `Axes` as the canonical tab label; accept legacy initial-tab names.
- Keep GUI copy short and do not use ellipses in menu or tab labels.
- Preserve the existing coordinate-state colors, accuracy-stripe colors, and backlash tooltip semantics.
- Write and observe failing tests before production changes.

---

### Task 1: Build the axis-oriented settings widget

**Files:**

- Create: `probe_station_gui/dialogs/settings/axis_settings.py`
- Modify: `probe_station_gui/dialogs/settings/__init__.py`
- Modify: `tests/ui/test_precision_approach_settings.py`

**Interfaces:**

- Consumes: `AxisACalibrationSettings`, `AxisZCalibrationSettings`, `PrecisionApproachSettings`, and `Settings`.
- Produces: `AxisSettingsWidget(axis_a_calibration, axis_z_calibration, approach_settings, parent=None)` and `AxisSettingsWidget.to_settings(settings)`.
- Test-facing controls: `_axis_list`, `_pages`, `_rows`, `_calibration_messages`, and `_calibration_checkboxes`, keyed by the axis names `X`, `Y`, `Z`, `A`, `B`, `C` where applicable.

- [ ] **Step 1: Replace the aggregate precision-widget tests with failing axis-page tests**

```python
def test_axis_settings_selector_and_per_axis_content() -> None:
    settings = Settings()
    widget = AxisSettingsWidget(
        settings.axis_a_calibration,
        settings.axis_z_calibration,
        settings.precision_approach,
    )

    assert [widget._axis_list.item(i).text() for i in range(6)] == [
        "X", "Y", "Z", "A", "B", "C"
    ]
    assert widget.selected_axis() == "Z"
    assert widget._rows["A"].backlash_spin.suffix() == " mm"
    assert widget._rows["B"].backlash_spin.suffix() == " °"
    assert widget._calibration_messages["X"].text() == "No calibration curve for this axis."
    assert "A" in widget._calibration_checkboxes
    assert "Z" in widget._calibration_checkboxes
```

Add a second test that changes B backlash/direction, switches to X and back, calls `to_settings()`, and asserts that the unapplied B values survive and are stored. Add a third test that verifies the A and Z source/error labels and enabled checkboxes preserve the cloned calibration metadata.

- [ ] **Step 2: Run the new tests and verify the missing widget failure**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_precision_approach_settings.py -q
```

Expected: collection fails because `probe_station_gui.dialogs.settings.axis_settings` or `AxisSettingsWidget` does not exist.

- [ ] **Step 3: Implement the persistent per-axis pages**

Create `AxisSettingsWidget` with a `QListWidget` on the left and a `QStackedWidget` on the right. Create all six pages once. Store precision controls in an `_AxisPrecisionControls` dataclass:

```python
@dataclass(frozen=True)
class _AxisPrecisionControls:
    enabled_checkbox: QCheckBox
    backlash_spin: QDoubleSpinBox
    direction_combo: QComboBox
    preview_label: QLabel
```

For each page, add a `Precision approach` group with the current enable/backlash/direction/preview behavior. Add a `Coordinate calibration` group: A and Z get the existing enable/source/RMSE/max controls; other axes get exactly `No calibration curve for this axis.` Use `QListWidget.setCurrentRow(2)` for the default Z selection. Implement:

```python
def selected_axis(self) -> str:
    item = self._axis_list.currentItem()
    return item.text() if item is not None else "Z"

def to_settings(self, settings: Settings) -> None:
    settings.precision_approach = PrecisionApproachSettings({
        axis: PrecisionApproachProfile(
            row.enabled_checkbox.isChecked(),
            row.backlash_spin.value(),
            int(row.direction_combo.currentData() or 1),
        )
        for axis, row in self._rows.items()
    })
    axis_a = self._axis_a_calibration.clone()
    axis_a.configured = self._calibration_checkboxes["A"].isChecked()
    settings.axis_a_calibration = axis_a
    axis_z = self._axis_z_calibration.clone()
    axis_z.configured = self._calibration_checkboxes["Z"].isChecked()
    settings.axis_z_calibration = axis_z
```

Export `AxisSettingsWidget` from `probe_station_gui.dialogs.settings`. Populate
`_pages`, `_calibration_messages`, and `_calibration_checkboxes` as the pages are
created so the UI state can be verified without searching the widget tree.

- [ ] **Step 4: Run the widget tests and verify they pass**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_precision_approach_settings.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Commit the standalone widget**

```powershell
git add probe_station_gui/dialogs/settings/axis_settings.py probe_station_gui/dialogs/settings/__init__.py tests/ui/test_precision_approach_settings.py
git commit -m "feat: add axis-oriented settings widget"
```

### Task 2: Replace the two SettingsDialog tabs

**Files:**

- Modify: `probe_station_gui/dialogs/settings_dialog.py`
- Modify: `probe_station_gui/dialogs/settings/__init__.py`
- Delete: `probe_station_gui/dialogs/settings/precision_approach.py`
- Modify: `tests/ui/test_precision_approach_settings.py`
- Test: `tests/ui/test_settings_objectives_coordinates.py`

**Interfaces:**

- Consumes: `AxisSettingsWidget` from Task 1.
- Produces: `SettingsDialog._axes_tab`; canonical `Axes` tab; legacy initial-tab aliases.

- [ ] **Step 1: Add failing dialog-integration tests**

```python
@pytest.mark.parametrize("initial_tab", ["Axes", "Axis Calibration", "Precision approach"])
def test_settings_dialog_uses_one_axes_tab_and_legacy_aliases(initial_tab: str) -> None:
    dialog = SettingsDialog(Settings(), initial_tab=initial_tab)
    labels = [dialog._tabs.tabText(i) for i in range(dialog._tabs.count())]
    assert "Coordinates" in labels
    assert "Axes" in labels
    assert "Axis Calibration" not in labels
    assert "Precision approach" not in labels
    assert dialog._tabs.currentWidget() is dialog._axes_tab
```

Extend the collection test to edit X precision and disable A calibration through `dialog._axes_tab`, call `_collect_settings()`, and assert both changes appear in `result_settings()` while A calibration coefficients/source remain unchanged.

- [ ] **Step 2: Run the integration tests and verify the old tabs fail expectations**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_precision_approach_settings.py tests/ui/test_settings_objectives_coordinates.py -q
```

Expected: failures report missing `_axes_tab` and old `Axis Calibration` / `Precision approach` labels.

- [ ] **Step 3: Wire the combined widget into SettingsDialog**

Import `AxisSettingsWidget`, construct only `self._axes_tab`, and add it as `Axes` immediately after `Coordinates`. Remove construction, tab insertion, and collection calls for `_axis_calibration_tab` and `_precision_approach_tab`. Replace them with:

```python
self._axes_tab = AxisSettingsWidget(
    self._settings.axis_a_calibration,
    self._settings.axis_z_calibration,
    self._settings.precision_approach,
    self,
)
self._tabs.addTab(self._axes_tab, "Axes")
```

Collect with `self._axes_tab.to_settings(self._settings)`. Normalize the requested initial tab before scanning:

```python
requested_tab = (initial_tab or "").strip().lower()
if requested_tab in {"axis calibration", "precision approach"}:
    requested_tab = "axes"
```

Delete `AxisCalibrationSettingsWidget` from `settings_dialog.py` and its now-unused calibration imports. Leave `CoordinateSystemSettingsWidget` untouched. Delete the now-unused aggregate `precision_approach.py` widget and remove `PrecisionApproachSettingsWidget` from `probe_station_gui.dialogs.settings.__init__`; the precision value-object module under `probe_station_gui.settings` remains unchanged.

- [ ] **Step 4: Run the focused Settings UI tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_precision_approach_settings.py tests/ui/test_settings_objectives_coordinates.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit dialog integration**

```powershell
git add probe_station_gui/dialogs/settings_dialog.py probe_station_gui/dialogs/settings/__init__.py probe_station_gui/dialogs/settings/precision_approach.py tests/ui/test_precision_approach_settings.py tests/ui/test_settings_objectives_coordinates.py
git commit -m "refactor: combine axis settings tabs"
```

### Task 3: Rebuild the coordinate legend with aligned widgets

**Files:**

- Modify: `probe_station_gui/views/stage_position_panel.py`
- Modify: `tests/ui/test_stage_position_panel.py`

**Interfaces:**

- Consumes: existing color constants `EDITED_BACKGROUND`, `EXACT_STRIPE`, and `APPROXIMATE_STRIPE` plus the current homed/unhomed/limit colors.
- Produces: `StagePositionPanel.legend_widget`, `_legend_titles`, and `_legend_groups`; each legend group contains `_LegendItem(swatch, label)` records.

- [ ] **Step 1: Replace the rich-text assertion with failing structure tests**

```python
def test_position_legend_uses_aligned_semantic_groups(qt_app: QApplication) -> None:
    panel = StagePositionPanel(("X",))

    assert tuple(panel._legend_titles) == ("Field state:", "Accuracy:")
    assert tuple(item.label.text() for item in panel._legend_groups["Field state:"]) == (
        "Homed", "Unhomed", "Limit", "Edited"
    )
    assert tuple(item.label.text() for item in panel._legend_groups["Accuracy:"]) == (
        "Exact", "Approximate"
    )
    state_sizes = {item.swatch.size() for item in panel._legend_groups["Field state:"]}
    accuracy_sizes = {item.swatch.size() for item in panel._legend_groups["Accuracy:"]}
    assert len(state_sizes) == 1
    assert len(accuracy_sizes) == 1
    assert "backlash" in panel.legend_widget.toolTip().lower()
```

Also assert that no legend child label uses rich text or a 9 px stylesheet.

- [ ] **Step 2: Run the legend test and verify the old QLabel API fails**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_stage_position_panel.py::test_position_legend_uses_aligned_semantic_groups -q
```

Expected: failure reports missing `_legend_titles`, `_legend_groups`, or `legend_widget`.

- [ ] **Step 3: Implement the two-group widget legend**

Add a private record and build the legend with nested horizontal layouts:

```python
@dataclass(frozen=True)
class _LegendItem:
    swatch: QFrame
    label: QLabel
```

Create a `self._legend_widget` with a zero-margin `QHBoxLayout`, add the title
`Field state:`, four square 10×10 swatches and labels, then a `QFrame.VLine`,
then `Accuracy:` with two 14×4 stripe swatches and labels. Add every widget with
`Qt.AlignVCenter`, use 4 px inside item pairs and 10 px between items, and add a
stretch at the end. Store titles and records in `_legend_titles` and
`_legend_groups`. Apply the existing backlash tooltip to the legend widget.
Remove the 9 px stylesheet and rich-text square/stripe glyphs.

Expose:

```python
@property
def legend_widget(self) -> QWidget:
    return self._legend_widget
```

- [ ] **Step 4: Run all stage-position panel tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_stage_position_panel.py -q
```

Expected: all tests pass; coordinate fill and stripe styling tests remain unchanged.

- [ ] **Step 5: Commit the legend cleanup**

```powershell
git add probe_station_gui/views/stage_position_panel.py tests/ui/test_stage_position_panel.py
git commit -m "style: align coordinate legend"
```

### Task 4: Regression verification and cleanup

**Files:**

- Test: `tests/settings/test_precision_approach_settings_model.py`
- Test: `tests/settings/test_axis_calibration_config.py`
- Test: `tests/ui/test_precision_approach_settings.py`

**Interfaces:**

- Consumes: completed `AxisSettingsWidget` and SettingsDialog integration.
- Produces: no new runtime interface; verifies schema compatibility and removes the unused legacy widget module.

- [ ] **Step 1: Prove the legacy widget has no runtime consumers**

Run:

```powershell
rg -n "PrecisionApproachSettingsWidget|dialogs\.settings\.precision_approach" probe_station_gui tests -g "*.py"
```

Expected: no results. The similarly named value-object module under `probe_station_gui.settings` is intentionally retained.

- [ ] **Step 2: Run settings model and focused UI regressions**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_precision_approach_settings_model.py tests/settings/test_axis_calibration_config.py tests/settings/test_section_parsing.py tests/ui/test_precision_approach_settings.py tests/ui/test_settings_objectives_coordinates.py -q
```

Expected: all tests pass, proving the JSON-facing models are unchanged.

- [ ] **Step 3: Run the complete suite**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
```

Expected: all tests pass. Local calibration `.npz` files are available in this main checkout.

- [ ] **Step 4: Inspect branch hygiene**

Run:

```powershell
git diff --check
git status --short
git diff --stat HEAD~2..HEAD
```

Expected: no whitespace errors or unrelated files; only the axis-settings UI, its tests, and documentation changed.

- [ ] **Step 5: Commit only if verification required a correction**

When a failing regression required a narrowly scoped correction, stage only that correction and its test:

```powershell
git add probe_station_gui/dialogs/settings/axis_settings.py probe_station_gui/dialogs/settings_dialog.py probe_station_gui/views/stage_position_panel.py tests/ui/test_precision_approach_settings.py tests/ui/test_settings_objectives_coordinates.py tests/ui/test_stage_position_panel.py
git commit -m "test: verify combined axis settings"
```

When verification required no source change, do not create an empty commit.
