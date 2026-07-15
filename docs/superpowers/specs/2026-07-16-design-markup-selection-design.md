# Design Markup and CAD-Style Selection

**Status:** Approved in conversation on 2026-07-16. Written for user review
before implementation planning.

**Branch:** `codex/klayout-design-renderer`

## Context

The design window can render and locally snap to large hierarchical GDS files,
but it cannot create construction geometry. The operator needs temporary guide
segments to locate pad centers precisely, then place route points at guide
endpoints, midpoints, or intersections.

The existing toolbar also labels its default action `Select`, although a click
while route editing currently adds a route point. Selection is primarily driven
through the route table. The new feature makes `Select` a real, shared CAD-style
selection mechanism and moves route-point creation into an explicit `Point`
tool.

## Goals

- Add a visible, hideable `Markup` layer of finite dashed guide segments.
- Snap to guide endpoints, midpoints, and finite-segment intersections.
- Make `Select` the default selection tool for route points and guide segments.
- Match SOLIDWORKS box/cross selection direction, color, line style, and
  modifier behavior.
- Let `Array` copy any selected mix of route points and guide segments as one
  geometric group.
- Share hit-testing, selection, deletion, translation, and array behavior
  through one geometric entity interface.
- Restore markup after application restart without modifying the source GDS.
- Preserve route-measurement safety and keep all design mutations disabled
  when route editing is unsafe.

## Non-goals

- Modifying, augmenting, or rewriting the source GDS.
- Creating a combined temporary GDS for display.
- Loading markup into KLayout render, snap, or minimap databases.
- Infinite construction lines, circles, polygons, or freehand markup.
- Selecting GDS polygons, paths, labels, cells, or layers.
- Lasso selection or SOLIDWORKS selection filters.
- Changing Pause, Resume, Interrupt, autofocus, contact, or measurement
  semantics.
- Exposing markup through the external API in this pass.

## Terminology

- **Route point:** an existing measurement-route point with measurement-domain
  metadata and a design-space center.
- **Guide segment:** one finite dashed segment stored in the `Markup` layer.
- **Selectable entity:** the shared geometric projection of a route point or a
  guide segment.
- **Box selection:** left-to-right selection that requires full containment.
- **Cross selection:** right-to-left selection that includes contained and
  crossed entities.
- **Source selection:** the entities copied by `Array`.

## Toolbar and Tool Behavior

The design toolbar contains these mutually exclusive tools:

1. `Select` — default selection tool.
2. `Point` — place route points.
3. `Guide` — create guide segments.
4. `Measure` — existing ruler behavior.
5. `Array` — copy selected entities in a two-direction pattern.
6. `Rotate` — existing immediate rotation command.

An independent eye button controls `Markup` visibility. It is not part of the
exclusive tool group.

### Select

- A click on a visible route point or guide segment replaces the selection with
  that entity.
- A click on empty space clears the selection.
- A left-button drag beginning in empty graphics space performs box or cross
  selection after the normal drag threshold is exceeded.
- Middle-button drag pans the view. The wheel retains the current zoom behavior.
- Route-table multi-selection and canvas route-point selection are bidirectionally
  synchronized.
- Hidden markup is neither selectable nor retained as a hidden selection.

### Point

- Each left click places one route point through the existing correlated snap
  path.
- The tool remains active for repeated point placement.
- It is disabled whenever route editing is unsafe.

### Guide

- The first left click sets the start point.
- Pointer motion displays a live dashed preview.
- The second left click commits the segment and clears the pending start.
- The tool remains active for repeated construction.
- `Esc` cancels only the unfinished segment. It does not delete committed
  segments.
- The options panel exposes `Undo Last` and `Clear All`.
- `Undo Last` removes the most recent segment committed directly by the Guide
  tool. It is not a general route or array undo stack.
- `Clear All` deletes every guide segment and its persisted markup file.

## SOLIDWORKS Selection Semantics

The behavior follows official SOLIDWORKS documentation:

- Left-to-right drag displays a blue solid rectangle and selects only entities
  completely inside it.
- Right-to-left drag displays a green dashed rectangle and selects entities
  inside it plus entities crossed by the rectangle boundary.
- With no modifier, the new result replaces the previous selection.
- `Shift` adds every matched entity and preserves previous entities outside the
  rectangle.
- `Ctrl` inverts membership of every matched entity and preserves previous
  entities outside the rectangle.

References:

- <https://help.solidworks.com/2026/English/SolidWorks/sldworks/c_Cross_Selection.htm>
- <https://help.solidworks.com/2026/english/swtutorialonline/c_selection.htm>

For a route point, containment and crossing use its design-space center. For a
finite straight segment, full containment requires both endpoints inside the
axis-aligned selection rectangle. Cross selection matches when any part of the
segment lies inside or intersects the rectangle boundary.

## Shared Geometric Entity Model

Selection and transform behavior live behind one small domain interface rather
than being duplicated in route and markup UI code.

```text
SelectableDesignEntity
  id: stable entity id
  owner: route | markup
  geometry: PointGeometry | SegmentGeometry
  hit_test(...)
  is_contained_by(...)
  crosses(...)
  translated(dx, dy)
```

The shared type owns geometry only. It does not absorb measurement behavior:

- A route adapter projects `RoutePoint.camera_center` as `PointGeometry` and
  retains the original route point ID/index mapping.
- A markup adapter projects a `GuideSegment` as `SegmentGeometry`.
- Translating a route entity copies the full route point using existing route
  copy semantics and translates its design-space center.
- Translating a guide entity copies both endpoints and creates a fresh guide ID.

`SelectionModel` stores stable entity IDs and implements replace, add, invert,
clear, and stale-ID pruning. UI widgets receive immutable snapshots instead of
mutating selection independently.

## Delete

`Delete` applies to the complete mixed selection:

- Selected route points are removed from the route.
- Selected guide segments are removed from markup.
- The operation first validates every ID and the shared edit-safety gate.
- If any selected mutation is unsafe or invalid, nothing is changed.
- During an active or otherwise unsafe route measurement, markup-only deletion
  is also blocked.
- The route table, canvas, array preview, selection, and persisted markup update
  from one successful in-memory operation plan.

Persistence is asynchronous, so atomicity here means the route and markup
in-memory state cannot be partially applied. A later storage error leaves the
new markup state in memory and reports the save failure.

## Array

`Array` is a selection-driven pattern command.

- At least one route point or guide segment must be selected. With no selection,
  `Create` is disabled.
- The old fallback that creates a new point grid from `Origin` is removed from
  this tool.
- The source selection can contain points, segments, or both.
- Direction 1, count 1, direction 2, count 2, `Pick Direction`, and serpentine
  traversal retain their current controls.
- `Origin`, `Pick Origin`, `Pick Extent`, and `Replace` are removed because a
  selection-driven pattern has no separate point-grid origin or replacement
  mode.
- Array cells are translation offsets from the selected source geometry. The
  zero cell represents the originals and does not create duplicates.
- Every nonzero cell copies the complete source group, preserving relative
  point and segment positions.
- Route-point copies retain their existing measurement/copy metadata behavior.
- Guide copies receive fresh stable IDs.
- Preview draws both route-point copies and dashed guide-segment copies.
- Source selection remains selected after creation so parameters can be adjusted
  or the pattern can be repeated intentionally.
- The full mixed operation is prevalidated and committed atomically. If route
  editing is unsafe, guide copies are not created either.

Serpentine affects route-point traversal/order. Guide geometry is placed in the
same cells but has no measurement order.

## Markup Geometry and Snap

Guide snap candidates are:

- both endpoints of every visible guide segment;
- the midpoint of every visible guide segment;
- intersections that lie on both finite segments.

Arbitrary projected points along a guide line are not snap candidates. Parallel
segments and collinear overlaps produce no additional intersection candidate.
Coincident candidates are deduplicated with a scale-aware geometric tolerance.

The existing 14-screen-pixel snap radius remains the single user-facing snap
radius. Markup and KLayout candidates are compared in screen space; the nearest
valid candidate wins. Stable type labels are:

- `Guide End`
- `Guide Center`
- `Guide Intersection`

For a file-backed design:

1. The GUI computes the small markup candidate set without KLayout work.
2. The existing correlated KLayout request continues independently.
3. A click action stores its best markup candidate alongside the pending request.
4. When the current KLayout result arrives, the two candidates are compared and
   exactly one action continues.
5. Existing request/config/source/stale gates remain authoritative.

Markup editing never reloads KLayout render, snap, or minimap workers. Markup,
selection rectangles, selection highlights, and array previews are native
overlays above the raster.

When `Markup` is hidden:

- guide segments and guide snap markers are not drawn;
- guide entities are removed from the active selection;
- guide candidates are excluded from snap;
- hidden guides are excluded from `Array` source selection.

## Markup Persistence

The source GDS remains immutable. Markup is stored under:

```text
%LOCALAPPDATA%\ProbeStationGUI\Markup\<normalized-path-hash>.json
```

The JSON document contains:

- schema version;
- normalized absolute source path;
- source fingerprint (`size` and nanosecond modification time);
- layer visibility;
- guide records with stable IDs and two design-space endpoints.

Selection, pending construction, and undo history are not persisted.

Persistence rules:

- A normal application close preserves markup for restart.
- A committed guide edit, mixed delete, mixed array, or visibility change
  publishes an immutable persistence snapshot.
- `Clear All` publishes a delete operation and removes the persisted markup
  file instead of writing an empty replacement.
- An explicit design `Unload` deletes the stored markup for that source.
- A dedicated persistence worker performs file parsing/writing off the GUI
  thread, coalesces pending saves to the newest snapshot, and uses same-directory
  temporary-file replacement for atomic writes.
- A read or write failure does not block GDS rendering. The current markup state
  remains usable in memory and the UI shows concise product text.

If the same source path has a different fingerprint, loading pauses for one
choice:

- `Keep Markup` — retain guides and bind them to the new fingerprint.
- `Start Empty` — delete the old markup and load an empty layer.
- `Cancel` — cancel loading the changed design.

## Safety and Enablement

All geometry mutations use the existing route-edit enablement/safety state:

- `Point`, `Guide`, `Delete`, `Clear All`, `Undo Last`, and `Array Create` are
  disabled when route editing is unsafe.
- No markup-only exception bypasses that gate.
- Read-only selection and layer visibility may remain available while a route is
  running, but selected geometry cannot be mutated.
- Pause, Resume, Interrupt, route checkpoints, autofocus restoration, serial
  safety, and stage movement are outside this feature and must remain unchanged.

## Component Boundaries

The implementation should keep these responsibilities separate:

- `design/selection_geometry.py`: Qt-free point/segment geometry, hit testing,
  box/cross predicates, intersections, deduplication, and translations.
- `design/selection_model.py`: stable IDs, entity adapters, immutable selection
  updates, delete/array operation planning.
- `design/markup.py`: guide document, schema, fingerprint decisions, and pure
  serialization.
- `design/markup_store.py`: background newest-only load/save/delete worker and
  atomic filesystem replacement.
- `views/design_plot_pane.py`: pointer event routing and native overlay items.
- `views/design_navigator_panel.py`: toolbar/options, enablement, route-table
  selection synchronization, and product-facing messages.
- The existing main/design navigation adapter remains the owner of route-domain
  mutation and applies mixed operation plans without using the localhost API.

Exact filenames may be adjusted during implementation planning if an existing
module offers a deeper interface, but geometry, state, persistence, view, and
route side effects must remain separated.

## Error Handling

- A stale or failed KLayout snap request follows the existing correlated failure
  behavior and does not execute a raw-coordinate click.
- Corrupt markup JSON is not applied. The design still opens with markup
  disabled and a short recovery message.
- A failed save retains the in-memory state and shows `Markup could not be saved.`
- Invalid or stale mixed-selection IDs reject the entire mutation and refresh
  selection from current entities.
- Array parameters that produce no nonzero cells leave state unchanged.
- Degenerate zero-length guides are not committed.

## Testing and Acceptance

### Pure geometry

- Point and segment click hit-testing.
- Left-to-right full containment.
- Right-to-left crossing behavior.
- Segment midpoint and finite intersection generation.
- Parallel, collinear, endpoint-touch, and coincident-candidate cases.
- Translation of mixed point/segment groups.

### Selection and operations

- Replace, `Shift` add, `Ctrl` invert, clear, and stale-ID pruning.
- Canvas/table route-point synchronization.
- Mixed delete and array planning.
- One selected entity is sufficient for `Array`.
- No-selection array is disabled.
- Relative geometry and zero-cell exclusion.
- All-or-nothing rejection under unsafe route state.

### Persistence

- Path hashing, schema round trip, and stable IDs.
- Normal restart restoration.
- Explicit unload deletion.
- Visibility persistence.
- Fingerprint choices: keep, start empty, cancel.
- Corrupt JSON recovery.
- Newest-only coalescing and atomic replacement.

### UI and integration

- Toolbar exclusivity and separate visibility eye.
- `Select`, `Point`, and `Guide` pointer state machines.
- Blue solid box and green dashed cross-selection rectangle.
- Drag threshold and middle-button pan coexistence.
- Single-click selection, empty-click clear, `Delete`, `Esc`, `Undo Last`, and
  `Clear All`.
- Mixed array point/segment preview.
- Markup hidden means no draw, selection, snap, or array source.
- KLayout and markup candidate arbitration under stale and failed responses.
- No KLayout worker reload on markup edits.
- Route Pause/Resume/Interrupt regression tests remain unchanged and passing.

### Verification

- Focused design geometry/model/store/UI suites.
- Existing KLayout worker, canvas, snap, minimap, and design navigation suites.
- Full repository pytest and configured Ruff gate.
- Real two-layer GDS smoke proving markup interaction does not regress render,
  zoom, minimap, or local-snap responsiveness.

## Success Criteria

The feature is complete when an operator can draw two snapped guide diagonals,
switch to `Point`, place a route point at their intersection, select mixed route
points and guides with SOLIDWORKS-style boxes, copy that mixed group with
`Array`, restart the application and recover the markup, and perform none of
those mutations while route editing is unsafe. No operation may rewrite the
source GDS or reload KLayout solely because markup changed.
