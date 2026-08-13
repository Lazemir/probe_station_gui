# Settings Dialog Transaction Design

## Goal

Extract the settings-dialog commit policy from Main into one deep, Qt-free
module while preserving the exact settings, coordinate, objective,
exposure-policy, and presentation ordering.

This is deliberately not an extraction of Main._apply_settings(). That method
is the application composition root for Stage, joystick, needle, coordinate,
design, meter, oscillation, API, Telegram, and display owners. Moving it would
require Main, a mega-port, or a broad callback bundle and would create a
shallow module.

## Alternatives Considered

### Full settings application runtime

Rejected. The interface would need nearly every application owner. Deleting
such a module would reveal only pass-through calls, not hidden policy.

### Objective mutation runtime

Rejected for this pass. Pure objective decisions already belong to
probe_station_gui/design/objective_alignment.py. A new runtime would either
layer over those plans or expose many command variants for selection, profiles,
offsets, distortion, and calibration.

### Settings dialog transaction

Selected. The existing Main._apply_settings_from_dialog() contains one
cohesive transaction with the highest current Main method complexity
(Radon CC 25). Its clone/diff, rollback, preservation, persistence, and
coordinate reconciliation rules can sit behind one method and two immutable
values.

## Module and Interface

Create probe_station_gui/settings/dialog_transaction.py with three frozen data
types: SettingsDialogContext, SettingsDialogNotice, and
SettingsDialogOutcome. Context contains stage_busy, objective_mutation_busy,
and one cache-only MachineCoordinateSnapshot or None. Outcome contains
accepted, apply_objective_runtime, refresh_coordinate_frame_display,
observe_coordinate_authority, and ordered post_apply_notices.

SettingsDialogTransaction is constructed with the one SettingsManager, the one
CoordinateSystemCoordinator, and two narrow internal adapters:
publish_notice(message, timeout_ms) and publish_transition(transition). Its one
public operation is:

    apply(
        submitted: object,
        context: SettingsDialogContext,
    ) -> SettingsDialogOutcome

The adapters have two real implementations: Main production rendering and
deterministic in-memory tests. They preserve the existing pre-save validation
notices and exact save -> custom transition -> calibration reconciliation
order without passing Main into the module.

Construction performs no I/O. apply() may perform the same settings save that
the current GUI action performs. machine_snapshot is cache-only; the
transaction never initiates Stage I/O.

## Transaction Behavior

apply() owns these rules in this order:

1. Ignore a non-Settings submission without side effects.
2. Reject a busy Stage with the existing 4000 ms status and no clone,
   coordinator mutation, or save.
3. Clone the submission and compare it with current software coordinates,
   axis calibrations, and objectives.
4. If custom frames changed while frames are loaded, synchronize them through
   the one coordinate coordinator. On TypeError or ValueError, restore the
   previous custom frames, publish the existing validation message, and
   recompute whether coordinates changed.
5. If the B-axis pivot changed for an active Design frame, validate rotation
   geometry and require B from the supplied cached Machine snapshot. On
   DesignModelError, TypeError, or ValueError, restore only the previous pivot,
   publish the existing message, and preserve all unrelated settings.
6. If objective mutation is busy, preserve the current active objective name
   and active profile while accepting unrelated objective profiles and
   unrelated settings.
7. Compute whether objective, pivot, or axis-calibration authority changed.
8. Save with preserve_exposure_policy=True.
9. Publish the custom-system transition, if any.
10. Observe design calibration fingerprints, publish its transition, and
    report whether coordinate-frame display must refresh.
11. Return whether Main must apply objective runtime, refresh coordinate
    display, observe coordinate authority, and show the existing post-apply
    active-objective rejection notice.

Main remains the Qt adapter. It constructs SettingsDialogContext using state
flags and latest_machine_coordinate_snapshot() only. It calls the transaction,
returns on rejection, refreshes the coordinate-frame display when requested,
invokes the existing _apply_settings() with the returned objective flag,
observes coordinate authority after runtime application, shows post-apply
notices, and logs completion.

## Error and Ordering Guarantees

- No settings are saved when the Stage is busy.
- Invalid custom frames or pivot edits roll back only their own submitted
  section; unrelated settings remain eligible for save.
- An active-objective mutation rejected for busy optical/Stage work does not
  discard inactive-profile or unrelated settings changes.
- Concurrent exposure-policy state remains authoritative through
  preserve_exposure_policy=True.
- Coordinate transitions are rendered after save and in their original order.
- Coordinate-authority observation remains after _apply_settings().
- Status text and timeout values remain exact.
- No Qt signal is emitted while the transaction owns a lock; the transaction
  introduces no lock.
- No Stage, camera, instrument, network, or file read is introduced beyond the
  existing settings save and supplied cached snapshot.

## Ownership and Deletion Test

SettingsDialogTransaction.apply is the sole owner of the transaction policy.
The following policy names and decisions disappear from Main:

- custom_frames_changed
- pivot_changed
- active_objective_update_rejected
- objective_authority_changed
- calibration-fingerprint reconciliation
- direct replace_and_save for dialog submission

Deleting the module forces the clone/diff, three rollback policies,
exposure-safe persistence, two coordinate reconciliation phases, and authority
flags back into Main. The module therefore earns its seam rather than wrapping
existing calls.

Main._apply_settings_from_dialog remains only because it is the Qt callback
adapter. It must not reimplement policy or expose compatibility forwarding.

## Test Strategy

Add direct owner tests before production migration:

- absent module/class RED;
- invalid submission and busy Stage are side-effect free;
- custom-frame success and rollback;
- pivot success and rollback with no live Stage read;
- busy active-objective preservation with inactive/unrelated updates retained;
- exposure-policy-preserving save;
- exact save/transition/reconciliation ordering;
- coordinate refresh and authority flags;
- exact early and post-apply notices.

Add an architecture test for canonical identity, exact public surface,
construction in Main, physical policy deletion from Main, absence of aliases,
package re-exports, __getattr__, reverse imports, Qt imports, and direct
manager/coordinator ownership.

Keep focused Main integration tests for the adapter order:

- save before general apply;
- general apply before coordinate-authority observation;
- rejected transaction performs no downstream rendering;
- busy active objective skips objective runtime but applies unrelated settings;
- settings-dialog objective restoration remains exact.

Then run affected settings/objective/coordinate tests, five fresh owner
processes, deterministic all-node partitions, configured Ruff, scoped format,
compileall, diff-check, AST/import/protected gates, Radon/Lizard/MI, exact
scope/hash freeze, and an independent Critical/Important/Minor review.

## Scope

Expected production scope:

- create probe_station_gui/settings/dialog_transaction.py;
- modify main.py only for construction and the thin Qt adapter;
- do not modify SettingsManager, CoordinateSystemCoordinator, Stage owners,
  objective planners, route, camera, instrument, or Telegram production code.

Existing tests may change only where they directly call the old private Main
transaction or assert its source text. New direct owner and architecture tests
replace private policy seams; unique end-to-end behavior tests remain.
