# Remove Microscope Scan Approach Design

## Goal

Remove the microscope-scan-specific tile approach because absolute coordinate
moves already use the shared precision-approach mechanism configured for the
stage axes.

## Behavior

- The Microscope Scan dialog no longer exposes an `Approach` field.
- A scan issues one absolute coordinate move to each tile center. Backlash and
  directional loading remain the responsibility of `StageController`.
- The area-scan API rejects the obsolete `tile_approach_mm` and `approach_mm`
  fields with a validation response instead of silently ignoring them.
- Scan manifests and other acquisition behavior are unchanged.

## Compatibility

Callers must remove the obsolete API fields. Explicit rejection prevents an
old client from appearing to configure motion behavior that no longer exists.

## Verification

- Dialog tests assert that no approach control exists.
- API tests assert that both obsolete fields are rejected before a scan starts.
- Scan runner tests assert one coordinate move per tile.
- Existing microscope scan and stage precision-motion tests continue to pass.
