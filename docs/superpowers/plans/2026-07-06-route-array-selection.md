# Route Array From Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the route planner duplicate selected route-point structures as an array while keeping the source points as cell `[0,0]`.

**Architecture:** Put copy semantics in `MeasurementRoute`, keep table selection and preview in `DesignNavigatorPanel`, and let `Main._add_route_array_points` choose between legacy single-point grid creation and new selection-copy creation. Existing route measurement execution stays unchanged.

**Tech Stack:** Python 3.11, PySide6, unittest/pytest, existing route/design model classes.

---

## File Structure

- Modify `probe_station_gui/route_model.py`: add route-model method for copying selected route points across array cells.
- Modify `probe_station_gui/views/design_navigator_panel.py`: allow multi-row route selection, preserve active row, emit selected indices with array requests, and preview selected structures.
- Modify `main.py`: accept selected indices from the panel and call the route-model copy method for multi-point selections.
- Modify `tests/test_route_model.py`: add TDD coverage for route-level copy behavior.
- Modify or add a lightweight UI/controller test if local imports allow PySide6 without hardware.

---

### Task 1: Route Model Selection Array

**Files:**
- Modify: `probe_station_gui/route_model.py`
- Test: `tests/test_route_model.py`

- [ ] **Step 1: Write failing test for preserving selected source points**

Add this test to `MeasurementRouteTest` in `tests/test_route_model.py`:

```python
    def test_add_array_copies_from_points_keeps_source_cell(self) -> None:
        route = MeasurementRoute.default_for_document(self._make_document())
        first = route.add_point((10.0, 20.0), label="A")
        second = route.add_point((12.0, 25.0), label="B")

        added = route.add_array_copies_from_points(
            [0, 1],
            (100.0, 0.0),
            2,
            (0.0, 50.0),
            2,
        )

        self.assertEqual([point.camera_center for point in route.points[:2]], [(10.0, 20.0), (12.0, 25.0)])
        self.assertEqual(
            [point.camera_center for point in added],
            [
                (110.0, 20.0),
                (112.0, 25.0),
                (10.0, 70.0),
                (12.0, 75.0),
                (110.0, 70.0),
                (112.0, 75.0),
            ],
        )
        self.assertEqual(len(route.points), 8)
        self.assertNotIn(first.id, [point.id for point in added])
        self.assertNotIn(second.id, [point.id for point in added])
        self.assertEqual([point.label for point in added[:2]], ["P003", "P004"])
        self.assertEqual(added[0].metadata["array_source_point_id"], "p001")
        self.assertEqual(added[1].metadata["array_source_point_id"], "p002")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/test_route_model.py::MeasurementRouteTest::test_add_array_copies_from_points_keeps_source_cell -q
```

Expected: FAIL with `AttributeError: 'MeasurementRoute' object has no attribute 'add_array_copies_from_points'`.

- [ ] **Step 3: Implement minimal route-model copy method**

Add helper method to `MeasurementRoute` in `probe_station_gui/route_model.py`:

```python
    def add_array_copies_from_points(
        self,
        template_indices: list[int],
        step_x: Point2D,
        count_x: int,
        step_y: Point2D,
        count_y: int,
        *,
        serpentine: bool = False,
    ) -> list[RoutePoint]:
        x_count = int(count_x)
        y_count = int(count_y)
        if x_count <= 0 or y_count <= 0:
            raise RouteModelError("Array route point counts must be positive.")
        if len(template_indices) < 2:
            raise RouteModelError("Select at least two route points to copy a structure.")

        source_points: list[RoutePoint] = []
        seen_indices: set[int] = set()
        for index in template_indices:
            point_index = int(index)
            if point_index in seen_indices:
                continue
            if not 0 <= point_index < len(self.points):
                raise RouteModelError("Selected route point is not in the current route.")
            seen_indices.add(point_index)
            source_points.append(self.points[point_index])

        step_x_dx, step_x_dy = float(step_x[0]), float(step_x[1])
        step_y_dx, step_y_dy = float(step_y[0]), float(step_y[1])
        if x_count > 1 and abs(step_x_dx) <= 1e-12 and abs(step_x_dy) <= 1e-12:
            raise RouteModelError("Array direction 1 step must be non-zero.")
        if y_count > 1 and abs(step_y_dx) <= 1e-12 and abs(step_y_dy) <= 1e-12:
            raise RouteModelError("Array direction 2 step must be non-zero.")

        added: list[RoutePoint] = []
        for y_index in range(y_count):
            if serpentine and y_index % 2 == 1:
                x_indices = range(x_count - 1, -1, -1)
            else:
                x_indices = range(x_count)
            for x_index in x_indices:
                if x_index == 0 and y_index == 0:
                    continue
                dx = step_x_dx * float(x_index) + step_y_dx * float(y_index)
                dy = step_x_dy * float(x_index) + step_y_dy * float(y_index)
                for source in source_points:
                    metadata = dict(source.metadata)
                    metadata["array_source_point_id"] = source.id
                    point = self.add_point(
                        (
                            source.camera_center[0] + dx,
                            source.camera_center[1] + dy,
                        )
                    )
                    self.points[-1] = replace(point, metadata=metadata)
                    added.append(self.points[-1])
        return added
```

- [ ] **Step 4: Run route-model test**

Run same command as Step 2. Expected: PASS.

- [ ] **Step 5: Add failing tests for serpentine and invalid selection**

Add:

```python
    def test_add_array_copies_from_points_serpentine_changes_cell_order(self) -> None:
        route = MeasurementRoute.default_for_document(self._make_document())
        route.add_point((0.0, 0.0))
        route.add_point((1.0, 0.0))

        added = route.add_array_copies_from_points(
            [0, 1],
            (10.0, 0.0),
            3,
            (0.0, 100.0),
            2,
            serpentine=True,
        )

        self.assertEqual(
            [point.camera_center for point in added],
            [
                (10.0, 0.0),
                (11.0, 0.0),
                (20.0, 0.0),
                (21.0, 0.0),
                (20.0, 100.0),
                (21.0, 100.0),
                (10.0, 100.0),
                (11.0, 100.0),
                (0.0, 100.0),
                (1.0, 100.0),
            ],
        )

    def test_add_array_copies_from_points_rejects_missing_selection(self) -> None:
        route = MeasurementRoute.default_for_document(self._make_document())
        route.add_point((0.0, 0.0))
        route.add_point((1.0, 0.0))

        with self.assertRaises(RouteModelError):
            route.add_array_copies_from_points([0, 9], (10.0, 0.0), 2, (0.0, 1.0), 1)
```

- [ ] **Step 6: Run route-model tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/test_route_model.py -q
```

Expected: PASS.

---

### Task 2: Panel Selection and Array Request

**Files:**
- Modify: `probe_station_gui/views/design_navigator_panel.py`

- [ ] **Step 1: Update route-array signal to include selected indices**

Change `route_array_requested` signature from ten fields to eleven fields by adding `object` as the last argument:

```python
    route_array_requested = Signal(
        float,
        float,
        float,
        float,
        int,
        float,
        float,
        int,
        bool,
        bool,
        object,
    )
```

- [ ] **Step 2: Allow multi-row selection**

Change route table selection mode:

```python
        self._route_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
```

- [ ] **Step 3: Add selected index helper**

Add to `DesignNavigatorPanel`:

```python
    def _selected_route_row_indices(self) -> list[int]:
        selection_model = self._route_table.selectionModel()
        if selection_model is None:
            return []
        return sorted({index.row() for index in selection_model.selectedRows()})
```

- [ ] **Step 4: Preserve active selected row and emit it**

Update `_on_route_selection_changed`:

```python
    def _on_route_selection_changed(self) -> None:
        selected_rows = self._selected_route_row_indices()
        if not selected_rows:
            self._selected_route_point_index = -1
            self.route_selected.emit(-1)
            self._update_enabled_state()
            return
        row = selected_rows[0]
        self._selected_route_point_index = row
        self.route_selected.emit(row)
        self._update_enabled_state()
        self._update_route_array_preview()
```

- [ ] **Step 5: Emit selected rows with array request**

Update `_emit_route_array_requested`:

```python
    def _emit_route_array_requested(self) -> None:
        dir1 = self._route_array_dir1_step()
        dir2 = self._route_array_dir2_step()
        self.route_array_requested.emit(
            self._route_array_origin_x_spin.value(),
            self._route_array_origin_y_spin.value(),
            dir1[0],
            dir1[1],
            self._route_array_dir1_count_spin.value(),
            dir2[0],
            dir2[1],
            self._route_array_dir2_count_spin.value(),
            self._route_array_serpentine_checkbox.isChecked(),
            self._route_array_replace_checkbox.isChecked(),
            self._selected_route_row_indices(),
        )
        self._set_design_tool("select")
```

- [ ] **Step 6: Disable Replace when selected structure is active**

In `_update_enabled_state`, after enabling array controls, add:

```python
        has_selection_array = len(self._selected_route_row_indices()) >= 2
        self._route_array_replace_checkbox.setEnabled(
            has_document and not route_running and not has_selection_array
        )
```

- [ ] **Step 7: Preview selected structures**

In `_update_route_array_preview`, branch on selected rows. If two or more selected rows and a route exists, build preview from selected source centers:

```python
        selected_rows = self._selected_route_row_indices()
        if self._route is not None and len(selected_rows) >= 2:
            source_points = [
                self._route.points[row].camera_center
                for row in selected_rows
                if 0 <= row < len(self._route.points)
            ]
            points = self._build_array_preview_points_for_sources(
                source_points,
                dir1,
                count1,
                dir2,
                count2,
            )
        else:
            points = self._build_array_preview_points(origin, dir1, count1, dir2, count2)
```

Add helper:

```python
    def _build_array_preview_points_for_sources(
        self,
        sources: list[Point2D],
        dir1: Point2D,
        count1: int,
        dir2: Point2D,
        count2: int,
    ) -> list[Point2D]:
        cells = self._build_array_preview_points((0.0, 0.0), dir1, count1, dir2, count2)
        points: list[Point2D] = []
        for cell in cells:
            if abs(cell[0]) <= 1e-12 and abs(cell[1]) <= 1e-12:
                continue
            for source in sources:
                points.append((source[0] + cell[0], source[1] + cell[1]))
        return points
```

---

### Task 3: Main Wiring

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Extend `_add_route_array_points` signature**

Add last parameter:

```python
        selected_indices: object = None,
```

- [ ] **Step 2: Normalize selected indices**

Inside `_add_route_array_points`, before `try`, add:

```python
        selection: list[int] = []
        if isinstance(selected_indices, (list, tuple)):
            for item in selected_indices:
                try:
                    selection.append(int(item))
                except (TypeError, ValueError):
                    continue
```

- [ ] **Step 3: Call new route method for selected structures**

Replace the direct `route.add_grid_points(...)` call with:

```python
            if len(selection) >= 2:
                added = route.add_array_copies_from_points(
                    selection,
                    (float(step_x_dx), float(step_x_dy)),
                    int(count_x),
                    (float(step_y_dx), float(step_y_dy)),
                    int(count_y),
                    serpentine=bool(serpentine),
                )
                replace_existing = False
            else:
                added = route.add_grid_points(
                    (float(origin_x), float(origin_y)),
                    (float(step_x_dx), float(step_x_dy)),
                    int(count_x),
                    (float(step_y_dx), float(step_y_dy)),
                    int(count_y),
                    serpentine=bool(serpentine),
                    clear_existing=bool(replace_existing),
                )
```

- [ ] **Step 4: Update status text**

Keep current message for grid mode. For selected structures:

```python
        if len(selection) >= 2:
            self._show_status(
                f"Added {len(added)} copied route points from {len(selection)} selected points.",
                4000,
            )
            return
```

---

### Task 4: Verification

**Files:**
- Test: `tests/test_route_model.py`
- Test: `tests/test_design_workflow.py` or targeted panel test if importable

- [ ] **Step 1: Run focused route tests**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/test_route_model.py -q
```

Expected: PASS.

- [ ] **Step 2: Run design workflow tests**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/test_design_workflow.py tests/test_design_click_navigation.py -q
```

Expected: PASS.

- [ ] **Step 3: Run broader non-hardware tests touched by route/design wiring**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/test_route_model.py tests/test_design_workflow.py tests/test_design_click_navigation.py tests/test_main_coordinate_feedrate.py -q
```

Expected: PASS or only pre-existing unrelated skips.

- [ ] **Step 4: Review git diff**

```powershell
git diff -- probe_station_gui/route_model.py probe_station_gui/views/design_navigator_panel.py main.py tests/test_route_model.py
```

Expected: changes match spec; no route measurement pause/resume/interrupt behavior changed.

