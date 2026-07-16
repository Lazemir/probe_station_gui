# KLayout-Style Vector Tool UX

**Status:** Approved in conversation on 2026-07-16. Written for final user
review before implementation planning.

**Branch:** `codex/klayout-design-renderer`

## Context

The first Markup implementation makes guide snap points permanently visible and
therefore adds visual noise. Its vector tools also lack KLayout's angle
constraints, `Esc` does not consistently leave the active tool, and the ruler
tool is still presented as `Measure`.

This change refines the existing Guide, ruler, and Array interactions. It does
not change stored markup geometry, route measurement behavior, KLayout raster
rendering, or the source GDS.

## Goals

- Draw quiet guide overlays without permanent endpoint, midpoint, or
  intersection symbols.
- Preserve endpoint, midpoint, intersection, and segment snap feedback while
  the pointer is close enough to use it.
- Apply KLayout's exact modifier-to-angle mapping to Guide, Ruler, and both
  Array direction picks.
- Make `Esc` cancel unfinished input and return every active drawing tool to
  `Select` without deleting completed geometry.
- Rename the design drawing tool from `Measure` to `Ruler` in both UI text and
  internal tool identifiers.

## Non-goals

- Renaming the route-measurement `Measure` action or changing its semantics.
- Adding new guide geometry or snap candidate types.
- Changing selection-box modifiers or behavior.
- Persisting rulers across application restarts.
- Changing KLayout render, minimap, or file-backed snap worker architecture.

## Quiet Markup Rendering

When the Markup layer is visible and idle, each guide is drawn only as its
dashed segment. Endpoint, midpoint, and intersection symbols are not drawn
continuously.

The underlying snap candidates remain unchanged:

- guide endpoints;
- guide midpoint;
- finite intersections of guide segments.

When the pointer is within the existing snap radius, the current hover snap
marker identifies the winning candidate. Existing segment highlighting remains
available when the hovered snap result refers to a segment. Hiding permanent
markers must not remove candidates from hit testing or snapping.

## KLayout Angle Constraints

Guide, Ruler, Array Direction 1, and Array Direction 2 share one angle-constraint
function. The function receives an anchor, a proposed endpoint, and the keyboard
modifier snapshot associated with that pointer action.

The mapping follows KLayout exactly:

| Modifiers | Allowed direction |
| --- | --- |
| none | unrestricted |
| `Shift` | horizontal or vertical |
| `Ctrl` | horizontal, vertical, or a 45-degree diagonal |
| `Shift+Ctrl` | unrestricted |

References:

- <https://www.klayout.de/doc/manual/measure.html>
- <https://www.klayout.de/doc-qt5/code/class_AngleConstraint.html>

For constrained input, the proposed endpoint is first obtained through the
existing geometry-snap path. It is then projected onto the nearest allowed ray
from the tool's anchor. This ordering keeps KLayout/GDS and Markup snap
arbitration unchanged while making the final vector obey the selected angle
constraint.

The same calculation is used for live preview and final click so the committed
geometry cannot jump away from its preview.

File-backed snap is asynchronous. Each correlated snap request therefore stores
the modifier snapshot from the originating hover or click. Completion must use
that stored snapshot rather than the keyboard state at response time. Releasing
a modifier while KLayout is working must not change the resulting vector.

Only `Shift` and `Ctrl` participate in angle constraints. Existing selection
modifier behavior remains confined to the Select tool.

## Ruler Naming and State

The design toolbar label is `Ruler`. The panel, plot pane, emitted tool signal,
and tests use the canonical internal token `ruler`; the drawing tool no longer
translates to or accepts `measure`.

This rename applies only to the design drawing tool. The route-measurement
button and workflow keep the product term `Measure`.

Completed ruler segments remain visible for the current design-window session,
as they do now. This change does not add ruler persistence.

## Escape Behavior

Pressing `Esc` while a non-Select drawing tool is active performs one atomic UI
transition:

1. Cancel any unfinished anchor, direction pick, live preview, or pending click
   action owned by that tool.
2. Switch the active toolbar tool to `Select`.
3. Leave all previously committed route points, guide segments, rulers, and
   array results unchanged.

This applies whether or not the active tool currently has an unfinished first
point. In particular:

- Guide loses only its uncommitted first endpoint and preview.
- Ruler loses only its uncommitted anchor/preview; completed ruler segments stay.
- Array loses an unfinished direction anchor and preview without applying it.
- Point cancels any pending click action and returns to Select.

While Select is already active, `Esc` cancels an in-progress selection drag or
other transient canvas interaction but does not clear the completed selection.
Repeated `Esc` is idempotent.

Late asynchronous snap responses belonging to a cancelled tool action are
stale and must not recreate a preview, commit geometry, or switch the tool back.

## Component Boundaries

- A Qt-free helper owns angle projection and its modifier mapping.
- `design_plot_pane.py` owns modifier capture, pointer preview, pending correlated
  snap metadata, and quiet overlay drawing.
- `design_navigator_panel.py` owns canonical tool names, ruler state, Array
  direction state, toolbar selection, and the transition back to Select.
- The design window's existing `Esc` shortcut coordinates pane cancellation
  before panel tool cancellation.

The implementation may place the pure helper in the existing selection geometry
module or a nearby design module, whichever produces the narrower dependency.

## Testing and Acceptance

### Pure geometry

- Every modifier combination maps to the documented allowed directions.
- Horizontal, vertical, diagonal, tie, negative-quadrant, and zero-length inputs
  project deterministically.

### Plot pane

- Idle guides draw dashed segments without permanent endpoint, midpoint, or
  intersection items.
- Endpoint, midpoint, and intersection candidates still participate in snap.
- Guide preview and commit use the same constraint result.
- Pending file-backed hover/click requests retain their originating modifiers.
- A late response after `Esc` is ignored.

### Navigator and window

- Toolbar and emitted/internal tool token are `Ruler`/`ruler`.
- The route-measurement `Measure` control is unchanged.
- Ruler and both Array direction previews use the shared constraints.
- `Esc` from Point, Guide, Ruler, and Array returns to Select.
- `Esc` removes unfinished state but preserves committed guides, rulers, route
  points, and array results.
- `Esc` in Select cancels only transient interaction.

### Regression verification

- Existing guide snapping, mixed selection, mixed Array, markup persistence,
  KLayout snap correlation, minimap, and route-measurement safety tests pass.
- Full repository tests and the configured lint gate pass.

## Success Criteria

The feature is complete when idle markup shows only dashed guide segments; the
operator can constrain Guide, Ruler, and either Array direction with the exact
KLayout modifier layout; preview and commit agree even across delayed snap
responses; and one `Esc` cancels unfinished input and returns to Select without
deleting any completed work.
