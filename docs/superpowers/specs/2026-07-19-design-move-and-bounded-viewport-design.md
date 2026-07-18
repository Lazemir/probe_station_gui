# Design Move Tool and Bounded Viewport

## Purpose

Restore an explicit and predictable click-to-move workflow in Design Window,
prevent accidental navigation arbitrarily far from the loaded layout, and make
file-backed geometry snapping unable to monopolize the application at extreme
zoom levels.

The operator should be able to enter a persistent Move mode, click successive
design targets, and leave it with Escape. Normal opening still frames the GDS,
while an internal navigation envelope retains access to every route and markup
entity without exposing a separate `Fit All` command.

## Current Problems

- Click-to-move still exists internally, but navigation requires a double click
  and the default Select event filter consumes the relevant mouse events. There
  is no discoverable toolbar mode for moving the stage from the design canvas.
- The pyqtgraph ViewBox has no document-derived zoom or pan limits. Repeated
  zoom-out can make raw design coordinates grow by orders of magnitude and can
  leave the operator far from all useful content.
- Snap tolerance is converted from a fixed screen radius into unrestricted
  design units. At the extreme view observed on 2026-07-18, a KLayout hover
  query received an approximately ten-million-unit radius and spent 11.6 s
  enumerating recursive geometry.
- The snap result is discarded when it is stale, but an already-running stale
  query continues consuming the worker and can contend for the Python GIL.

## Scope

This design includes:

- a dedicated persistent Move tool in the Design Window toolbar;
- single-click design-to-stage movement with existing snapping and safety;
- drag discrimination so panning cannot issue a move;
- a dynamic, content-derived navigation envelope;
- GDS-only initial framing and a GDS-only Home action;
- bounded file-backed snap work with cooperative stale-request cancellation;
- regression tests for the new tool, viewport limits, and extreme snap cases.

This design does not:

- add a visible `Fit All` command;
- remove, hide, or discard distant route or markup entities;
- modify the SolidWorks-style Select behavior;
- modify GDS rendering or selection-rectangle composition;
- change registration mathematics, stage safety gates, precision-approach
  planning, route Pause/Resume/Interrupt behavior, or hardware protocols.

## Move Tool

### Toolbar and State

The Design Window toolbar gains a normal checkable `Move` tool immediately
after `Select`. It participates in the existing exclusive design-tool group.

Move is enabled only when:

- a design document is loaded; and
- design registration is valid, so a design coordinate can be converted to a
  stage target.

Stage busy state and needle safety remain execution-time concerns owned by the
existing controller path. The toolbar does not duplicate those safety rules.

Move remains active after a successful or rejected movement, allowing several
successive targets. Escape cancels any pending click interaction and returns to
Select, following the behavior of the other persistent tools.

### Pointer Interaction

While Move is active:

- an unmodified left click that stays below the platform drag threshold selects
  one move target;
- left drag pans the design view and never emits a movement request;
- the mouse wheel retains normal bounded zoom;
- double click has no separate meaning and cannot enqueue two moves;
- geometry and markup hover snap use the existing highlighting semantics;
- the accepted snapped point, or the exact cursor point when no snap is
  available, is emitted through the existing `move_requested` path.

The interaction uses an explicit press/move/release state rather than relying
on pyqtgraph's scene-click signal alone. This gives Move and Select the same
drag threshold and makes the distinction between pan and hardware movement
deterministic.

### Motion Execution

The canvas does not send G-code. The accepted design point follows the current
in-process path:

1. validate the active registration;
2. transform the design point to an absolute XY stage target;
3. submit it to the shared stage controller;
4. apply existing motion safety and axis-limit checks;
5. execute through the shared precision-approach mechanism.

The GUI thread performs only pointer handling and widget updates. Coordinate
conversion that depends on current controller state and all serial work remain
off the GUI thread. In-process movement is not routed through the local API.

## Navigation Envelope

### Content Bounds

Two bounds have distinct purposes:

- **GDS bounds** come from `DesignDocument.bounds` and define the initial and
  Home view.
- **Content bounds** are the union of GDS bounds and every finite visible
  coordinate belonging to the current route and persisted markup. Segment
  endpoints and rendered route offsets are included, so a distant entity always
  remains reachable and deletable.

No route or markup coordinate is classified as an outlier merely because it is
far from the GDS. Non-finite or structurally invalid coordinates remain model
validation errors and do not enter ViewBox limits.

Content bounds receive a small named proportional padding. The padded bounds
are aspect-fitted to the current canvas to produce the **navigation frame**.
Using an aspect-fitted rectangle avoids contradictory X/Y limits while the
ViewBox aspect ratio is locked.

### Zoom and Pan Limits

The navigation frame defines all four ViewBox translation limits and the
maximum X/Y ranges. Consequently:

- the wheel cannot zoom out beyond a view that contains the padded union of
  GDS, route, and markup;
- panning cannot move the viewport outside the same frame;
- if only GDS exists, the maximum view is the padded GDS;
- a very distant saved entity intentionally enlarges the maximum view, but the
  operator cannot zoom or pan beyond that entity into unbounded empty space.

The limits are installed before a range change reaches the raster controller
or hover snapping. They are not implemented as a repeating corrective timer.

On document load, the visible range is set to the GDS bounds with the existing
small framing padding, not to the full content bounds. There is no visible
`Fit All` control. Pressing Home restores the GDS-only view while leaving the
larger internal content envelope available through ordinary pan and zoom.

### Dynamic Updates

The navigation frame is recomputed when:

- the design document, top cell, rotation, or visible persistent content
  changes;
- a route point or markup entity is added, moved, arrayed, rotated, restored,
  or deleted;
- the canvas aspect ratio changes.

Expanding the frame never changes the current visible range automatically.
When the frame shrinks, an already-valid view is preserved. A view outside the
new frame is clamped once to the nearest valid range; if it is larger than the
new maximum, it is reduced to that maximum. Deleting a distant last entity
therefore cannot leave the operator stranded in now-invalid empty space.

Bounds computation is a pure helper over model coordinates. It does not parse
GDS, perform hardware I/O, or rebuild widgets.

## Bounded Geometry Snap

### Fast Rejection

Before submitting a file-backed KLayout query:

- reject non-finite points or radii;
- intersect the model-space search box with the transformed GDS bounds;
- skip KLayout geometry work immediately when there is no intersection.

Markup snapping remains available because it uses the already-materialized
markup candidates rather than recursive GDS enumeration.

### Useful-Scale Guard

The screen-space snap radius remains unchanged for normal work. If converting
that radius into design units would make the query cover an excessive share of
the GDS at the current scale, file-backed geometry snapping is temporarily
unavailable for that request. Hover clears the GDS highlight; a click continues
with the best markup snap or the exact cursor point.

The threshold is a named, documented constant based on the fraction of the GDS
bounds touched by the query, rather than a per-design absolute distance. This
keeps the behavior scale-independent across layouts expressed in different
units.

### Work and Cancellation Budgets

The KLayout backend must not materialize an unbounded number of vertices and
segments. Each request receives named limits for:

- recursive shapes inspected;
- generated geometry candidates; and
- elapsed wall time checked periodically during iteration.

Exceeding any limit aborts that snap as unavailable; partial geometry is never
reported as a trustworthy nearest result.

The worker already coalesces pending hover requests. It additionally exposes a
thread-safe cooperative cancellation check to the in-flight backend loop. A
hover request stops when a newer hover, a priority click, a configuration
generation change, or worker shutdown makes it obsolete. Click requests retain
FIFO priority but are still subject to the geometry and time budgets.

Budget exhaustion and useful-scale skips are normal debug-level diagnostics,
not user-facing render failures. Click-to-move remains usable at the exact
cursor position.

## Failure Handling

- Move is disabled when registration is unavailable rather than silently
  accepting an untransformable target.
- Registration invalidation between click and execution rejects the movement
  through the existing controller result path.
- A drag, cancelled press, click outside the plot, or Escape emits no movement.
- Axis-limit, needle-safety, busy, cancellation, and precision-approach errors
  retain their current status handling.
- Invalid navigation content is ignored for bounds and logged without blocking
  a valid GDS from opening.
- A KLayout snap skip or budget abort returns no GDS snap and cannot stall later
  hover requests.
- View limits are always derived from the current model; no stale envelope is
  persisted across documents.

## Testing Strategy

### Pure Bounds Tests

- GDS-only content and padding;
- union with route points, markup points, and guide endpoints;
- distant finite markup remains part of the envelope;
- non-finite coordinates cannot corrupt limits;
- aspect fitting for wide, tall, and resized canvases;
- expansion preserves the current view;
- deletion shrinks and clamps only when necessary.

### Move Interaction Tests

- the Move button is adjacent to Select and follows registration availability;
- one left click emits exactly one snapped target;
- repeated clicks work without reselecting the tool;
- a left drag pans and emits no movement;
- a double click cannot emit two movements;
- Escape returns to Select and cancels a pending press;
- route Point, Align, Guide, Ruler, Array, and Select semantics remain intact;
- the emitted target still reaches the shared precision-move path.

### ViewBox Tests

- document opening frames only GDS even when distant markup exists;
- Home restores GDS bounds;
- wheel zoom-out stops at the internal content frame;
- pan stops at every edge of the frame;
- deletion of the outermost entity updates the maximum range;
- resizing preserves aspect-correct limits;
- constrained ranges are visible to the raster controller, so no out-of-range
  render request is scheduled.

### Snap Tests

- a search box outside GDS returns immediately without recursive iteration;
- normal-scale vertex, segment, and center snapping remain unchanged;
- extreme zoom triggers the useful-scale guard;
- shape, candidate, and time budgets return no partial result;
- a newer hover cooperatively cancels an in-flight stale hover;
- a click preempts pending hover work without starving later hover;
- markup snap remains usable when file-backed GDS snap is skipped;
- the 2026-07-18 extreme-radius scenario completes within the bounded test
  deadline and does not delay a following normal request.

All automated tests use the shared project virtual environment and offscreen Qt
with fake or synthetic KLayout backends. No hardware-dependent code runs.

## Acceptance Criteria

- Design Window has a discoverable persistent Move tool next to Select.
- A single Move click uses snap, registration, safety checks, and the shared
  precision approach; dragging never moves hardware.
- Escape always leaves Move and returns to Select.
- Opening and Home frame the GDS, even when route or markup exists far away.
- All finite route and markup entities remain inside the internal navigation
  envelope and can be reached and deleted.
- Zoom and pan cannot leave that envelope or grow to unbounded coordinates.
- Extreme zoom cannot launch an unbounded KLayout geometry scan or make the GUI
  wait seconds for stale snap work.
- Normal-scale KLayout rendering, snapping, selection, markup, and route
  behavior remain unchanged.
- Route Pause/Resume/Interrupt behavior and all motion-safety gates retain their
  existing semantics.
