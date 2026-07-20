# One-Shot Camera Auto-Exposure Design

## Goal

Run the verified application-controlled exposure adjustment automatically before microscope scans, apply the calibrated flat-field profile to the live microscope view, expose the same operator action through the camera API, and stop reporting isolated Spinnaker `NEW_BUFFER_DATA` timeouts as camera failures.

## User Behavior

- Design scans started from the GUI run one-shot auto-exposure by default.
- Area scans started through the API run one-shot auto-exposure by default.
- API callers can set `auto_exposure=false` to retain the current manual exposure.
- The camera API exposes `POST /api/v1/camera/auto-exposure` for adjustment without starting a scan.
- One-shot adjustment runs at the current stage position before any scan movement.
- On success, exposure remains manual and active after the scan. Gain is fixed at `0 dB`; the current white-balance ratios are preserved and automatic white balance is disabled.
- On failure or non-convergence, the original camera state is restored and the scan does not move the stage.

## Architecture

Extract the custom exposure loop into a transport-independent controller. It consumes ordered camera-setting read/write callbacks and fresh raw RGB frames. The controller owns convergence, clipping response, state restoration on failure, and the result payload.

`Main` owns one controller instance backed directly by `CameraApiBroker` and the existing raw-frame condition. GUI scan workers call it directly. The FastAPI route calls a dedicated callback into the same controller; in-process GUI code never calls the app's localhost API.

The existing diagnostic imports the shared controller math so the standalone comparison and production one-shot path cannot diverge.

## Live Flat-Field

The live camera path loads the active objective profile from
`<config_dir>/calibrations/flat-field/<objective>/current.json`. The stored
reference frame is converted into a compiled RGB gain map and cached until the
objective or profile files change. The processing order is raw frame,
flat-field correction, lens-distortion correction, then GUI/stage delivery.

Raw frames remain available immediately to auto-exposure and the raw camera API.
Flat-field and lens correction run in a dedicated latest-frame processor: when
processing falls behind, an unprocessed frame is replaced by the newest frame
instead of building latency. The corrected API, stage controller, notifications,
and microscope view receive the processed frame. Missing profiles are a normal
pass-through state. An invalid profile is logged once per changed profile
signature and does not stop live acquisition.

## Scan Integration

Auto-exposure configuration is explicit scan metadata with `enabled=true` by default. The scan worker runs adjustment before `begin_external_task`, needle movement, or tile movement. Successful results are written into scan correction metadata, including target, final exposure, gain, convergence count, and brightness trace. Existing camera locking still fixes all automatic camera modes for every tile.

Only one auto-exposure operation may run at a time. A standalone API request is rejected while a microscope scan is active, and a scan aborts cleanly if the camera operation is already busy.

## API

`POST /api/v1/camera/auto-exposure` requires `camera_write`. It accepts an optional configuration object containing only declared one-shot tuning fields. Missing fields use production defaults. The Python client exposes `camera.auto_exposure(...)` and uses a timeout long enough for bounded convergence.

The response contains `accepted`, `converged`, `final_exposure_us`, `final_gain_db`, `final_frame_counter`, `brightness_trace`, and serialized configuration. Invalid tuning values return HTTP 400; busy camera state or non-convergence returns HTTP 409.

## Spinnaker Timeout Handling

Spinnaker exception code `-1011` with the `NEW_BUFFER_DATA` wait message is treated as a transient acquisition timeout. An isolated timeout emits no error signal, Telegram alert, or frame-gap warning. A named consecutive-timeout threshold escalates once through the existing camera error path. The counter resets on the next valid frame, after which future failures can escalate again.

All other acquisition exceptions retain current error behavior.

## Verification

- Unit tests cover convergence, clipping reduction, rollback, serialization, and busy behavior.
- API/server/client tests cover authorization, payload validation, and response mapping.
- Scan tests prove adjustment occurs before stage-task and motion calls, can be disabled, records metadata, and aborts motion on failure.
- Worker tests prove isolated `-1011` is silent, repeated `-1011` escalates once, recovery resets state, and unrelated exceptions remain errors.
- Live-correction tests prove the stored profile is loaded and cached, flat-field precedes distortion, raw frames remain untouched, stale pending frames are dropped, and shutdown terminates the worker.
- The full test suite runs before hardware verification.
- Hardware verification invokes the endpoint on the current structure, confirms the applied manual settings, and confirms no `-1011` error entry or alert is generated by an isolated timeout.
