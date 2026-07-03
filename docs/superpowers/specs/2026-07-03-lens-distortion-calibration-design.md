# Lens Distortion Calibration Design

## Goal

Add per-objective lens distortion calibration for the microscope camera. The calibrated objective must show a geometrically straightened live image, and downstream image consumers must receive the straightened frame: click-to-move, hover/ruler measurement, autofocus, phase-correlation click calibration, route photos, Telegram photos, and microscope scan tiles.

## Calibration Target

The calibration sample is a bright-line grid with 4x4 cells, 5x5 grid intersections, and 50 um physical spacing between adjacent grid lines. A single 50x frame may contain only part of the 5x5 intersections, so calibration must not require all 25 intersections in one image.

## User Flow

Add a `Lens Distortion Calibration` action under the existing `Calibration` menu. The dialog shows the active objective, correction status, last residual metrics, and actions:

- `Calibrate`: run an automatic multi-frame capture around the current stage position.
- `Reset`: clear the active objective's distortion correction.
- `Close`: dismiss the dialog without changing runtime state.

Calibration uses the current objective profile. The user places the grid under the objective and starts calibration. The app verifies that the stage is idle, serial is connected, X/Y coordinates are available, and motion safety accepts movement. Needles must remain raised through the existing safety gate.

## Automatic Capture

The calibration runner captures the current frame, then moves the stage through a small XY scan pattern around the starting position. Movement must use existing stage-controller motion paths and return to the starting XY position at the end or on failure when the controller position can be read.

The first implementation uses a conservative pattern sized for a 50 um grid:

- center
- +/- one grid step on X
- +/- one grid step on Y
- four diagonal one-step offsets

The step is 0.05 mm in stage X/Y coordinates. It does not depend on existing click-to-move calibration. The scan pattern should be represented as named configuration in the calibration module, not scattered constants.

## Grid Detection

Use OpenCV on grayscale camera frames. The detector targets the real bright-line sample:

- normalize uneven illumination;
- threshold or enhance bright grid lines;
- find dominant vertical and horizontal line centers;
- compute visible line intersections;
- reject UI overlays by using raw camera frames, not screenshots;
- report how many vertical/horizontal lines and intersections were usable per frame.

The fitter combines detections from multiple stage offsets into a shared calibration dataset. It should tolerate partial views and only require enough well-spread intersections to fit a stable image correction. If coverage is insufficient, it must fail without persisting a correction.

## Correction Model

Store a per-objective distortion correction payload in `ObjectiveCalibrationSettings`:

- `distortion_correction_configured`;
- source frame size;
- grid dimensions and 50 um spacing;
- capture offsets;
- detected image points and corresponding straightened grid coordinates;
- residual metrics;
- model version.

At runtime, construct OpenCV remap arrays lazily for the active objective and frame size. The first implementation should use a smooth image-to-image correction from the fitted grid dataset. If the frame size changes, the model is considered unavailable until recalibrated for that size.

## Frame Pipeline

Apply correction at the main-window camera-frame boundary. `_on_camera_frame` receives the raw `QImage`, applies the active objective correction if configured, and stores/emits the corrected frame to:

- `_latest_camera_frame`;
- `_latest_camera_frame_for_notifications`;
- `MicroscopeView.set_frame`;
- `StageController.on_frame_ready`.

If no correction is configured, the current behavior is unchanged. When a correction is saved or reset for an objective, clear that objective's click-to-move XY calibration because the pixel geometry changed.

## Settings And UI

Settings parsing must be backward-compatible with existing files. Invalid or incomplete correction payloads are ignored and do not make the objective unusable.

The existing objective settings tab should show distortion status near click calibration status. The dedicated calibration dialog owns calibration and reset actions; the settings tab remains a status/editor surface.

## Error Handling

Calibration must not block GUI startup. Detection and fitting run in a background task. GUI updates happen through Qt signals.

Failures should keep the previous correction, if any. Status copy should be short and product-like, for example:

- `Grid not found. Center the calibration sample and try again.`
- `Grid coverage is too small.`
- `Lens correction saved for X50.`

## Tests

Add regression tests for:

- objective settings parse/clone/to_dict for correction payloads;
- synthetic partial bright-grid multi-frame detection/fitting;
- correction application leaves image size stable and straightens a synthetic warped grid;
- main camera-frame pipeline sends corrected frames to latest-frame storage, view, and stage controller;
- reset clears distortion correction and invalidates click-to-move calibration for that objective.

Hardware-dependent movement is not exercised in automated tests. The automatic capture runner should be factored so detection/fitting can be tested with synthetic frames and declared stage offsets.
