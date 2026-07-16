# KLayout-Style Vector Tool UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Guide, Ruler, and Array direction picking follow KLayout angle constraints, remove persistent guide-point clutter, and make `Esc` return drawing tools to Select without deleting completed work.

**Architecture:** A Qt-free helper in the existing selection geometry module projects endpoints onto KLayout's allowed directions. The plot pane captures Shift/Ctrl with each local or asynchronous snap action and constrains Guide; the navigator panel consumes the same boolean snapshot for Ruler and Array state. Existing public hover reporting remains one-argument, while a narrow internal signal carries modifier flags to the embedded navigator.

**Tech Stack:** Python 3.11+, PySide6, pyqtgraph, dataclasses, pytest.

## Global Constraints

- KLayout modifier mapping is exact: none unrestricted; Shift horizontal/vertical; Ctrl horizontal/vertical/45-degree; Shift+Ctrl unrestricted.
- Geometry snap arbitration happens before angle projection; preview and commit use the same projection.
- Idle Markup draws dashed guide segments without permanent endpoint, midpoint, or intersection symbols; snap candidates remain unchanged.
- `Esc` cancels unfinished state, returns Point/Guide/Ruler/Array to Select, and preserves completed points, guides, rulers, and arrays.
- The design drawing tool is `Ruler`/`ruler`; the route-measurement `Measure` control and safety semantics do not change.
- Late asynchronous snap results cancelled by `Esc` do nothing.
- KLayout raster, minimap, source GDS, markup persistence, route Pause/Resume/Interrupt, autofocus, contact, serial safety, and stage movement are unchanged.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python command.

---

## File Map

- `probe_station_gui/design/selection_geometry.py`: Qt-free KLayout endpoint projection.
- `probe_station_gui/design/klayout_types.py`: modifier snapshot on a correlated pending click.
- `probe_station_gui/views/design_plot_pane.py`: quiet guide overlay, canonical `ruler` token, modifier capture, Guide projection, and cancellation of pending tool snaps.
- `probe_station_gui/views/design_navigator_panel.py`: Ruler label/token, Ruler/Array projection, internal modifier-aware hover path, and non-destructive `Esc` transition.
- `tests/design/test_selection_geometry.py`: modifier mapping and projection edge cases.
- `tests/ui/test_design_plot_selection.py`: quiet Markup, Guide projection, canonical Ruler, and window escape behavior.
- `tests/ui/test_design_plot_klayout.py`: delayed hover/click modifier snapshots and cancellation.
- `tests/ui/test_design_navigator_panel.py`: Ruler/Array preview and commit plus non-destructive tool cancellation.

---

### Task 1: Qt-Free KLayout Angle Projection

**Files:**
- Modify: `probe_station_gui/design/selection_geometry.py`
- Modify: `tests/design/test_selection_geometry.py`

**Interfaces:**
- Consumes: `Point2D`, two booleans named `shift` and `control`.
- Produces: `constrain_vector_endpoint(anchor: Point2D, proposed: Point2D, *, shift: bool, control: bool) -> Point2D`.

- [ ] **Step 1: Write failing mapping and projection tests**

```python
@pytest.mark.parametrize(
    ("proposed", "shift", "control", "expected"),
    [
        ((3.0, 2.0), False, False, (3.0, 2.0)),
        ((3.0, 2.0), True, False, (3.0, 0.0)),
        ((2.0, 3.0), True, False, (0.0, 3.0)),
        ((4.0, 1.0), False, True, (4.0, 0.0)),
        ((4.0, 3.0), False, True, (3.5, 3.5)),
        ((4.0, -3.0), False, True, (3.5, -3.5)),
        ((3.0, 2.0), True, True, (3.0, 2.0)),
        ((0.0, 0.0), False, True, (0.0, 0.0)),
    ],
)
def test_klayout_angle_constraint_mapping(proposed, shift, control, expected):
    assert constrain_vector_endpoint(
        (0.0, 0.0), proposed, shift=shift, control=control
    ) == expected

def test_angle_constraint_is_relative_to_anchor_and_breaks_ties_stably():
    assert constrain_vector_endpoint(
        (10.0, -5.0), (11.0, -4.0), shift=True, control=False
    ) == (11.0, -5.0)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_selection_geometry.py -q`

Expected: FAIL because `constrain_vector_endpoint` cannot be imported.

- [ ] **Step 3: Implement deterministic nearest-line projection**

```python
def constrain_vector_endpoint(
    anchor: Point2D,
    proposed: Point2D,
    *,
    shift: bool,
    control: bool,
) -> Point2D:
    origin = (float(anchor[0]), float(anchor[1]))
    endpoint = (float(proposed[0]), float(proposed[1]))
    if bool(shift) == bool(control):
        return endpoint
    dx = endpoint[0] - origin[0]
    dy = endpoint[1] - origin[1]
    candidates = [(origin[0] + dx, origin[1]), (origin[0], origin[1] + dy)]
    if control:
        positive = (dx + dy) * 0.5
        negative = (dx - dy) * 0.5
        candidates.extend([
            (origin[0] + positive, origin[1] + positive),
            (origin[0] + negative, origin[1] - negative),
        ])
    return min(candidates, key=lambda point: (
        (point[0] - endpoint[0]) ** 2 + (point[1] - endpoint[1]) ** 2
    ))
```

Candidate order makes exact ties deterministic: horizontal, vertical, +45°, then -45°.

- [ ] **Step 4: Run the pure geometry suite GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_selection_geometry.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the geometry helper**

```powershell
git add -- probe_station_gui/design/selection_geometry.py tests/design/test_selection_geometry.py
git commit -m "feat: add KLayout angle constraints"
```

---

### Task 2: Quiet Markup and Canonical Ruler Naming

**Files:**
- Modify: `probe_station_gui/views/design_plot_pane.py`
- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Modify: `tests/ui/test_design_plot_selection.py`
- Modify: `tests/ui/test_design_navigator_panel.py`

**Interfaces:**
- Consumes: existing `_tool_sketch_segments` and `_markup_snap_candidates`.
- Produces: idle guide line data only; canonical plot/panel token `ruler`; toolbar text `Ruler`.

- [ ] **Step 1: Change UI tests to require quiet guide data and Ruler naming**

```python
def test_markup_overlay_draws_only_guides_but_keeps_all_snap_candidates(pane, tmp_path):
    pane.set_markup(_markup(tmp_path))
    for item in (
        pane._tool_sketch_point_item,
        pane._tool_sketch_midpoint_item,
        pane._tool_sketch_intersection_item,
    ):
        x_data, y_data = item.getData()
        assert len(x_data) == len(y_data) == 0
    assert {candidate.mode for candidate in pane._markup_snap_candidates} == {
        "guide_end", "guide_center", "guide_intersection"
    }

def test_design_ruler_uses_canonical_label_and_token(qt_app):
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._update_enabled_state()
    emitted = []
    panel.active_design_tool_changed.connect(emitted.append)
    panel._ruler_tool_button.click()
    assert panel._ruler_tool_button.text() == "Ruler"
    assert emitted[-1] == "ruler"
    assert panel._route_run_button.text() == "Measure"
```

Add the canonical-token plot assertion:

```python
pane.set_active_design_tool("ruler")
assert pane.active_design_tool == "ruler"
with pytest.raises(ValueError, match="Unknown design tool"):
    pane.set_active_design_tool("measure")
```

- [ ] **Step 2: Run focused UI tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_selection.py tests/ui/test_design_navigator_panel.py -q`

Expected: FAIL because marker data is populated, toolbar text is `Measure`, panel emits `measure`, and the pane accepts only `measure`.

- [ ] **Step 3: Clear marker items while retaining candidates and guide lines**

At the start of `_redraw_tool_sketch`, always clear the three permanent marker items:

```python
self._tool_sketch_point_item.setData([], [])
self._tool_sketch_midpoint_item.setData([], [])
self._tool_sketch_intersection_item.setData([], [])
```

Build only `line_x` and `line_y` from committed segments plus a two-point active Guide preview. Remove endpoint, midpoint, and intersection `setData` population. Do not change `guide_snap_candidates` or `_best_markup_snap`.

- [ ] **Step 4: Rename only the design drawing tool**

```python
self._ruler_tool_button = self._make_tool_button(
    self._tool_toolbar, "Ruler", self._make_tool_icon("ruler"), "Ruler"
)
self.active_design_tool_changed.emit(tool)
```

In `_DesignPlotPane.set_active_design_tool`, replace `measure` with `ruler` in both the accepted set and crosshair-cursor set. Leave `_route_run_button = QPushButton("Measure", route_group)` unchanged.

- [ ] **Step 5: Run focused UI tests GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_selection.py tests/ui/test_design_navigator_panel.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the visual and naming refinement**

```powershell
git add -- probe_station_gui/views/design_plot_pane.py probe_station_gui/views/design_navigator_panel.py tests/ui/test_design_plot_selection.py tests/ui/test_design_navigator_panel.py
git commit -m "fix: quiet design markup overlays"
```

---

### Task 3: Capture Modifier Snapshots and Constrain Guide

**Files:**
- Modify: `probe_station_gui/design/klayout_types.py`
- Modify: `probe_station_gui/views/design_plot_pane.py`
- Modify: `tests/ui/test_design_plot_selection.py`
- Modify: `tests/ui/test_design_plot_klayout.py`
- Modify: `tests/design/test_click_navigation.py`

**Interfaces:**
- Consumes: `constrain_vector_endpoint`, Qt keyboard modifiers, existing correlated snap IDs.
- Produces: `PendingClick.shift_constraint`, `PendingClick.control_constraint`, `tool_hover_snap_changed(SnapResult | None, bool, bool)`, and modifier-aware five-argument `route_pick_requested`.

- [ ] **Step 1: Write failing Guide preview/commit tests**

```python
def test_shift_constrains_guide_preview_and_commit_to_same_endpoint(pane):
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    emitted = []
    pane.guide_requested.connect(lambda start, end: emitted.append((start, end)))
    pane._execute_click_action("guide_point", (), SnapResult((1.0, 2.0), "free", 0.0))
    result = SnapResult((6.0, 4.0), "vertex", 0.1)
    pane._set_hover_snap(result, shift=True, control=False)
    assert pane._tool_sketch_points == [(1.0, 2.0), (6.0, 2.0)]
    pane._execute_click_action("guide_point", (), result, shift=True, control=False)
    assert emitted == [((1.0, 2.0), (6.0, 2.0))]
```

Cover the other modifier layouts with the same state machine:

```python
@pytest.mark.parametrize(
    ("shift", "control", "raw", "expected"),
    [
        (False, True, (6.0, 6.0), (5.5, 6.5)),
        (True, True, (6.0, 4.0), (6.0, 4.0)),
    ],
)
def test_guide_uses_ctrl_diagonal_and_shift_ctrl_free(
    pane, shift, control, raw, expected
):
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    pane._accept_guide_point((1.0, 2.0))
    result = SnapResult(raw, "vertex", 0.1)
    pane._set_hover_snap(result, shift=shift, control=control)
    assert pane._tool_sketch_points[-1] == expected
```

- [ ] **Step 2: Write failing delayed-snap and cancellation tests**

```python
def test_file_backed_click_keeps_originating_modifier_snapshot(pane, tmp_path):
    pane.set_document(_document(tmp_path / "layout.gds"))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    pane._accept_guide_point((0.0, 0.0))
    pane._submit_file_backed_click(
        "guide_point", (4.0, 3.0), modifiers=Qt.ControlModifier
    )
    request = pane._snap_worker.click_requests[-1]
    pending = pane._pending_clicks[request.request_id]
    assert (pending.shift_constraint, pending.control_constraint) == (False, True)

def test_escape_discards_late_file_backed_tool_click(pane, tmp_path):
    pane.set_document(_document(tmp_path / "layout.gds"))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("point")
    emitted = []
    pane.point_requested.connect(lambda x, y: emitted.append((x, y)))
    pane._submit_file_backed_click("point", (4.0, 3.0))
    request = pane._snap_worker.click_requests[-1]
    pane.cancel_active_interaction()
    pane._snap_worker.snap_ready.emit(
        SnapResponse(
            request_id=request.request_id,
            config_generation=request.config.generation,
            raw_point=request.point,
            result=SnapResult((4.0, 3.0), "vertex", 0.1),
            elapsed_ms=1.0,
            shapes_inspected=1,
            purpose="click",
        )
    )
    assert emitted == []
```

Exercise the delayed hover path explicitly:

```python
events = []
pane.tool_hover_snap_changed.connect(
    lambda result, shift, control: events.append((result, shift, control))
)
pane._submit_file_backed_hover((4.0, 3.0), modifiers=Qt.ControlModifier)
request = pane._snap_worker.hover_requests[-1]
pane._snap_worker.snap_ready.emit(
    SnapResponse(
        request_id=request.request_id,
        config_generation=request.config.generation,
        raw_point=request.point,
        result=SnapResult((4.0, 3.0), "vertex", 0.1),
        elapsed_ms=1.0,
        shapes_inspected=1,
        purpose="hover",
    )
)
assert events[-1][1:] == (False, True)
```

Prove a late hover cannot recreate a Guide/Ruler/Array preview after `Esc`:

```python
pane._submit_file_backed_hover((8.0, 7.0), modifiers=Qt.ShiftModifier)
cancelled = pane._snap_worker.hover_requests[-1]
events.clear()
pane.cancel_active_interaction()
pane._snap_worker.snap_ready.emit(
    SnapResponse(
        request_id=cancelled.request_id,
        config_generation=cancelled.config.generation,
        raw_point=cancelled.point,
        result=SnapResult((8.0, 7.0), "vertex", 0.1),
        elapsed_ms=1.0,
        shapes_inspected=1,
        purpose="hover",
    )
)
assert events == []
```

- [ ] **Step 3: Run focused tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py tests/design/test_click_navigation.py -q`

Expected: FAIL because modifier parameters, pending fields, internal signal, and pending-action cancellation do not exist.

- [ ] **Step 4: Add correlated boolean fields and modifier conversion**

```python
@dataclass(frozen=True)
class PendingClick:
    request_id: int
    config_generation: int
    action: str
    raw_point: Point2D
    payload: tuple[object, ...] = ()
    markup_result: SnapResult | None = None
    markup_generation: int = 0
    shift_constraint: bool = False
    control_constraint: bool = False

@staticmethod
def _constraint_flags(modifiers: Qt.KeyboardModifiers) -> tuple[bool, bool]:
    return (
        bool(modifiers & Qt.ShiftModifier),
        bool(modifiers & Qt.ControlModifier),
    )
```

Use `getattr(event, "modifiers", lambda: Qt.NoModifier)()` in `_on_mouse_clicked` so existing lightweight test events remain valid.

- [ ] **Step 5: Thread the snapshot through local and file-backed paths**

Change `_pending_hover_markup` values to `(markup_result, shift, control)` tuples. Capture `QApplication.keyboardModifiers()` when `_flush_hover_snap` submits a hover. Store click booleans in `PendingClick`. The modifier-aware hover method is:

```python
def _set_hover_snap(
    self,
    snap_result: SnapResult | None,
    *,
    shift: bool = False,
    control: bool = False,
) -> None:
    if snap_result is None:
        self._latest_hover_request_id = 0
    self._hover_snap = snap_result
    if self._active_design_tool == "guide" and self._guide_anchor is not None:
        if snap_result is None:
            self._tool_sketch_points = [self._guide_anchor]
        else:
            endpoint = constrain_vector_endpoint(
                self._guide_anchor,
                snap_result.point,
                shift=shift,
                control=control,
            )
            self._tool_sketch_points = [self._guide_anchor, endpoint]
        self._redraw_tool_sketch()
    self._redraw_hover()
    self.hover_snap_changed.emit(snap_result)
    self.tool_hover_snap_changed.emit(snap_result, bool(shift), bool(control))
```

Extend `_execute_click_action` with the same keyword booleans. Before its existing signal dispatch, constrain `(x_value, y_value)` only when `action == "guide_point"` and `_guide_anchor is not None`; emit route picks with `(mode, x_value, y_value, bool(shift), bool(control))`.

Emit existing `hover_snap_changed(snap_result)` unchanged and add:

```python
tool_hover_snap_changed = Signal(object, bool, bool)
self.tool_hover_snap_changed.emit(snap_result, bool(shift), bool(control))
```

Emit route picks as `(mode, x, y, shift, control)`. Apply `constrain_vector_endpoint` only when Guide already has an anchor, both in preview and second-click commit.

- [ ] **Step 6: Invalidate only pending drawing-tool actions on cancel**

```python
def _cancel_pending_tool_snaps(self) -> None:
    self._latest_hover_request_id = 0
    self._pending_hover_markup.clear()
    self._pending_clicks = {
        request_id: pending
        for request_id, pending in self._pending_clicks.items()
        if pending.action not in {"point", "guide_point", "route_pick"}
    }
```

Call it from `cancel_active_interaction` before clearing the Guide anchor and selection drag.

- [ ] **Step 7: Run focused tests GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py tests/design/test_click_navigation.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit modifier propagation and Guide constraints**

```powershell
git add -- probe_station_gui/design/klayout_types.py probe_station_gui/views/design_plot_pane.py tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py tests/design/test_click_navigation.py
git commit -m "feat: constrain guide vectors like KLayout"
```

---

### Task 4: Constrain Ruler and Array, Then Make Escape Non-Destructive

**Files:**
- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Modify: `tests/ui/test_design_navigator_panel.py`
- Modify: `tests/ui/test_design_plot_selection.py`

**Interfaces:**
- Consumes: `constrain_vector_endpoint`, `tool_hover_snap_changed(result, shift, control)`, and five-argument `route_pick_requested`.
- Produces: `set_tool_hover_snap(result, shift, control)`, modifier-aware `apply_route_pick`, and one-step return to Select.

- [ ] **Step 1: Write failing Ruler and Array constraint tests**

```python
def test_ruler_preview_and_commit_share_shift_constraint(qt_app):
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._set_design_tool("ruler")
    previews, completed = [], []
    panel.tool_measure_preview_changed.connect(previews.append)
    panel.tool_measurements_changed.connect(completed.append)
    panel.apply_route_pick("ruler", 1.0, 2.0, False, False)
    panel.set_tool_hover_snap(SnapResult((6.0, 4.0), "vertex", 0.1), True, False)
    assert previews[-1] == [(1.0, 2.0), (6.0, 2.0)]
    panel.apply_route_pick("ruler", 6.0, 4.0, True, False)
    assert completed[-1] == [((1.0, 2.0), (6.0, 2.0))]

def test_array_direction_commit_uses_ctrl_diagonal(qt_app):
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._start_route_pick_mode("array_dir1")
    panel.apply_route_pick("array_dir1", 0.0, 0.0, False, False)
    panel.apply_route_pick("array_dir1", 4.0, 3.0, False, True)
    assert panel._route_array_dir1_step_x_spin.value() == pytest.approx(3.5 * 2**0.5)
    assert panel._route_array_dir1_step_y_spin.value() == pytest.approx(45.0)
```

Cover the second Array direction preview:

```python
panel._start_route_pick_mode("array_dir2")
panel.apply_route_pick("array_dir2", 0.0, 0.0, False, False)
panel.set_tool_hover_snap(
    SnapResult((4.0, -3.0), "vertex", 0.1), False, True
)
assert previews[-1] == [(0.0, 0.0), (3.5, -3.5)]
```

- [ ] **Step 2: Write failing Escape preservation tests**

```python
def test_escape_exits_ruler_but_preserves_completed_segments(qt_app):
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._set_design_tool("ruler")
    panel.apply_route_pick("ruler", 0.0, 0.0)
    panel.apply_route_pick("ruler", 2.0, 0.0)
    panel.apply_route_pick("ruler", 5.0, 5.0)
    panel.cancel_active_tool()
    assert panel._active_design_tool == "select"
    assert panel._ruler_anchor is None
    assert panel._ruler_segments == [((0.0, 0.0), (2.0, 0.0))]

@pytest.mark.parametrize("tool", ["point", "guide", "array"])
def test_escape_returns_drawing_tools_to_select(qt_app, tool):
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._set_design_tool(tool)
    panel.cancel_active_tool()
    assert panel._active_design_tool == "select"
    assert panel._select_tool_button.isChecked()
```

Cover the coordinated window escape path:

```python
window = DesignLayoutWindow()
window.navigator_panel._document = object()
window.navigator_panel._set_design_tool("guide")
window._main_view.set_active_design_tool("guide")
window._main_view._guide_anchor = (1.0, 2.0)
window._cancel_active_interaction()
assert window._main_view._guide_anchor is None
assert window._main_view.active_design_tool == "select"
assert window.navigator_panel._active_design_tool == "select"
```

- [ ] **Step 3: Run focused tests and verify RED**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_navigator_panel.py tests/ui/test_design_plot_selection.py -q`

Expected: FAIL because the panel lacks modifier-aware calls and Guide currently remains active after `Esc`, while Ruler clear deletes completed segments.

- [ ] **Step 4: Add the internal modifier-aware hover connection**

Keep `DesignLayoutWindow.hover_snap_changed(SnapResult | None)` unchanged. Connect the pane's signal directly:

```python
self._main_view.tool_hover_snap_changed.connect(
    self.navigator_panel.set_tool_hover_snap
)

def set_tool_hover_snap(
    self, snap_result: SnapResult | None, shift: bool, control: bool
) -> None:
    self._update_tool_hover_preview(
        snap_result, shift=bool(shift), control=bool(control)
    )
```

Remove preview mutation from `set_hover_snap`; it continues to update only the product-facing snap label.

- [ ] **Step 5: Apply the shared helper in Ruler and Array**

Extend `apply_route_pick` and `_update_tool_hover_preview` with `shift: bool = False` and `control: bool = False` parameters. For a second Ruler or Array point, use this exact projection:

```python
point = constrain_vector_endpoint(
    anchor,
    point,
    shift=bool(shift),
    control=bool(control),
)
```

Use `point` for preview, ruler labels, completed ruler segments, Array length/angle, and mixed Array preview overrides. `_route_vector_anchor_or_none` emits `[point]` only while establishing the first point; when it returns an existing anchor it emits no two-point preview.

- [ ] **Step 6: Make cancel preserve completed state and return to Select**

```python
def cancel_active_tool(self) -> None:
    if self._active_design_tool == "select":
        self._clear_route_pick_mode("")
        return
    if self._active_design_tool == "ruler":
        self._ruler_anchor = None
        self._ruler_end = None
        self._update_ruler_labels()
        self.tool_measure_preview_changed.emit(None)
    self._set_design_tool("select")
```

Do not call `_clear_ruler`; that remains the explicit Clear-button action. `_set_design_tool("select")` clears transient Array/route-pick previews without mutating committed entities or selection.

- [ ] **Step 7: Run focused UI tests GREEN**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_navigator_panel.py tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py tests/design/test_click_navigation.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit Ruler/Array and Escape behavior**

```powershell
git add -- probe_station_gui/views/design_navigator_panel.py tests/ui/test_design_navigator_panel.py tests/ui/test_design_plot_selection.py
git commit -m "fix: align design tools with KLayout UX"
```

---

### Task 5: Regression and Repository Verification

**Files:**
- Modify only files already listed if a regression test exposes a defect.

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: verified feature branch with no unrelated behavior changes.

- [ ] **Step 1: Run all design-focused suites**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_navigator_panel.py tests/design/test_click_navigation.py tests/ui/test_main_window_auxiliary.py -q`

Expected: all tests pass.

- [ ] **Step 2: Run full repository tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q`

Expected: all tests pass with no hardware access.

- [ ] **Step 3: Run syntax, lint, and whitespace checks**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q probe_station_gui tests
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui/design/selection_geometry.py probe_station_gui/design/klayout_types.py probe_station_gui/views/design_plot_pane.py probe_station_gui/views/design_navigator_panel.py tests/design/test_selection_geometry.py tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_navigator_panel.py tests/design/test_click_navigation.py
git diff --check
```

Expected: all commands exit 0. If Ruff reports only repository-pre-existing findings in touched legacy modules, compare against `HEAD` and report them explicitly without broadening scope.

- [ ] **Step 4: Inspect final branch state**

```powershell
git status --short --branch
git log -5 --oneline
```

Expected: branch `codex/klayout-design-renderer`, clean working tree, and focused commits for geometry, quiet markup, Guide constraints, and Ruler/Array/Escape behavior.
