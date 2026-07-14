# Embedded KLayout Design Renderer

## Status

Approved production design following the interactive prototype on 2026-07-14.
The implementation lives on `codex/klayout-design-renderer`. The rejected custom
LOD implementation remains preserved on `codex/design-lod-renderer-v1`, and the
throwaway prototype remains preserved on `codex/klayout-render-prototype`.

## Problem

The custom design renderer makes geometry visibly coarse while panning, delays
frame labels and layer 2, and withholds geometry snap until a global index has
finished. The standalone KLayout prototype rendered the same hierarchy and
labels correctly and provided local snap queries without launching the KLayout
application, but it lost the old full-segment hover overlay and automatic
segment-midpoint snap. Its zoom redraw also took roughly 250 ms because every
wheel step requested a large oversampled frame.

Runtime logs confirm that the GDS document itself loaded in about one second,
while the old snap path continued returning raw coordinates for tens of seconds.
The expensive and user-visible problem is the custom presentation/index layer,
not basic document persistence or route logic.

## Goals

- Render GDS geometry, hierarchy, layers, fills, and labels with KLayout inside
  the existing design window.
- Keep every existing PySide6/pyqtgraph overlay and interaction in the same
  window: route editing, registration marks, targets, stage/FOV position,
  rulers, arrays, and navigation clicks.
- Make pan and wheel zoom immediately responsive without substituting a coarse
  overview or blocking the Qt event loop.
- Make snap independent from render completion and global preprocessing.
- Restore the previous automatic priority for midpoint, vertex, and segment
  snap, including full selected-segment highlighting.
- Preserve top-cell, rotation, visible-layer, session, route, and registration
  behavior.
- Load KLayout lazily so normal application startup remains lightweight.

## Non-goals

- Do not launch `klayout.exe`, embed a native KLayout Qt widget, or open a
  second design window.
- Do not route the feature through the application's localhost API.
- Do not change camera, serial, motion, autofocus, contact, or route-control
  semantics.
- Do not redesign route, registration, or measurement tools.
- Do not rewrite the full `DesignDocument` persistence/domain API in this
  iteration. It remains the compatibility and session-state adapter; KLayout
  becomes authoritative for displayed pixels and interactive geometry queries.
- Do not retain the throwaway prototype in the production branch after its
  validated logic has been absorbed.

## Dependency and Licensing

Add the official `klayout==0.30.9` wheel as a production dependency and remove
the prototype-only optional dependency group. The package is GPL-3.0-or-later,
which the user explicitly accepted. Import `klayout.db` and `klayout.lay` only
inside the design workers after a design is opened.

## Architecture

### Existing interactive scene

`_DesignPlotPane` keeps its pyqtgraph `PlotWidget`, `ViewBox`, and all current
overlay items. The old per-layer `PlotDataItem` document paths and the global
snap-geometry builder are removed from its active path.

A single raster graphics item sits below every overlay. It stores a `QImage`
and the exact design-space box represented by that image. Qt transforms this
item synchronously during pan and zoom, so pointer interaction never waits for
KLayout. When a newer frame arrives, the item replaces its image and world box
without rebuilding the other widgets or overlays.

### Render worker

One daemon worker thread exclusively owns a Qt-less
`klayout.lay.LayoutView`. It:

1. lazily loads the current file;
2. selects the requested top cell;
3. enables full hierarchy depth with `max_hier()`;
4. applies visible-layer state through KLayout layer properties;
5. renders the requested source-space box with
   `get_pixels_with_options`;
6. decodes the returned PNG into a detached `QImage` off the GUI thread; and
7. emits an immutable frame value containing the image, design-space box,
   configuration generation, viewport generation, density, and timing.

All KLayout objects remain on this thread. Requests are coalesced: at most one
render is in flight and one newest pending request is retained. No Qt signal is
emitted while a non-reentrant lock is held.

### Snap worker

A second daemon worker thread exclusively owns an independent
`klayout.db.Layout`. It loads the same file in parallel with the renderer and
uses `Cell.begin_shapes_rec_touching` for a small design-space box around the
cursor. This is a local hierarchical query; it does not flatten the design or
build an application-wide index.

Hover requests are replaceable and coalesced. Click requests use a small FIFO
with priority over hover so every deliberate click receives an exact result.
Rendering can never postpone snap because the workers do not share a KLayout
layout or queue.

### File-backed domain compatibility

`DesignDocument` continues to carry path, selected top cell, visible layers,
rotation, bounds, units, and route/session compatibility data. Its existing GDS
load remains in the current background load flow for this iteration. The design
plot no longer iterates its polygon/path caches and no longer builds its snap
arrays. This sharply limits the production change while removing the two
user-visible bottlenecks. Replacing the remaining compatibility loader with
KLayout can be evaluated separately after this renderer is proven in daily use.

## Coordinate and Rotation Rules

KLayout DBox coordinates are treated as design micrometres, matching the
existing plotted coordinate space. A configuration carries the document's
`rotation_quarter_turns`.

For a non-zero quarter-turn rotation:

- render target corners are inverse-rotated around the unrotated selected-cell
  bounds centre before calling KLayout;
- odd rotations swap source render width and height;
- the returned image is rotated into the existing document coordinate space;
- snap query points are inverse-rotated before the recursive query; and
- returned targets and segment endpoints are forward-rotated before delivery.

Distance, midpoint, and threshold comparisons are invariant under these
quarter-turn transforms. Tests must cover all four orientations.

## Rendering and Zoom Policy

The prototype used a 25% margin, a 1.5x output in both dimensions, and KLayout
oversampling 2. On the recent real GDS this took 244-262 ms. Measurements of the
same renderer showed:

| Request | Full design | Zoomed area |
|---|---:|---:|
| Exact viewport, oversampling 1 | 78 ms | 54 ms |
| 12.5% margin, oversampling 1 | 104 ms | 83 ms |
| Prototype 25% margin, oversampling 2 | 262 ms | 244 ms |

Visual comparison at label scale showed no material readability loss with
oversampling 1. Production therefore uses oversampling 1 only.

On every pyqtgraph range change, the current raster is transformed immediately.
A 16 ms single-shot scheduler classifies the change:

- **Zoom:** request the exact current viewport at device-pixel resolution.
- **Pan:** keep translating the existing margin frame. Request a replacement
  when it no longer covers the viewport plus a 4% viewport guard.
- **Settled view:** after 120 ms without another range change, request a frame
  with a 12.5% margin at matching pixel density for subsequent pan.

The renderer retains only the newest pending request. A returned frame is
accepted when its document/layer/top-cell/rotation generation matches and
either its viewport generation is current or its world box fully covers the
current viewport at no lower density than the displayed frame. Other stale
frames are discarded.

Output dimensions are based on physical device pixels and bounded by the named
`MAX_RENDER_DIMENSION_PX = 4096` safety limit. This is not an LOD level and must
be tested on high-DPI inputs.

## Snap Semantics

The snap radius remains screen-space based and is converted to design units at
the current view scale. Only visible layers participate.

For boxes, polygons, paths, and edges returned by KLayout, apply the recursive
iterator transform and collect local vertices and segments. Ignore text origins
as standalone snap targets. For every candidate segment compute both its
closest projection and midpoint.

Selection exactly preserves the previous automatic behavior:

1. choose the nearest midpoint when it is within the snap radius and closer
   than the nearest vertex;
2. otherwise choose the nearest vertex when it wins the existing 1.8 vertex
   priority ratio over the closest segment projection;
3. otherwise choose the closest segment projection within the radius; and
4. otherwise return the raw point with mode `free`.

Results use the existing `SnapResult` contract:

- `vertex` includes the chosen point;
- `segment` includes the projected point and full source segment endpoints;
- `segment_center` includes the midpoint and full source segment endpoints;
- `free` includes the raw cursor point.

Both `segment` and `segment_center` draw the complete selected segment in the
hover overlay. The sidebar describes `segment_center` as `Center`, `segment` as
`Line`, and `vertex` as `Corner`.

## Click Semantics

Hover remains asynchronous and may be dropped. A click captures its current
semantic action and raw design coordinate, then sends a priority snap request.
The action executes only when the matching response returns:

- registration mark selection;
- route point placement;
- array/ruler route-pick actions;
- navigation move/double-click; and
- any existing left/right button distinction.

If snap is disabled, the action executes immediately at the raw coordinate. If
snap is enabled but the snap worker is still loading, the click waits; it must
not silently place a raw point. Configuration generations prevent a response
from an old document, layer set, top cell, or rotation from executing an action.

## Lifecycle and Failure Handling

- Construct workers lazily when a file-backed document is assigned to the
  design plot, not during `main.py` startup.
- Stop and detach workers when the design window closes or its document source
  changes. A bounded join is allowed only during explicit shutdown, never on
  pan, zoom, layer change, or normal document update.
- Reuse loaded layouts for layer, top-cell, and rotation changes when the file
  path is unchanged.
- A layer/configuration change invalidates hover and pending replaceable render
  work immediately.
- Log load, render, discarded-frame, slow-snap, and worker-failure timings at
  diagnostic levels without logging every mouse event.
- On load/render failure, show a short finished-product status in the design
  pane and keep overlays inert. Do not fall back to synchronous full gdstk
  rendering or global snap indexing.
- If the optional design surface is never opened, no KLayout layout/view or
  worker thread is created.

## File Boundaries

The production files and responsibilities are:

- `probe_station_gui/design/klayout_types.py`: immutable configuration,
  viewport, frame, snap-request, and pending-click values plus quarter-turn
  coordinate helpers; no worker lifecycle.
- `probe_station_gui/design/klayout_geometry.py`: KLayout shape-to-contour
  conversion and legacy-compatible local snap selection.
- `probe_station_gui/design/klayout_workers.py`: single-owner render and snap
  threads, coalescing, generation handling, and lazy KLayout imports.
- `probe_station_gui/views/design_klayout_raster.py`: the pyqtgraph-compatible
  QImage raster item and render scheduler.
- `probe_station_gui/views/design_plot_pane.py`: wiring to the existing scene,
  asynchronous click continuation, and overlay updates.
- `probe_station_gui/views/design_navigator_panel.py`: only the small snap hint
  copy update required for midpoint mode.
- `probe_station_gui/prototypes/`: removed from this production branch after
  the validated code is rewritten into the bounded production modules.

## Testing

### Pure geometry

- box, polygon, path, edge, hole, and recursive instance transforms;
- nearest vertex and segment projection;
- automatic midpoint priority matching the legacy rule;
- vertex 1.8 priority ratio;
- full segment metadata for `segment` and `segment_center`;
- visible-layer filtering and free fallback; and
- all four quarter-turn orientations.

### Worker contracts

- independent render and snap ownership;
- newest-only render and hover coalescing;
- click FIFO priority over hover;
- no signal emission while worker locks are held;
- path reuse for configuration changes;
- stale configuration/viewport rejection; and
- bounded shutdown and late-response suppression.

### UI integration

- KLayout raster remains below every existing overlay;
- document assignment no longer creates per-layer plot paths or starts global
  snap preprocessing;
- pan translates an existing covering frame without blanking or rebuilding
  overlays;
- zoom requests oversampling 1 and the exact viewport;
- settle requests the 12.5% margin;
- `segment` and `segment_center` highlight the full edge;
- click actions wait for their own snap response and preserve existing route,
  registration, and navigation semantics; and
- layer/top-cell/rotation changes invalidate stale frames and results.

### Verification

- Run the full repository test suite with the shared project virtual
  environment and local calibration fixtures available.
- Run a synthetic KLayout GDS test generated in a temporary directory; do not
  rely on the repository's placeholder `synthetic.gds`.
- Run an opt-in smoke on the recent real two-layer GDS and record first-frame,
  repeated zoom, settled-margin, layer-toggle, and snap timings.
- Do not start `main.py`, camera, serial, motion, autofocus, or route hardware
  during automated verification.

## Acceptance Criteria

- The design remains in the existing window with all current overlays and tools.
- No full KLayout GUI process or localhost API path is used.
- The recent real GDS shows hierarchy, labels, layer 1, and layer 2 in the first
  KLayout frame.
- Pan and wheel input never perform GDS parsing, rendering, PNG decoding, or
  geometry search on the GUI thread.
- On the recent real GDS, exact zoom-frame p95 is at most 150 ms and settled
  margin-frame p95 is at most 180 ms on the current workstation.
- Local hover/click snap p95 is at most 20 ms after the snap layout is ready.
- Snap readiness depends only on the independent KLayout load, not on completed
  rendering or a global index.
- Midpoint snap, vertex priority, projected segment snap, and full segment
  highlighting match the behavior defined above.
- Existing route, registration, minimap, layer, top-cell, rotation, navigation,
  Pause/Resume/Interrupt, and API route-control regression tests remain passing.
- Normal application startup does not import or instantiate KLayout rendering
  objects before a design is opened.
