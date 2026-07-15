# Embedded KLayout Design Renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the production full-polygon design presentation, minimap, and global snap index with embedded KLayout rendering and local hierarchy queries while preserving all existing overlays and actions.

**Architecture:** `DesignDocument` loads only immutable file metadata with a temporary KLayout database. Independent daemon workers own the long-lived KLayout render and snap databases; the GUI owns only raster frames, viewport scheduling, overlays, and asynchronous click continuations. The microscope minimap uses a separate coalescing low-resolution render worker so it cannot postpone the design canvas.

**Tech Stack:** Python 3.11, PySide6, pyqtgraph, NumPy, `klayout==0.30.9`, pytest.

## Global Constraints

- Run Python only through `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`.
- Do not start `main.py`, camera, serial, motion, autofocus, contact, or route hardware during automated verification.
- Keep GDS parsing, rendering, PNG decoding, and geometry queries outside the GUI thread.
- Keep route, registration, measurement, Pause/Resume/Interrupt, and API route-control semantics unchanged.
- Use `MAX_RENDER_DIMENSION_PX = 4096`, KLayout oversampling 1, a 16 ms active scheduler, a 120 ms settle timer, a 4% pan guard, and a 12.5% settled margin.
- Preserve midpoint, vertex, segment, full-segment highlight, and screen-space snap-radius behavior.
- Import KLayout lazily only after a design restore/open begins.

---

### Task 1: Lightweight file-backed document metadata

**Files:**
- Modify: `pyproject.toml`
- Modify: `probe_station_gui/design/model.py`
- Test: `tests/design/test_klayout_document.py`

**Interfaces:**
- Consumes: a GDS/OASIS path.
- Produces: `DesignDocument.load(path)` with `file_backed=True`, `available_layers`, `cell_bounds`, metadata-only `with_visible_layers`, `with_top_cell`, and quarter-turn rotation.

- [ ] **Step 1: Write failing metadata tests**

Create a synthetic KLayout GDS in `tmp_path`, then assert that `DesignDocument.load` exposes both layers, bounds and cells while `polygons_by_layer`, `plot_paths_by_layer`, and snap arrays stay empty. Assert that toggling layers does not call `_extract_polygons` and that odd rotations swap width/height around the same centre.

- [ ] **Step 2: Verify RED**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_klayout_document.py -q
```

Expected: failures because file-backed metadata fields and the KLayout loader do not exist.

- [ ] **Step 3: Implement the metadata path**

Move `klayout==0.30.9` into production dependencies. Add immutable metadata fields with defaults so existing in-memory fixtures remain valid. In `load`, import `klayout.db` locally, read the layout, select the same effective top cell policy, convert `bbox().to_dtype(layout.dbu)` to design coordinates, collect layer/datatype pairs and top-cell bounds, then discard the layout before returning.

- [ ] **Step 4: Verify GREEN and existing model tests**

Run the new test plus `tests/design/test_workflow.py` and `tests/design/test_click_navigation.py`; expect PASS.

- [ ] **Step 5: Commit**

Commit message: `feat: load design metadata with KLayout`.

### Task 2: Immutable render/snap contracts and pure geometry

**Files:**
- Create: `probe_station_gui/design/klayout_types.py`
- Create: `probe_station_gui/design/klayout_geometry.py`
- Test: `tests/design/test_klayout_types.py`
- Test: `tests/design/test_klayout_geometry.py`

**Interfaces:**
- Produces: `KLayoutConfig`, `RenderRequest`, `RenderFrame`, `SnapRequest`, `SnapResponse`, `PendingClick`; `inverse_rotate_box`, `forward_rotate_point`; `select_snap(raw_point, vertices, segments, radius)`.

- [ ] **Step 1: Write failing contract and selection tests**

Cover all four quarter turns, box round trips, nearest midpoint versus vertex, the 1.8 vertex priority ratio, segment projection, free fallback, and full segment endpoints for `segment` and `segment_center`.

- [ ] **Step 2: Verify RED**

Run both new test files; expect import failures for the missing modules.

- [ ] **Step 3: Implement minimal immutable types and geometry**

Use frozen dataclasses and pure float tuple helpers. Keep KLayout and Qt imports out of both modules. Return the existing `SnapResult` contract from the selector.

- [ ] **Step 4: Verify GREEN**

Run both new files and `tests/design/test_navigation_geometry.py`; expect PASS.

- [ ] **Step 5: Commit**

Commit message: `feat: define KLayout render and snap contracts`.

### Task 3: Independent coalescing workers

**Files:**
- Create: `probe_station_gui/design/klayout_workers.py`
- Test: `tests/design/test_klayout_workers.py`
- Test: `tests/design/test_klayout_real_smoke.py`

**Interfaces:**
- Produces: `KLayoutRenderWorker.submit(RenderRequest)`, `KLayoutSnapWorker.submit_hover(SnapRequest)`, `submit_click(SnapRequest)`, `stop(timeout_s=...)`; Qt signals `frame_ready`, `snap_ready`, `failed`, and `loaded`.

- [ ] **Step 1: Write failing worker-contract tests**

Inject fake render and snap backends. Assert newest-only render/hover behavior, FIFO click priority, independent ownership, no signal emission while the condition lock is owned, stale configuration replacement, bounded stop, and late-response suppression.

- [ ] **Step 2: Verify RED**

Run `tests/design/test_klayout_workers.py`; expect import failure.

- [ ] **Step 3: Implement worker queues and KLayout backends**

Each worker owns one daemon Python thread and creates all KLayout objects inside it. The render backend loads a Qt-less `LayoutView`, applies top cell/full hierarchy/visible layers, renders oversampling 1, decodes a detached vertically-oriented `QImage`, and emits timing. The snap backend owns a separate `db.Layout`, queries `begin_shapes_rec_touching`, applies recursive transforms, and calls the pure selector.

- [ ] **Step 4: Verify GREEN and real synthetic smoke**

Generate a temporary two-layer hierarchical GDS. Assert a non-empty frame and a local snap result without importing `main.py` or hardware modules.

- [ ] **Step 5: Commit**

Commit message: `feat: add independent KLayout workers`.

### Task 4: Raster viewport scheduler and design plot integration

**Files:**
- Create: `probe_station_gui/views/design_klayout_raster.py`
- Modify: `probe_station_gui/views/design_plot_pane.py`
- Modify: `probe_station_gui/views/design_navigator_panel.py`
- Test: `tests/ui/test_design_klayout_raster.py`
- Test: `tests/ui/test_design_plot_klayout.py`

**Interfaces:**
- Produces: `KLayoutRasterItem.set_frame(frame)`, `KLayoutRasterController.set_document(document)`, `request_exact()`, `request_settled()`, `shutdown()`; asynchronous plot click continuation keyed by snap request id and configuration generation.

- [ ] **Step 1: Write failing raster/controller tests**

Assert raster z-order below all overlays, immediate reuse of the previous frame during pan, exact zoom request, 12.5% settle margin, oversampling 1, 4096-pixel cap, physical-pixel sizing, coalescing, and stale-frame rejection.

- [ ] **Step 2: Verify RED**

Run both new UI test files offscreen; expect missing module/controller failures.

- [ ] **Step 3: Implement raster/controller**

Use one `QGraphicsObject` for the image and exact world box. Connect range changes to 16 ms and 120 ms timers. Never clear a valid frame while a request is pending.

- [ ] **Step 4: Replace plot document paths and global snap**

For file-backed documents, remove active calls to `visible_plot_paths`, `_start_snap_geometry_build`, and synchronous `_resolve_snap_result`. Configure render/snap workers, forward hover queries, queue click actions, accept only matching generations, and highlight full segments for both `segment` and `segment_center`. Retain the legacy in-memory path only for existing fixture documents.

- [ ] **Step 5: Verify GREEN and existing navigator tests**

Run the new UI tests plus `tests/ui/test_design_navigator_panel.py` and `tests/app/test_main_design_navigation.py`; expect PASS.

- [ ] **Step 6: Commit**

Commit message: `feat: render design canvas with KLayout`.

### Task 5: KLayout microscope minimap

**Files:**
- Modify: `probe_station_gui/views/microscope_view.py`
- Test: `tests/ui/test_microscope_view_minimap.py`
- Test: `tests/ui/test_microscope_view_klayout_minimap.py`

**Interfaces:**
- Consumes: the same file-backed `DesignDocument` metadata.
- Produces: newest-only full-cell minimap render requests and cached `QPixmap` delivery; native route/mark/FOV overlays remain unchanged.

- [ ] **Step 1: Write failing minimap tests**

Assert that assigning a file-backed document never calls `visible_polygons`, resize retains only the newest request, a stale frame is ignored, layer/rotation changes invalidate the frame, and document removal stops the worker without blocking the GUI.

- [ ] **Step 2: Verify RED**

Run the two minimap test files; expect failures because the current renderer iterates every polygon.

- [ ] **Step 3: Implement worker-backed minimap**

Use a dedicated `KLayoutRenderWorker` with one low-resolution full-bounds request. Keep the existing placeholder and overlay drawing while pending. Remove `_render_minimap_background_image` from the file-backed path and prevent parallel per-resize threads.

- [ ] **Step 4: Verify GREEN**

Run both minimap test files; expect PASS with no old `MINIMAP RENDER simplified` path for a file-backed document.

- [ ] **Step 5: Commit**

Commit message: `feat: render design minimap with KLayout`.

### Task 6: Production cleanup and verification

**Files:**
- Modify: `probe_station_gui/prototypes/` (remove throwaway implementation after parity is covered)
- Modify: `pyproject.toml` (remove prototype-only dependency group)
- Modify: relevant tests and docs only if verification exposes a contract gap.

**Interfaces:**
- Produces: one production KLayout path with no prototype duplication.

- [ ] **Step 1: Run focused regression suites**

Run all design, navigator, minimap, and main design-navigation tests through the shared virtual environment; expect PASS.

- [ ] **Step 2: Run the full repository suite and ruff**

Run `python -m pytest tests` and the configured ruff command; expect the established skips only and no new failures.

- [ ] **Step 3: Run real-GDS performance smoke**

Without launching `main.py`, measure metadata load, first full frame, ten exact zoom frames, ten settled frames, layer toggle, minimap, and local snap on the recent two-layer GDS. Acceptance: zoom p95 <=150 ms, settled p95 <=180 ms, snap p95 <=20 ms, and no global polygon/snap preprocessing.

- [ ] **Step 4: Remove prototype and diagnostics debris**

Delete the throwaway viewer only after production smoke passes. Search for stale `DESIGN SNAP geometry ready`, `MINIMAP RENDER simplified`, prototype dependency names, and temporary debug prefixes.

- [ ] **Step 5: Final review and commit**

Review the complete branch against the design spec, then commit with the measured cause in the message body: full second-layer polygon traversal and global snap indexing starved the GUI.
