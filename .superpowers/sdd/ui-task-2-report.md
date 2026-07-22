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
