# Camera Exposure Policy And Optical Session Design

## Goal

Replace the raw `ExposureAuto` operator control and scan-specific one-shot logic
with one persistent exposure policy used by the GUI, API, autofocus, optical
calibrations, and multi-frame scans. Operators choose independently whether
automatic exposure is enabled and whether the application or camera performs the
adjustment. Every optical operation acquires a common session that fixes exposure
before movement and restores the selected policy afterward.

This design also keeps isolated Spinnaker `NEW_BUFFER_DATA` timeouts from being
reported as camera failures.

## Superseded Behavior

This design supersedes only the exposure-control portions of these earlier
designs:

- `2026-07-16-camera-settings-api-design.md`: `ExposureAuto` is no longer
  directly writable through the generic settings API once the policy controller
  owns it.
- `2026-07-16-one-shot-auto-exposure-design.md`: the scan-specific
  `auto_exposure` switch and software-only pre-scan adjustment are replaced by a
  mandatory optical session using the selected engine.
- `2026-07-17-optical-calibration-wizard-design.md`: the full wizard holds one
  outer optical session instead of running an independent software one-shot for
  each nested stage.

The camera-settings transport, raw/corrected frame separation, flat-field
processing, lens fitting, calibration paths, and scan geometry remain unchanged.

## Operator Model

Exposure configuration consists of two independent persisted values:

- `auto_enabled`: `false` for manual exposure or `true` for automatic exposure.
- `engine`: `software` or `camera`.

The engine remains meaningful while automatic exposure is disabled. It selects
the implementation used by the `Adjust Exposure` action and by mandatory
adjustments around optical operations. The initial and migration default is
`Auto + Software`.

The four combinations behave as follows:

| Exposure | Engine | Steady-state behavior | Adjustment behavior |
| --- | --- | --- | --- |
| Manual | Software | Fixed exposure | Application one-shot, remain manual |
| Manual | Camera | Fixed exposure | Native camera one-shot, remain manual |
| Auto | Software | Application monitor | Immediate application one-shot |
| Auto | Camera | Native `ExposureAuto=Continuous` | Native one-shot, then restore continuous |

Disabling automatic exposure stops the active automatic mechanism, sets native
automatic exposure to `Off`, and retains the latest exposure time. Enabling
automatic exposure starts the selected engine. Software starts with a full
one-shot; camera performs a native one-shot and then enters `Continuous`.

Changing the engine while automatic exposure is disabled changes only the next
adjustment implementation. Changing it while automatic exposure is enabled is an
atomic transition: stop the old engine, adjust once with the new engine, then
start the new automatic mode. A failed transition restores the previous policy
and camera state. Exhausting the software adjustment iterations is recoverable:
the Software policy remains selected, exposes a warning, and its periodic monitor
continues retrying. Camera I/O and persistence failures still roll back.

## Software And Camera Engines

The software engine reuses `CameraAutoExposureController` and raw RGB frames. A
full adjustment retains the verified target: the 99.5th percentile of per-pixel
maximum RGB should reach 235. Full one-shot convergence requires measurements
within 2% of the target.

While `Auto + Software` is active, a worker obtains one fresh raw frame every five
seconds and computes the same percentile. It starts a full adjustment only when
brightness differs from the target by more than 5%. A successful manual `Once`,
policy transition, or optical-session exit restarts the five-second interval when
the resulting policy is `Auto + Software`.
Flat-field and lens corrections are never applied to exposure measurements.

The camera engine controls native `ExposureAuto` only. Its one-shot sequence sets
`ExposureAuto=Once`, waits for the node to return to `Off`, and requires a fresh
frame acquired after completion. `Auto + Camera` then sets
`ExposureAuto=Continuous`. This policy does not introduce automatic gain or
automatic white balance; their existing operator and one-shot behavior remains
unchanged.

## Architecture

`ExposurePolicyController` is an application service between all callers and the
existing camera broker and one-shot controller. It owns the persisted policy,
runtime transitions, the software monitor, manual `Once`, and the serialization
of exposure operations. The GUI sends commands and receives state updates through
direct methods and Qt signals; it does not call the local HTTP API.

GenICam access remains in the camera worker command path. Percentile calculation,
fresh-frame waiting, and policy transitions run outside the GUI thread and do not
stop or restart acquisition. The controller activates lazily when the camera
becomes ready so startup is not blocked by camera reads.

`OpticalSessionManager` depends on `ExposurePolicyController` and issues
`OpticalSession` leases. Autofocus, click-to-move calibration, flat-field
calibration, lens-distortion calibration, the calibration wizard, and area scans
are clients. `StageController` remains a client rather than owning exposure
policy.

Only the outermost session performs exposure transitions. A nested operation must
carry its parent's session token; it increments the nesting count without another
one-shot or restore. An unrelated caller cannot be treated as nested and receives
a busy result. This lets the full optical wizard hold one session across its
nested calibration and autofocus work.

## Optical Session Lifecycle

Opening the outermost session performs these steps before any stage or needle
movement:

1. Acquire the exclusive exposure-operation lock.
2. Record both `auto_enabled` and `engine` plus the current camera settings.
3. Stop the software monitor or native continuous mode.
4. Run `Once` with the selected engine.
5. Wait for a fresh post-adjustment frame and set native exposure to `Off`.
6. Mark the session ready so the optical operation may move and capture frames.

Exposure remains fixed for the complete session. While it is active, the two
policy controls and the `Once` button are disabled in the GUI. External attempts
to change policy or run `Once` receive HTTP 409. Requests are not queued for later
execution.

Closing the outermost session restores the recorded operator policy:

- `Manual + Software`: run a final software one-shot and remain `Off`.
- `Manual + Camera`: run a final native one-shot and remain `Off`.
- `Auto + Software`: resume the five-second software monitor.
- `Auto + Camera`: restore native `Continuous`.

The close path runs in `finally` for success, failure, and cancellation. A failed
pre-operation adjustment restores the previous state, aborts the operation before
movement, and reports failure. A failed final manual adjustment preserves the
completed operation result, leaves the last fixed exposure active, and shows a
warning. Nested session closure does not restore exposure until the outermost
lease closes.

The old per-scan `auto_exposure` option is removed from GUI and API scan models.
Every multi-frame optical scan uses this mandatory lifecycle, so there is no
second exposure mechanism or bypass with different semantics.

## Camera Settings UI

The Camera Settings dialog starts with a compact operator block:

- `Control`: a `Manual | Auto` dropdown.
- `Method`: a `Software | Camera` dropdown.
- `Timing`: a `Timed | Trigger width` dropdown backed by `ExposureMode`.
- `Exposure time`: editable only in manual mode and outside an optical session.
- `Adjust Exposure`: a push button with a busy state while adjustment is running.

`Control` and `Method` remain independent and are both persisted. They may be
changed in any steady state. `Trigger width` is available only with the `Camera`
method because the software engine adjusts `ExposureTime`. Selecting `Software`
while `Trigger width` is active first writes and confirms `Timed`, then changes
the exposure policy. During an optical session, the complete block is disabled
rather than accepting deferred commands.

Camera field edits, timing, and exposure-policy selections are dialog-local until
the operator presses `Apply` or `Save`. `Cancel` performs no camera writes. Apply
operations are ordered and confirmed one at a time so dependent settings cannot
overtake each other; `Save` closes only after the complete camera transaction
finishes. `Adjust Exposure` remains an immediate command and requires pending
settings to be applied first.

Raw `ExposureAuto` is hidden from the generic operator node list because its
`Off`, `Once`, and `Continuous` values are controller implementation state, not
the saved operator policy. `ExposureMode` and `ExposureTime` appear only in the
operator block, not a second time in the generic list. `ExposureCompensationAuto`
and `ExposureCompensation` are omitted from operator settings. The dialog remains
lazy and all camera reads continue through its worker-backed settings source.

## API

The policy API uses the existing camera permissions:

- `GET /api/v1/camera/exposure-policy` requires `camera_read` and returns the two
  configured values plus the current runtime state and busy flag.
- `PUT /api/v1/camera/exposure-policy` requires `camera_write`, accepts both
  configured values, and completes only after the required transition succeeds.
- `POST /api/v1/camera/exposure-once` requires `camera_write`, uses the selected
  engine, and returns the engine, final exposure, final frame counter, and
  convergence result.

The previous `POST /api/v1/camera/auto-exposure` route and corresponding Python
client method are replaced by `exposure-once`; they are not retained as a fallback
alias.

The generic camera settings read API may still report native `ExposureAuto` for
diagnostics, but marks it as policy-managed. Generic writes to `ExposureAuto` are
rejected. Writes to `ExposureTime` are rejected while automatic exposure or an
optical session is active. This prevents external clients from making the camera
state disagree with the policy reported by the GUI and API.

Policy changes and `Once` return HTTP 409 while an optical session is active.
Invalid engines return HTTP 400. Camera-unavailable and bounded operation timeout
errors use the existing structured API error format.

## Error Handling

A failed periodic software check or adjustment does not silently switch engines
or disable the configured policy. The controller records the failure, keeps the
configured state, and retries on the next interval. Repeated failures are
rate-limited in logs and reflected in runtime state.

A manual `Once` or policy transition snapshots affected camera values and restores
them on failure. The GUI reverts to the last successful policy instead of showing
an uncommitted selection.

A Spinnaker exception with code `-1011` while waiting for
`NEW_BUFFER_DATA` is a transient frame timeout. One occurrence does not emit the
camera error signal, operator alert, or warning log. A named consecutive-timeout
threshold escalates only when no valid frame has recovered; the next valid frame
resets the counter. Other Spinnaker exceptions keep their existing behavior.

Shutdown stops the software monitor, closes any idle controller resources, and
does not wait indefinitely for a fresh frame. An active optical operation retains
the existing controlled cancellation and stage-restoration behavior.

## Testing

Hardware-independent tests cover:

- persistence, migration to `Auto + Software`, and all four policy combinations;
- enabling, disabling, and engine changes in manual and automatic modes;
- the five-second software check, 5% drift threshold, 2% convergence threshold,
  and interval reset after an explicit adjustment;
- native `Once` completion and `Continuous -> Once -> Continuous` restoration;
- manual `Once` with each engine and rollback on failure;
- outer, nested, failed, and cancelled optical sessions;
- pre-operation failure before movement and post-operation warning behavior;
- disabled GUI controls, manual exposure editability, persistence, and failed
  transition rollback;
- policy API authorization, validation, response mapping, HTTP 409 busy behavior,
  managed-node write rejection, and removal of the old route;
- autofocus, click-to-move calibration, both optical calibrations, the full
  wizard, and area scans entering the common session at their shared execution
  boundaries;
- removal of the per-scan exposure switch and one fixed exposure across every
  captured frame in a session;
- isolated `NEW_BUFFER_DATA [-1011]`, repeated timeout escalation, and recovery
  counter reset.

Tests use fake camera settings, synthetic raw frames, fake clocks, and fake stage
operations. They assert ordering explicitly: adjustment completes before the
first movement, exposure remains fixed during capture, and restoration follows
the final frame.

After the focused and full automated suites pass, manual verification on the
connected camera covers both `Once` engines, both automatic modes, autofocus, and
one short optical calibration. Logs are checked to confirm that periodic software
checks do not stall acquisition and isolated `-1011` timeouts do not produce
operator-facing camera errors.

## Acceptance Criteria

- The operator controls automatic enablement and engine with two independent,
  persisted settings; startup defaults to `Auto + Software`.
- `Adjust Exposure` works with either engine and never changes the saved pair.
- `Auto + Software` checks every five seconds and performs full adjustment only
  beyond the 5% drift threshold.
- Every autofocus, optical calibration, click-to-move calibration, and multi-frame
  area scan captures with fixed exposure established before movement.
- Nested optical work performs one outer pre-adjustment and one outer restore.
- Exposure controls are unavailable during optical work, and no deferred command
  is applied afterward.
- GUI and API report the same policy, and direct native automatic-exposure writes
  cannot bypass it.
- The GUI thread remains responsive and acquisition is not restarted for policy
  checks or transitions.
- Isolated `NEW_BUFFER_DATA [-1011]` timeouts are silent while sustained camera
  failure still escalates.
