# Stitched Z Calibration and Hover Preview Design

## Goal

Replace the section-3-only test calibration archive with one strict calibration
curve assembled from all three measured forward Z sections. Make the embedded
calibration preview match the light desktop UI and report interpolated
controller/physical coordinates anywhere along the curve under the pointer.

## Stitched Z archive

The preparation script receives the three machine-local source archives
explicitly. It never changes them and the application does not learn their raw
schemas.

The output curve uses the established stitching geometry:

- section 1 contributes controller values from `0.020` up to but excluding
  `12.000` mm, without a physical offset;
- section 2 contributes `12.000` through `20.214` mm inclusive, with
  `+8.661368914604154` mm added to its indicator values;
- the direct pass of section 3 contributes values above `20.214` through
  `23.400` mm, with `+13.56547962940159` mm added to its indicator values.

After concatenation, a deterministic longest strictly increasing subsequence
removes only samples that prevent the final physical curve from being strictly
increasing. Controller and physical values retained in the output are otherwise
unchanged. The saved archive contains only scalar Unicode `axis`, and finite,
one-dimensional, equal-length `controller` and `physical` arrays. Both arrays
must be strictly increasing and contain at least two samples. Existing output
files are not overwritten by the converter.

The generated stitched NPZ is a machine-local ignored artifact. Tests use
small synthetic archives and verify section selection, offsets, seams,
monotonicity, schema, and overwrite protection.

## Preview appearance

The preview uses an explicit light palette rather than PyQtGraph's process-wide
default:

- white plotting background;
- dark axis text, ticks, and border;
- subtle light-gray grid;
- the existing blue calibration curve and samples;
- the existing contrasting current-position diamond and dashed position line.

The palette is applied only to this widget and does not modify global
PyQtGraph configuration.

## Pointer coordinates

The widget retains a private copy of the current curve arrays. Pointer motion
inside the plot is mapped to controller X in view coordinates. When X is within
the calibration domain, physical Y is calculated by linear interpolation of
the displayed curve.

A pair of thin dashed guide lines follows the interpolated point and a compact
label near the pointer displays:

`X: <controller> <unit> · Y: <physical> <unit>`

The label is clamped to the visible plot area so it remains readable near an
edge. The guides and label hide when the pointer leaves the plot, the curve is
cleared, or X is outside the curve domain. This hover state is independent of
the machine-position marker: disabling calibration still hides the live
machine marker while leaving file preview and pointer inspection available.

Pointer updates do not auto-range or rebuild curve items. They are rate-limited
through PyQtGraph's signal proxy so dense archives remain responsive.

## Error handling and verification

The stitched converter rejects missing fields, dimensional or length mismatch,
non-finite samples, non-increasing controller sections, unusable seams, and an
existing destination. It must not leave a partially valid result after input
validation fails.

Preview tests cover the light palette, interpolation between samples, unit
formatting, hide-on-leave/out-of-range behavior, independence from the live
position marker, and preservation of the user's view range. Converter tests
cover all three sections and the exact stitching boundaries and offsets. The
complete hardware-free test suite remains the final regression gate.
