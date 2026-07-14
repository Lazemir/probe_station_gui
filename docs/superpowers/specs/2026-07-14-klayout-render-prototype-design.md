# KLayout Render Prototype Design

## Question

Can KLayout's Qt-less rendering and hierarchical geometry queries provide a
responsive, usable design canvas inside the existing PySide6 process without
launching the KLayout application or integrating with production design code?

## Scope

The prototype is a standalone PySide6 window launched with one Python command.
It opens a GDS file, renders it with `klayout.lay.LayoutView`, supports pan,
wheel zoom, fit, layer visibility, and places numbered points using geometry
snap. It displays load, render, and snap timings so the experiment is observable.

The prototype does not import or modify the production design window, persist
state, access hardware, or become a production abstraction. It is throwaway
code and must either be deleted or rewritten after the experiment.

## Architecture

- The GUI thread owns only PySide6 widgets, coordinate transforms, overlays,
  and decoded `QImage` frames.
- A render thread owns a Qt-less KLayout `LayoutView`. It loads the GDS,
  enables full hierarchy depth, applies layer visibility, and renders the most
  recent viewport to PNG-backed `PixelBuffer` data.
- A snap thread owns an independent `klayout.db.Layout`. It performs recursive
  region queries near the cursor and computes the closest vertex or edge point
  among only the returned shapes.
- Render and snap requests are coalesced independently so a slow redraw cannot
  postpone geometry queries and stale mouse positions do not build a queue.
- Render requests include a margin around the viewport at matching pixel
  density. Panning translates the existing image without substituting a coarse
  overview while the latest KLayout frame is pending.

## Interaction

- Start without a path to choose a GDS file, or pass a path as the sole
  positional argument.
- Drag with the left mouse button to pan. A release without a drag places a
  numbered point.
- Use the mouse wheel to zoom around the cursor.
- Hover near visible geometry to see the snap target and timing.
- Toggle layers, change snap radius in pixels, fit the design, or clear points
  from the toolbar.

## Acceptance for the Prototype

- No `klayout.exe` or main probe-station application process is started.
- The real recent GDS opens and produces a visible full-hierarchy frame.
- Layer 1 and layer 2 can be independently hidden and shown.
- Pan and zoom keep the Qt event loop responsive and expose render latency.
- Snap becomes available after its independent GDS load, works before any
  global geometry preprocessing, and exposes query latency and candidate count.
- Clicking near an edge or vertex places the point at the returned snap target.

## Dependency and License

The prototype uses the official `klayout` Python wheel, version 0.30.9, under
GPL-3.0-or-later. It is an optional prototype dependency rather than a runtime
dependency of the production application at this stage.
