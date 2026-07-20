# Alignment and Precision Approach Design

## Purpose

Fix the design-alignment click conflict, support registration from any number
of matched marks, and introduce one reusable mechanism for approaching exact
stage targets from a configured side of mechanical backlash.

The same change also makes coordinate confidence visible and persistent, and
moves the serial terminal out of the Connection dialog into a lazy standalone
tool window.

## Current Problems

- The design Point tool consumes left clicks before the legacy alignment path,
  so left click creates a route point instead of selecting the first alignment
  mark. Right click still selects the second mark, leaving an inconsistent and
  effectively broken workflow.
- Design alignment is represented as two fixed slots. Although registration
  objects can carry additional marks, fitting and physical B preparation use
  only the first two.
- Autofocus has its own hard-coded final Z approach. Other exact moves do not
  share that logic, so coordinate entry, API moves, click-to-move, routes, and B
  alignment behave differently around mechanical backlash.
- The coordinate panel reports controller coordinates but does not tell the
  operator whether an axis is loaded on a known side of its backlash.
- The serial terminal is created synchronously as a second tab in Connection,
  even though it is an occasional tool.

## Scope

This design includes:

- a dedicated Align design-canvas tool;
- dynamic matched alignment pairs and least-squares similarity fitting;
- a per-axis precision-approach profile and pure motion planner;
- integration with coordinate entry, the local API, click-to-move, route
  positioning, final autofocus positioning, needle positioning, and design
  alignment;
- persistent per-axis coordinate-confidence state;
- coordinate-field underlines and a color legend;
- a lazy standalone Terminal window under Tools.

This design does not add firmware-level backlash correction, modify FluidNC,
compensate free jog trajectories, change homing commands, or alter route
Pause/Resume/Interrupt semantics.

## Terminology

- **Precision approach profile**: per-axis settings that define whether exact
  approach is enabled, the take-up distance, and the direction of the final
  motion.
- **Final direction `+`**: the final segment moves from a smaller displayed
  coordinate to the target.
- **Final direction `-`**: the final segment moves from a larger displayed
  coordinate to the target.
- **Exact coordinate**: the application knows that the axis completed a motion
  on the configured final side of backlash.
- **Approximate coordinate**: the controller coordinate is available, but the
  application cannot guarantee the physical axis is loaded on that side.
- **Preparation target**: the point one configured backlash distance opposite
  the final direction from the requested target.

The feature provides repeatable directional approach. It does not claim to
recover an absolute mechanical coordinate without a physical reference.

## Architecture

### PrecisionApproachProfile

Every supported stage axis can have a profile with:

- `enabled: bool`;
- `backlash: float`, non-negative;
- `final_direction: +1 | -1`.

Backlash is expressed in the same calibrated, user-facing units shown for the
axis: millimetres for linear axes and degrees for rotary axes. Preparation
targets are calculated in that coordinate space and then converted through the
existing A/Z calibration and work-coordinate mapping before G-code is built.

The profile model supports every available axis rather than hard-coding a
feature list. The initial defaults are:

- A: final direction `-` (physical approach from above), disabled until a
  backlash value is configured;
- Z: final direction `+` (physical approach from below), enabled with the
  existing autofocus final-approach value of `0.03 mm`;
- X, Y, B, and C: disabled with zero backlash until configured.

A and Z directions are editable. Changing enabled state, backlash, direction,
or a relevant axis calibration invalidates saved exact confidence for that
axis.

### PrecisionApproachPlanner

`PrecisionApproachPlanner` is a pure module. It receives:

- confirmed current coordinates;
- requested absolute targets;
- profiles and current confidence;
- axis coordinate converters;
- available axis limits.

It returns an immutable plan containing zero, one, or two absolute motion
segments and the axes whose confidence will change. It performs no serial I/O,
owns no Qt objects, and emits no signals.

For an enabled axis with backlash `b` and target `t`:

- final direction `+` uses preparation target `t - b`;
- final direction `-` uses preparation target `t + b`.

A preparation segment is unnecessary when the current exact state proves the
axis remains loaded on the correct side and the target does not reverse that
direction. An approximate axis can also move directly when the confirmed
travel toward the target is at least the configured backlash. Otherwise the
planner uses the preparation target followed by the requested target.

For a multi-axis request, the preparation segment moves only axes that require
take-up; other axes hold their current positions. The final segment contains
the complete requested target map so the exact endpoint remains one
coordinated move. If no axis needs preparation, the plan contains only the
final segment.

Relative GUI or API input is resolved against a fresh confirmed position to an
absolute target before planning. Click-to-move likewise converts its pixel
delta to an absolute XY target before planning.

All preparation and final targets are checked against axis limits before the
first command is sent. If a preparation target is outside a limit, the whole
request fails. The planner never silently reduces backlash or switches the
configured side.

### Precision Motion Execution

The existing stage background-motion infrastructure executes a precision plan
as one logical operation:

- one busy state;
- one `movement_started` event;
- cancellation and safety checks before every segment;
- one final success or failure result;
- a fresh controller position after completion.

The GUI thread remains responsible only for widget updates. Serial I/O, status
queries, coordinate conversion that needs controller state, and both motion
segments remain in the stage worker.

No Qt signal is emitted while holding a non-reentrant lock.

## Motion Integration

The common planner is used by:

- absolute and relative coordinates entered in the stage-position panel;
- the equivalent coordinate commands exposed by the local API;
- click-to-move XY targets;
- route travel between measurement points;
- the final return to the selected Z in autofocus;
- exact needle targets reached by the existing needle workflows;
- physical B rotation requested by design Alignment;
- other explicit target workflows that already use the shared absolute-target
  controller path.

Jog, step jog, homing, autofocus search sampling, oscillation, and continuous
service trajectories bypass the planner. Their confirmed motion can still
update confidence.

Autofocus removes its private final-backlash constant and delegates only its
final return to the common planner. Existing autofocus cancellation behavior
continues to restore the starting Z or another known safe Z before control is
returned.

Needle actions retain their current safety gates and physical raise/lower
semantics. A preparatory needle segment is permitted only at an existing safe
transition. Interrupt is checked between the preparation and final segments;
an interrupted workflow must not continue into lower, contact check, external
measurement, or later contact steps.

The implementation uses direct controller methods and Qt signals. In-process
features are not routed through the application's localhost API.

## Coordinate Confidence

### Runtime State

Each enabled profile has a confidence state:

- `exact`;
- `approximate`.

The tracker also records the last known loaded direction and confirmed machine
coordinate needed to evaluate later jog motion and restore persisted state.

An axis becomes exact after:

- a successful final segment produced by the precision planner; or
- confirmed jog travel in the configured direction sufficient to take up the
  full configured backlash.

An exact axis remains exact during a smaller jog in the same direction. It
becomes approximate immediately after a reversal, and becomes exact again only
after confirmed travel in the configured direction reaches the configured
backlash.

An enabled axis becomes approximate after:

- homing, because the GUI does not assume the FluidNC pull-off direction
  matches the profile;
- a controller reset or a changed FluidNC session;
- an unknown manual command from Tools > Terminal;
- a cancelled, interrupted, failed, or partially completed motion involving
  the axis;
- a profile or relevant calibration change;
- a live coordinate that no longer matches its persisted exact state.

When the exact set of axes affected by an unknown manual command cannot be
known safely, all enabled profiles become approximate.

### Persistence

Confidence extends the existing `controller-state.json` rather than introducing
a parallel state file. Exported controller state gains a versioned per-axis
record containing:

- confidence;
- loaded side;
- last confirmed machine coordinate;
- a fingerprint of the profile and coordinate calibration used to establish
  it.

The existing volatile FluidNC session marker remains the primary validation.
On application restart:

1. the cached marker must match the live controller marker;
2. the application waits for a fresh live status frame;
3. each saved machine coordinate is compared with the corresponding live
   coordinate using a named tolerance consistent with controller reporting
   resolution;
4. the current profile/calibration fingerprint must match the saved one.

Only axes passing all checks restore exact state. A coordinate mismatch affects
only that axis. A session-marker mismatch makes every enabled axis
approximate and follows the existing cached-controller-state clearing flow.

State is persisted after confirmed motion-state changes and during orderly
application shutdown. Predicted positions are never persisted as exact.

## Coordinate Panel Presentation

Existing field-fill semantics remain unchanged:

- blue: homed;
- yellow: not homed;
- red: active limit;
- light purple: edited target not yet applied;
- grey: unavailable.

An independent four-pixel underline shows coordinate confidence:

- green: exact;
- red: approximate.

The underline is hidden when the precision profile is disabled, the axis is
unavailable, or an active limit already owns the visual state. A limit field
therefore remains a plain red field with no confidence underline. Edited target
fields retain their purple fill and continue to show the confidence of the
current physical axis.

A compact legend below the coordinate row explains both the existing field
fills and the new underlines. Axis tooltips state whether the coordinate is
exact or approximate and, when approximate, that the next precision move will
finish from the configured side. Updates are event-driven; no periodic widget
rebuild is introduced.

## Settings UI

Settings gains a `Precision approach` section containing one row per available
axis. Each row has:

- Enabled;
- Backlash with the correct unit;
- Final direction (`+` or `-`);
- a short preview such as `9.97 -> 10.00`.

Input validation rejects negative, non-finite, or unsupported values. A zero
backlash acts as disabled even if an old settings file contains `enabled=true`.
The normal settings clone, normalization, serialization, replacement, and save
flow is used so default settings, user settings, and SettingsDialog remain in
sync.

## Dedicated Align Tool

### Canvas Interaction

The design toolbar gains a normal `Align` tool alongside Select, Point, Ruler,
and markup tools.

When Align starts, it creates a draft without clearing the active
registration. Left click uses the existing snap-to-geometry result and appends
numbered points `D1`, `D2`, `D3`, and so on. Right click has no special
alignment meaning.

The tool exposes:

- Undo: remove the last draft point;
- Clear: remove all draft points;
- Done: accept a draft containing at least two distinct points;
- Enter: same as Done;
- Escape: discard the draft, preserve the previous registration, and leave the
  tool.

Done also leaves the tool. Select's normal left/right crossing-window behavior
and the Point tool's route-point creation are unchanged because alignment
clicks exist only while Align is active.

### Alignment Panel

After Done, Alignment shows a dynamic row for every design point:

- `D1 / S1`, `D2 / S2`, ...;
- the design coordinate;
- the captured stage coordinate;
- Capture, changing to Replace after capture.

The next incomplete row is selected automatically. Capture reads a fresh
current XY coordinate and fills only that row. After the final S point is
captured, alignment preparation starts automatically.

The panel also provides Undo/Clear while collecting design points and reports
RMS and maximum source-pair residual after registration. The old left-click /
right-click registration hint is removed.

### Multi-Point Fit

All matched source pairs participate in a least-squares 2D similarity fit with:

- uniform scale;
- proper rotation;
- translation;
- no reflection.

The implementation uses a centred Procrustes/Umeyama-style solution. Two
distinct pairs remain sufficient. A set is rejected only when it is
mathematically degenerate, contains non-finite coordinates, has mismatched
design/stage counts, or has less than two usable pairs. No hidden residual
threshold rejects noisy but usable data; RMS and maximum residual make the
quality visible.

The angular component of the all-pair fit determines the corrective physical B
rotation. Every captured stage mark is then rotated about the existing B-axis
stage pivot by that correction. After the B move succeeds, the final
similarity registration is rebuilt from all design marks and all rotated stage
marks.

If the B correction is negligible, the new registration is committed without
motion. If B has an enabled precision profile, its physical rotation uses the
common planner. B has no special initialization, homing emulation, or separate
readiness model.

The previous registration remains active during draft design-point selection
and stage-point capture. When a real B move begins it is marked stale because
the physical design is changing. The new registration is committed only after
the complete B motion succeeds. A failed or interrupted B motion leaves the
old registration stale; the application must not claim that the pre-motion
registration is still valid.

### Alignment Persistence

The design-session persistence format is versioned forward from its current
two-slot representation. Source design and stage marks become variable-length
ordered lists. Existing version-1 data restores as a list containing its two
slots, preserving old registrations and routes.

Draft Align points are not persisted. Only an accepted set and completed stage
captures belong to persisted design state.

## Standalone Terminal Tool

The Terminal tab is removed from the Connection dialog. Connection keeps only
connection controls.

Tools gains a laconic `Terminal` action. The first activation lazily creates a
modeless standalone window containing the existing `SerialTerminalWindow`.
Later activations show, raise, and focus the same instance. Closing the window
hides it and does not disconnect the controller.

When created, the window binds to the current serial connection and stage
controller. Later connect/disconnect events update it only if it exists. Its
history, send behavior, live polling, and Ctrl+X support remain unchanged.
`manual_command_sent` continues to invalidate needle knowledge as today and
also marks enabled precision profiles approximate.

The lazy construction removes terminal creation from synchronous main-window
startup.

## Failure Handling

- Invalid settings are normalized or rejected before they reach planning.
- Missing current coordinates reject precision planning without sending a
  command.
- A preparation target outside limits rejects the full plan.
- Cancellation or failure after any segment marks every involved enabled axis
  approximate.
- A stale status response cannot mark an axis exact; exact state is applied
  only after a successful motion and fresh reconciliation.
- Alignment input errors retain the draft and identify the affected row.
- Alignment B failure retains captured pairs for retry but keeps registration
  stale after physical movement begins.
- Persistence parse failures fall back to approximate state without preventing
  startup.

## Testing Strategy

### Pure Planning and State Tests

- positive and negative final directions;
- one-segment and two-segment plans;
- approximate and exact starting confidence;
- zero/disabled profiles;
- calibrated A/Z coordinate conversion;
- coordinated multi-axis preparation and final segments;
- preparation and target limit failures;
- jog reversals and accumulated take-up distance;
- failure and cancellation state transitions;
- profile/calibration fingerprint invalidation.

### Persistence Tests

- export/import through the existing controller-state model;
- matching FluidNC marker and matching live machine coordinates;
- marker mismatch;
- one-axis position mismatch without invalidating unrelated axes;
- no restoration from predicted positions;
- malformed and older cached state.

### Integration Tests

- GUI G90 and G91 coordinate entry use the planner;
- local API absolute and relative coordinates use the same planner;
- click-to-move resolves an absolute XY target and uses the planner;
- route travel uses the planner without changing Pause/Resume/Interrupt
  semantics;
- Interrupt between needle preparation and final/contact motion prevents all
  later contact steps;
- autofocus final return uses the planner and interrupted autofocus restores a
  safe Z;
- Alignment B commit occurs only after all precision segments succeed.

### Alignment Tests

- Align left click cannot create a route point;
- Done/Enter require two points and Escape preserves the active registration;
- dynamic row capture, replacement, and auto-advance;
- exact N-pair similarity recovery;
- noisy N-pair residual reporting;
- degenerate input rejection and reflection prevention;
- rotation of every stage mark about the B pivot;
- version-1 two-slot persistence migration.

### UI and Terminal Tests

- green/red underlines and suppression for disabled, unavailable, and limit
  axes;
- existing blue/yellow/red/purple/grey fill semantics remain intact;
- legend and tooltips reflect current state;
- Tools contains Terminal and Connection no longer contains its tab;
- terminal construction is lazy, reopening reuses the window, and connection
  changes synchronize an existing window;
- a terminal manual command makes enabled profiles approximate.

All tests use the shared project virtual environment and mocked stage/serial
objects. Hardware-dependent code is not run in automation.

## Acceptance Criteria

- Point mode left click creates route points; Align mode left click creates
  only numbered alignment marks.
- The operator can register a design with two or more matched pairs, and every
  pair affects the fitted transformation and reported residuals.
- Coordinate entry, local API moves, click-to-move, route travel, final
  autofocus return, needle targets, and Alignment use one precision planner.
- Jog and homing remain uncompensated.
- Enabled axes finish precision moves from their configured direction or the
  request fails before motion when that approach is impossible.
- Coordinate confidence is visibly green or red, suppressed for limit and
  unavailable fields, and restored only for the same verified controller
  session and matching live position.
- Existing route interrupt and autofocus recovery safety tests continue to
  pass.
- Terminal is absent from Connection, opens lazily from Tools, and retains its
  current behavior.
- Main-window startup performs no new hardware reads, scans, parsing, or heavy
  widget creation.
