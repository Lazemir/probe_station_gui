# NPZ Task 1 Report

## Scope

Implemented only Task 1 from `npz-task-1-brief.md` on base
`e7a2c6f1e0b8a0b7cf37fed80fed9cc064aa7098`:

- pure NPZ import and deterministic normalization;
- persistent A/Z interpolation snapshot fields;
- interpolation-aware settings parsing and legacy-model compatibility;
- bundled default persistence;
- importer and settings regression tests.

No runtime coordinate mapping, stage behavior, GUI controls, hardware code, or
route-control behavior was changed or exercised.

## Implementation

### Pure NPZ importer

Created `probe_station_gui/settings/axis_calibration_npz.py` with the requested
public interface:

- `LINEAR_INTERPOLATION_MODEL = "linear_interpolation"`;
- `AxisCalibrationImportError(ValueError)`;
- frozen `ImportedAxisCalibration`;
- `load_axis_calibration_npz(path, *, axis, final_direction)`.

The importer:

1. resolves and records the selected file path;
2. loads with `np.load(..., allow_pickle=False)`;
3. requires finite, one-dimensional, shape-compatible `gcode` and `indicator`
   arrays;
4. selects the requested branch when `direction` is present;
5. sorts by G-code coordinate;
6. combines duplicate G-code samples with the median indicator reading;
7. converts A display values to `-indicator` and leaves Z as `indicator`;
8. collapses adjacent equal display plateaus using their median G-code
   coordinate;
9. requires at least two usable points and a strictly increasing display curve;
10. converts malformed input into finished-product
    `AxisCalibrationImportError` messages.

### Settings and persistence

Extended both A and Z calibration records with:

- `calibration_file`;
- `interpolation_gcode_mm`;
- `interpolation_display_mm`;
- `interpolation_direction`.

Serialization, settings clones, and parser fallback clones copy both point
lists. Parsing now preserves a valid `linear_interpolation` model, accepts a
snapshot only when both arrays are finite, equal-length, contain at least two
points, and are strictly increasing, and disables/clears an invalid snapshot.
Legacy cosine/quintic models continue through their existing parametric
validation paths. Legacy `.png` source metadata is normalized to an empty
string.

Updated both calibration sections in
`probe_station_gui/default_settings.json` with empty file/snapshot values,
`null` interpolation direction, and empty legacy source metadata.

## TDD evidence

Development used vertical RED -> GREEN cycles through public interfaces.
Representative RED results included:

- initial importer collection failure:
  `ModuleNotFoundError: probe_station_gui.settings.axis_calibration_npz`;
- directionless A import failing on a missing `direction` key;
- missing arrays leaking `KeyError`;
- shape/non-finite/branch/unique-point/monotonic validation not raising;
- duplicate samples remaining unaggregated;
- plateau data being rejected before plateau normalization;
- malformed numeric data leaking `ValueError`;
- settings constructors rejecting the new snapshot fields;
- parser forcing `linear_interpolation` back to the legacy model;
- PNG source metadata surviving parsing;
- parser fallback clones aliasing the default point lists.

Each failure was followed by the minimum implementation needed to make the
new behavior pass before adding the next behavior. Refactoring and formatting
were done only with the tests green.

## Final verification

All commands used the shared project virtual environment.

```text
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui/settings/axis_calibration_npz.py probe_station_gui/settings/axis_calibration_config.py tests/settings/test_axis_calibration_npz.py tests/settings/test_axis_calibration_config.py
All checks passed!

C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff format --check probe_station_gui/settings/axis_calibration_npz.py probe_station_gui/settings/axis_calibration_config.py tests/settings/test_axis_calibration_npz.py tests/settings/test_axis_calibration_config.py
4 files already formatted

C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_axis_calibration_npz.py tests/settings/test_axis_calibration_config.py tests/settings/test_default_file.py -q
36 passed in 0.33s

C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings -q
87 passed in 0.41s
```

`git diff --check` also completed without whitespace errors. The only emitted
Git messages were the repository's existing Windows LF-to-CRLF warnings for
tracked files.

## Nuances

- A directionless file records `branch_direction=None`; a file with a
  direction array records the selected direction.
- Equal display plateaus are represented by the median G-code coordinate,
  giving the later inverse interpolation a deterministic central value.
- Invalid interpolation snapshots retain their normalized file metadata but
  are disabled and have empty point lists; no external file is read during
  settings parsing.
- Runtime interpolation consumption and direction-compatibility GUI behavior
  remain intentionally deferred to Tasks 2 and 3.

## Review follow-up

Two Important findings were addressed in a separate follow-up:

- Added a real truncated-ZIP regression. Before the fix,
  `load_axis_calibration_npz` leaked `zipfile.BadZipFile: File is not a zip
  file`; the importer now translates `BadZipFile` into the same
  operator-facing `AxisCalibrationImportError` used for other malformed
  calibration files.
- Removed both `zip(..., strict=True)` calls while preserving the already
  validated equal-length iteration logic, keeping the importer compatible
  with the project's Python 3.9 minimum.

Fresh review-fix verification:

```text
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/settings/test_axis_calibration_npz.py tests/settings/test_axis_calibration_config.py tests/settings/test_default_file.py -q
37 passed in 0.31s

C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui/settings/axis_calibration_npz.py tests/settings/test_axis_calibration_npz.py
All checks passed!

C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff format --check probe_station_gui/settings/axis_calibration_npz.py tests/settings/test_axis_calibration_npz.py
2 files already formatted
```
