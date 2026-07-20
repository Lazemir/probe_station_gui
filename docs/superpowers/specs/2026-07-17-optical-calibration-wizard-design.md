# Optical Calibration Wizard Design

## Goal

Provide one GUI workflow for calibrating the active objective's flat field and
lens geometry in sequence, while allowing either calibration to run by itself.
The workflow must use the verified shifted-frame flat-field method and the
center/edge/corner geometry method.

## Operator Workflow

The Calibration menu opens an `Optical Calibration` wizard. Its first page
shows the active objective and offers three modes:

- `Full Calibration`
- `Flat Field Only`
- `Lens Distortion Only`

For flat-field calibration, the wizard asks the operator to center a clean,
feature-free area of the current substrate. Starting the page runs one-shot
software auto-exposure, fixes the camera settings for the acquisition, raises
the needles, captures a shifted 3x3 raw-frame grid with 80% overlap, and returns
the stage to the page's starting position. The median reference and profile are
saved in a timestamped objective directory. `current.json` is replaced only
after every profile artifact has been written successfully.

For lens calibration, the wizard asks the operator to center the test
structure. Starting the page runs one-shot software auto-exposure, raises the
needles, and captures nine positions: center, four edges, and four corners. The
stage returns to the page's starting position before the page reports success.
The fit uses frames corrected by the active flat-field profile, then saves the
distortion model and calibrated pixel-to-stage matrix through the existing
objective settings path.

Full calibration pauses between the two acquisitions so the operator can move
from the blank substrate area to the test structure. Each acquisition has its
own start position and restores that position independently.

## Architecture

`OpticalCalibrationWizard` owns presentation and navigation only. It emits
requests for the flat-field and lens stages and receives progress/completion
updates from `Main` through Qt signals. Closing is disabled while a hardware
stage is active.

A camera calibration profile module owns flat-field paths, manifest loading,
reference loading, and atomic installation. Both the live correction pipeline
and calibration runners use the same loader, preventing the GUI and fitter from
interpreting a profile differently.

`Main` retains hardware orchestration because it already owns raw camera frame
conditions, auto-exposure, stage external-task reservation, motion feedrates,
and objective settings. The flat-field and lens runners execute in background
threads and communicate with the wizard only through queued Qt signals.

The existing lens calibration dialog remains available for status and reset.
Its Calibrate action opens the new wizard directly in lens-only mode.

## Failure Behavior

There is no uncorrected fallback for lens fitting. Lens calibration requires a
valid flat-field profile for the active objective and fails before movement when
the profile is missing or invalid. It also requires click-to-move calibration.

A failed acquisition restores the stage position and releases the external
task. A failed flat-field run leaves the previous `current.json` untouched. In
full mode, a successful flat-field calibration remains installed if the later
lens stage fails; the result page reports both stage outcomes explicitly.

## Verification

- Profile-store tests cover timestamped artifacts, atomic current-manifest
  replacement, objective validation, and reference loading.
- Runner tests cover the 3x3 flat-field path, nine lens positions, raw-frame
  capture, flat-field-before-fit ordering, and stage restoration.
- Wizard tests cover all three modes, page routing, running-state close guards,
  progress, success, and failure.
- Menu tests cover the new action and the existing lens dialog handoff.
- The focused calibration/UI suites and the full test suite run before a live
  GUI launch.
