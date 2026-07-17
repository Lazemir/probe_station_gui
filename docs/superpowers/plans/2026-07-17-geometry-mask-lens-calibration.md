# Geometry-Mask Lens Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fit lens geometry from illumination-invariant binary masks of all visible aluminum structures and show nine-frame geometric alignment before and after calibration in the optical wizard.

**Architecture:** Add a focused `geometry_mask` camera module that segments raw frames, extracts real corner/end-point features, builds unique stage-predicted tracks, and renders alignment previews. Reuse the existing robust stage-geometry optimizer in `distortion.py`; keep flat-field correction out of this data path. Deliver preview `QImage` objects beside the serializable correction payload and construct `QPixmap` objects only on the GUI thread.

**Tech Stack:** Python 3.11, PySide6 `QImage`/widgets, NumPy, OpenCV, SciPy `linear_sum_assignment`, pytest.

## Global Constraints

- Lens-distortion calibration uses raw camera frames only and never loads or applies a flat-field profile.
- The central grid and surrounding comb structures both contribute real detected features.
- Do not create intersections by taking the Cartesian product of independently detected horizontal and vertical lines.
- Matching is one-to-one and stage-predicted; observations outside a named gate are rejected.
- `Before` and `After` use identical masks, crop, canvas size, and scale.
- Preview failure fails the run before saving a model.
- Stage return, camera setting restoration, and calibration run-identity checks remain unchanged.
- Hardware commands remain outside automated tests; use the existing saved X20 replay before the final live run.

---

## File Structure

- Create `probe_station_gui/camera/geometry_mask.py`: segmentation, real feature extraction, one-to-one tracking, conversion to `StageFeatureObservation`, and mask preview composition.
- Modify `main.py`: raw-only worker integration, mask fit orchestration, transient result bundle, and completion delivery.
- Modify `probe_station_gui/dialogs/optical_calibration_wizard.py`: side-by-side preview result UI.
- Create `tests/camera/test_geometry_mask.py`: algorithm tests for segmentation, features, tracks, fit inputs, and previews.
- Modify `tests/app/test_main_lens_distortion.py`: raw-only worker and result-bundle tests; remove raw-versus-flat selection expectations.
- Modify `tests/ui/test_optical_calibration_wizard.py`: preview delivery, scaling, reset, and stale-run tests.

### Task 1: Adaptive Aluminum Mask And Real Features

**Files:**
- Create: `probe_station_gui/camera/geometry_mask.py`
- Create: `tests/camera/test_geometry_mask.py`

**Interfaces:**
- Produces: `GeometryMaskFrame(mask: np.ndarray, features: tuple[GeometryMaskFeature, ...], frame_size: tuple[int, int])`.
- Produces: `segment_aluminum_geometry(frame: object) -> GeometryMaskFrame`.
- Produces: `GeometryMaskFeature(point_px, descriptor, response, component_id)` with actual mask corner/end-point coordinates.

- [ ] **Step 1: Write failing segmentation tests**

Create synthetic RGB images containing a connected 4x4 grid and disconnected combs over a dark background. Generate variants with multiplicative brightness gradients, channel scaling, and exposure changes. Assert mask intersection-over-union above `0.90`, grid and comb occupancy present, and background occupancy below `0.02`.

```python
def test_segmentation_is_stable_under_illumination_and_color_changes():
    reference, expected = synthetic_aluminum_pattern()
    variants = illumination_variants(reference)
    masks = [segment_aluminum_geometry(frame).mask for frame in variants]
    assert all(mask_iou(mask, expected) > 0.90 for mask in masks)
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/camera/test_geometry_mask.py -q
```

Expected: collection/import failure because `geometry_mask.py` does not exist.

- [ ] **Step 3: Implement adaptive segmentation**

Implement RGB extraction for `QImage` and ndarray input, conversion to Lab, a slowly varying Gaussian background estimate, positive local color-contrast score, adaptive Otsu thresholding, and small morphology cleanup. Derive kernel sizes from image dimensions and detected line support; keep named minimum/maximum bounds. Filter tiny connected components and border-only artifacts.

```python
@dataclass(frozen=True)
class GeometryMaskFrame:
    mask: object
    features: tuple[GeometryMaskFeature, ...]
    frame_size: tuple[int, int]

def segment_aluminum_geometry(frame: object) -> GeometryMaskFrame:
    rgb = _rgb_array(frame)
    mask = _adaptive_aluminum_mask(rgb)
    features = _actual_mask_features(mask)
    if len(features) < MIN_GEOMETRY_FEATURES:
        raise ValueError("Insufficient aluminum geometry was detected.")
    return GeometryMaskFrame(mask, features, (rgb.shape[1], rgb.shape[0]))
```

- [ ] **Step 4: Write and pass real-feature tests**

Assert extracted points lie on actual corners/endpoints in the expected mask neighborhood. Include a grid/comb layout where a vertical grid line and a horizontal comb line have crossing projections but no physical intersection; assert no feature appears at that synthetic Cartesian crossing.

- [ ] **Step 5: Commit Task 1**

```powershell
git add probe_station_gui/camera/geometry_mask.py tests/camera/test_geometry_mask.py
git commit -m "feat: segment aluminum calibration geometry"
```

### Task 2: Unique Stage-Predicted Feature Tracking

**Files:**
- Modify: `probe_station_gui/camera/geometry_mask.py`
- Modify: `tests/camera/test_geometry_mask.py`

**Interfaces:**
- Consumes: `GeometryMaskFrame` and `GridCalibrationFrame.stage_offset_mm`.
- Produces: `build_geometry_feature_observations(frames, masks, frame_size, image_pixels_to_mm, match_gate_px=12.0) -> tuple[StageFeatureObservation, ...]`.
- Uses: `scipy.optimize.linear_sum_assignment` for one-to-one assignments.

- [ ] **Step 1: Write failing one-to-one matching tests**

Build three synthetic frames with known stage shifts, repeated comb spacing, one missing feature, and one distractor within the broad old `30 px` radius. Assert each feature track has at most one observation per frame, distractors remain unmatched, and grid plus edge-comb tracks survive.

```python
observations = build_geometry_feature_observations(
    frames,
    masks,
    frame_size=(640, 400),
    image_pixels_to_mm=matrix,
    match_gate_px=12.0,
)
assert_unique_frame_per_track(observations)
assert any(obs.pixel_xy[0] < 80 for obs in observations)
assert any(obs.pixel_xy[0] > 560 for obs in observations)
```

- [ ] **Step 2: Run matching tests and verify RED**

Run the individual new tests with the shared virtual environment. Expected: missing `build_geometry_feature_observations`.

- [ ] **Step 3: Implement world prediction and assignment**

Convert each feature to approximate world coordinates using the known stage offset and image-coordinate pixel matrix. Build a gated cost matrix from world distance plus a bounded binary-patch descriptor distance. Use `linear_sum_assignment`, accept only costs inside the named gate, update track centroids, and create tracks for unmatched features. Emit observations only for tracks seen in at least two frames.

- [ ] **Step 4: Add fit integration test**

Generate nine masked frames from a known affine and radial/tangential distortion model, build observations, call `fit_stage_geometry_from_observations`, and assert residual improvement plus recovery tolerances for matrix and coefficients.

- [ ] **Step 5: Commit Task 2**

```powershell
git add probe_station_gui/camera/geometry_mask.py tests/camera/test_geometry_mask.py
git commit -m "feat: match calibration mask features"
```

### Task 3: Before And After Alignment Previews

**Files:**
- Modify: `probe_station_gui/camera/geometry_mask.py`
- Modify: `tests/camera/test_geometry_mask.py`

**Interfaces:**
- Produces: `build_geometry_alignment_previews(raw_frames, masks, initial_pixels_to_mm, fitted_payload) -> tuple[QImage, QImage]`.
- Consumes persisted-matrix convention and converts it to image Y-down convention internally.

- [ ] **Step 1: Write failing preview tests**

Use three shifted binary masks with a known radial displacement. Assert both previews are non-null, have identical dimensions, include edge combs, and that the fitted preview has a lower mask-spread metric than the initial preview.

- [ ] **Step 2: Run preview tests and verify RED**

Expected: missing preview function.

- [ ] **Step 3: Implement common-canvas composition**

For each frame, warp its mask with either identity geometry or the fitted distortion, translate it to the center-stage reference using the corresponding pixel matrix, and accumulate occupancy on a shared float canvas. Compute one union crop with a fixed margin and apply it to both canvases. Render occupancy as grayscale `QImage`: reinforced overlap is white and duplicated contours remain gray.

- [ ] **Step 4: Add failure tests**

Assert frame-size mismatch, empty union, malformed fitted matrix, and correction-map errors raise explicit preview exceptions rather than returning blank images.

- [ ] **Step 5: Commit Task 3**

```powershell
git add probe_station_gui/camera/geometry_mask.py tests/camera/test_geometry_mask.py
git commit -m "feat: render lens alignment comparison"
```

### Task 4: Raw-Only Calibration Worker Integration

**Files:**
- Modify: `main.py`
- Modify: `tests/app/test_main_lens_distortion.py`

**Interfaces:**
- Consumes: segmentation, observation, fit, and preview functions from `geometry_mask.py`.
- Produces: private immutable `_LensDistortionCalibrationOutput(payload: dict[str, object], before_preview: QImage, after_preview: QImage)`.
- Changes completion payload from a bare settings dict to the result bundle internally; settings persistence still receives only `output.payload`.

- [ ] **Step 1: Replace candidate-selection tests with failing raw-mask tests**

Assert the worker never calls `FlatFieldCalibrationStore.load` or `apply_flat_field_correction`, segments all nine raw frames, sends mask observations into the fitter, and returns both preview images. Preserve assertions for camera lock, exact stage return, and run context.

- [ ] **Step 2: Run focused app tests and verify RED**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/app/test_main_lens_distortion.py -q
```

- [ ] **Step 3: Implement raw-only orchestration**

Remove `_fit_lens_distortion_candidates`, `_select_lens_distortion_fit_candidate`, candidate metadata, and all flat-field loading/application from the lens worker. Segment captured raw frames, build observations, fit via `fit_stage_geometry_from_observations`, convert the fitted matrix back to persisted GUI convention, validate residuals, build previews, and return `_LensDistortionCalibrationOutput`.

- [ ] **Step 4: Update completion persistence tests**

Assert successful completion saves only `output.payload`, preserves click-to-move recalibration behavior, and forwards preview images only to the current wizard run. Assert stale completion cannot replace the active preview.

- [ ] **Step 5: Commit Task 4**

```powershell
git add main.py tests/app/test_main_lens_distortion.py
git commit -m "feat: calibrate lens geometry from raw masks"
```

### Task 5: Wizard Comparison UI

**Files:**
- Modify: `probe_station_gui/dialogs/optical_calibration_wizard.py`
- Modify: `tests/ui/test_optical_calibration_wizard.py`

**Interfaces:**
- Changes: `set_lens_distortion_result(success, message, *, run_id=None, before_preview=None, after_preview=None)`.
- Produces: result-page side-by-side `Before` and `After` image labels scaled from retained `QImage` copies.

- [ ] **Step 1: Write failing UI tests**

Create distinct test `QImage` objects, complete a current lens run, advance Qt events, and assert the result page contains two non-null pixmaps with equal displayed bounds. Assert `prepare()` clears previous previews and a stale run ID cannot overwrite current images.

- [ ] **Step 2: Run UI tests and verify RED**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_optical_calibration_wizard.py -q
```

- [ ] **Step 3: Implement responsive result preview**

Replace the form-only result layout with a compact status section followed by a two-column preview grid. Retain copied `QImage` sources, create `QPixmap` only in `set_lens_result`/resize handling on the GUI thread, use aspect-ratio-preserving smooth scaling, and increase the wizard's default size while retaining responsive minimums.

- [ ] **Step 4: Run app and UI test groups**

Run both optical wizard and lens calibration test modules. Expected: all pass without Qt warnings.

- [ ] **Step 5: Commit Task 5**

```powershell
git add probe_station_gui/dialogs/optical_calibration_wizard.py tests/ui/test_optical_calibration_wizard.py
git commit -m "feat: show lens calibration comparison"
```

### Task 6: Replay, Full Verification, And Live Run

**Files:**
- Modify only if a verified defect is found in Tasks 1-5.

**Interfaces:**
- Uses saved replay frames in `.scratch/lens-flat-compare-20260717` without committing them.

- [ ] **Step 1: Replay the X20 capture**

Load the nine saved raw frames and manifest, execute production segmentation/matching/fit/preview functions, and record feature count, observation count, before residual, fitted mean/max residual, and preview paths under `.scratch`.

- [ ] **Step 2: Inspect preview images**

Use `view_image` on both preview PNGs. Verify the central grid and combs are visible, crops match, `After` has less doubled contour than `Before`, and neither image is blank.

- [ ] **Step 3: Run the complete suite**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
```

Expected: all tests and subtests pass.

- [ ] **Step 4: Restart the GUI and run calibration from the wizard**

Restart the existing `main.py` process, wait for API/stage readiness, and launch lens calibration from the optical calibration wizard. Verify the status log reports success, the stage returns to the starting coordinate, the saved objective payload contains no flat-field candidate metadata, and the wizard displays both preview panels.

- [ ] **Step 5: Final commit if verification required fixes**

Stage only verified source/test changes, commit them with a focused message, and leave `.scratch` ignored.
