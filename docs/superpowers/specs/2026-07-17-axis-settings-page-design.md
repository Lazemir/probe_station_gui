# Axis Settings Page Design

## Goal

Replace the separate `Axis Calibration` and `Precision approach` settings tabs
with one axis-oriented `Axes` tab. Keep the global `Coordinates` tab unchanged.
The user selects one stage axis and sees every setting that belongs to that axis
in one place.

## Scope

- Keep `Coordinates` as its own top-level settings tab.
- Remove the top-level `Axis Calibration` and `Precision approach` tabs.
- Add one top-level `Axes` tab.
- Preserve the existing settings models, JSON schema, controller behavior, units,
  defaults, and apply/save semantics.
- Do not add calibration models for axes that do not already support them.

## Layout

The `Axes` tab uses a two-column layout:

- A fixed selector on the left lists `X`, `Y`, `Z`, `A`, `B`, and `C`.
- A stacked page on the right shows settings for the selected axis.

Each axis page contains these groups in order:

1. `Precision approach`
   - Enabled checkbox.
   - Backlash value with the existing axis-specific unit.
   - Final approach direction.
   - The existing concise path preview.
2. `Coordinate calibration`
   - For A and Z: the existing enable checkbox, curve source, RMS error, and
     maximum error.
   - For X, Y, B, and C: a read-only message, `No calibration curve for this
     axis.`

The left selector remains visible while the right page changes. Every axis owns
its own controls, so switching axes cannot discard unapplied edits.

## State and Data Flow

The combined widget receives clones of `AxisACalibrationSettings`,
`AxisZCalibrationSettings`, and `PrecisionApproachSettings`.

- One precision editor page is created for every supported stage axis.
- A and Z pages additionally bind the existing calibration settings.
- `to_settings()` collects all six precision profiles and both calibration
  enabled states into the existing `Settings` object.
- Calibration curve coefficients, source, and fit statistics remain read-only
  and are preserved verbatim.

No settings migration is required because persisted keys remain unchanged.

## Compatibility

`SettingsDialog(initial_tab=...)` accepts `Axes` as the canonical tab name.
Legacy requests for `Axis Calibration` or `Precision approach` select the new
`Axes` tab. They do not create hidden legacy tabs.

The default selected axis is Z because its precision approach is enabled in the
shipped defaults and it is the most commonly adjusted profile. Tests may select
any axis directly through the selector.

## Code Boundaries

- Put the combined axis-oriented widget in a focused settings module rather
  than growing `settings_dialog.py` further.
- Keep reusable precision row/page construction private to that module.
- Remove the old fixed two-axis calibration widget from `settings_dialog.py`.
- Reuse the existing settings value objects; no stage-controller changes are
  part of this feature.

## Validation and Errors

Existing spin-box ranges and `PrecisionApproachProfile` validation remain the
source of truth. Unsupported calibration axes show the explanatory read-only
state and expose no inactive fake controls.

## Tests

Qt tests will verify:

- The settings tabs contain `Coordinates` and `Axes`, but not `Axis Calibration`
  or `Precision approach`.
- The selector exposes all six axes and defaults to Z.
- Switching axes preserves unapplied precision edits.
- Units and path previews remain correct for linear and rotary axes.
- A and Z expose their calibration state and metadata.
- X, Y, B, and C show the unsupported-calibration message.
- Applying the dialog writes all precision profiles and both calibration enabled
  states without changing curve metadata.
- Legacy `initial_tab` names open `Axes`.

Existing parsing, persistence, and controller tests remain unchanged because the
underlying configuration schema and behavior do not change.

## Coordinate Legend Cleanup

Replace the single rich-text legend under the stage coordinate fields with a
real horizontal widget layout. The legend has two visibly separated semantic
groups on one baseline:

- `Field state:` followed by equal square swatches for `Homed`, `Unhomed`,
  `Limit`, and `Edited`.
- `Accuracy:` followed by equal horizontal stripe swatches for `Exact` and
  `Approximate`.

Use normal UI font sizing, fixed inter-item spacing, vertically centered labels
and swatches, and a vertical separator between groups. The legend remains
left-aligned with the stage-position panel. Existing colors and the backlash
tooltip remain unchanged.

Qt tests will verify the group titles and item order, equal swatch dimensions
within each group, a shared vertical alignment, and the existing tooltip text.
