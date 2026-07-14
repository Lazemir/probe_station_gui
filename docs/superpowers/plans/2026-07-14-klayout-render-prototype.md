# KLayout Render Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a throwaway, one-command PySide6 viewer that evaluates KLayout offscreen rendering and local hierarchical snap on a real GDS.

**Architecture:** A custom Qt canvas receives PNG frames from a dedicated KLayout `LayoutView` thread and snap results from a separate `klayout.db.Layout` thread. Both workers coalesce pending work, and the production design modules remain untouched.

**Tech Stack:** Python 3.11, PySide6, KLayout 0.30.9, standard-library threading.

## Global Constraints

- Run Python only through `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.
- Do not start `main.py`, camera, serial, motion, autofocus, route, or local API code.
- Keep GDS parsing, rendering, and geometry queries outside the GUI thread.
- Treat the implementation as throwaway prototype code; no production imports, persistence, or test-suite abstractions.
- Use `klayout==0.30.9` only as an optional prototype dependency.

---

### Task 1: Runnable KLayout design canvas

**Files:**
- Create: `probe_station_gui/prototypes/__init__.py`
- Create: `probe_station_gui/prototypes/klayout_design_viewer.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: one optional positional GDS path and PySide6 mouse/resize events.
- Produces: `python -m probe_station_gui.prototypes.klayout_design_viewer [design.gds]`, an interactive standalone prototype window.

- [x] **Step 1: Register the isolated optional dependency**

Add the following optional dependency group without changing production
dependencies:

```toml
klayout-prototype = [
    "klayout==0.30.9",
]
```

- [x] **Step 2: Implement the render worker**

Create a daemon thread that owns `klayout.lay.LayoutView`, calls
`load_layout(path, False)` and `max_hier()`, emits cell bounds/layers/load time,
then renders only the latest request with:

```python
pixels = view.get_pixels_with_options(
    request.width,
    request.height,
    1,
    2,
    0,
    db.DBox(*request.render_box),
)
signals.frame_ready.emit(
    request.generation,
    bytes(pixels.to_png_data()),
    request.render_box,
    elapsed_ms,
)
```

Layer visibility must be applied through `begin_layers()` and
`set_layer_properties(iterator, properties)` before rendering.

- [x] **Step 3: Implement the snap worker**

Create a second daemon thread that owns `klayout.db.Layout`, loads the same
file independently, and answers only its latest request. For every visible
layer, query:

```python
iterator = top_cell.begin_shapes_rec_touching(
    layer_index,
    db.DBox(x - radius, y - radius, x + radius, y + radius),
)
```

Transform returned box, polygon, path, and edge geometry with
`iterator.dtrans()`. Compare cursor distance to vertices and closest points on
segments, and emit the closest target inside the requested radius together
with elapsed time and inspected shape count.

- [x] **Step 4: Implement the observable Qt canvas**

Create a `QWidget` that:

- maps between screen and design coordinates using an aspect-correct view box;
- translates the current expanded render frame during pan;
- sends coalesced render requests after pan, zoom, resize, fit, and layer changes;
- sends coalesced hover snap requests and a distinct click request;
- draws the hovered snap target and numbered placed points above the frame;
- exposes Open, Fit, Clear points, visible-layer checkboxes, snap radius, and
  load/render/snap timings in a compact toolbar/status area.

- [x] **Step 5: Verify the command without hardware**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m probe_station_gui.prototypes.klayout_design_viewer --help
```

Expected: exit code 0 and usage containing the optional `design` argument.

Run a headless timed smoke that opens the recent real GDS, waits for render and
snap readiness, performs one render and one local snap query, then exits.
Expected: exit code 0, non-empty PNG data, both layers reported, and a snap
query completing without production GUI or hardware imports.

- [x] **Step 6: Commit the prototype**

```powershell
git add pyproject.toml probe_station_gui/prototypes docs/superpowers
git commit -m "prototype: try embedded KLayout rendering"
```

### Task 2: User evaluation notes

**Files:**
- Create: `probe_station_gui/prototypes/KLAYOUT_PROTOTYPE_NOTES.md`

**Interfaces:**
- Consumes: the user's interactive verdict on rendering, labels, layer 2, and snap.
- Produces: a durable keep/rewrite/delete decision before production integration.

- [x] **Step 1: Record the experiment question and launch command**

Create a short note that labels the code as throwaway, lists the exact launch
command, and leaves four unchecked verdict rows: pan/zoom quality, frame-label
latency, layer-2 latency, and snap responsiveness.

- [ ] **Step 2: Record the decision after the user tries it**

After feedback, replace the unchecked rows with observations and one decision:
delete, revise the prototype, or rewrite the validated approach for production.
