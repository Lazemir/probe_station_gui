# Coordinate Lifecycle Cleanup and Module Extraction

## Status

Approved in design review on 2026-08-07. The user selected the two-module
architecture after comparing it with a single coordinator and helper-only
extraction.

## Purpose

Close four non-blocking defects left after the software-coordinate Design/UI
work, then move coordinate lifecycle state out of `Main` before Plan 3 adds
frame-relative motion and API resolution.

The refactor must preserve the behaviour verified at branch head `9955ce3`.
It must not add Plan 3 motion, Plan 4 B-axis point hold, or new hardware
behaviour.

## Decisions

- Use two deep modules rather than one general coordinator:
  `CoordinateFrameLifecycle` and `DesignRegistrationLifecycle`.
- `Main` remains the Qt and hardware adapter. The lifecycle modules perform no
  serial I/O, filesystem I/O, Qt signal emission, or widget mutation.
- Module inputs and outputs are immutable typed values. Callers do not inspect
  mutable internal collections or reproduce lifecycle rules.
- Extraction is incremental and characterization-test driven. Each moved
  behaviour remains green before the next state family is transferred.
- Existing route Pause/Resume/Interrupt semantics and API route-control naming
  are unchanged.

## Behavioural Cleanup

### Temporary authority loss preserves selection intent

An already-selected Design or Custom frame remains the GUI selection when B or
another required Machine authority becomes temporarily unavailable. The
position fields become unavailable/yellow, editing and movement remain blocked,
and the persisted selected frame ID is not replaced with Machine. When authority
returns, the same frame becomes usable automatically.

Permanent invalidity is different: explicit deletion, missing durable record,
or a permanently rejected record falls back to Machine. An explicit attempt to
newly select a currently unavailable frame is rejected.

Presentation therefore distinguishes:

- selected frame intent;
- current frame usability;
- permanent absence.

It never displays Machine values under a selector that still names another
frame.

### Explicit deletion cannot resurrect duplicate UUID records

Deleting a frame ID removes every accepted and rejected raw slot that explicitly
contains the same UUID. This applies to durable Design records and Custom-frame
settings. Unknown raw mappings with that UUID are removed even if the rest of
their schema is unsupported. Raw values with no recoverable frame ID remain
preserved.

The deletion operation is explicit and lossless for unrelated records. A
save/reload cycle cannot promote a previously rejected duplicate and resurrect
the deleted frame.

### Coordinate-store backend creation is retryable

If `CoordinateFrameStoreWorker` cannot create its backend:

- every operation already queued for that failed worker run receives a typed
  failure;
- the worker thread retires and clears its live-thread reference under its
  existing lock;
- no Qt signal is emitted while that lock is held;
- a later submission starts a fresh worker run and retries the backend factory.

Stopping remains final and does not permit restart. A backend failure is not
treated as `stop()`.

### FOV dimensions are strictly positive

Focus-candidate selection rejects non-finite, zero, and negative original FOV
width or height. Signed values are validated before any normalization; `abs()`
is not used. Existing positive FOV behaviour is unchanged.

## Deep Modules

### `CoordinateFrameLifecycle`

Location: `probe_station_gui/coordinates/lifecycle.py`.

This module owns frame lifecycle policy that is currently distributed across
`Main`, presentation helpers, connection-flow callbacks, and persistence
callbacks:

- selected frame intent and restore eligibility;
- temporary authority/readiness blocks versus permanent absence;
- current immutable Design-frame usability snapshots;
- coordinate document load generation and stale-result rejection;
- registration-publication acknowledgement and rollback chains;
- save request ordering needed to reconcile memory with durable state.

Its small external interface consists of domain operations that return immutable
decisions/effects. The initial interface is:

```python
class CoordinateFrameLifecycle:
    def plan_selection(self, context: FrameSelectionContext) -> FrameSelectionDecision: ...
    def design_usability(self, context: DesignUsabilityContext) -> DesignFrameUsabilitySnapshot: ...
    def begin_load(self, request_id: int) -> FrameLifecycleEffects: ...
    def accept_load(self, result: FrameLoadResult) -> FrameLifecycleEffects: ...
    def track_publication(self, publication: FramePublication) -> None: ...
    def finish_publication(self, result: FramePublicationResult) -> FrameLifecycleEffects: ...
```

`FrameLifecycleEffects` describes registry replacements, session invalidation or
relinking, persistence rollback, and UI refresh requirements. `Main` executes
those effects through existing adapters. The module does not own the Qt worker,
the registry implementation, SettingsManager, or widgets.

### `DesignRegistrationLifecycle`

Location: `probe_station_gui/design/registration_lifecycle.py`.

This module owns registration-operation state:

- immutable document/top-cell/frame/version/mark-set operation tokens;
- pending physical mark batches and their Machine B/pivot context;
- baseline evidence used for transactional rollback;
- focus candidate/token validity and first-contact eligibility;
- cancellation on document, top-cell, frame, mark-set, Z, or route-context
  change.

Its initial interface is:

```python
class DesignRegistrationLifecycle:
    def begin_capture(self, context: RegistrationCaptureContext) -> RegistrationCaptureToken: ...
    def accept_sample(self, token: RegistrationCaptureToken, sample: RegistrationSample) -> RegistrationEffects: ...
    def set_focus_candidate(self, candidate: FocusCandidate, context: RegistrationContext) -> RegistrationEffects: ...
    def accept_focus(self, context: RegistrationContext) -> RegistrationEffects: ...
    def accept_first_contact(self, context: RegistrationContext, a_mm: float) -> RegistrationEffects: ...
    def cancel(self, reason: RegistrationCancellation) -> RegistrationEffects: ...
```

The implementation may use private internal modules, but callers learn only
this interface. It does not fit transforms itself; existing registration math
remains the adapter used when `RegistrationEffects` requests a commit.

## Data Flow

1. A Qt or controller callback constructs an immutable context from the current
   synchronized Machine snapshot, durable registry snapshot, design identity,
   and settings snapshot.
2. `Main` calls one lifecycle module operation.
3. The module validates generation and operation tokens, changes its private
   state, and returns immutable effects.
4. `Main` executes effects through existing registry, persistence, UI, and
   controller adapters.
5. Async completions return with their original token/request ID and are
   accepted only by the owning module.

No module reads widgets, global settings, serial state, or files on demand.

## Error Handling

- Invalid/stale lifecycle events return a blocked/no-op effect with a surfaced
  reason; they never partially mutate state.
- Persistence failure decisions preserve the proven rollback semantics at
  `9955ce3`, including coalesced and interleaved publication chains.
- Registration cancellation restores its baseline or discards tentative
  evidence before a later operation may begin.
- Module exceptions do not cross Qt callbacks for expected domain failures;
  expected failures are typed outcomes. Unexpected exceptions remain logged and
  fail closed.

## Migration Strategy

1. Fix and commit the four independent behavioural defects.
2. Add pure characterization tests for the lifecycle decisions currently made
   by `Main`.
3. Introduce `CoordinateFrameLifecycle`, move one state family at a time, and
   keep compatibility methods in `Main` only as thin adapters.
4. Introduce `DesignRegistrationLifecycle`, transfer operation tokens and
   evidence ownership, then remove the corresponding mutable `Main` fields.
5. Delete pass-through helpers after every production call site uses the new
   interface.

No compatibility layer may contain a second copy of lifecycle policy.

## Testing

- Unit tests exercise each module only through its external interface.
- Presentation tests cover temporary authority loss and permanent deletion as
  separate cases.
- Persistence tests cover rejected duplicate resurrection and backend-factory
  fail-once/recover behaviour, including queued requests and thread retirement.
- Focus-candidate tests cover negative, zero, non-finite, and positive FOV.
- Integration tests prove `Main` delegates frame selection, load validation,
  publication rollback, capture cancellation, focus, and first contact without
  changing user-visible behaviour.
- Existing route, shutdown, coordinate, Design navigation, and API regressions
  remain mandatory gates, followed by the complete repository suite.

Hardware-dependent execution remains outside automated verification.

## Out of Scope

- Plan 3 frame-relative movement and API resolver implementation.
- Plan 4 compensated B point-hold or pivot calibration.
- Generic axis enable switches or C-axis removal.
- Changes to route Pause/Resume/Interrupt semantics.
- Broad reorganization of unrelated `Main` responsibilities.
