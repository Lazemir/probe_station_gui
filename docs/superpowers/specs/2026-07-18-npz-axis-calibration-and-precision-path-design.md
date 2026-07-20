# NPZ Axis Calibration and Precision Path Design

## Scope

This change corrects two problems in the axis-oriented settings and motion
work introduced on `codex/alignment-backlash-approach`:

1. A and Z coordinate calibration must be replaceable from the GUI by
   selecting an external `.npz` measurement file.
2. A multi-axis precision move must not hold an axis at its current coordinate
   merely because that axis does not need backlash preparation.

The `Path` preview in the **Precision approach** group is unrelated to the
calibration file and will be removed.

## Calibration file UI

The **Coordinate calibration** group for A and Z contains:

- an `Enabled` checkbox;
- a read-only `File` field containing the selected external NPZ path;
- a `Browse` button that replaces the selected calibration;
- a `Reset` button;
- a compact status containing the accepted point count and G-code range.

`Curve source` and `Fit error` are removed. They describe the old parametric
fit and are misleading for an interpolated curve. In particular, a PNG plot
must never be presented or accepted as a calibration file.

Reset clears the path and imported samples and disables calibration. The
current legacy Z PNG source is cleared. Selecting a valid file enables the new
interpolation model; cancelling the file dialog leaves the existing model
unchanged.

## NPZ contract and import

An accepted file contains finite, one-dimensional `gcode` and `indicator`
arrays of equal length with at least two usable points. An optional `direction`
array may identify multiple captured travel branches. When it is present, the
branch matching the axis's configured precision final direction is imported.
If no matching branch exists, import fails without replacing the current
calibration.

Import performs the following deterministic normalization:

1. select the applicable direction branch;
2. sort by G-code coordinate;
3. merge duplicate G-code coordinates using their median indicator reading;
4. convert the measured indicator to the application's display convention
   (`-indicator` for A and `indicator` for Z);
5. require a monotonic display curve, collapsing identical display plateaus
   for the inverse lookup.

Non-finite, mismatched, non-monotonic, or incomplete data is rejected with a
finished-product error message. There is no polynomial/cosine fit, smoothing,
or extrapolation.

The imported, normalized point snapshot is stored in settings together with
the selected external path. Runtime startup uses the stored points and does
not synchronously parse the external file on the GUI thread. Replacing the
file through `Browse` imports a new snapshot in a worker and updates the UI
only after validation succeeds. This preserves the selected path while
keeping startup lightweight and preventing a missing or later-edited external
file from silently changing the coordinate system.

## Runtime interpolation

The runtime calibration model is `linear_interpolation`. Forward conversion
linearly interpolates G-code to display coordinate. Inverse conversion uses
the same normalized points with the axes exchanged.

Status display may clamp a raw value at an interpolation endpoint so a status
frame cannot crash the GUI. A requested calibrated target outside the imported
domain is rejected by the existing round-trip validation before any movement
command is sent.

The existing parametric A and Z readers remain available for backward
compatibility with old settings, but new GUI selections always create the
linear interpolation model. Legacy `source` metadata is not shown.

## Precision movement path

The current planner builds the preparation segment as follows:

- an axis that needs preparation receives `target - direction * backlash`;
- an axis that does not need preparation incorrectly remains at `current`.

For mixed-sign XY moves this creates a visible hook: one axis moves alone in
the preparation segment and the other makes its entire move during the final
segment.

The corrected rule is universal for every axis in the requested target:

- an axis that needs preparation receives its backlash-offset target;
- every other axis receives its final target in the preparation segment.

The final segment still contains the complete final target. Axes already at
their final target therefore do not move in that segment. If no axis needs
preparation, only the ordinary final segment is sent.

For a move from `(0, 0)` to `(0.024, -0.026)` with `+0.005 mm` profiles and
preparation required only for Y, the segments are:

1. preparation `(X=0.024, Y=-0.031)`;
2. final `(X=0.024, Y=-0.026)`.

Only Y moves during the final backlash take-up. Existing validation, motion
safety, cancellation between segments, and confidence transitions remain
unchanged. Debug logging records the calculated preparation and final targets.

## Error handling and persistence

- A failed NPZ import keeps the previously applied calibration intact.
- Apply/Save persists the imported snapshot, path, enabled state, and branch
  direction.
- Changing precision final direction away from the imported branch disables
  that calibration until a matching curve is imported.
- Missing configured points disable the interpolation model safely.
- Resetting calibration never moves hardware and does not alter precision
  approach values.

## Tests

Tests will cover:

- valid A and Z NPZ import and forward/inverse linear interpolation;
- optional direction-branch selection;
- duplicate G-code normalization and monotonic validation;
- invalid-file rollback and Reset behavior;
- settings serialization and startup reconstruction without reading NPZ;
- removal of the precision `Path`, `Curve source`, and `Fit error` rows;
- the real settings dialog file-selection flow with worker completion;
- mixed-sign XY planning where the non-prepared axis reaches its final target
  in the preparation segment;
- both-axes preparation, single-axis moves, validation, cancellation, and
  route/click-to-move regression coverage.

Hardware-dependent code will not be exercised by automated tests.
