# Camera Settings API And Auto-Exposure Diagnostic Design

## Goal

Expose the microscope camera's operator-facing controls through the authenticated
local API without interrupting acquisition, then use that API to compare the
camera's native automatic exposure with a deterministic application-side exposure
controller. The diagnostic determines which mode protects bright aluminium detail
while retaining useful signal on silicon; it does not add a new production auto
exposure mode to the GUI.

The work is isolated on `codex/camera-settings-api`.

## Scope

The API covers only controls an operator needs while imaging:

- `ExposureAuto`
- `ExposureTime`
- `GainAuto`
- `Gain`
- `BlackLevel`
- `BalanceWhiteAuto`
- `BalanceRatioSelector`
- `BalanceRatio`

Acquisition, trigger, transport, pixel-format, and arbitrary GenICam nodes remain
outside the API even if the existing camera settings dialog can display them.
There is no generic node passthrough and no substitution of one node for another
when a requested node is unavailable.

## Architecture

The FastAPI server receives authenticated camera requests through dedicated,
thread-safe callbacks. Camera setting operations are placed in the existing
`Grabber` command queue and are executed by the camera worker while acquisition
continues. Results carry a request identifier and complete a waiting API operation
without blocking the GUI thread.

The path is:

1. FastAPI validates authentication, node names, and payload shape.
2. A camera operation broker creates a request identifier and queues the operation
   in `Grabber`.
3. `Grabber` reads or writes GenICam nodes in its existing camera command path.
4. The result signal resolves the matching broker operation.
5. The FastAPI worker returns the result or a bounded timeout.

The API does not import `rotpy`, access the live camera object, stop acquisition,
or route camera operations through the app's own HTTP API. The broker performs no
image processing and emits no Qt signals while holding its pending-operation lock.

Raw and corrected frames already have separate storage and monotonic counters in
the main window. The frame endpoint reads a copied frame under the existing frame
condition. Waiting for a newer frame occurs in the FastAPI worker, never in the
GUI thread.

## Permissions

Add two API-key permissions:

- `camera_read`: read camera settings and frames.
- `camera_write`: change allowlisted camera settings.

The API-key settings table exposes both permissions. New keys follow explicit
defaults: camera read is enabled and camera write is disabled.

For a legacy stored key whose permission object has no camera fields,
`camera_read` inherits its stored `stage_read` value and `camera_write` inherits
its stored `stage_write` value. An explicitly stored camera permission is always
respected. This migration preserves existing operator keys without silently
granting more access than their previous stage privileges.

## Settings Read API

`GET /api/v1/camera/settings` requires `camera_read`.

An optional repeated `name` query parameter limits the result. With no names, the
endpoint returns every allowlisted node. Names outside the allowlist produce HTTP
400. A supported but unavailable camera node remains visible as unavailable; the
server does not query aliases or return a guessed replacement.

Successful response:

```json
{
  "camera_ready": true,
  "nodes": [
    {
      "name": "ExposureTime",
      "display_name": "Exposure Time",
      "type": "float",
      "value": 1800.0,
      "readable": true,
      "writable": true,
      "minimum": 20.0,
      "maximum": 30000000.0,
      "increment": 1.0,
      "unit": "us",
      "enum_entries": []
    }
  ]
}
```

The node metadata is the camera's current metadata, not hardcoded ranges.

## Settings Write API

`PATCH /api/v1/camera/settings` requires `camera_write` and accepts an ordered
list:

```json
{
  "settings": [
    {"name": "ExposureAuto", "value": "Off"},
    {"name": "ExposureTime", "value": 1800.0},
    {"name": "GainAuto", "value": "Off"},
    {"name": "Gain", "value": 0.0}
  ]
}
```

Order is significant because automatic control must be disabled before its manual
value becomes writable. Duplicate names are rejected. Every node must be
allowlisted, available, readable, and writable before its update is accepted.

The worker snapshots all original values, applies settings in request order, and
reads back each resulting node. If validation or a write fails, it restores every
changed value in reverse order and returns failure with any rollback errors. The
API never reports partial success as success and never retries with different
nodes, values, or modes.

The response contains the read-back node payloads and
`frame_counter_at_completion`, sampled when the worker result reaches the broker.
The client uses that counter as the lower bound for subsequent frame requests, so
the first analysed frame cannot predate the completed settings change. A broker
timeout does not cause an automatic retry because the original camera command may
still complete.

## Frame API

`GET /api/v1/camera/frame` requires `camera_read` and returns a PNG.

Query parameters:

- `space`: `raw` or `corrected`; default `raw`.
- `after_counter`: optional non-negative frame counter.
- `timeout_ms`: bounded wait for a frame newer than `after_counter`.

Response headers include:

- `X-Camera-Frame-Counter`
- `X-Camera-Frame-Space`
- `X-Camera-Frame-Width`
- `X-Camera-Frame-Height`

Without `after_counter`, the endpoint returns the latest available frame. With it,
the endpoint returns only a newer frame, or HTTP 504 on timeout. This lets a client
apply settings, skip settling frames by counter, and prove that analysed pixels
were acquired after the settings operation. Raw space means the camera image
before flat-field and distortion correction; it never means a GUI screenshot.

## Auto-Exposure Diagnostic

Add a standalone script that uses only the public API. It does not import GUI
internals and does not move the stage. The operator places the aluminium-on-silicon
structure in view and runs the script while the GUI and camera are active.

The script snapshots all affected settings before changing anything. Before the
first exposure test, it disables automatic white balance, enumerates the available
`BalanceRatioSelector` entries, reads each corresponding `BalanceRatio`, and then
uses those same per-channel ratios for every tested mode. It restores the original
selector after each enumeration. Black level is recorded and left at its starting
value. Missing required colour controls fail the diagnostic explicitly rather than
silently changing the comparison. It then compares:

1. Native full automatic mode: `ExposureAuto=Continuous` and
   `GainAuto=Continuous`.
2. Native exposure-only mode: `ExposureAuto=Continuous`, `GainAuto=Off`, and
   `Gain=0 dB`.
3. Application controller: both automatic modes off, `Gain=0 dB`, and only
   `ExposureTime` adjusted.

The application controller uses raw RGB frames and camera-reported exposure
limits. Its configurable default target is the 99.5th percentile of per-pixel
maximum RGB at 235. Exposure is updated multiplicatively from target divided by
measured percentile, with a configurable bounded step ratio. Saturation causes an
immediate decrease. Convergence requires repeated fresh frames within configured
brightness and exposure tolerances and has a fixed iteration/time limit. Failure
is reported; it does not fall back to a native mode.

Each native mode is given a bounded convergence period based on fresh-frame
brightness, reported exposure, and gain stability. After every settings batch, the
script requests frames newer than the returned `frame_counter_at_completion` and
discards the configured settling count. It records the whole convergence trace
rather than relying on a single final frame.

## Metrics And Output

For each mode, save raw final frames and machine-readable measurements:

- final exposure and gain;
- convergence time and frame count;
- saturated and near-saturated pixel fractions;
- luminance percentiles and highlight headroom;
- temporal brightness drift after convergence;
- temporal noise in low-gradient, darker silicon pixels;
- silicon signal-to-noise estimate;
- camera setting and image trace for every iteration.

Thresholds affecting the comparison are named command-line options and are
written into report metadata. The report presents all modes and marks a preferred
mode only when it satisfies the configured clipping limit. Among compliant modes,
selection prioritizes silicon signal-to-noise, then temporal stability, then
convergence time. If no mode satisfies the clipping limit, the report says so
instead of naming a winner.

Artifacts are written under
`.scratch/camera-auto-exposure-<timestamp>/`, including `report.json`, a concise
Markdown summary, the traces, and the final PNGs. `.scratch` remains ignored.

By default the script restores the exact pre-run settings in a `finally` block.
An explicit `--apply-winner` option leaves the selected mode configured after a
successful comparison. If there is no unambiguous winner, settings are restored
even when that option is present. Restore failures are prominent in the terminal
and report.

## Error Handling

Camera-not-ready, unavailable node, read-only node, invalid enum, out-of-range
value, operation timeout, stale frame, PNG encoding failure, and rollback failure
are distinct errors. API responses use existing structured error conventions and
appropriate HTTP status codes.

The GUI remains responsive during all camera reads, writes, frame waits, and PNG
encoding. Camera operations have bounded waits, and periodic GUI camera-settings
refresh remains independent from API requests.

## Tests

Add automated coverage for:

- camera permission defaults, legacy migration, persistence, and authorization;
- API allowlist enforcement and camera read/write permission separation;
- ordered batch validation, read-back, rollback, rollback-error reporting, and no
  fallback behavior;
- broker request correlation, timeout cleanup, late completion, and concurrent
  read requests;
- raw/corrected frame selection, counter waiting, timeout, headers, and PNG data;
- diagnostic percentile controller convergence on synthetic frame sequences;
- metric calculation and winner/no-winner decisions;
- restoration on success, comparison failure, and interruption.

Hardware-independent tests use fake GenICam node results and synthetic images.
One manual hardware verification checks that acquisition continues without a
visible frame gap while settings are read and changed, then runs the diagnostic on
the current aluminium-on-silicon structure.

## Acceptance Criteria

- An authorized external client can read and atomically change only the listed
  operator camera settings.
- Camera acquisition does not stop or restart for API settings operations.
- A client can request a provably newer raw frame after a settings change.
- Invalid or unavailable settings fail explicitly with no alternate-node fallback.
- Existing API keys retain camera privileges derived from their previous stage
  privileges until explicitly edited.
- The diagnostic produces reproducible frames, traces, metrics, and restoration
  behavior for all three exposure modes.
- No custom auto-exposure loop is added to normal GUI acquisition in this branch.
