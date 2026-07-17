# Geometry-Mask Lens Calibration Design

## Goal

Calibrate objective-specific lens geometry from the nine-position stage scan without allowing illumination, exposure, white balance, or flat-field correction to influence feature matching. After a GUI calibration, show an immediately inspectable `Before` and `After` comparison built from the captured positions.

## Scope

- Lens-distortion calibration uses raw camera frames only.
- Flat-field calibration remains available for live images and microscope scans, but is not loaded or applied by lens calibration.
- The complete visible aluminum pattern participates in calibration, including the central grid and surrounding comb structures.
- The result page of the optical calibration wizard shows geometric alignment before and after the fitted correction.
- This change does not alter normal mosaic photometry or flat-field application outside lens calibration.

## Segmentation

Each raw frame is converted into a binary aluminum mask for geometric analysis. Segmentation operates on local color contrast rather than absolute RGB values:

1. Convert the frame to a perceptual luminance/chroma representation.
2. Estimate the slowly varying local background inside the usable image area.
3. Threshold the difference between each pixel and its local background with an adaptive threshold derived from that frame.
4. Apply small morphology operations to remove isolated sensor noise and reconnect narrow aluminum lines without joining nearby independent structures.
5. Reject border artifacts and components below a minimum geometric support.

The detector does not modify the source image and does not produce a corrected RGB frame. Different exposure or illumination may change the threshold value, but should not change the resulting aluminum contour.

## Geometric Features

Features are extracted from actual mask components and centerlines. The detector records component membership, position, orientation, length, and line-center samples. It must not independently detect all horizontal and vertical coordinates and form their Cartesian product, because that creates nonexistent intersections between the grid and combs.

The central grid provides strong two-dimensional anchors. The combs remain in the feature set because they provide useful distortion constraints near the image edges.

## Cross-Frame Matching

Known stage offsets and the current click-to-move matrix predict where every feature should appear in another frame. Matching uses these predictions as follows:

- Candidate pairs must have compatible component geometry and orientation.
- Candidate distance is measured after applying the predicted stage displacement.
- Assignment is one-to-one within each frame pair; one detected feature cannot satisfy multiple tracks.
- Pairs outside a named pixel-distance gate are rejected rather than accepted as a fallback.
- Tracks must contain observations from at least two camera positions.

The resulting tracks feed the existing robust affine plus low-order distortion fit. Residual validation remains mandatory before saving the model.

## Calibration Flow

1. Run one-shot exposure and lock camera settings.
2. Capture the existing nine-position path as raw frames.
3. Return the stage to the exact starting position.
4. Segment all frames and extract geometric features.
5. Build unique cross-frame tracks from stage-coordinate predictions.
6. Fit affine pixel geometry, optical center, and distortion coefficients.
7. Validate mean and maximum residuals.
8. Build previews from the same nine masks.
9. Save only the serializable correction payload and display the previews in the wizard.

No raw-versus-flat candidate selection remains in this flow.

## Before And After Preview

Both panels use the same mask data, crop, canvas size, and display scale.

- `Before` places the nine masks in a common reference coordinate system using the pre-calibration click-to-move geometry and no new distortion correction.
- `After` places them using the fitted affine geometry and distortion correction.
- Overlaid mask occupancy is rendered in grayscale. Correctly aligned contours reinforce to white; misalignment remains visible as doubled gray contours. No colored seam guides are drawn.
- The panels show the complete useful aluminum pattern, not only the central grid.
- Mean and maximum residuals are shown with the preview.

The worker returns a result object containing the serializable correction payload and two `QImage` previews. The GUI creates `QPixmap` objects only on the GUI thread. Preview images are transient and are not written into objective settings.

The wizard result page expands to a side-by-side layout when preview images are available. A failed calibration remains on the capture page and does not show a misleading `After` image.

## Error Handling

- Insufficient aluminum pixels: fail with a segmentation-specific message.
- Too few repeated feature tracks: fail with a matching-specific message.
- Ambiguous assignments outside the matching gate: reject those observations.
- Residual over the configured limits: do not save the correction.
- Preview generation failure after a valid fit: fail the run before saving the model and report the preview error.
- Stage and camera restoration semantics remain unchanged.

## Verification

Automated tests cover:

- stable masks under synthetic brightness gradients, channel scaling, and exposure changes;
- separation of nearby grid and comb components;
- absence of synthetic Cartesian intersections;
- one-to-one matching and rejection of ambiguous pairs;
- contribution of comb features near image edges;
- lens calibration making no flat-field store or correction calls;
- residual improvement on synthetic distorted nine-frame data;
- identical crop and dimensions for `Before` and `After` previews;
- wizard result-page preview delivery and stale-run rejection;
- unchanged stage return and camera restore behavior.

The implementation is also replayed against the existing X20 nine-frame debug capture before a new hardware run. A final hardware run must return to the starting stage coordinate, pass residual validation, save the objective model, and display the two preview panels in the GUI.
