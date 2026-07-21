# Software Coordinate Systems, Design Registration, and B-Axis Point-Hold Rotation

## Status

Approved in design review on 2026-07-22.

## Purpose

Replace the controller-WCO-oriented coordinate selector with a software
coordinate-frame system that is visible next to the position readout and is
safe to use with nonlinear per-axis calibration curves.

The application will always retain a calibrated physical Machine frame. Every
loaded design registration becomes a separate software frame, including
multiple physical registrations of the same GDS. Operators may also create
named custom frames in Settings. Position display, absolute targets, relative
movement, keyboard jog, Design Window navigation, and external API movement
will all resolve through the same typed transformation boundary.

The design also adds an experimental operation that rotates physical B while
moving X/Y so the sample point under the microscope remains at the center of
the view. The operation initially uses the assumed machine rotation pivot
`Machine XY = (0, 0)` and later uses a camera-derived pivot calibration.

## Non-Negotiable Decisions

- Software frames do not use G54-G59 or another controller work-coordinate
  offset. Exact movement resolves to controller Machine coordinates and is sent
  using G53 semantics.
- Raw controller MPos is never treated as physical position when a universal
  axis calibration is enabled. The existing universal calibration mapper on
  current `main` runs before every software-frame transform and after every
  inverse transform.
- There is no singleton `Chip` frame. Each design registration is an
  independently identified coordinate frame.
- Design registration readiness is ordered `X/Y/B -> Z -> A`. Changing or
  invalidating Z always invalidates A. There is no same-transaction exception.
- Known physical B motion does not invalidate B-attached design or custom
  frames. They are recomputed from the actual physical B and the global
  rotation pivot.
- X/Y homing is required before a design or custom frame may become active.
  B cannot be homed and is governed by its tracked coordinate authority.
- GUI and API frame selections are independent. An API movement can never
  temporarily change the GUI selector.
- C remains in the backend axis model, calibration settings, serialization,
  controller compatibility, and manual terminal path. It is shallowly hidden
  from ordinary position and motion controls until it is used as an objective
  changer. There is no generic per-axis Enabled switch.
- Manual terminal commands remain unrestricted. If they make position
  authority uncertain, the application invalidates or blocks the affected
  software-frame axes instead of pretending the command used the selected
  GUI frame.

## Scope

This design includes:

- a calibrated `PhysicalMachinePose` boundary;
- Machine, design-registration, and named custom frames;
- multiple persistent design-registration instances;
- selector and per-axis readiness indication beside the position display;
- frame-relative absolute, relative, Step, WASD, and API movement;
- explicit feedrate ownership for GUI and API movement;
- optional physical alignment of a design through B;
- experimental rotation around the current microscope view center;
- a machine-level B rotation-pivot model and camera calibration workflow;
- versioned persistence, migration, typed failure results, and tests;
- shallow hiding of C from ordinary GUI controls.

This design does not:

- use or manage G54-G59 for the new frames;
- extrapolate or clamp an enabled calibration curve;
- perform similarity scaling to make a design fit the measured chip;
- make ordinary B jog compensate X/Y;
- allow ordinary simultaneous frame-relative XY+B jog in a rotating frame;
- change manual terminal semantics;
- change route-measurement Pause/Resume/Interrupt behavior;
- declare hardware validation complete from automated tests alone;
- remove C from controller, settings, calibration, API-compatible backend data,
  or future hardware support.

## Existing Coordinate Boundary

The universal calibration implementation now present on `main` is the base of
this design. `StageAxisCalibrationMapper` owns a strictly increasing,
piecewise-linear controller-to-physical curve independently for X, Y, Z, A, B,
and C. A disabled curve is the identity. An enabled curve has a closed measured
domain and cannot extrapolate or clamp.

The required forward pipeline is:

```text
FluidNC raw MPos
    -> StageAxisCalibrationMapper.controller_to_physical(...)
    -> PhysicalMachinePose
    -> selected software frame
    -> position display / API status
```

The required inverse pipeline is:

```text
software-frame target
    -> PhysicalMachinePose target
    -> StageAxisCalibrationMapper.physical_to_controller(...)
    -> raw controller Machine target
    -> limits and safety
    -> G53 movement
```

X, Y, Z, and A use millimetres. B and C use degrees. The existing configured or
work-coordinate/WCO mapper must not be inserted into either new pipeline.

Calibration settings are immutable for the duration of an active operation.
Applying a new axis calibration waits until the stage is idle and increments a
calibration generation/fingerprint used by persisted registrations and motion
snapshots.

## Domain Model

### PhysicalMachinePose

`PhysicalMachinePose` is an immutable mapping over the physical axes. It is the
only pose accepted by software-frame transforms. It must not contain raw
controller coordinates, WCO coordinates, UI-formatted strings, or Qt state.

The coordinate layer distinguishes:

- `ControllerMachinePose`: raw FluidNC Machine coordinates;
- `PhysicalMachinePose`: raw Machine coordinates passed through the universal
  calibration curves;
- `FramePose`: values expressed in one software frame;
- `AxisAuthority`: whether the physical controller coordinate for an axis is
  currently trustworthy;
- `FrameAxisReadiness`: whether a frame has a usable origin/direction for an
  axis.

Controller authority and frame readiness are separate. Losing a serial status
can temporarily block a stored frame without deleting its measured references.

### CoordinateFrameRegistry

One non-Qt registry owns immutable frame records and publishes versioned
snapshots. A record has:

- stable UUID `frame_id`;
- `kind`: `machine`, `design`, or `custom`;
- user-facing name;
- record version;
- transform parameters;
- per-axis readiness and invalidation reasons;
- provenance and calibration fingerprints.

Machine has a stable built-in identity. Design and custom frames use UUIDs so
renaming does not break API clients, active snapshots, or the persisted last
selection.

### B-Attached Planar Frame

Design and custom frames use the same B-attached planar transform. At a
reference physical angle `b_ref`, the frame stores:

- Machine XY origin `o_ref`;
- frame XY orientation `theta_ref` relative to Machine X;
- physical Machine B corresponding to the frame's rotary zero `b_zero`;
- physical Machine Z and A origins when available.

For the current physical Machine angle `b`, with machine rotation pivot `c`:

```text
delta_b = b - b_ref
o(b) = c + R(delta_b) * (o_ref - c)
theta(b) = theta_ref + delta_b
frame_xy = R(-theta(b)) * (machine_xy - o(b))
machine_xy = o(b) + R(theta(b)) * frame_xy
frame_b = b - b_zero
frame_z = machine_z - z_zero
frame_a = machine_a - a_zero
```

Angles used for planar rotation are interpreted in degrees at the public
boundary and converted to radians inside the math implementation. Display may
normalize an orientation angle, but physical B targets retain their actual
unwrapped/calibrated value so soft limits and calibration domains remain
unambiguous.

For a design registration, rigid fitting determines `theta_ref`. Its derived B
zero is the physical B at which the design axes would align with Machine axes.
Optional physical alignment moves to this B zero, making displayed design B
equal to zero. A custom frame may explicitly configure both its planar
orientation and rotary zero.

### Machine Rotation Geometry

The B rotation pivot is machine-level data, never a property of one design or
custom frame. Its record contains:

- physical Machine `pivot_x_mm` and `pivot_y_mm`;
- source: `assumed`, `camera_calibrated`, or `manual`;
- calibration version and timestamp;
- objective and optical-calibration provenance;
- sampled physical B range;
- fit RMS, maximum residual, and held-out validation residuals.

The initial record is `(0, 0)` with source `assumed`, representing the expected
center of the work area. The B universal calibration curve remains the sole
owner of controller-to-physical angle scale and sign; pivot calibration does
not duplicate or silently modify it.

## Frame Kinds

### Machine

Machine is always present and is the startup fallback. Its displayed values
are `PhysicalMachinePose` values. Absolute motion still requires the relevant
Machine axis authority, including X/Y homing where applicable.

### Design Registration

Each registration is a separate instance, even when two records reference the
same source GDS and top cell. A design frame contains:

- registration-instance UUID;
- source document identity and display name;
- design unit conversion to millimetres;
- source and check mark pairs;
- rigid planar fit and residual diagnostics;
- reference physical B and B zero;
- focus-derived physical Z zero;
- contact-derived physical A zero;
- readiness and stale reasons;
- calibration and machine-profile fingerprints.

The active Design Window may still show only one document. Loading a design
creates a draft record when no registration instance was chosen. A `New
registration` action creates another UUID for a second physical chip using the
same GDS. Existing instances remain in the registry and selector. Duplicate
default names are disambiguated as `name`, `name (2)`, and so on.

There is no mutable `Chip` alias. The selector shows the actual design or
registration name.

### Custom

Custom systems are configured in Settings and are directly relative to
Machine. Their editor supports Add, Duplicate, Rename, and Delete and exposes:

- name;
- Machine XY origin at reference B;
- reference physical B;
- XY orientation at reference B;
- Machine B zero;
- Machine Z zero;
- Machine A zero;
- `Use current position` and `Use current angle` actions.

A new custom system remains a draft until all required numeric values are
finite and explicitly saved. Editing upstream geometry obeys the same
dependency direction as measured frames: changing X/Y/B clears downstream Z
and A references; changing Z always clears A. A must then be explicitly
re-entered or captured after the new Z is accepted. Updating Z and A in one
Settings transaction does not bypass this rule.

## Design Registration Workflow

### Prerequisites and Initial State

A loaded design appears in the selector immediately but begins with X, Y, Z,
A, and B yellow. It cannot become the selected movement frame until X and Y
have confirmed Machine homing. Registration operations themselves run in the
physical Machine frame so they can establish previously unavailable design
origins.

B is not homed. Its use depends on the existing tracked Machine coordinate and
the universal B calibration. If B authority is temporarily unavailable, the
registration record remains stored but B-dependent calculations are blocked.

### X/Y/B From Alignment Marks

The operator selects at least two corresponding design and camera/stage marks.
Every captured stage mark is a calibrated physical Machine XY coordinate, not
raw controller MPos.

Design coordinates are converted from GDS database units to millimetres before
fitting. The accepted transform is rigid: translation plus rotation only. A
Kabsch/SVD-style fit estimates rotation and translation for more than two
points. Measured and design distances also produce scale-ratio diagnostics,
but scale is never applied to the transform. Residual RMS, maximum residual,
and scale mismatch are shown in the registration details and logged.

After a valid fit:

- X and Y become blue;
- B becomes blue;
- Z remains yellow;
- A remains yellow.

Physical alignment is optional. If requested, B rotates toward the derived
design B zero using the compensated point-hold planner. The new registration
state is committed only after the full alignment operation succeeds.

### Z From a Central FOV Structure

After X/Y/B are available, the application searches design geometry near the
design center for a structure that fits inside the current objective's field
of view. The candidate is highlighted in Design Window. The operator may
accept it or choose another point in Design Window.

The stage moves to the selected structure and performs a local autofocus. The
first successful registration autofocus records the resulting physical Machine
Z as design `Z = 0`. Z becomes blue and the design registration is considered
complete.

Ordinary later autofocus does not change this reference. Only the explicit
`Reset focus reference` or a real upstream invalidation clears/replaces it.
Resetting or changing it always invalidates A.

### A From Contact

A remains yellow until the first successful contact operation for that design
after its current Z reference is established. That physical Machine A becomes
design `A = 0`, and A becomes blue.

Contact inherits all existing needle safety and interruption behavior. A
failed or interrupted contact never establishes the reference.

### Readiness Dependency

Design readiness is a directed dependency graph:

```text
X/Y/B alignment
    -> Z focus reference
        -> A contact reference
```

The invalidation rules are exact:

- invalidating X, Y, or B invalidates X/Y/B and then Z and A;
- invalidating or changing Z invalidates Z and A;
- invalidating A affects only A;
- a temporary loss of controller authority blocks dependent values but does
  not delete stored references;
- a successful, measured B move does not invalidate the registration.

## Movement Eligibility

Yellow means the selected frame lacks an absolute origin or trusted direction
for that axis. It does not universally mean the physical axis may not move.
There is no hidden per-axis fallback to Machine when a design/custom frame is
selected.

The design-frame rules are:

| Axis state | Absolute target | Relative movement |
| --- | --- | --- |
| X/Y before marks | rejected | rejected because the frame basis is unknown |
| X/Y after marks | allowed | allowed in frame directions |
| B before marks | rejected | allowed as a physical B delta |
| B after marks | allowed | allowed as a physical B delta |
| Z before focus | rejected | allowed as a physical Z delta |
| Z after focus | allowed | allowed |
| A before contact | rejected | allowed subject to needle safety |
| A after contact | allowed | allowed subject to needle safety |

A mixed command is atomic. Every requested component must be resolvable and
safe before any component moves.

Registration-only moves intentionally bypass the incomplete design frame and
use calibrated Machine physical coordinates. They do not bypass stage safety,
limits, calibration domains, operation reservations, or cancellation.

## Motion Resolution

### Intent and Immutable Plan

All non-terminal GUI and API movement enters a coordinate-agnostic boundary as
`MotionIntent`. The resolver captures:

- request source (`gui`, `api`, registration workflow, or point-hold planner);
- frame ID and frame record version;
- absolute or relative mode;
- original frame targets/vector;
- current calibrated physical Machine pose;
- axis-authority and readiness snapshot;
- calibration generation;
- feedrate policy;
- cancellation/operation context.

It produces an immutable `ResolvedMotionPlan` containing:

- original intent;
- resolved physical Machine target or segments;
- resolved raw controller Machine targets;
- expected calibration and frame versions;
- complete limit/safety/preflight result;
- feedrate ownership;
- audit metadata.

`StageMotionExecution` remains coordinate-system agnostic. It executes already
resolved raw targets and owns serial I/O, operation lifecycle, stop behavior,
and completion/error reporting.

Switching the GUI frame while a plan is active only changes later display and
future intents. It never recomputes the active target.

### Absolute and Step Movement

Absolute frame targets transform to `PhysicalMachinePose`, pass through the
inverse universal calibration mapper, and execute as explicit Machine targets
using G53. Step accumulation likewise maintains a physical/frame target and
resolves an exact endpoint rather than accumulating formatted display deltas.

All physical and controller calibration domains, Machine soft limits, B soft
limits, homing/authority, needle safety, and operation conflicts are checked
before a command is accepted.

### WASD and Continuous Jog

WASD directions use the currently selected GUI frame. On key press, the frame
basis and version are captured once and the requested XY direction is rotated
into Machine axes. The resulting jog retains the existing realtime stop on key
release.

Changing the selected frame while any coordinate key is held sends jog stop,
clears the key state, and requires a fresh press. The running direction cannot
turn under the operator's hand.

Machine-frame mixed XY+B jog retains its existing behavior. Ordinary
simultaneous XY+B jog is rejected for a B-attached design/custom frame because
the basis rotates continuously and a single frozen transform would be
misleading. Coordinated XY/B behavior is available only through the dedicated
point-hold planner.

Relative jog that remains in G91 is independent of controller WCO. Exact
absolute endpoints and segmented plans use G53 Machine targets.

## GUI

### Selector and Position Readout

The coordinate selector sits directly beside the bottom-left position readout.
It is grouped as:

- Machine;
- Designs;
- Custom.

The selector uses concise names. Live transforms, matrices, residuals, and
implementation details belong in tooltips or detail dialogs, not the menu
labels.

The displayed X/Y/Z/A/B values always belong to the selected GUI frame. Blue
indicates an available registered origin/direction. Yellow indicates missing,
stale, or temporarily blocked readiness, with a tooltip describing the exact
reason and next action.

C is omitted through one narrow ordinary-GUI visible-axis definition such as
`VISIBLE_STAGE_AXES = ("X", "Y", "Z", "A", "B")`. Backend axis definitions
remain `(X, Y, Z, A, B, C)`. Existing saved C calibration data is preserved and
can later support objective selection without a schema migration.

### Last Selection

The GUI persists `last_selected_frame_id`. Startup initially displays Machine
and holds a pending restore. After X/Y homing is confirmed, the prior selection
is restored only when:

- the frame still exists;
- its machine profile and source design are current;
- X/Y/B and Z references are valid;
- the operator has not selected another frame during startup.

A yellow A does not prevent restoring a completed design frame; it only blocks
absolute A movement. A missing/stale Z means registration is incomplete, so
startup remains in Machine. Missing, deleted, or stale frames remain visible
where useful but are not silently selected.

## External API

GUI and API coordinate context are independent. API movement defaults to the
built-in Machine frame. A caller may provide an explicit `coordinate_system`
or stable frame UUID per request.

A read-only API endpoint lists:

- available frame IDs, names, and kinds;
- version and availability;
- per-axis readiness and reasons;
- current values when resolvable;
- machine/pivot provenance needed for diagnostics.

Movement responses echo:

- requested frame ID and version;
- original target;
- resolved physical Machine target;
- resolved raw controller target or segment summary;
- actual frame used;
- feedrate policy.

Unknown IDs and temporarily unavailable existing frames are distinct typed
errors. An API request never changes, highlights, or temporarily selects a GUI
frame.

Existing API clients that omit frame data continue to target Machine. Response
schemas may add the resolution metadata without renaming the existing `API
route control` workflow or changing route control semantics.

## Feedrate Ownership

Feedrate behavior is explicit in every motion snapshot:

- API without a feedrate uses the shared current GUI/default speed. The speed
  slider may reissue an active ordinary jog/move through its existing path.
- API with an explicit feedrate owns a pinned speed. Later slider changes affect
  only future requests.
- GUI ordinary movement uses the shared current speed unless its operation
  explicitly pins one.
- segmented point-hold rotation pins one speed for the complete trajectory.
  The speed slider cannot cancel and restart individual segments.

Changing a frame selection never changes the feedrate policy of an active
plan.

## Rotate Around View Center

### Separate Experimental Operation

`Rotate around view center` is an explicit operation independent of the
selected coordinate frame. Ordinary B jog and ordinary B coordinate entry do
not compensate X/Y.

At operation start the planner snapshots:

- calibrated physical Machine pose `(x0, y0, b0)`;
- physical Machine coordinate of the current microscope view center;
- active objective/camera offset conversion;
- machine rotation pivot `c` and its provenance;
- desired B delta or target;
- calibration generation, limits, safety, and pinned feedrate.

The current microscope point `q0` is followed through rotation by:

```text
q(delta_b) = c + R(delta_b) * (q0 - c)
```

The required raw stage XY target is obtained through the existing conversion
between optical-axis stage position and raw stage position, then through the
inverse universal X/Y calibration. At each planned node, B passes through the
inverse universal B calibration. This matches the design-registration
convention that a sample feature's required stage coordinate rotates around
the physical pivot.

### Segmentation and Preflight

FluidNC linearly interpolates one X/Y/B block, so a circular path is represented
by short chord segments. Segment count is derived from named user settings:

- maximum physical B step per segment;
- maximum allowed XY chord/sagitta error.

There are no hidden per-objective motion constants. The planner computes every
physical and controller endpoint before issuing the first command and checks:

- X/Y homing and B authority;
- enabled calibration domains in both directions;
- X/Y software limits;
- B soft limits and the current allowed interval;
- needle safety and required raised state;
- operation availability;
- all intermediate, not merely final, targets.

If any node is invalid, no movement starts. The complete trajectory uses one
operation reservation, one cancellation token, and one pinned feedrate.

### Cancellation and Failure

Cancellation uses the normal realtime stop path and does not blindly return to
the start. The application queries the final actual MPos, reconstructs the
calibrated physical pose, and leaves every B-attached frame evaluated at that
actual B.

If final controller position cannot be confirmed, B authority becomes
temporarily unavailable and dependent frame coordinates are blocked. Stored
registrations are not deleted merely because a tracked rotation was
interrupted.

When point-hold rotation is part of optional physical registration alignment,
the alignment draft is committed only after the complete operation succeeds.

### Logging

The operation logs:

- frame-independent Machine start and target;
- pivot coordinates and provenance;
- objective and optical offset;
- physical and raw planned nodes;
- requested and actual B/XY at checkpoints;
- cancellation or failure location;
- optional image-registration stability residuals.

This evidence will determine later UX, speed, and segmentation decisions after
hardware testing.

## Rotation-Pivot Calibration

The automatic workflow follows the established OpenPnP-style runout approach
but is implemented with Python-native dependencies:

1. Require X/Y homing, trusted B coordinate, raised needles, selected
   objective, and valid camera pixel-to-physical calibration.
2. Ask the operator to center a high-contrast feature near the workspace.
3. Sample multiple safe physical B angles around the current angle.
4. At each angle, reacquire or recenter the same feature using OpenCV image
   registration and convert pixel displacement to physical Machine XY.
5. Use the MIT-licensed `circle-fit` package for a Taubin SVD initial circle.
6. Refine pivot and residuals with `scipy.optimize.least_squares` using a robust
   loss such as `soft_l1`.
7. Retain angles for independent validation and report RMS/max error.
8. Return using a separately preflighted safe path.
9. Show the proposed pivot and diagnostics; save only after user confirmation.

Manual pivot entry remains an advanced review/fallback path and records source
`manual`. The intended normal source is camera calibration, not hand-entered
numbers.

The calibration workflow does not fit B sign or scale. A detected angular
inconsistency reports that the universal B calibration must be corrected first.

The implementation may use the MIT-licensed `spatialmath-python` package for
SE(2) composition and `circle-fit` for the initial circle. SciPy and OpenCV
provide robust optimization and image alignment. LinuxCNC kinematics and
OpenPnP calibration are behavioral references only; GPL/AGPL source is not
copied into this repository. Any future direct dependency with reciprocal
licensing requires an explicit distribution review.

## Persistence

### Storage Separation

Data is intentionally split into three lifetimes:

- `settings.json` stores user-authored custom frames, pivot calibration,
  segmentation settings, and `last_selected_frame_id`;
- new versioned `coordinate-frames.json` stores measured design-registration
  instances, focus/contact references, diagnostics, and provenance;
- `controller-state.json` stores only transient controller position authority,
  confidence, homing/session state, and reconnect verification data.

Clearing controller state must not delete an expensive design registration.
It makes the registration unavailable until Machine authority is reestablished.

`coordinate-frames.json` uses atomic replace and newest-only background
persistence, following the existing app-owned design-markup storage pattern.
A failed save leaves the last complete file intact. Parsing and hashing design
files remain off the GUI thread.

### Design Identity and Staleness

A design record stores resolved source path, size, modification time, and a
background content fingerprint together with top cell and design unit. Moving
a file may be resolved by an explicit relink whose fingerprint matches.
Changing content marks the record stale rather than deleting it.

Calibration fingerprints are stored per referenced axis. A real calibration
change applies dependency invalidation:

- X/Y/B calibration change invalidates design X/Y/B, Z, and A;
- Z calibration change invalidates Z and A;
- A calibration change invalidates A.

Changing the rotation pivot does not erase source marks or the base transform.
It changes evaluation at physical B and is applied only while idle, with a
visible diagnostic because displayed coordinates may shift.

### Invalid Data

One malformed custom or design record cannot prevent other records from
loading. Parsing returns per-record diagnostics and leaves the original file
unchanged until the operator explicitly saves a corrected collection. Invalid
records are never silently normalized into plausible physical coordinates.

Deleting the selected frame switches GUI display to Machine. API calls using a
deleted UUID return not-found; an already resolved active plan remains governed
by its immutable Machine snapshot.

## Migration

Legacy G54-G59 preferences are not converted to custom frames. Controller WCO
values live on the wrong side of nonlinear calibration and cannot be imported
safely. Legacy settings remain readable/preserved for compatibility during the
first migration but are ignored by the new software-frame selector.

The existing persisted single `DesignSession` may migrate into one design-frame
record:

- generate a stable registration-instance UUID;
- preserve original mark pairs and document/view metadata;
- refit the marks as a rigid transform without scale;
- retain prior scale difference only as a diagnostic;
- mark X/Y/B available when the refit and controller/machine provenance are
  valid;
- leave Z and A yellow because the old schema has no focus or contact reference.

Because Z is unavailable, a migrated record is incomplete and is not
automatically restored as the selected GUI frame. The legacy state is not
removed until the new record has been written successfully.

## Errors and Diagnostics

Coordinate resolution returns typed failures rather than fallback targets.
Representative categories are:

- `coordinate_system_not_found`;
- `coordinate_system_unavailable`;
- `axis_origin_unregistered`;
- `machine_axis_authority_unavailable`;
- `calibration_out_of_domain`;
- `target_out_of_bounds`;
- `motion_conflict`;
- `frame_changed_before_start`;
- `rotation_path_unavailable`.

GUI messages remain concise and place axis, required action, matrix, fit, and
provenance detail in tooltips/dialogs/logs. API errors include frame ID,
record version, axis, reason, and required action. Unknown, stale, incomplete,
and temporarily blocked frames remain distinguishable.

## Threading and Ownership

- Pure frame math, readiness, rigid fitting, and path planning have no Qt or
  hardware dependency.
- Registry updates occur through immutable snapshots/version changes.
- File parsing, hashing, camera matching, pivot fitting, and serial operations
  remain outside the GUI thread.
- Qt signals are emitted only after locks are released.
- Applying settings that affect transforms or calibration requires idle stage
  state.
- `StageMotionExecution` and the existing operation lifecycle retain sole
  ownership of hardware execution, reservation, cancellation, and completion.
- In-process GUI movement calls the resolver/controller directly and never
  loops through the local API.

## Implementation Sequence

The feature is implemented as independently testable vertical stages on
`codex/software-coordinate-systems`:

1. Coordinate core: physical pose, frame math, registry snapshots, readiness,
   and universal-mapper integration.
2. Persistence and Settings: custom frames, `coordinate-frames.json`, pivot
   profile, migration, and unconditional Z-to-A invalidation.
3. GUI and registration: selector, colors, multiple design instances, central
   FOV focus, A contact, and shallow C hiding.
4. Motion resolution: immutable plans, exact moves, Step, WASD, API contexts,
   and feedrate ownership.
5. Experimental point-hold rotation with assumed `(0, 0)` pivot, full preflight,
   execution, and logs.
6. Camera pivot calibration, robust fit, validation, review, and persistence.

Each stage ends in focused tests and a logical commit. Integration moves to the
new core only after the corresponding path is complete; old behavior is not
deleted speculatively.

## Automated Testing

### Pure Coordinate Math

- Machine/frame forward-inverse round trips;
- multiple B values and pivots;
- custom independent B zero and XY orientation;
- universal nonlinear curve composition and inversion;
- exact endpoint handling and rejection outside calibration domains;
- rigid registration without applying scale;
- pivot-attached frame origin/orientation evolution;
- point-hold path nodes and chord-error bounds.

### Readiness and Persistence

- every yellow/blue transition;
- dependency cascades `X/Y/B -> Z -> A` and `Z -> A`;
- no atomic Z/A bypass;
- temporary authority loss without reference deletion;
- multiple physical registrations of one GDS;
- design file and machine-calibration fingerprint changes;
- versioned round trips, malformed-record isolation, and atomic-save rollback;
- old single-session migration;
- delayed last-selection restore after X/Y homing;
- A-yellow restore allowed, Z-yellow restore rejected.

### Movement

- absolute rejection on each unavailable axis;
- permitted relative B/Z/A movement;
- atomic mixed-command rejection;
- frame-to-physical-to-controller planning through G53;
- no WCO involvement;
- active-plan immutability across GUI selection changes;
- jog stop and fresh press when the frame changes;
- ordinary rotating-frame XY+B jog rejection;
- GUI/API context isolation;
- shared versus pinned feedrate behavior;
- whole-path rejection when any rotation node fails preflight;
- actual-position recovery after cancellation/failure;
- known B motion does not stale registrations.

### GUI and API

- selector placement and Machine/Designs/Custom grouping;
- actual design names and UUID-backed duplicate registrations;
- per-axis colors and reason tooltips;
- C absent from ordinary controls while backend/settings serialization retain
  it;
- custom frame CRUD and downstream invalidation;
- central FOV candidate confirmation/change in Design Window;
- API list/status metadata and structured failures;
- API movement never changes the GUI selector.

### Rotation Calibration

- synthetic circle recovery with noise and outliers;
- Taubin initialization and robust refinement;
- held-out validation and diagnostic thresholds;
- objective/pixel-to-physical conversion;
- no mutation of B calibration scale/sign;
- cancellation and safe final-position reporting.

The complete existing test suite must remain green. Route Pause/Resume/Interrupt
regression tests remain mandatory whenever an integration point touches route,
autofocus, photo, contact, or stage lifecycle code.

## Hardware Acceptance

Automated tests do not run camera or FluidNC hardware. Hardware acceptance is:

1. Verify calibrated Machine display and inverse targeting on every installed
   axis.
2. Register one design through X/Y/B, central-structure focus Z, and contact A.
3. Register a second instance of the same GDS and switch between both.
4. Verify rotated-frame WASD at a conservative user-selected speed.
5. Compare GUI and API movement without changing the GUI selector.
6. Execute a small `Rotate around view center` move using assumed pivot `(0, 0)`
   and inspect planned/actual logs and image stability.
7. Run camera pivot calibration over a conservative configured B range, review
   its validation error, and repeat the point-hold test with the calibrated
   pivot.

The point-hold command remains explicitly experimental until these tests
provide evidence for suitable speed, segmentation, and usable angular range.

## Reference Implementations and Licensing

- Grbl/FluidNC G53 jog and Machine-coordinate behavior informs raw command
  execution.
- LinuxCNC rotary-table kinematics informs the pivot-attached frame model.
- OpenPnP runout calibration informs camera sampling and pivot fitting.
- OpenCV provides image registration; SciPy provides robust least squares.
- `spatialmath-python` and `circle-fit` are MIT-licensed Python-native
  dependencies suitable for direct import.

LinuxCNC/OpenPnP source is not copied. Their behavior and public documentation
are design references. The selected initial Python packages avoid introducing
GPL or AGPL linkage into this application.
