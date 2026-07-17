# RGB Seam Diagnostics Design

## Goal

Make the lens-calibration result visually measurable. The optical calibration wizard compares every non-central capture with the central capture and renders horizontal, vertical, and diagonal seam disagreement as independent RGB channels.

This design replaces the grayscale `Before` and `After` preview described in `2026-07-17-geometry-mask-lens-calibration-design.md`. It does not change acquisition, segmentation, matching, fitting, model validation, or persistence.

## Result Layout

The result page keeps two equal-size panels with a shared crop and scale:

- `Without calibration` uses the initial click-to-move matrix and uncorrected masks.
- `With calibration` uses the fitted pixel-to-stage matrix and fitted distortion correction.

Each heading includes that mode's residual statistics in pixels:

- `Without calibration` shows `baseline_residual_mean_px` and `baseline_residual_max_px`.
- `With calibration` shows `residual_mean_px` and `residual_max_px`.

The preview keeps the complete nine-frame grayscale mask mosaic as spatial context. RGB disagreement layers are composited over that mosaic.

## Reference And Seam Classes

Stage offsets are converted back into predicted image-plane displacements with the initial pixel-to-stage matrix. The median nonzero spacing on each image axis defines that axis's zero-position tolerance. The central capture must be the unique frame inside both axis tolerances.

The other eight captures are classified from those predicted image-plane displacements. Classification uses the initial pixel-to-stage matrix rather than raw stage-axis components so that affine cross-axis terms do not misclassify a capture:

- red: the left and right captures;
- green: the top and bottom captures;
- blue: the four diagonal captures.

The layout is valid only when it contains one center, two horizontal neighbors, two vertical neighbors, and four diagonal neighbors. An invalid layout raises a preview error; there is no grayscale fallback.

## Disagreement Channels

For each mode, every neighbor mask and its valid image footprint are transformed onto the shared preview canvas. A neighbor contributes only where its transformed footprint overlaps the transformed footprint of the central capture.

For a neighbor `i`, the binary disagreement is:

`difference_i = abs(mask_i - mask_center)`

For each seam class, the channel intensity is the mean disagreement over the valid neighbors at that canvas pixel:

`channel = mean(difference_i) * 255`

This normalization makes intensity proportional to the fraction of neighbors in that class that disagree with the center. For example, disagreement with one of two horizontal neighbors produces half-strength red; disagreement with both produces full-strength red. Pixels without a valid center-to-neighbor comparison receive zero channel intensity.

The existing grayscale occupancy image remains visible where there is no disagreement. Let `alpha = max(red, green, blue) / 255`. The final pixel is `gray * (1 - alpha) + (red, green, blue)`, clipped to the channel range. This preserves pure channel colors at full disagreement and naturally produces additive colors when multiple seam classes disagree at the same pixel.

## Legend

A shared legend is centered below the two panels. It contains only three compact 3x3 pictograms:

- the middle-left and middle-right cells are red;
- the top-middle and bottom-middle cells are green;
- the four corner cells are blue.

The center cell is dark in every pictogram. There are no separate color swatches or visible text labels. Each pictogram has an English tooltip naming its seam class and compared positions.

## Data Flow

`geometry_mask.build_geometry_alignment_previews` continues to receive the nine raw-frame records, nine binary masks, initial matrix, and fitted payload. It produces two RGB `QImage` values. The calibration worker reads both baseline and fitted statistics from the already validated fit payload and passes them beside the images to the result page.

Preview computation stays in the calibration worker. The GUI thread receives immutable images and text metrics, converts images to pixmaps, and renders the three lightweight legend widgets. Preview images and diagnostic layers remain transient and are not persisted in objective settings.

## Failure Behavior

- A missing or ambiguous central frame fails preview generation.
- Missing or duplicate seam positions fail preview generation.
- Non-finite transforms, empty masks, mismatched frame sizes, or an empty shared canvas retain their current explicit failures.
- Missing or non-finite baseline/fitted statistics fails result validation instead of showing an unlabeled or misleading panel.
- Preview failure prevents saving the newly fitted calibration, matching the current no-fallback behavior.

## Verification

Automated tests cover:

- central-frame selection independent of frame order;
- robust horizontal, vertical, and diagonal classification with affine cross-axis terms;
- red-only, green-only, and blue-only synthetic disagreement;
- proportional channel intensity when only some neighbors in a class disagree;
- exclusion of pixels outside the center-to-neighbor footprint overlap;
- identical dimensions and crop for both result images;
- `Without calibration` and `With calibration` headings with their own mean/max statistics;
- exactly three legend pictograms, their channel colors, spatial patterns, and English tooltips;
- explicit rejection of an incomplete or ambiguous 3x3 layout;
- unchanged calibration persistence and stage/camera restoration behavior.

The focused camera and wizard tests run before the complete test suite. The final check replays the saved X50 nine-frame calibration data and opens the wizard result page to verify that the calibrated panel contains less RGB disagreement where the fit improves alignment.
