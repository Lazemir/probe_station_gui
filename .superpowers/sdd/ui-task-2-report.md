# UI Task 2 Report: Multiple Persistent Design Frame Instances and Migration

Status: complete

## Delivered

- Added durable `DesignFrameMetadata`, independent UUID/name drafts, rigid X/Y/B commits, fingerprint staling, and idempotent legacy-frame matching.
- Added `DesignSession.active_frame_id`; v3 controller state stores only the durable frame link while v1/v2 registration payloads remain migratable.
- Loaded and saved `coordinate-frames.json` independently on the existing background store facade; Main owns and stops the store.
- Linked existing frames, created clean independent drafts for reset/re-registration, preserved records on unload, reconciled restored/interactive top-cell changes, and kept canonical design marks stable across rotated-view reloads.
- Kept durable marks in calibrated physical Machine XY while projecting them through physical-to-configured calibration and objective offsets only for legacy navigation.
- Made legacy migration transactional: the legacy controller payload remains durable until a successful frame-store acknowledgement, coalesced saves retain the transaction, controller rewrite failures remain pending, and crash retries reuse an equivalent frame.
- Added immutable active-frame ID/version snapshots to API and GUI route-start plans. No Pause/Resume/Interrupt behavior was changed.
- Physical alignment remains disabled; registration commits issue no hardware motion.

## TDD and verification

- Initial RED: `tests/design/test_frame_registration.py` failed collection with `ModuleNotFoundError: probe_station_gui.design.frame_registration`.
- Additional RED regressions covered migration ordering with the real controller exporter shape, calibration failures before mutation, non-zero objective offsets, top-cell reconciliation, rotated-view restart, route snapshot isolation, and committed-frame source-mark replacement.
- Exact Task 2 suite: `65 passed in 0.88s`.
- Affected integration suite: `216 passed, 3 subtests passed in 2.03s`.
- Full suite: `2522 passed in 52.43s`.
- `git diff --check`: clean; only Git's expected LF-to-CRLF working-copy warnings were emitted.
- Hardware-dependent application code was not launched.

## Review

Read-only review was repeated after each audit repair. Final verdict: ready, with no remaining Critical or Important findings.

No known Task 2 blockers remain. Z/A reference capture and compensated physical alignment remain intentionally deferred to their later planned tasks.

## Official review fix wave

Status: complete

- Reworked coordinate-frame authority so X/Y use controller homing while B authority comes only from a fresh tracked status value that successfully passes calibrated B conversion. Authority blocks are projected into the active session only; the durable registry record, version, readiness, and metadata are never changed. Disconnect, reboot, missing-B status, and failed conversion block the frame, while later fresh status or successful homing can restore the eligible axes.
- Removed durable-registration invalidation from manual B jog, coordinate-target B motion, and status-observed B motion. Every fresh status now refreshes authority; a known physical B value reprojects active source/check marks through the durable `BFrameTransform` and configured pivot, while an unknown B value blocks navigation.
- Split check-only registration updates from source fitting. Check captures are normalized from the physical capture angle back to the durable reference-B basis, preserve the transform and readiness, and update only check evidence and residual diagnostics. Adding later source evidence at another known B uses all source/check evidence in the current-B basis before fitting.
- Added side-effect-free link preparation and delayed registry add/replace until projection succeeds. Legacy migration and document reconciliation now leave the registry snapshot/generation and active session link unchanged when calibration or navigation projection fails.
- Bound the immutable start-plan design-frame snapshot to both the actual GUI `RouteMeasurementRunner` and the API `RouteExternalMeasurementSessionRunner`. Frame ID/version lineage is exposed by runner status, audit history, and result payloads and remains stable after the active Design Window frame changes.
- Enforced design identity as resolved source path plus top-cell name for explicit, automatic, persisted, and interactive activation. Wrong-top frames are rejected or skipped rather than linked.
- Pause/Resume/Interrupt state handling and contact/autofocus propagation were not changed.

### Review-wave TDD and verification

- Strict RED/GREEN regressions covered: B authority without B homing queries; unknown/missing B status; manual, coordinate, and status-observed B motion; current-B reprojection with a non-zero pivot; check capture at a changed B; source capture at a changed B with existing check evidence; migration/reconciliation projection failure atomicity; GUI/API runner lineage; and explicit, automatic, and persisted wrong-top selection.
- Exact Task 2 suite: `71 passed in 0.89s`.
- Affected integration suite: `297 passed, 3 subtests passed in 5.19s`.
- Full suite: `2537 passed, 12 subtests passed in 50.59s`.
- `git diff --check`: no whitespace errors; only Git's expected LF-to-CRLF working-copy warnings were emitted.
- Hardware-dependent application code was not launched.
