# Coordinate System Coordinator Design

## Problem

Coordinate-system policy is split between `main.py`,
`main_window_connection_flow.py`, `main_window_stage_position_panel.py`,
`CoordinateFrameLifecycle`, `DesignRegistrationLifecycle`, the frame registry,
and `DesignSession`. `Main` constructs lifecycle contexts, applies lifecycle
effects, mutates the registry/session, orders persistence, and then updates Qt.
Tests therefore reproduce private `Main` state instead of exercising one domain
boundary.

The current Wily baseline is:

- `main.py`: 12,188 lines, aggregate cyclomatic complexity 2,219, MI 0;
- `tests/app/test_main_design_navigation.py`: 5,008 lines, MI 0;
- 30 repository files at MI 0 (22 production, 8 test files).

This track addresses the Design/coordinate orchestration cluster only. Every
pass must lower `Main` size/complexity and must not move policy into another Qt
helper.

## Domain Language

- **Coordinate System** is a user-selectable view used for displayed positions
  and relative movement: Machine, Design, or Custom.
- **Coordinate Frame** is the stored registration containing transform,
  readiness, and provenance that relates a Coordinate System to the Stage.
- **Stage** is the physical machine/controller coordinate authority.

Do not use WCO for software Coordinate Systems or Coordinate Frames.

## Architecture

`CoordinateSystemCoordinator` is the single public application-domain boundary
for Coordinate Systems. It owns, by the end of the migration:

- `CoordinateFrameRegistry`;
- Coordinate Frame durable document and request generations;
- `CoordinateFrameLifecycle`;
- `DesignRegistrationLifecycle`;
- the coordinate-relevant `DesignSession` state and active frame link;
- load/save coalescing, acknowledgement, and rollback ordering;
- selection intent, runtime authority, usability, and presentation projection.

It has no Qt, `QObject`, widgets, serial access, camera access, filesystem I/O,
or `SettingsManager` dependency. The public facade is kept small; private
Qt-free reducers live in `coordinator_persistence.py`,
`coordinator_registration.py`, and `coordinator_selection.py`. Existing
lifecycle and transform modules stay as private in-process collaborators; their
effect objects do not cross the coordinator boundary after their migration
pass.

`Main` becomes an adapter. It builds immutable observations, executes returned
adapter intents, feeds asynchronous completions back by opaque intent ID, and
renders the returned snapshot/notices.

## Public Shape

The interface is caller-first and workflow-specific. It deliberately has no
generic `dispatch(Event)` method.

```python
@dataclass(frozen=True)
class CoordinateTransition:
    snapshot: CoordinateSystemSnapshot
    intents: tuple[CoordinateAdapterIntent, ...] = ()
    notices: tuple[CoordinateNotice, ...] = ()


class CoordinateSystemCoordinator:
    def start(self, profile: MachineProfileObservation) -> CoordinateTransition: ...
    def complete(self, completion: CoordinateAdapterCompletion) -> CoordinateTransition: ...
    def publish_frame_records(self, publication: FrameRecordsPublication) -> CoordinateTransition: ...
    def activate_design(self, request: DesignActivationRequest) -> CoordinateTransition: ...
    def close_design(self) -> CoordinateTransition: ...
    def select_system(self, request: CoordinateSystemSelection) -> CoordinateTransition: ...
    def observe_authority(self, observation: CoordinateAuthorityObservation) -> CoordinateTransition: ...
    def capture_registration_mark(self, request: RegistrationCaptureRequest) -> CoordinateTransition: ...
    def offer_focus_candidate(self, request: FocusCandidateRequest) -> CoordinateTransition: ...
    def use_focus_reference(self, request: FocusReferenceRequest) -> CoordinateTransition: ...
    def reset_focus_reference(self) -> CoordinateTransition: ...
    def arm_first_contact(self, request: FirstContactRequest) -> CoordinateTransition: ...
    def cancel_registration(self, reason: RegistrationCancellation) -> CoordinateTransition: ...
    def synchronize_custom_systems(self, request: CustomSystemsRequest) -> CoordinateTransition: ...
    def snapshot(self) -> CoordinateSystemSnapshot: ...
    def close(self) -> CoordinateTransition: ...
```

`complete()` is the sole generic operation and accepts only typed adapter
completion values carrying a coordinator-issued intent ID. User actions remain
explicit named methods.

During the first migration pass only, existing not-yet-migrated domain callers
use `publish_frame_records(FrameRecordsPublication)` to hand a complete immutable
frame proposal and its exact session checkpoint to the coordinator. The
coordinator adopts the one registry/session instance during Task 1 and is the
only code that installs proposed records, links the session for a publication,
or restores publication rollback. Main may still use the same DesignSession for
not-yet-migrated non-publication behavior, but no old publisher mutates it or
the registry before the call. This is not a Main forwarding wrapper: it is the
one persistence-domain input and every old publisher calls it directly.
Registration and selection passes replace those call sites with workflow
methods; the transitional operation is deleted when all registry/session
behavior becomes exclusive.

## Transition Contract

`CoordinateSystemSnapshot` is immutable and sufficient for all Coordinate
System rendering and motion consumers. It includes:

- available Coordinate Systems and selected user intent;
- the active Design Coordinate Frame ID/version;
- frame readiness/provenance and runtime authority availability;
- Design registration instance/progress/focus/contact presentation;
- complete coordinate display plan and Design usability lease data.

`CoordinateAdapterIntent` is a closed typed union. The migration introduces
only intents backed by real adapters:

- load/save Coordinate Frame document;
- persist GUI Coordinate System selection;
- capture synchronized physical Machine pose;
- move to a focus target;
- run autofocus;
- read physical A/contact state.

`CoordinateNotice` contains finished-product status text and severity/duration.
Expected rejections and stale callbacks are transition values, not exceptions.
Malformed/non-finite programmer input may raise `TypeError` or `ValueError`.

Coordinate Frame persistence is acknowledged: no registration success notice
appears before a save completion. GUI Coordinate System selection persistence
keeps its existing fire-and-forget behavior; a `PersistCoordinateSelectionIntent`
has no success completion and adapter failure returns the existing error status.

## Ordering and Safety Invariants

1. Coordinator calls run on the creator/GUI thread. Hardware and filesystem
   work are intents executed elsewhere.
2. Starting a load allocates an intent ID, marks runtime provenance pending,
   invalidates the active session link, and returns an unavailable snapshot
   before the adapter load is submitted.
3. Only the current load completion may install a document/records. Stale load
   results are inert.
4. A frame mutation, exact previous frame, and exact registration/session
   rollback baseline are recorded before a save intent is returned.
5. Older save failure is deferred while a newer publication is outstanding.
   Terminal failure collapses each frame chain to its earliest durable
   predecessor, restores all frames in deterministic frame-ID order, and
   restores each exact registration baseline once.
6. Success copy is returned only after durable acknowledgement. Synchronous
   submission failure is fed through the same completion path.
7. Persistent readiness/provenance is never overwritten by transient
   controller authority. X/Y homing, physical B, pivot, and source/profile
   authority are recomputed from observations.
8. Capture identity is checked before snapshot conversion, status output, or
   session mutation. Mixed-B samples normalize around the first indexed sample
   using each sample's captured pivot/objective context.
9. Registration cancellation restores its baseline at most once. Focus order
   remains target-bound move, move success, autofocus success, Z commit. Contact
   remains Z-gated and first-write-wins for A.
10. Explicit temporary Coordinate System unavailability preserves selection
    intent. Permanent corruption falls back to Machine using current persistence
    rules. Motion uses only a current immutable Design usability lease.
11. No coordinator path issues B motion. Existing route Pause/Resume/Interrupt
    and stage safety semantics remain unchanged.

## Migration

There is no compatibility facade and no private pass-through wrapper in `Main`.
Each pass introduces coordinator behavior, redirects its real callers, deletes
the replaced policy/effect code, and moves its tests to the coordinator
boundary in the same commit.

1. Frame load/save/publication/rollback.
2. Registration capture/commit/focus/contact.
3. Selection/usability/authority/presentation and final state ownership.

The public GUI/package behavior remains stable throughout. Temporary shared
state is allowed only for a not-yet-migrated later pass; migrated state has one
owner immediately.

## Acceptance

- behavior parity through focused and full tests;
- disconnected GUI opens/closes and exposes selector, Design Window, and
  Coordinate Systems settings without blocking startup;
- no hardware access during automated verification;
- `tests/app/test_main_design_navigation.py` and every new coordinator file have
  Wily/Radon MI greater than zero;
- `main.py` LOC and aggregate complexity decrease in every pass and never
  regress; its global MI>0 gate belongs to completion of the full multi-cluster
  MI-zero program, because the current Radon inputs require roughly a 94%
  proportional reduction and cannot be reached inside the Coordinate System
  domain alone;
- no new MI-0 file and repository MI-0 count decreases;
- if the three planned passes do not satisfy the selected-cluster metric gate,
  additional extraction is limited to residual Design/coordinate adapters and
  tests; no camera, route-control, or instrument policy is pulled into scope.

## Non-goals

- changing calibration curves or coordinate math;
- Plan 3 frame-relative motion or Plan 4 B-axis point hold;
- camera or serial redesign;
- route-control semantic changes;
- fixing unrelated MI-0 clusters in this branch.
