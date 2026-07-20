# Axis Calibration Current Position Card Design

## Goal

Make the live axis position in the calibration preview readable without hover
and remove visual elements that make it look like an editing tool.

## Current-position card

A compact two-column card appears immediately above the calibration plot. It
has no redundant `Current position` sentence. The columns are:

| Controller `<axis>` | Physical `<axis>` |
| --- | --- |
| raw controller coordinate with unit | calibrated physical coordinate with unit |

The headers use subdued text and the numeric values are visually stronger. The
card follows the surrounding Settings palette and uses the same quiet border
language as the coordinate-calibration group.

When calibration is enabled and the current raw coordinate is inside the loaded
curve, both values are shown. If calibration is disabled or no calibrated value
can be derived, the raw value remains available and the physical cell shows an
em dash. If no current raw coordinate is available, both cells show an em dash.
The card keeps a stable height as its values change.

## Plot marker

The current calibrated point is a filled red circle with an 8-pixel diameter
and a restrained red outline. It is visible only while calibration is enabled
and both current coordinates are valid and inside the loaded curve.

The orange dashed current-position vertical line is removed. The independent
pointer-hover guides and interpolated hover label remain unchanged.

## Updates and verification

The card and marker update from the existing current-position snapshot flow;
they do not poll hardware or trigger plot auto-ranging. Replacing or clearing a
curve, changing the selected axis, toggling calibration, losing a coordinate,
and moving outside the calibration range must update both representations
without rebuilding the plot.

Widget tests cover the two-column labels and formatted values, unavailable and
disabled states, the small red circular marker, absence of the old position
line, and preservation of hover behavior and the current view range.
