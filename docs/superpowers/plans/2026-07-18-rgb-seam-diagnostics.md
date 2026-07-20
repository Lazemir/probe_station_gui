# RGB Seam Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the grayscale lens-calibration comparison with two RGB seam-diagnostic mosaics that compare all eight neighbors with the central capture and show per-mode residual statistics plus a compact pictogram legend.

**Architecture:** Keep acquisition, segmentation, fitting, and persistence unchanged. Extend `geometry_mask` with a strict 3x3 role classifier and an RGB compositor that overlays center-relative disagreement on the existing occupancy mosaic; pass already-fitted baseline/current metrics through `Main`; render labels, metrics, and three custom legend widgets on the wizard result page.

**Tech Stack:** Python 3.12, NumPy, OpenCV, PySide6 `QImage`/`QPainter`, pytest.

## Global Constraints

- The central frame is the sole comparison reference.
- Red is left/right disagreement, green is top/bottom disagreement, and blue is four-corner disagreement.
- Channel intensity is the valid-footprint mean of `abs(mask_neighbor - mask_center)` multiplied by 255.
- The complete nine-frame occupancy mosaic remains as grayscale spatial context.
- Panels are named `Without calibration` and `With calibration`.
- Each panel shows its own `mean` and `max` residual in pixels.
- The shared legend contains only three colored 3x3 pictograms; visible text swatches and legend labels are forbidden.
- Invalid 3x3 layouts fail explicitly; no grayscale or partial-layout fallback is allowed.
- Preview work remains in the calibration worker; Qt widget updates remain in the GUI thread.
- Raw frames and preview images remain transient and are not added to objective settings.

---

## File Structure

- Modify `probe_station_gui/camera/geometry_mask.py`: classify the 3x3 capture roles, transform masks and valid footprints, compute RGB disagreement, and return RGB previews.
- Modify `tests/camera/test_geometry_mask.py`: replace arbitrary-frame grayscale expectations with strict nine-frame RGB behavior and error cases.
- Modify `main.py`: require both baseline and fitted residual metrics and pass them to the wizard.
- Modify `tests/app/test_main_lens_distortion.py`: cover metric validation and delivery without changing hardware orchestration.
- Modify `probe_station_gui/dialogs/optical_calibration_wizard.py`: render semantic headings, per-panel metrics, and the pictogram legend.
- Modify `tests/ui/test_optical_calibration_wizard.py`: cover labels, metric formatting, icons, tooltips, resizing, clearing, and stale results.
- Modify `tests/app/test_main_optical_calibration.py`: keep the wizard fake compatible with the widened result callback.

---

### Task 1: Center-Relative RGB Preview Compositor

**Files:**
- Modify: `tests/camera/test_geometry_mask.py:569-951`
- Modify: `probe_station_gui/camera/geometry_mask.py:41-47,220-477`

**Interfaces:**
- Consumes: `Sequence[GridCalibrationFrame]`, binary masks, the initial persisted GUI pixel-to-stage matrix, and a validated `stage_geometry` fit payload.
- Produces: `build_geometry_alignment_previews(...) -> tuple[QImage, QImage]`, with both images in `QImage.Format_RGB888` and identical bounds.
- Internal role API: `_preview_grid_roles(stage_offsets, initial_image_matrix) -> tuple[int, tuple[str | None, ...]]`, where roles are `horizontal`, `vertical`, `diagonal`, and `None` for the center.

- [ ] **Step 1: Replace grayscale test helpers with RGB and strict-grid helpers**

Add an RGB conversion helper and a deterministic 3x3 fixture whose persisted GUI matrix maps back to identity image coordinates:

```python
def _preview_rgb_array(image: QImage) -> np.ndarray:
    assert image.format() == QImage.Format_RGB888
    rows = np.frombuffer(
        image.constBits(), dtype=np.uint8, count=image.sizeInBytes()
    ).reshape((image.height(), image.bytesPerLine()))
    return rows[:, : image.width() * 3].reshape((image.height(), image.width(), 3)).copy()


def _nine_preview_frames(step_px: int = 4) -> tuple[GridCalibrationFrame, ...]:
    offsets = [(0.0, 0.0)]
    offsets.extend(
        (float(x), float(y))
        for y in (-step_px, 0, step_px)
        for x in (-step_px, 0, step_px)
        if (x, y) != (0, 0)
    )
    return tuple(
        GridCalibrationFrame(frame=None, stage_offset_mm=offset)
        for offset in offsets
    )
```

Use persisted matrix `((1.0, 0.0), (0.0, -1.0))`, so the compositor's image-coordinate matrix is identity.

- [ ] **Step 2: Write failing channel and proportional-intensity tests**

Build aligned masks with one central landmark and move only selected neighbor landmarks by one pixel. Assert:

```python
before_rgb, _after_rgb = build_geometry_alignment_previews(...)
values = _preview_rgb_array(before_rgb)
red_chroma = values[:, :, 0].astype(int) - values[:, :, 1].astype(int)
blue_chroma = values[:, :, 2].astype(int) - values[:, :, 1].astype(int)

assert red_chroma.max() == 128  # one of two horizontal neighbors disagrees
assert blue_chroma.max() <= 0
```

Add separate cases proving vertical-only disagreement changes green chroma and corner-only disagreement changes blue chroma. Add a case where both horizontal neighbors disagree and assert full-strength red chroma `255`.

- [ ] **Step 3: Write failing role, footprint, and validation tests**

Add tests that:

```python
shuffled = tuple(frames[index] for index in (8, 2, 5, 0, 7, 1, 4, 6, 3))
shuffled_masks = tuple(masks[index] for index in (8, 2, 5, 0, 7, 1, 4, 6, 3))
assert np.array_equal(render(frames, masks), render(shuffled, shuffled_masks))
```

Use an affine persisted matrix with nonzero off-diagonal terms and assert two horizontal, two vertical, four diagonal, and one center role. Remove one frame and assert `GeometryAlignmentPreviewError` contains `3x3`. Put a disagreement outside the shared center/neighbor footprint and assert all channel chroma remains zero there.

- [ ] **Step 4: Run the new tests and verify RED**

Run:

```powershell
& 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/camera/test_geometry_mask.py -q
```

Expected: the new tests fail because previews are `Format_Grayscale8`, arbitrary frame counts are accepted, and no RGB role compositor exists.

- [ ] **Step 5: Add strict 3x3 role classification**

In `geometry_mask.py`, convert stage offsets to predicted image displacement with the inverse initial image matrix. Derive each axis's spacing from the median nonzero absolute displacement and classify a component as zero only within a named fraction of that spacing. Return the unique center index and role tuple, then require exact counts:

```python
_PREVIEW_GRID_ZERO_TOLERANCE_FRACTION = 0.20
_PREVIEW_ROLE_COUNTS = {"horizontal": 2, "vertical": 2, "diagonal": 4}


def _preview_grid_roles(stage_offsets, initial_image_matrix):
    stage_to_pixel = np.linalg.inv(initial_image_matrix)
    displacements = tuple(stage_to_pixel @ np.asarray(offset, dtype=float) for offset in stage_offsets)
    # Classify each component against spacing-derived zero tolerances.
    # Require one center and exact 2/2/4 neighbor counts; otherwise raise
    # GeometryAlignmentPreviewError("Preview captures must form a complete 3x3 grid.")
```

Keep the tolerance named and local to preview-grid validation; do not add objective-specific constants.

- [ ] **Step 6: Transform mask footprints beside masks**

Create a valid footprint of ones for every source mask. For the fitted mode, remap the footprint with the same cached distortion maps and `INTER_NEAREST` as its mask. Place masks and footprints with the same affine translations on the shared canvas. Reuse one fitted correction object so map construction remains one-shot.

- [ ] **Step 7: Compose occupancy and three disagreement channels**

Warp the center mask/footprint and every neighbor mask/footprint to the common canvas. For each role, accumulate binary differences and valid comparison counts:

```python
valid = center_footprint & neighbor_footprint
differences[channel] += ((center_mask != neighbor_mask) & valid)
comparisons[channel] += valid
channels = np.divide(
    differences,
    comparisons,
    out=np.zeros_like(differences),
    where=comparisons > 0,
)
```

Convert channels to `[0, 255]`, retain the existing occupancy decay for the grayscale base, and composite:

```python
alpha = channels.max(axis=2, keepdims=True)
rgb = gray[:, :, None] * (1.0 - alpha) + channels * 255.0
```

Return a copied `QImage.Format_RGB888` image. Use the same canvas origin and size for uncalibrated and calibrated modes.

- [ ] **Step 8: Adapt existing preview tests and verify GREEN**

Convert the comb/distortion regression to a full 3x3 frame set. Assert comb occupancy still exists in all RGB channels where disagreement is zero and assert total chroma decreases in the fitted image:

```python
def chroma_energy(rgb: np.ndarray) -> int:
    return int((rgb.max(axis=2) - rgb.min(axis=2)).sum())

assert chroma_energy(after_values) < chroma_energy(before_values)
```

Keep the existing one-shot fitted-map assertion, common crop assertion, signed-axis assertion, and input error tests, updated to valid nine-frame fixtures.

Run:

```powershell
& 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/camera/test_geometry_mask.py -q
```

Expected: all geometry-mask tests pass.

- [ ] **Step 9: Commit the compositor**

```powershell
git add probe_station_gui/camera/geometry_mask.py tests/camera/test_geometry_mask.py
git commit -m "feat: render RGB seam diagnostics"
```

---

### Task 2: Baseline And Fitted Metric Delivery

**Files:**
- Modify: `tests/app/test_main_lens_distortion.py:102-142,213-381,1039-1098`
- Modify: `main.py:5295-5353,5508-5564`

**Interfaces:**
- Consumes: validated payload fields `baseline_residual_mean_px`, `baseline_residual_max_px`, `residual_mean_px`, and `residual_max_px`.
- Produces: wizard keyword arguments `without_calibration_metrics=(mean_px, max_px)` and `with_calibration_metrics=(mean_px, max_px)`.

- [ ] **Step 1: Add baseline fields to test payloads and a failing validation test**

Extend `_stage_geometry_payload`:

```python
"baseline_residual_mean_px": 0.8,
"baseline_residual_max_px": 1.4,
```

Add parameterized cases where either baseline field is missing, boolean, `nan`, or infinite. Assert `_validate_lens_distortion_fit_payload` raises a message naming the field and that no objective settings are saved.

- [ ] **Step 2: Add a failing wizard-delivery assertion**

Update the successful completion expectation to require:

```python
{
    "run_id": 27,
    "before_preview": before_preview,
    "after_preview": after_preview,
    "without_calibration_metrics": (0.8, 1.4),
    "with_calibration_metrics": (0.2, 0.4),
}
```

Use RGB preview images in this test so integration rejects accidental grayscale-only assumptions.

- [ ] **Step 3: Run the app tests and verify RED**

Run:

```powershell
& 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/app/test_main_lens_distortion.py -q
```

Expected: baseline validation and wizard metric delivery tests fail.

- [ ] **Step 4: Validate all four residual fields**

Change the residual loop in `_validate_lens_distortion_fit_payload` to include:

```python
for field in (
    "baseline_residual_mean_px",
    "baseline_residual_max_px",
    "residual_mean_px",
    "residual_max_px",
):
```

Keep pass/fail thresholds applied only to fitted residuals. Require finite numeric, non-boolean values for all four fields.

- [ ] **Step 5: Pass both metric pairs to the wizard**

After payload validation and before calling the wizard, extract floats from the payload and pass:

```python
without_calibration_metrics=(
    float(payload["baseline_residual_mean_px"]),
    float(payload["baseline_residual_max_px"]),
),
with_calibration_metrics=(
    float(payload["residual_mean_px"]),
    float(payload["residual_max_px"]),
),
```

Do not duplicate these values in `_LensDistortionCalibrationOutput`; the payload remains the single worker result source.

- [ ] **Step 6: Run tests and verify GREEN**

```powershell
& 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/app/test_main_lens_distortion.py -q
```

Expected: all main lens-distortion tests pass.

- [ ] **Step 7: Commit metric delivery**

```powershell
git add main.py tests/app/test_main_lens_distortion.py
git commit -m "feat: report calibration comparison metrics"
```

---

### Task 3: Semantic Panel Headings And Pictogram Legend

**Files:**
- Modify: `tests/ui/test_optical_calibration_wizard.py:11-18,55-72,298-429`
- Modify: `tests/app/test_main_optical_calibration.py:37-72`
- Modify: `probe_station_gui/dialogs/optical_calibration_wizard.py:5-18,141-242,380-402`

**Interfaces:**
- Extends: `OpticalCalibrationWizard.set_lens_distortion_result(..., without_calibration_metrics: tuple[float, float] | None = None, with_calibration_metrics: tuple[float, float] | None = None)`.
- Extends: `_ResultPage.set_lens_result(...)` with the same metric pairs.
- Produces: headings `Without calibration` and `With calibration`; three `_SeamLegendIcon` widgets with English tooltips.

- [ ] **Step 1: Write failing heading and metric tests**

Pass `(2.56, 5.00)` and `(2.42, 6.41)` from `_complete_lens_capture`, then assert:

```python
assert result._without_heading.text() == "Without calibration"
assert result._with_heading.text() == "With calibration"
assert result._without_metrics.text() == "2.56 px mean · 5.00 px max"
assert result._with_metrics.text() == "2.42 px mean · 6.41 px max"
```

Add a validation test that supplying previews without both finite metric pairs raises `ValueError` and does not expose the comparison container.

- [ ] **Step 2: Write failing legend tests**

Assert exactly three widgets, no visible legend text labels, and exact tooltips:

```python
assert [icon.toolTip() for icon in result._seam_legend_icons] == [
    "Horizontal seams: center compared with left and right.",
    "Vertical seams: center compared with top and bottom.",
    "Corner seams: center compared with four corners.",
]
```

Render each icon with `icon.grab().toImage()` and assert the horizontal icon contains red in middle-left/middle-right, the vertical icon green in top-middle/bottom-middle, and the corner icon blue in all four corners. Assert every icon has a dark center cell.

- [ ] **Step 3: Run wizard tests and verify RED**

```powershell
& 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/ui/test_optical_calibration_wizard.py tests/app/test_main_optical_calibration.py -q
```

Expected: semantic headings, metric labels, widened callback, and legend tests fail.

- [ ] **Step 4: Implement `_SeamLegendIcon`**

Import `QColor`, `QPainter`, and `QPaintEvent`. Implement a fixed-size widget that paints a stable 3x3 grid with 2 px gaps. Store highlighted `(row, column)` cells and a `QColor`; paint the center dark, inactive cells neutral gray, and active cells with the seam color. Set `setFixedSize(28, 28)` and the exact tooltip strings above.

Instantiate:

```python
_SeamLegendIcon(((1, 0), (1, 2)), QColor(255, 63, 72), horizontal_tooltip)
_SeamLegendIcon(((0, 1), (2, 1)), QColor(45, 219, 104), vertical_tooltip)
_SeamLegendIcon(((0, 0), (0, 2), (2, 0), (2, 2)), QColor(67, 132, 255), corner_tooltip)
```

Center the three icons in one `QHBoxLayout` below both preview labels. Do not add color squares or visible legend text.

- [ ] **Step 5: Replace headings and format panel metrics**

Replace `Before`/`After` labels with persistent attributes `_without_heading` and `_with_heading`. Add `_without_metrics` and `_with_metrics` beside their corresponding heading. Format with two decimals and a middle dot:

```python
def _format_residual_metrics(metrics: tuple[float, float]) -> str:
    mean_px, max_px = metrics
    if not all(math.isfinite(value) and value >= 0.0 for value in metrics):
        raise ValueError("Lens calibration preview metrics must be finite and non-negative.")
    return f"{mean_px:.2f} px mean · {max_px:.2f} px max"
```

`clear_lens_previews` must clear images and metric text and hide the entire comparison container. Keep both image labels in equal grid columns with stable minimum size.

- [ ] **Step 6: Widen wizard callbacks and fakes**

Thread both metric keyword pairs through `set_lens_distortion_result` to `_ResultPage.set_lens_result`. Update `_FakeWizard.set_lens_distortion_result` in `tests/app/test_main_optical_calibration.py` to accept `**kwargs` without routing through the local API.

- [ ] **Step 7: Run UI and app tests and verify GREEN**

```powershell
& 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/ui/test_optical_calibration_wizard.py tests/app/test_main_optical_calibration.py tests/app/test_main_lens_distortion.py -q
```

Expected: all selected tests pass with no Qt warnings.

- [ ] **Step 8: Commit the result-page UI**

```powershell
git add probe_station_gui/dialogs/optical_calibration_wizard.py tests/ui/test_optical_calibration_wizard.py tests/app/test_main_optical_calibration.py
git commit -m "feat: show RGB seam comparison legend"
```

---

### Task 4: Regression And Real-Frame Replay

**Files:**
- Verify only: all files changed in Tasks 1-3
- Transient output: `.scratch/lens-flat-compare-20260717/rgb-without-calibration.png`
- Transient output: `.scratch/lens-flat-compare-20260717/rgb-with-calibration.png`

**Interfaces:**
- Consumes: `.scratch/lens-flat-compare-20260717/manifest.json`, `raw-01.png` through `raw-09.png`, and `fit-production.json`.
- Produces: two ignored PNG artifacts for visual inspection; no source or settings artifacts.

- [ ] **Step 1: Run the focused calibration suite**

```powershell
& 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests/camera/test_geometry_mask.py tests/camera/test_distortion.py tests/ui/test_optical_calibration_wizard.py tests/app/test_main_lens_distortion.py tests/app/test_main_optical_calibration.py -q
```

Expected: all focused tests pass.

- [ ] **Step 2: Replay the nine saved raw frames**

Run a one-shot stdin script from PowerShell; do not add a debug script to the repository:

```powershell
@'
import json
from pathlib import Path
from PySide6.QtGui import QImage
from probe_station_gui.camera.distortion import GridCalibrationFrame
from probe_station_gui.camera.geometry_mask import (
    build_geometry_alignment_previews,
    segment_metal_geometry,
)

root = Path('.scratch/lens-flat-compare-20260717')
manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
payload = json.loads((root / 'fit-production.json').read_text(encoding='utf-8'))
frames = []
masks = []
for capture in manifest['captures']:
    image = QImage(str(root / capture['file']))
    frames.append(GridCalibrationFrame(image, tuple(capture['stage_offset_mm'])))
    masks.append(segment_metal_geometry(image))
without_image, with_image = build_geometry_alignment_previews(
    tuple(frames), tuple(masks), manifest['pixels_to_mm'], payload
)
assert without_image.save(str(root / 'rgb-without-calibration.png'))
assert with_image.save(str(root / 'rgb-with-calibration.png'))
print(without_image.width(), without_image.height(), without_image.format())
'@ | & 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -
```

Expected: both images save, dimensions match, and the printed format is `Format_RGB888`.

- [ ] **Step 3: Inspect replay output**

Open both PNG files with the image viewer. Confirm:

- the entire central grid and side combs remain visible in grayscale;
- red, green, and blue appear only where center-relative seams disagree;
- calibrated chroma is visibly reduced where alignment improves;
- no colored frame-boundary rectangles or invalid-footprint artifacts appear.

- [ ] **Step 4: Run the full test suite**

```powershell
& 'C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe' -m pytest tests -q
```

Expected: the complete suite passes.

- [ ] **Step 5: Check formatting and worktree scope**

```powershell
git diff --check
git status --short
git diff --stat HEAD~3..HEAD
```

Expected: no whitespace errors; only the planned source, tests, design, and plan files are tracked. `.scratch` and `.superpowers` remain ignored.

- [ ] **Step 6: Commit any verification-only test adjustment**

Only if replay exposed a missing regression assertion:

```powershell
git add tests/camera/test_geometry_mask.py tests/ui/test_optical_calibration_wizard.py
git commit -m "test: cover RGB seam replay behavior"
```

Do not commit replay PNG files or temporary scripts.
