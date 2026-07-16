# Design Markup and CAD-Style Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent dashed construction guides, SOLIDWORKS-style mixed selection, explicit route-point placement, and selection-driven mixed arrays to the embedded design window.

**Architecture:** Qt-free geometry, markup, and selection modules own predicates and immutable operation plans. The plot pane owns pointer state and native overlays, while the navigator owns tool controls and the main/design adapter remains the only route-domain mutation boundary. A coalescing worker persists markup outside the GUI thread without touching KLayout or the source GDS.

**Tech Stack:** Python 3.11+, dataclasses, PySide6, pyqtgraph, existing KLayout workers, pytest, pytest-qt, JSON, pathlib, `os.replace`.

## Global Constraints

- Source GDS files are immutable; markup never enters KLayout render, snap, or minimap databases.
- `Select` is the default tool; `Point`, `Guide`, `Measure`, and `Array` are mutually exclusive; `Rotate` remains immediate.
- Left-to-right selection is blue/solid/full-containment; right-to-left is green/dashed/crossing; Shift adds and Ctrl inverts.
- Hidden markup is not drawn, snapped, selected, deleted, or used as an Array source.
- Point, Guide, Delete, Clear All, Undo Last, and Array Create use the existing route-edit safety gate; unsafe mixed operations change nothing.
- Array requires at least one selected entity, excludes the zero cell, preserves group geometry, and leaves source selection selected.
- Markup lives under `%LOCALAPPDATA%\ProbeStationGUI\Markup`; saves are newest-only and atomic; normal close preserves, Clear All and explicit Unload delete.
- The existing 14-screen-pixel snap radius and correlated KLayout stale/failure gates remain authoritative.
- Route Pause/Resume/Interrupt, autofocus restoration, contact, serial safety, and stage movement semantics must not change.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python command.

---

## File Map

- `probe_station_gui/design/selection_geometry.py`: Qt-free point/segment geometry, hit tests, box/cross predicates, intersections, snap candidates, translations.
- `probe_station_gui/design/markup.py`: guide records, source fingerprint, immutable markup document, JSON validation/serialization.
- `probe_station_gui/design/markup_store.py`: path hashing, background load/save/delete queue, newest-only coalescing, atomic replacement.
- `probe_station_gui/design/selection_model.py`: stable mixed entity projections, selection updates, delete/array planning.
- `probe_station_gui/design/navigation_adapter.py`: apply route half of validated mixed plans and preserve RoutePoint metadata.
- `probe_station_gui/design/session.py`: exact-ID route removal/insertion helpers used by atomic operation application.
- `probe_station_gui/views/design_plot_pane.py`: tool pointer state machines, hit/box selection, markup overlays, guide preview, mixed array preview, snap arbitration.
- `probe_station_gui/views/design_navigator_panel.py`: toolbar/options, Markup eye, Delete shortcut, route-table synchronization, selection-driven Array UI.
- `probe_station_gui/views/main_window_auxiliary.py`: connect new window signals to main-owned route mutations.
- `main.py`: own per-design markup session, safety validation, persistence lifecycle, and one-shot mixed commit.
- `tests/design/test_selection_geometry.py`, `tests/design/test_markup.py`, `tests/design/test_markup_store.py`, `tests/design/test_selection_model.py`: pure/model/store coverage.
- `tests/ui/test_design_plot_selection.py`, `tests/ui/test_design_navigator_panel.py`, `tests/app/test_main_design_navigation.py`: pointer, toolbar, and integration coverage.

---

### Task 1: Qt-Free Point and Segment Geometry

**Files:**
- Create: `probe_station_gui/design/selection_geometry.py`
- Create: `tests/design/test_selection_geometry.py`

**Interfaces:**
- Consumes: `Point2D = tuple[float, float]` convention.
- Produces: `SelectionRect`, `PointGeometry`, `SegmentGeometry`, `GuideSnapCandidate`, `guide_snap_candidates(segments)`, and `translated_geometry(geometry, dx, dy)`.

- [ ] **Step 1: Write failing predicate and snap tests**

```python
from probe_station_gui.design.selection_geometry import (
    PointGeometry, SegmentGeometry, SelectionRect, guide_snap_candidates,
)

def test_box_contains_but_cross_box_touches_segment():
    rect = SelectionRect.from_drag((0.0, 0.0), (10.0, 10.0))
    segment = SegmentGeometry((-2.0, 5.0), (5.0, 5.0))
    assert not segment.contained_by(rect)
    assert segment.crosses(rect)

def test_guide_candidates_include_end_center_and_finite_intersection():
    candidates = guide_snap_candidates([
        SegmentGeometry((0.0, 0.0), (10.0, 10.0)),
        SegmentGeometry((0.0, 10.0), (10.0, 0.0)),
    ])
    assert ((5.0, 5.0), "guide_intersection") in {
        (item.point, item.mode) for item in candidates
    }
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_selection_geometry.py -q`

Expected: FAIL with `ModuleNotFoundError: probe_station_gui.design.selection_geometry`.

- [ ] **Step 3: Implement immutable geometry and robust finite intersection**

```python
@dataclass(frozen=True)
class SelectionRect:
    left: float
    right: float
    bottom: float
    top: float

    @classmethod
    def from_drag(cls, start: Point2D, end: Point2D) -> "SelectionRect":
        return cls(min(start[0], end[0]), max(start[0], end[0]),
                   min(start[1], end[1]), max(start[1], end[1]))

@dataclass(frozen=True)
class PointGeometry:
    point: Point2D
    def contained_by(self, rect: SelectionRect) -> bool:
        return rect.contains(self.point)
    def crosses(self, rect: SelectionRect) -> bool:
        return rect.contains(self.point)
    def translated(self, dx: float, dy: float) -> "PointGeometry":
        return PointGeometry((self.point[0] + dx, self.point[1] + dy))

@dataclass(frozen=True)
class SegmentGeometry:
    start: Point2D
    end: Point2D
    def contained_by(self, rect: SelectionRect) -> bool:
        return rect.contains(self.start) and rect.contains(self.end)
    def crosses(self, rect: SelectionRect) -> bool:
        return segment_intersects_rect(self, rect)
```

Implement squared-distance point/segment hit tests, inclusive rectangle edges, Liang-Barsky or orientation-based segment/rectangle crossing, non-collinear finite intersection with scale-aware epsilon, endpoint-touch support, midpoint generation, and stable candidate deduplication. Collinear overlaps add no intersection candidate.

- [ ] **Step 4: Add edge-case tests and run them green**

```python
def test_parallel_and_collinear_segments_add_no_intersection():
    segments = [
        SegmentGeometry((0.0, 0.0), (4.0, 0.0)),
        SegmentGeometry((0.0, 1.0), (4.0, 1.0)),
        SegmentGeometry((2.0, 0.0), (6.0, 0.0)),
    ]
    assert not [c for c in guide_snap_candidates(segments)
                if c.mode == "guide_intersection"]

def test_endpoint_touch_adds_one_deduplicated_intersection():
    segments = [
        SegmentGeometry((0.0, 0.0), (2.0, 0.0)),
        SegmentGeometry((2.0, 0.0), (2.0, 2.0)),
    ]
    matches = [c.point for c in guide_snap_candidates(segments)
               if c.point == (2.0, 0.0)]
    assert matches == [(2.0, 0.0)]

def test_point_and_segment_translate_without_mutating_source():
    source = SegmentGeometry((1.0, 2.0), (3.0, 4.0))
    assert source.translated(5.0, -2.0) == SegmentGeometry((6.0, 0.0), (8.0, 2.0))
    assert source == SegmentGeometry((1.0, 2.0), (3.0, 4.0))

def test_hit_tolerance_is_measured_in_design_units():
    segment = SegmentGeometry((0.0, 0.0), (10.0, 0.0))
    assert segment.hit_test((5.0, 0.49), tolerance=0.5)
    assert not segment.hit_test((5.0, 0.51), tolerance=0.5)
```

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_selection_geometry.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add probe_station_gui/design/selection_geometry.py tests/design/test_selection_geometry.py
git commit -m "feat: add selectable design geometry"
```

---

### Task 2: Markup Document and Validated JSON

**Files:**
- Create: `probe_station_gui/design/markup.py`
- Create: `tests/design/test_markup.py`

**Interfaces:**
- Consumes: `Point2D`, `SegmentGeometry`.
- Produces: `SourceFingerprint`, `GuideSegment`, `MarkupDocument`, `MarkupSnapshot`, `MarkupDecodeError`, `fingerprint_source(path)`.

- [ ] **Step 1: Write failing round-trip and fingerprint tests**

```python
def test_markup_round_trip_preserves_ids_visibility_and_points(tmp_path):
    source = tmp_path / "chip.gds"
    source.write_bytes(b"gds")
    document = MarkupDocument.empty(source, visible=False).append_guide(
        (1.0, 2.0), (3.0, 4.0), guide_id="guide-1"
    )
    restored = MarkupDocument.from_dict(document.to_dict())
    assert restored == document

def test_changed_source_fingerprint_is_detected(tmp_path):
    source = tmp_path / "chip.gds"
    source.write_bytes(b"old")
    old = fingerprint_source(source)
    source.write_bytes(b"new-layout")
    assert fingerprint_source(source) != old

def test_corrupt_or_unknown_schema_is_rejected():
    with pytest.raises(MarkupDecodeError):
        MarkupDocument.from_dict({"schema_version": 99})
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_markup.py -q`

Expected: FAIL because `markup` does not exist.

- [ ] **Step 3: Implement immutable markup state**

```python
MARKUP_SCHEMA_VERSION = 1

@dataclass(frozen=True)
class SourceFingerprint:
    size: int
    mtime_ns: int

@dataclass(frozen=True)
class GuideSegment:
    id: str
    start: Point2D
    end: Point2D
    def geometry(self) -> SegmentGeometry:
        return SegmentGeometry(self.start, self.end)

@dataclass(frozen=True)
class MarkupDocument:
    source_path: str
    source_fingerprint: SourceFingerprint
    visible: bool = True
    guides: tuple[GuideSegment, ...] = ()

    def append_guide(self, start, end, *, guide_id=None) -> "MarkupDocument":
        guide = GuideSegment(guide_id or uuid.uuid4().hex, coerce_point(start), coerce_point(end))
        validate_guide(guide)
        return replace(self, guides=(*self.guides, guide))

    def remove_ids(self, ids: Collection[str]) -> "MarkupDocument":
        removed = frozenset(ids)
        return replace(self, guides=tuple(g for g in self.guides if g.id not in removed))

    def with_visibility(self, visible: bool) -> "MarkupDocument":
        return replace(self, visible=bool(visible))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": MARKUP_SCHEMA_VERSION,
            "source_path": self.source_path,
            "source_fingerprint": asdict(self.source_fingerprint),
            "visible": self.visible,
            "guides": [
                {"id": g.id, "start": list(g.start), "end": list(g.end)}
                for g in self.guides
            ],
        }

    @classmethod
    def from_dict(cls, value: object) -> "MarkupDocument":
        mapping = require_mapping(value)
        require_schema(mapping, MARKUP_SCHEMA_VERSION)
        document = cls(
            source_path=normalize_source_path(mapping["source_path"]),
            source_fingerprint=SourceFingerprint.from_dict(mapping["source_fingerprint"]),
            visible=bool(mapping.get("visible", True)),
            guides=tuple(GuideSegment.from_dict(item) for item in require_list(mapping, "guides")),
        )
        validate_document(document)
        return document
```

Normalize source paths with `Path.resolve(strict=False)` and `normcase`; reject non-finite coordinates, duplicate/empty IDs, zero-length segments, wrong schema, and malformed fingerprints. Return new documents for every mutation.

- [ ] **Step 4: Run complete markup tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_markup.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add probe_station_gui/design/markup.py tests/design/test_markup.py
git commit -m "feat: model persistent design markup"
```

---

### Task 3: Newest-Only Background Markup Store

**Files:**
- Create: `probe_station_gui/design/markup_store.py`
- Create: `tests/design/test_markup_store.py`

**Interfaces:**
- Consumes: `MarkupDocument`, `MarkupSnapshot`.
- Produces: `markup_path_for_source(source, root=None)`, `MarkupStoreWorker(QObject)` signals `loaded`, `failed`, slots `load`, `publish`, `delete`, `stop`; `MarkupStoreController` GUI-thread facade.

- [ ] **Step 1: Write failing filesystem and coalescing tests**

```python
def test_markup_path_is_stable_hash_under_markup_root(tmp_path):
    first = markup_path_for_source("C:/DESIGNS/chip.gds", root=tmp_path)
    second = markup_path_for_source("c:/designs/./chip.gds", root=tmp_path)
    assert first == second
    assert first.parent == tmp_path
    assert first.suffix == ".json"

def test_atomic_save_round_trip_leaves_no_temp_file(tmp_path):
    path = tmp_path / "markup.json"
    _write_atomic(path, b'{"schema_version": 1}')
    assert path.read_bytes() == b'{"schema_version": 1}'
    assert list(tmp_path.glob("*.tmp")) == []

def test_pending_saves_coalesce_to_newest_snapshot(tmp_path, qtbot):
    controller = MarkupStoreController(root=tmp_path)
    controller.publish(first)
    controller.publish(second)
    qtbot.waitUntil(lambda: controller.is_idle)
    assert read_saved(tmp_path, second.source_path) == second
```

- [ ] **Step 2: Run tests and verify import failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_markup_store.py -q`

Expected: FAIL because `markup_store` does not exist.

- [ ] **Step 3: Implement store worker and atomic replace**

```python
def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

class MarkupStoreWorker(QObject):
    loaded = Signal(int, object, object)
    failed = Signal(int, str, str)
    operation_finished = Signal(int)

    @Slot(int, object)
    def publish(self, generation: int, snapshot: MarkupSnapshot) -> None:
        self._pending_by_path[snapshot.source_path] = (generation, snapshot)
        self._drain_newest()
```

Use a dedicated `QThread`; do all `read_text`, `write_bytes`, JSON parsing, mkdir, unlink, and replace on that thread. Coalesce unpublished saves per source path, preserve generation IDs, and make `stop()` bounded/non-blocking from the GUI shutdown path.

- [ ] **Step 4: Test delete, corruption, error reporting, and thread affinity**

```python
def test_delete_removes_persisted_file(tmp_path, qtbot, markup_snapshot):
    controller = MarkupStoreController(root=tmp_path)
    controller.publish(markup_snapshot)
    qtbot.waitUntil(lambda: controller.is_idle)
    controller.delete(markup_snapshot.source_path)
    qtbot.waitUntil(lambda: controller.is_idle)
    assert not markup_path_for_source(markup_snapshot.source_path, root=tmp_path).exists()
    controller.stop()

def test_corrupt_json_reports_failure_without_document(tmp_path, qtbot, source_path):
    markup_path_for_source(source_path, root=tmp_path).write_text("broken", encoding="utf-8")
    controller = MarkupStoreController(root=tmp_path)
    failures = []
    controller.failed.connect(failures.append)
    controller.load(source_path)
    qtbot.waitUntil(lambda: bool(failures))
    assert failures[-1].operation == "load"
    controller.stop()

def test_latest_generation_wins_when_old_load_finishes_late(qtbot, store_controller):
    accepted = []
    store_controller.loaded.connect(accepted.append)
    old_generation = store_controller.load("C:/a.gds")
    new_generation = store_controller.load("C:/b.gds")
    store_controller._on_loaded(old_generation, old_document, None)
    store_controller._on_loaded(new_generation, new_document, None)
    assert accepted == [new_document]

def test_filesystem_work_runs_off_gui_thread(qtbot, store_controller, monkeypatch):
    gui_thread = QThread.currentThread()
    observed = []
    monkeypatch.setattr(markup_store, "_read_document", lambda path: observed.append(QThread.currentThread()))
    store_controller.load("C:/a.gds")
    qtbot.waitUntil(lambda: bool(observed))
    assert observed[0] is not gui_thread
```

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_markup_store.py -q`

Expected: PASS with no `QThread: Destroyed while thread is still running` warning.

- [ ] **Step 5: Commit**

```powershell
git add probe_station_gui/design/markup_store.py tests/design/test_markup_store.py
git commit -m "feat: persist markup off the GUI thread"
```

---

### Task 4: Shared Selection and Atomic Mixed Operation Plans

**Files:**
- Create: `probe_station_gui/design/selection_model.py`
- Modify: `probe_station_gui/design/session.py`
- Modify: `probe_station_gui/design/navigation_adapter.py`
- Create: `tests/design/test_selection_model.py`
- Modify: `tests/design/test_navigation_adapter.py`

**Interfaces:**
- Consumes: `RoutePoint`, `GuideSegment`, point/segment geometry.
- Produces: `EntityOwner`, `SelectableDesignEntity`, `SelectionModel`, `MixedDeletePlan`, `MixedArrayPlan`, `project_entities(route, markup)`, `plan_mixed_delete(entities, selection, edit_safe)`, `plan_mixed_array(entities, selection, request, edit_safe)`, `apply_route_entity_changes(session, plan)`.

- [ ] **Step 1: Write failing selection transition tests**

```python
def test_selection_replace_add_invert_and_prune():
    model = SelectionModel()
    assert model.replace({"a"}).ids == frozenset({"a"})
    assert model.add({"b"}).ids == frozenset({"a", "b"})
    assert model.invert({"a", "c"}).ids == frozenset({"b", "c"})
    assert model.prune({"b"}).ids == frozenset({"b"})
```

- [ ] **Step 2: Write failing mixed delete/array tests**

```python
def test_mixed_array_excludes_zero_cell_and_preserves_relative_geometry(route_point, guide):
    entities = project_entities(route_with(route_point), markup_with(guide))
    request = MixedArrayRequest((10.0, 0.0), 2, (0.0, 20.0), 1, False)
    plan = plan_mixed_array(entities, {route_point.id, guide.id}, request, edit_safe=True)
    assert [p.camera_center for p in plan.route_copies] == [(route_point.camera_center[0] + 10.0, route_point.camera_center[1])]
    assert plan.guide_copies[0].start == (guide.start[0] + 10.0, guide.start[1])

def test_one_selected_guide_is_valid_array_source(guide):
    entities = project_entities(None, markup_with(guide))
    plan = plan_mixed_array(entities, {guide.id}, MixedArrayRequest((5.0, 0.0), 2, (0.0, 0.0), 1, False), edit_safe=True)
    assert plan.accepted and len(plan.guide_copies) == 1

def test_no_selection_or_stale_id_rejects_entire_plan(guide):
    entities = project_entities(None, markup_with(guide))
    assert not plan_mixed_array(entities, set(), valid_request(), edit_safe=True).accepted
    assert not plan_mixed_delete(entities, {"missing"}, edit_safe=True).accepted

def test_unsafe_delete_and_array_return_rejected_plan_without_changes(guide):
    entities = project_entities(None, markup_with(guide))
    assert not plan_mixed_delete(entities, {guide.id}, edit_safe=False).accepted
    assert not plan_mixed_array(entities, {guide.id}, valid_request(), edit_safe=False).accepted

def test_route_copy_retains_enabled_metadata_and_gets_fresh_id(route_point):
    copy = copy_route_point(route_point, dx=4.0, dy=-2.0)
    assert copy.id != route_point.id
    assert copy.enabled == route_point.enabled
    assert copy.metadata == route_point.metadata
```

- [ ] **Step 3: Run tests and verify failures**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_selection_model.py tests/design/test_navigation_adapter.py -q`

Expected: FAIL on missing selection types and mixed plan functions.

- [ ] **Step 4: Implement projections and pure plans**

```python
class EntityOwner(StrEnum):
    ROUTE = "route"
    MARKUP = "markup"

@dataclass(frozen=True)
class SelectableDesignEntity:
    id: str
    owner: EntityOwner
    geometry: PointGeometry | SegmentGeometry
    route_index: int | None = None

@dataclass(frozen=True)
class SelectionModel:
    ids: frozenset[str] = frozenset()
    def replace(self, ids): return SelectionModel(frozenset(ids))
    def add(self, ids): return SelectionModel(self.ids | frozenset(ids))
    def invert(self, ids): return SelectionModel(self.ids ^ frozenset(ids))
    def clear(self): return SelectionModel()
    def prune(self, valid_ids): return SelectionModel(self.ids & frozenset(valid_ids))

@dataclass(frozen=True)
class MixedArrayPlan:
    accepted: bool
    route_copies: tuple[RoutePoint, ...] = ()
    guide_copies: tuple[GuideSegment, ...] = ()
    status_message: str = ""
```

Generate row-major cell offsets with serpentine ordering for route copies, skip `(0, 0)`, use `dataclasses.replace` plus new UUIDs for full RoutePoint copies, and keep guide placement order independent of measurement order. Validate safety, every selected ID, visibility, counts, and finite vectors before returning any copies/removals.

- [ ] **Step 5: Add exact-ID route application helpers and run green**

Add session/adapter helpers that validate all route IDs before one slice assignment, preserve the current route object/binding, refresh `updated_at_utc`, and return an adapter plan without partially mutating on stale IDs.

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_selection_model.py tests/design/test_navigation_adapter.py tests/design/test_workflow.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add probe_station_gui/design/selection_model.py probe_station_gui/design/session.py probe_station_gui/design/navigation_adapter.py tests/design/test_selection_model.py tests/design/test_navigation_adapter.py
git commit -m "feat: plan atomic mixed design edits"
```

---

### Task 5: Plot Tools, Native Overlays, and Snap Arbitration

**Files:**
- Modify: `probe_station_gui/views/design_plot_pane.py`
- Create: `tests/ui/test_design_plot_selection.py`
- Modify: `tests/ui/test_design_plot_klayout.py`

**Interfaces:**
- Consumes: entity projections, `MarkupDocument`, `SelectionModel`, `GuideSnapCandidate`.
- Produces signals `entity_selection_requested(object, str)`, `guide_requested(object, object)`, `point_requested(float, float)`, and methods `set_active_design_tool`, `set_selectable_entities`, `set_selection`, `set_markup`, `set_mixed_array_preview`.

- [ ] **Step 1: Write failing pointer-state tests**

```python
def test_select_click_emits_nearest_entity_and_empty_click_clears(qtbot, pane):
    pane.set_selectable_entities([point_entity("p", (5.0, 5.0))])
    requests = collect_signal(pane.entity_selection_requested)
    click_design(pane, (5.0, 5.0))
    click_design(pane, (50.0, 50.0))
    assert requests == [({"p"}, "replace"), (set(), "replace")]

def test_left_to_right_drag_emits_containment_with_replace_modifier(qtbot, pane):
    pane.set_selectable_entities(selection_fixture_entities())
    requests = collect_signal(pane.entity_selection_requested)
    drag_design(pane, (0.0, 0.0), (10.0, 10.0))
    assert requests[-1] == ({"inside-point", "inside-segment"}, "replace")

def test_right_to_left_drag_emits_crossing_with_ctrl_modifier(qtbot, pane):
    pane.set_selectable_entities(selection_fixture_entities())
    requests = collect_signal(pane.entity_selection_requested)
    drag_design(pane, (10.0, 10.0), (0.0, 0.0), modifier=Qt.ControlModifier)
    assert requests[-1] == ({"inside-point", "inside-segment", "crossing-segment"}, "invert")

def test_middle_drag_remains_pan_and_does_not_select(qtbot, pane):
    requests = collect_signal(pane.entity_selection_requested)
    before = pane._plot.getViewBox().viewRange()
    drag_design(pane, (0.0, 0.0), (10.0, 0.0), button=Qt.MiddleButton)
    assert requests == [] and pane._plot.getViewBox().viewRange() != before

def test_point_tool_stays_active_after_correlated_click(qtbot, pane):
    pane.set_active_design_tool("point")
    click_design(pane, (4.0, 5.0))
    complete_latest_snap(pane, (4.0, 5.0))
    assert pane.active_design_tool == "point"

def test_guide_click_click_preview_and_escape(qtbot, pane):
    pane.set_active_design_tool("guide")
    click_design(pane, (0.0, 0.0))
    move_design(pane, (5.0, 5.0))
    assert pane.pending_guide == ((0.0, 0.0), (5.0, 5.0))
    pane.cancel_active_interaction()
    assert pane.pending_guide is None
```

- [ ] **Step 2: Run tests and verify missing API failures**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_selection.py -q`

Expected: FAIL on missing plot-pane selection API.

- [ ] **Step 3: Implement tool routing and selection rectangle**

```python
def _selection_mode(start: Point2D, current: Point2D) -> str:
    return "contain" if current[0] >= start[0] else "cross"

def _selection_modifier(modifiers: Qt.KeyboardModifiers) -> str:
    if modifiers & Qt.ControlModifier:
        return "invert"
    if modifiers & Qt.ShiftModifier:
        return "add"
    return "replace"
```

Use Qt's drag-distance threshold. Start left-drag only from empty graphics in Select. Draw blue solid containment and green dashed cross rectangles. Convert the 14-pixel click radius through current ViewBox scale before pure hit testing. Keep middle-button ViewBox pan and wheel zoom unchanged.

- [ ] **Step 4: Replace latent sketch overlay with Markup/selection/preview overlays**

Reuse native pyqtgraph line/scatter items for dashed guides, endpoints/midpoints, selected guide highlight, selected route-point highlight, pending guide, and mixed array preview. `set_markup` must redraw overlays only and must not call `set_document`, `_configure_klayout_view`, worker restart, minimap rebuild, or `DesignDocument.build_snap_geometry`.

- [ ] **Step 5: Integrate nearest markup/KLayout snap arbitration**

```python
@dataclass(frozen=True)
class _PendingClick:
    action: str
    payload: tuple[object, ...]
    raw_point: Point2D
    markup_result: SnapResult | None

def _nearest_screen_result(self, klayout, markup):
    valid = [item for item in (klayout, markup) if item is not None]
    return min(valid, key=lambda item: self._screen_distance(item.point))
```

For file-backed snap, capture the best visible markup candidate in the correlated pending click, await the matching KLayout response, compare in screen pixels, then execute exactly once. Preserve request/config/source generation checks. A stale/failed KLayout click still executes no raw fallback; it may use the already-correlated markup candidate only when the current design/config generation still matches.

- [ ] **Step 6: Add snap and worker-stability regressions**

```python
def test_hidden_markup_has_no_overlay_hit_or_snap_candidate(pane, hidden_markup):
    pane.set_markup(hidden_markup)
    assert pane.visible_guide_entities == ()
    assert pane._best_markup_snap((1.0, 1.0)) is None

def test_markup_candidate_beats_farther_klayout_candidate_in_screen_space(pane):
    markup = SnapResult((1.0, 0.0), "guide_center", 1.0)
    klayout = SnapResult((4.0, 0.0), "vertex", 4.0)
    assert pane._nearest_screen_result(klayout, markup) is markup

def test_stale_response_executes_neither_candidate(pane):
    actions = collect_signal(pane.point_requested)
    request = submit_point_click(pane, markup_point=(2.0, 2.0))
    pane.set_document(new_document())
    emit_snap_response(pane, request, (3.0, 3.0))
    assert actions == []

def test_markup_edit_does_not_replace_render_snap_or_minimap_worker(pane):
    workers = (pane._render_worker, pane._snap_worker, pane._minimap_worker)
    pane.set_markup(markup_with_one_guide())
    assert (pane._render_worker, pane._snap_worker, pane._minimap_worker) == workers

def test_zero_length_guide_is_not_emitted(pane):
    guides = collect_signal(pane.guide_requested)
    commit_guide(pane, (1.0, 1.0), (1.0, 1.0))
    assert guides == []
```

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_klayout_raster.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add probe_station_gui/views/design_plot_pane.py tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py
git commit -m "feat: add CAD selection and markup overlays"
```

---

### Task 6: Toolbar, Markup Controls, Table Sync, and Selection-Driven Array

**Files:**
- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Modify: `probe_station_gui/views/design_navigator_enablement.py`
- Modify: `tests/ui/test_design_navigator_panel.py`

**Interfaces:**
- Consumes: immutable selection/entity/markup snapshots and route edit enablement.
- Produces signals `active_design_tool_changed(str)`, `selection_requested(object, str)`, `delete_selection_requested()`, `guide_undo_requested()`, `guide_clear_requested()`, `markup_visibility_changed(bool)`, `mixed_array_requested(object)`.

- [ ] **Step 1: Write failing toolbar and enablement tests**

```python
def test_select_point_guide_measure_array_are_exclusive_and_select_is_default(qtbot, panel):
    assert panel._select_tool_button.isChecked()
    qtbot.mouseClick(panel._guide_tool_button, Qt.LeftButton)
    assert panel._guide_tool_button.isChecked()
    assert not panel._select_tool_button.isChecked()

def test_markup_eye_is_independent_and_prunes_guides_when_hidden(qtbot, panel):
    panel.set_selection(SelectionModel(frozenset({"route-1", "guide-1"})))
    panel.set_entities(route_and_guide_entities())
    qtbot.mouseClick(panel._markup_visibility_button, Qt.LeftButton)
    assert panel.selection.ids == frozenset({"route-1"})
    assert panel._select_tool_button.isChecked()

def test_point_guide_delete_clear_undo_and_array_disable_when_edit_is_unsafe(panel):
    panel.set_route_edit_enabled(False)
    assert not panel._point_tool_button.isEnabled()
    assert not panel._guide_tool_button.isEnabled()
    assert not panel._selection_delete_button.isEnabled()
    assert not panel._guide_clear_button.isEnabled()
    assert not panel._guide_undo_button.isEnabled()
    assert not panel._route_array_create_button.isEnabled()

def test_array_create_requires_one_selected_visible_entity(panel):
    panel.set_route_edit_enabled(True)
    panel.set_selection(SelectionModel())
    assert not panel._route_array_create_button.isEnabled()
    panel.set_selection(SelectionModel(frozenset({"guide-1"})))
    panel.set_entities([guide_entity("guide-1")])
    assert panel._route_array_create_button.isEnabled()

def test_array_page_has_no_origin_extent_or_replace_controls(panel):
    assert not hasattr(panel, "_route_array_origin_x_spin")
    assert not hasattr(panel, "_route_array_pick_extent1_button")
    assert not hasattr(panel, "_route_array_pick_extent2_button")
    assert not hasattr(panel, "_route_array_replace_checkbox")
```

- [ ] **Step 2: Run tests and verify failures**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_navigator_panel.py -q`

Expected: FAIL because Point/Guide/Markup and mixed selection controls do not exist.

- [ ] **Step 3: Implement laconic tool controls**

Add Point and Guide tool buttons to the exclusive button group, keep Rotate outside it, and add an independent checkable eye button with tooltip `Show Markup`. Guide options contain only `Undo Last` and `Clear All`. The Select page reports selection count and offers `Delete`. Add a WindowShortcut `Delete`; retain the existing Esc shortcut behavior.

- [ ] **Step 4: Replace old Array fallback UI**

Remove Origin X/Y, Pick Origin, Pick Extent 1/2, and Replace. Keep direction length/angle, count, Pick Direction 1/2, serpentine, Create, Cancel. Emit:

```python
MixedArrayRequest(
    source_ids=self._selection.ids,
    direction_1=self._route_array_dir1_step(),
    count_1=self._route_array_dir1_count_spin.value(),
    direction_2=self._route_array_dir2_step(),
    count_2=self._route_array_dir2_count_spin.value(),
    serpentine=self._route_array_serpentine_checkbox.isChecked(),
)
```

Preview points and segments from the selected mixed source. Do not mutate selection after Create.

- [ ] **Step 5: Synchronize route table and canvas selection by stable IDs**

Block table signals while applying an external selection snapshot. On user row selection, translate rows to RoutePoint IDs and emit replace/add/invert using current keyboard modifiers. Preserve multiple rows; maintain `_selected_route_point_index` only as the primary row required by existing measurement controls.

- [ ] **Step 6: Run panel and route-control regression tests**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_navigator_panel.py tests/route/test_control_state.py tests/app/test_main_route_control.py -q`

Expected: PASS; existing Pause/Resume/Interrupt assertions unchanged.

- [ ] **Step 7: Commit**

```powershell
git add probe_station_gui/views/design_navigator_panel.py probe_station_gui/views/design_navigator_enablement.py tests/ui/test_design_navigator_panel.py
git commit -m "feat: expose markup and mixed selection tools"
```

---

### Task 7: Main-Owned Markup Lifecycle and Atomic Mixed Commits

**Files:**
- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Modify: `probe_station_gui/views/main_window_auxiliary.py`
- Modify: `probe_station_gui/views/main_window_shutdown.py`
- Modify: `main.py`
- Modify: `tests/app/test_main_design_navigation.py`
- Modify: `tests/app/test_main_route_measurement_session.py`

**Interfaces:**
- Consumes: plot/panel signals, store controller, mixed operation plans, existing `_route_edit_enabled` safety state.
- Produces: one controller path for add guide, point, selection, delete, clear, undo, visibility, mixed array, load decision, explicit unload, and shutdown.

- [ ] **Step 1: Write failing lifecycle and safety integration tests**

```python
def test_design_load_restores_matching_markup_and_normal_close_preserves_it(main, saved_markup):
    main._on_design_load_finished(document_for(saved_markup.source_path))
    complete_markup_load(main, saved_markup)
    assert main._design_markup == saved_markup
    main.close()
    assert persisted_markup(saved_markup.source_path) == saved_markup

def test_explicit_unload_deletes_markup_file(main, saved_markup):
    load_markup(main, saved_markup)
    main._unload_design_document()
    wait_for_markup_store(main)
    assert not persisted_markup_path(saved_markup.source_path).exists()

@pytest.mark.parametrize("choice, expected_count, accepted", [
    (FingerprintChoice.KEEP, 1, True),
    (FingerprintChoice.START_EMPTY, 0, True),
    (FingerprintChoice.CANCEL, 0, False),
])
def test_changed_fingerprint_choices(main, changed_document, old_markup, choice, expected_count, accepted):
    main._fingerprint_choice = lambda *args: choice
    result = main._apply_loaded_markup(changed_document, old_markup)
    assert result == accepted
    assert len(main._design_markup.guides) == expected_count

def test_corrupt_markup_does_not_block_design_render(main, document):
    main._on_design_load_finished(document)
    main._on_markup_load_failed("Markup could not be loaded.")
    assert main.design_layout_window._document is document
    assert main._design_markup.guides == ()

def test_unsafe_markup_only_delete_changes_neither_memory_nor_disk(main, saved_markup):
    load_markup(main, saved_markup)
    main._set_route_edit_enabled(False)
    before = persisted_markup(saved_markup.source_path)
    main._delete_design_selection({saved_markup.guides[0].id})
    assert main._design_markup == saved_markup
    assert persisted_markup(saved_markup.source_path) == before

def test_mixed_delete_and_array_apply_route_and_markup_together(main, route, saved_markup):
    load_route_and_markup(main, route, saved_markup)
    main._delete_design_selection({route.points[0].id, saved_markup.guides[0].id})
    assert route.points == [] and main._design_markup.guides == ()
```

- [ ] **Step 2: Run tests and verify missing controller behavior**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_design_navigation.py -q`

Expected: FAIL on missing markup lifecycle hooks.

- [ ] **Step 3: Add per-design state and non-blocking load lifecycle**

Initialize one `MarkupStoreController` lazily with the design window. On accepted GDS load, request the sidecar asynchronously and show the design immediately. Match source path/fingerprint on response. For a changed fingerprint, show `QMessageBox` buttons `Keep Markup`, `Start Empty`, `Cancel`; Keep republishes with the new fingerprint, Start Empty deletes the old sidecar, Cancel restores the pre-load design/session state.

- [ ] **Step 4: Apply mixed mutations through one validated boundary**

```python
def _apply_mixed_design_edit(self, plan: MixedDeletePlan | MixedArrayPlan) -> bool:
    if not plan.accepted or not self._design_route_edit_is_safe():
        self._show_design_status(plan.status_message or "Design editing is locked.")
        return False
    route_before = tuple(self._design_session.route.points) if self._design_session.route else ()
    markup_before = self._design_markup
    try:
        design_navigation.apply_route_entity_changes(self._design_session, plan)
        self._design_markup = apply_markup_entity_changes(markup_before, plan)
    except (DesignModelError, ValueError):
        if self._design_session.route is not None:
            self._design_session.route.points[:] = route_before
        self._design_markup = markup_before
        return False
    self._publish_design_markup()
    self._refresh_design_route_and_markup()
    return True
```

Prevalidate before mutation; rollback is only a defensive last line. Guide commits track a direct-guide ID stack for `Undo Last`; array-created guides do not enter it. Clear All deletes all guides and publishes a store delete. Hidden visibility prunes guide IDs from selection. All mutation entry points re-check the same live safety predicate at execution time.

- [ ] **Step 5: Wire signals and shutdown without route-control changes**

Connect window/panel signals in `main_window_auxiliary.py`; add owner Protocol method declarations. On normal shutdown, request store stop after the newest snapshot is queued but do not delete. On explicit design unload, queue delete for that source before clearing in-memory markup. Do not touch route runner, Pause/Resume/Interrupt, autofocus, stage, serial, or API-route-control functions.

- [ ] **Step 6: Run integration and safety regressions**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_design_navigation.py tests/app/test_main_route_measurement_session.py tests/app/test_main_route_control.py tests/route/test_measurement_api_route_control.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add main.py probe_station_gui/views/design_navigator_panel.py probe_station_gui/views/main_window_auxiliary.py probe_station_gui/views/main_window_shutdown.py tests/app/test_main_design_navigation.py tests/app/test_main_route_measurement_session.py
git commit -m "feat: integrate persistent mixed design editing"
```

---

### Task 8: End-to-End Acceptance and Regression Gate

**Files:**
- Modify only files required by failures discovered in this task.

**Interfaces:**
- Consumes: completed Tasks 1-7.
- Produces: verified feature branch with no known design, KLayout, route-control, or full-suite regression.

- [ ] **Step 1: Run focused feature suites**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_selection_geometry.py tests/design/test_markup.py tests/design/test_markup_store.py tests/design/test_selection_model.py tests/ui/test_design_plot_selection.py tests/ui/test_design_navigator_panel.py tests/app/test_main_design_navigation.py -q
```

Expected: PASS.

- [ ] **Step 2: Run existing KLayout/design regression suites**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_klayout_geometry.py tests/design/test_klayout_workers.py tests/design/test_klayout_document.py tests/design/test_klayout_real_smoke.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_klayout_raster.py tests/design/test_navigation_adapter.py tests/design/test_workflow.py -q
```

Expected: PASS; real smoke may skip only for an already-documented missing optional dependency/data condition.

- [ ] **Step 3: Run route safety regressions**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_route_control.py tests/app/test_main_route_measurement_session.py tests/route/test_control_state.py tests/route/test_control_operation.py tests/route/test_measurement_api_route_control.py tests/route/test_measurement.py -q
```

Expected: PASS.

- [ ] **Step 4: Run configured lint and full repository suite**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check .
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
```

Expected: Ruff PASS and full pytest PASS.

- [ ] **Step 5: Perform bounded real-GDS interaction smoke**

Using the existing real two-layer fixture and test harness, verify: opening does not block on markup load; guide edit does not change KLayout worker identities; two diagonals expose one intersection snap; Point emits that intersection; zoom/minimap/local snap remain within the existing smoke thresholds. Do not start hardware, camera, serial, or `main.py`.

- [ ] **Step 6: Review final diff and commit verification fixes**

```powershell
git diff --check
git status --short
git add probe_station_gui tests
git commit -m "fix: close design markup regressions"
```

Expected: `git diff --check` produces no output. Skip the final commit if verification required no code changes.
