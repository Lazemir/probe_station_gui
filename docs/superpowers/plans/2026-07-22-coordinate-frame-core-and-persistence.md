# Coordinate Frame Core and Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the hardware-independent physical-pose, B-attached frame, readiness, registry, settings, and versioned persistence foundation used by every later coordinate-system feature.

**Architecture:** Add a focused `probe_station_gui.coordinates` package whose types contain no Qt or serial dependencies. Keep user-authored custom frames and pivot settings in `Settings`, while measured design-frame records use a separate atomic `coordinate-frames.json` store. Existing WCO settings remain readable but are not used by the new frame registry.

**Tech Stack:** Python dataclasses/enums, NumPy, existing `StageAxisCalibrationMapper`, JSON, PySide6 signals for the background store facade, pytest.

## Global Constraints

- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python or pytest command.
- Raw controller MPos must pass through `StageAxisCalibrationMapper.controller_to_physical()` before entering this package; inverse targets must pass through `physical_to_controller()` afterward.
- Never use G54-G59/WCO values in software-frame math.
- X/Y/Z/A use millimetres; B/C use degrees; enabled calibration curves never clamp or extrapolate.
- Frame IDs are stable UUID strings; names are display-only and may change.
- Readiness dependency is `X/Y/B -> Z -> A`; every Z change invalidates A without exception.
- `VISIBLE_STAGE_AXES` omits C, while `STAGE_AXES`, settings parsing, and serialization retain C.
- Filesystem parsing/writing must stay outside the GUI thread; never emit a Qt signal while holding a non-reentrant lock.
- Do not change route Pause/Resume/Interrupt or manual terminal semantics.

---

## File Structure

- Create `probe_station_gui/coordinates/__init__.py`: public exports only.
- Create `probe_station_gui/coordinates/model.py`: axis constants, immutable pose/readiness/frame records, typed errors.
- Create `probe_station_gui/coordinates/transforms.py`: pure 2-D rotation and B-attached forward/inverse math.
- Create `probe_station_gui/coordinates/registry.py`: immutable snapshots, version-checked CRUD, dependency invalidation.
- Create `probe_station_gui/settings/software_coordinates.py`: parse/clone/serialize custom-frame and pivot settings.
- Modify `probe_station_gui/settings/manager.py`: own the new settings section while preserving legacy `coordinate_system`.
- Modify `probe_station_gui/default_settings.json`: add versioned software-coordinate defaults.
- Modify `probe_station_gui/settings/default_file.py`: fill the new section for old settings files.
- Create `probe_station_gui/coordinates/persistence.py`: versioned design-frame document, tolerant parsing, atomic backend, newest-only worker.
- Modify `probe_station_gui/settings/manager.py`: expose the `coordinate-frames.json` path without putting measured frames in `settings.json`.
- Create focused tests under `tests/coordinates/` and `tests/settings/`.

### Task 1: Physical Pose and B-Attached Transform Math

**Files:**
- Create: `probe_station_gui/coordinates/__init__.py`
- Create: `probe_station_gui/coordinates/model.py`
- Create: `probe_station_gui/coordinates/transforms.py`
- Test: `tests/coordinates/test_transforms.py`

**Interfaces:**
- Consumes: calibrated physical values supplied as `Mapping[str, float]`.
- Produces: `STAGE_AXES`, `VISIBLE_STAGE_AXES`, `PhysicalMachinePose`, `BFrameTransform`, `rotate_xy()`, `normalize_axis_values()`.

- [ ] **Step 1: Write failing transform tests**

```python
import math

import pytest

from probe_station_gui.coordinates.model import (
    STAGE_AXES,
    VISIBLE_STAGE_AXES,
    PhysicalMachinePose,
)
from probe_station_gui.coordinates.transforms import BFrameTransform


def test_c_is_retained_in_backend_but_hidden_from_ordinary_gui() -> None:
    assert STAGE_AXES == ("X", "Y", "Z", "A", "B", "C")
    assert VISIBLE_STAGE_AXES == ("X", "Y", "Z", "A", "B")


def test_physical_pose_rejects_raw_non_finite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        PhysicalMachinePose.from_mapping({"X": math.nan})


def test_b_attached_transform_round_trips_after_rotation() -> None:
    transform = BFrameTransform(
        origin_xy_at_reference_b=(10.0, 0.0),
        reference_b_deg=0.0,
        xy_angle_at_reference_b_deg=90.0,
        b_zero_machine_deg=-90.0,
        z_zero_machine_mm=5.0,
        a_zero_machine_mm=7.0,
    )
    machine_xy = transform.frame_xy_to_machine(
        (2.0, 3.0),
        machine_b_deg=90.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    assert machine_xy == pytest.approx((-2.0, 7.0))
    assert transform.machine_xy_to_frame(
        machine_xy,
        machine_b_deg=90.0,
        pivot_machine_xy=(0.0, 0.0),
    ) == pytest.approx((2.0, 3.0))
    assert transform.machine_b_to_frame(0.0) == pytest.approx(90.0)
    assert transform.machine_z_to_frame(6.5) == pytest.approx(1.5)
    assert transform.machine_a_to_frame(8.25) == pytest.approx(1.25)


def test_missing_z_or_a_origin_is_not_silently_machine_zero() -> None:
    transform = BFrameTransform(
        origin_xy_at_reference_b=(0.0, 0.0),
        reference_b_deg=0.0,
        xy_angle_at_reference_b_deg=0.0,
        b_zero_machine_deg=0.0,
    )
    with pytest.raises(ValueError, match="Z origin"):
        transform.machine_z_to_frame(1.0)
    with pytest.raises(ValueError, match="A origin"):
        transform.machine_a_to_frame(1.0)
```

- [ ] **Step 2: Run the test and confirm the module is missing**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_transforms.py -v
```

Expected: FAIL during collection with `ModuleNotFoundError: probe_station_gui.coordinates`.

- [ ] **Step 3: Implement immutable physical values and transform math**

Create `model.py` with these exact public shapes:

```python
from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

STAGE_AXES = ("X", "Y", "Z", "A", "B", "C")
VISIBLE_STAGE_AXES = ("X", "Y", "Z", "A", "B")


def normalize_axis_values(values: Mapping[str, float]) -> Mapping[str, float]:
    normalized: dict[str, float] = {}
    for raw_axis, raw_value in values.items():
        axis = str(raw_axis).strip().upper()
        if axis not in STAGE_AXES:
            raise ValueError(f"Unsupported stage axis {raw_axis!r}.")
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"{axis} coordinate must be finite.")
        normalized[axis] = value
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class PhysicalMachinePose:
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", normalize_axis_values(self.values))

    @classmethod
    def from_mapping(cls, values: Mapping[str, float]) -> "PhysicalMachinePose":
        return cls(values)

    def require(self, axis: str) -> float:
        normalized = str(axis).strip().upper()
        try:
            return self.values[normalized]
        except KeyError as exc:
            raise ValueError(f"Physical Machine {normalized} is unavailable.") from exc

    def to_dict(self) -> dict[str, float]:
        return dict(self.values)
```

Create `transforms.py` with a finite-value validating `BFrameTransform`. Use the
standard counter-clockwise matrix `((cos, -sin), (sin, cos))`; implement
`frame_xy_to_machine()` as `o(b) + R(theta(b)) * frame_xy` and
`machine_xy_to_frame()` as its exact transpose inverse. Z/A conversion must
raise when the corresponding optional origin is `None`.

```python
def rotate_xy(point: tuple[float, float], angle_deg: float) -> tuple[float, float]:
    angle_rad = math.radians(float(angle_deg))
    cosine = math.cos(angle_rad)
    sine = math.sin(angle_rad)
    return (
        cosine * float(point[0]) - sine * float(point[1]),
        sine * float(point[0]) + cosine * float(point[1]),
    )


@dataclass(frozen=True)
class BFrameTransform:
    origin_xy_at_reference_b: tuple[float, float]
    reference_b_deg: float
    xy_angle_at_reference_b_deg: float
    b_zero_machine_deg: float
    z_zero_machine_mm: float | None = None
    a_zero_machine_mm: float | None = None

    def machine_origin_xy(self, machine_b_deg: float, pivot_machine_xy: tuple[float, float]) -> tuple[float, float]:
        delta = float(machine_b_deg) - self.reference_b_deg
        relative = (
            self.origin_xy_at_reference_b[0] - pivot_machine_xy[0],
            self.origin_xy_at_reference_b[1] - pivot_machine_xy[1],
        )
        rotated = rotate_xy(relative, delta)
        return (pivot_machine_xy[0] + rotated[0], pivot_machine_xy[1] + rotated[1])

    def frame_xy_to_machine(self, frame_xy: tuple[float, float], *, machine_b_deg: float, pivot_machine_xy: tuple[float, float]) -> tuple[float, float]:
        angle = self.xy_angle_at_reference_b_deg + machine_b_deg - self.reference_b_deg
        rotated = rotate_xy(frame_xy, angle)
        origin = self.machine_origin_xy(machine_b_deg, pivot_machine_xy)
        return (origin[0] + rotated[0], origin[1] + rotated[1])

    def machine_xy_to_frame(self, machine_xy: tuple[float, float], *, machine_b_deg: float, pivot_machine_xy: tuple[float, float]) -> tuple[float, float]:
        origin = self.machine_origin_xy(machine_b_deg, pivot_machine_xy)
        angle = self.xy_angle_at_reference_b_deg + machine_b_deg - self.reference_b_deg
        return rotate_xy((machine_xy[0] - origin[0], machine_xy[1] - origin[1]), -angle)

    def machine_b_to_frame(self, machine_b_deg: float) -> float:
        return float(machine_b_deg) - self.b_zero_machine_deg

    def machine_z_to_frame(self, machine_z_mm: float) -> float:
        if self.z_zero_machine_mm is None:
            raise ValueError("Frame Z origin is unavailable.")
        return float(machine_z_mm) - self.z_zero_machine_mm

    def machine_a_to_frame(self, machine_a_mm: float) -> float:
        if self.a_zero_machine_mm is None:
            raise ValueError("Frame A origin is unavailable.")
        return float(machine_a_mm) - self.a_zero_machine_mm
```

Export the new public names from `coordinates/__init__.py`.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_transforms.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the transform core**

```powershell
git add probe_station_gui/coordinates tests/coordinates/test_transforms.py
git commit -m "feat: add software coordinate transform core"
```

### Task 2: Frame Records, Readiness, and Registry Versions

**Files:**
- Modify: `probe_station_gui/coordinates/model.py`
- Create: `probe_station_gui/coordinates/registry.py`
- Modify: `probe_station_gui/coordinates/__init__.py`
- Test: `tests/coordinates/test_registry.py`

**Interfaces:**
- Consumes: `BFrameTransform` from Task 1.
- Produces: `FrameKind`, `ReadinessStatus`, `AxisReadiness`, `CoordinateFrameRecord`, `CoordinateFrameRegistry`, `RegistrySnapshot`, `FrameVersionConflict`, `invalidate_axes()`.

- [ ] **Step 1: Write failing registry and cascade tests**

```python
from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
)
from probe_station_gui.coordinates.registry import (
    CoordinateFrameRegistry,
    FrameVersionConflict,
    invalidate_axes,
)
from probe_station_gui.coordinates.transforms import BFrameTransform


def _ready_design() -> CoordinateFrameRecord:
    return CoordinateFrameRecord.create_design(
        frame_id="8f54e770-d348-4c4f-8da8-d7d46678aa26",
        name="chip-a",
        transform=BFrameTransform.identity(),
        readiness={
            axis: AxisReadiness(ReadinessStatus.READY)
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata={},
    )


def test_z_invalidation_always_invalidates_a() -> None:
    changed = invalidate_axes(_ready_design(), {"Z"}, "Focus reference changed.")
    assert changed.readiness["Z"].status is ReadinessStatus.STALE
    assert changed.readiness["A"].status is ReadinessStatus.STALE
    assert changed.readiness["X"].available


def test_xy_or_b_invalidation_cascades_through_z_and_a() -> None:
    changed = invalidate_axes(_ready_design(), {"B"}, "B calibration changed.")
    assert {axis for axis, state in changed.readiness.items() if not state.available} == {
        "X", "Y", "Z", "A", "B"
    }


def test_registry_rejects_stale_writer_version() -> None:
    registry = CoordinateFrameRegistry()
    added = registry.add(_ready_design())
    registry.replace(added.with_name("first"), expected_version=added.version)
    try:
        registry.replace(added.with_name("second"), expected_version=added.version)
    except FrameVersionConflict as exc:
        assert exc.frame_id == added.frame_id
    else:
        raise AssertionError("stale replacement was accepted")


def test_temporary_authority_block_does_not_erase_reference_values() -> None:
    original = _ready_design()
    blocked = original.with_authority_block({"B"}, "Controller B unavailable.")
    assert blocked.transform == original.transform
    assert blocked.readiness["B"] == AxisReadiness(
        ReadinessStatus.BLOCKED,
        "Controller B unavailable.",
    )
```

- [ ] **Step 2: Run and verify missing record types**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_registry.py -v
```

Expected: FAIL importing `AxisReadiness` or `CoordinateFrameRegistry`.

- [ ] **Step 3: Implement record and registry APIs**

Add enums and immutable record fields exactly as follows:

```python
class FrameKind(str, Enum):
    MACHINE = "machine"
    DESIGN = "design"
    CUSTOM = "custom"


class ReadinessStatus(str, Enum):
    READY = "ready"
    MISSING = "missing"
    BLOCKED = "blocked"
    STALE = "stale"


@dataclass(frozen=True)
class AxisReadiness:
    status: ReadinessStatus
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.status is ReadinessStatus.READY


@dataclass(frozen=True)
class CoordinateFrameRecord:
    frame_id: str
    kind: FrameKind
    name: str
    version: int
    transform: BFrameTransform | None
    readiness: Mapping[str, AxisReadiness]
    metadata: Mapping[str, object]
```

Validate UUIDs, non-empty names, and all readiness axes in constructors.
`create_design()` requires explicit transform, readiness, and metadata; it is
the same production constructor used by design registration and persistence.

In `registry.py`, use one `threading.RLock`, increment the record version on
replace, and return `RegistrySnapshot(generation, records)` with a tuple sorted
Machine, Design name, Custom name. Copy records while locked; perform no signal
or callback while locked. `invalidate_axes()` must implement exact cascades:
`X|Y|B => X,Y,B,Z,A`; `Z => Z,A`; `A => A`.

- [ ] **Step 4: Run registry tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_registry.py tests\coordinates\test_transforms.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit registry behavior**

```powershell
git add probe_station_gui/coordinates tests/coordinates/test_registry.py
git commit -m "feat: add coordinate frame registry readiness"
```

### Task 3: User Settings for Custom Frames and Rotation Geometry

**Files:**
- Create: `probe_station_gui/settings/software_coordinates.py`
- Modify: `probe_station_gui/settings/manager.py:106-194,602-690`
- Modify: `probe_station_gui/settings/default_file.py:52-76`
- Modify: `probe_station_gui/default_settings.json`
- Test: `tests/settings/test_software_coordinates.py`

**Interfaces:**
- Consumes: UUID strings and finite physical values from the coordinate core.
- Produces: `CustomFrameSettings`, `RotationPivotSettings`, `SoftwareCoordinateSettings`, `parse_software_coordinate_settings()`; `Settings.software_coordinates`.

- [ ] **Step 1: Write failing settings round-trip and invalidation tests**

```python
from probe_station_gui.settings.software_coordinates import (
    CustomFrameSettings,
    SoftwareCoordinateSettings,
    parse_software_coordinate_settings,
)


def _custom(*, z_zero_mm: float = 6.0, a_zero_mm: float = 7.0) -> CustomFrameSettings:
    return CustomFrameSettings(
        frame_id="14c838bd-a9a5-47bd-9d22-6326ca63c469",
        name="fixture",
        origin_x_mm=1.0,
        origin_y_mm=2.0,
        reference_b_deg=3.0,
        xy_angle_deg=4.0,
        b_zero_deg=5.0,
        z_zero_mm=z_zero_mm,
        a_zero_mm=a_zero_mm,
    )


def test_software_coordinate_defaults_use_assumed_machine_zero_pivot() -> None:
    parsed = parse_software_coordinate_settings({})
    assert parsed.pivot.x_mm == 0.0
    assert parsed.pivot.y_mm == 0.0
    assert parsed.pivot.source == "assumed"
    assert parsed.last_selected_frame_id == "machine"


def test_custom_frame_round_trip_keeps_stable_id_and_physical_values() -> None:
    frame = _custom()
    settings = SoftwareCoordinateSettings(custom_frames=(frame,))
    restored = parse_software_coordinate_settings(settings.to_dict())
    assert restored == settings


def test_changing_custom_z_clears_a_even_when_new_a_is_supplied() -> None:
    original = _custom(z_zero_mm=1.0, a_zero_mm=2.0)
    changed = original.apply_geometry_edit(z_zero_mm=3.0, a_zero_mm=4.0)
    assert changed.z_zero_mm == 3.0
    assert changed.a_zero_mm is None


def test_parser_skips_one_invalid_custom_record_without_losing_valid_record() -> None:
    raw = SoftwareCoordinateSettings(
        custom_frames=(_custom(),)
    ).to_dict()
    raw["custom_frames"].append({"frame_id": "bad", "name": "broken"})
    parsed = parse_software_coordinate_settings(raw)
    assert len(parsed.custom_frames) == 1
    assert len(parsed.diagnostics) == 1
```

- [ ] **Step 2: Run and verify parser is absent**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\settings\test_software_coordinates.py -v
```

Expected: FAIL importing `probe_station_gui.settings.software_coordinates`.

- [ ] **Step 3: Implement settings models and manager wiring**

Use these concrete section defaults:

```python
SOFTWARE_COORDINATE_SETTINGS_VERSION = 1


@dataclass
class RotationPivotSettings:
    x_mm: float = 0.0
    y_mm: float = 0.0
    source: str = "assumed"
    calibration_version: int = 0
    objective_name: str = ""
    sampled_b_min_deg: float | None = None
    sampled_b_max_deg: float | None = None
    rms_error_mm: float | None = None
    max_error_mm: float | None = None


@dataclass
class SoftwareCoordinateSettings:
    version: int = SOFTWARE_COORDINATE_SETTINGS_VERSION
    custom_frames: tuple[CustomFrameSettings, ...] = ()
    pivot: RotationPivotSettings = field(default_factory=RotationPivotSettings)
    last_selected_frame_id: str = "machine"
    max_rotation_segment_deg: float = 0.5
    max_rotation_chord_error_mm: float = 0.005
    diagnostics: tuple[str, ...] = ()
```

`CustomFrameSettings.apply_geometry_edit()` must compare normalized X/Y/B/Z
values against the original. Any X/Y/reference-B/angle/B-zero change sets both
`z_zero_mm` and `a_zero_mm` to `None`; a Z change accepts the new Z but sets A
to `None` even if the caller also supplied A. A-only edits may set A.

Add `software_coordinates` to `Settings`, `clone()`, `to_dict()`, and
`_settings_from_raw()`. Keep the legacy `coordinate_system` field unchanged for
backward-compatible parsing but document it as legacy. Add a
`software_coordinates` JSON section to defaults and normalize it in
`normalize_default_settings_data()`.

- [ ] **Step 4: Run settings and existing manager tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\settings tests\ui\test_settings_objectives_coordinates.py -v
```

Expected: all tests PASS; existing WCO settings tests remain green because
legacy parsing is preserved.

- [ ] **Step 5: Commit settings support**

```powershell
git add probe_station_gui/settings probe_station_gui/default_settings.json tests/settings tests/ui/test_settings_objectives_coordinates.py
git commit -m "feat: persist software coordinate settings"
```

### Task 4: Versioned Measured-Frame Document and Atomic Backend

**Files:**
- Create: `probe_station_gui/coordinates/persistence.py`
- Modify: `probe_station_gui/settings/manager.py:196-230,414-448`
- Test: `tests/coordinates/test_persistence.py`

**Interfaces:**
- Consumes: `CoordinateFrameRecord` snapshots from Task 2.
- Produces: `CoordinateFrameDocument`, `FrameLoadDiagnostic`, `FilesystemCoordinateFrameBackend`, `CoordinateFrameStoreWorker`, `SettingsManager.coordinate_frames_path()`.

- [ ] **Step 1: Write failing tolerant-load and atomic-save tests**

```python
import json

from probe_station_gui.coordinates.model import CoordinateFrameRecord
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    FilesystemCoordinateFrameBackend,
)
from probe_station_gui.coordinates.model import AxisReadiness, ReadinessStatus
from probe_station_gui.coordinates.transforms import BFrameTransform


def _record() -> CoordinateFrameRecord:
    return CoordinateFrameRecord.create_design(
        frame_id="ba64e533-7143-49ef-b41a-290f16d7ca1b",
        name="valid",
        transform=BFrameTransform.identity(),
        readiness={
            axis: AxisReadiness(ReadinessStatus.READY)
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata={},
    )


def test_document_load_isolates_one_invalid_record(tmp_path) -> None:
    path = tmp_path / "coordinate-frames.json"
    valid = _record()
    payload = CoordinateFrameDocument(records=(valid,)).to_dict()
    payload["records"].append({"frame_id": "broken"})
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = FilesystemCoordinateFrameBackend(path).load()
    assert [record.name for record in loaded.records] == ["valid"]
    assert len(loaded.diagnostics) == 1


def test_failed_atomic_replace_preserves_previous_document(tmp_path, monkeypatch) -> None:
    path = tmp_path / "coordinate-frames.json"
    backend = FilesystemCoordinateFrameBackend(path)
    first = CoordinateFrameDocument(records=(_record(),))
    backend.save(first)
    previous = path.read_bytes()

    def fail_replace(_source, _target) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("probe_station_gui.coordinates.persistence.os.replace", fail_replace)
    try:
        backend.save(CoordinateFrameDocument())
    except OSError:
        pass
    assert path.read_bytes() == previous


def test_unknown_future_document_version_is_rejected(tmp_path) -> None:
    path = tmp_path / "coordinate-frames.json"
    path.write_text('{"version": 99, "records": []}', encoding="utf-8")
    try:
        FilesystemCoordinateFrameBackend(path).load()
    except ValueError as exc:
        assert "version" in str(exc)
    else:
        raise AssertionError("future schema was accepted")
```

- [ ] **Step 2: Run and verify persistence module is absent**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_persistence.py -v
```

Expected: FAIL importing `probe_station_gui.coordinates.persistence`.

- [ ] **Step 3: Implement document, serializers, atomic backend, and worker**

Use schema version exactly `1`:

```python
COORDINATE_FRAME_DOCUMENT_VERSION = 1


@dataclass(frozen=True)
class FrameLoadDiagnostic:
    index: int
    message: str


@dataclass(frozen=True)
class CoordinateFrameDocument:
    version: int = COORDINATE_FRAME_DOCUMENT_VERSION
    records: tuple[CoordinateFrameRecord, ...] = ()
    diagnostics: tuple[FrameLoadDiagnostic, ...] = ()
```

Serialize enums as their string values and transforms/readiness explicitly;
never pickle. `from_dict()` rejects a non-dict root and future/zero versions,
but catches each record error and appends `FrameLoadDiagnostic` while retaining
valid records. Persist Design records only; Machine is built in and Custom
records are materialized from `settings.json`. `save()` encodes sorted/indented JSON, writes a sibling temporary
file, flushes and `os.fsync()`s it, then calls `os.replace()`; on failure it
removes only the temporary file.

Implement `CoordinateFrameStoreWorker` with the same creator-thread contract as
`MarkupStoreWorker`, but only `load()` and coalesced newest `publish()`
operations for one file. Its signals carry immutable result objects. Post Qt
signals only after leaving the condition lock. `stop(timeout_s=1.0)` drains the
latest queued publish and uses a non-daemon drain thread if the timeout expires.

Add `COORDINATE_FRAMES_FILENAME = "coordinate-frames.json"` and
`coordinate_frames_path()` to `SettingsManager`.

- [ ] **Step 4: Run persistence and markup-store regression tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates\test_persistence.py tests\design\test_markup_store.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit measured-frame persistence**

```powershell
git add probe_station_gui/coordinates/persistence.py probe_station_gui/settings/manager.py tests/coordinates/test_persistence.py
git commit -m "feat: add persistent coordinate frame store"
```

### Task 5: Core Integration Regression Gate

**Files:**
- Modify: `probe_station_gui/coordinates/__init__.py`
- Test: `tests/coordinates/test_axis_calibration_boundary.py`
- Test: `tests/coordinates/test_public_api.py`

**Interfaces:**
- Consumes: all public core/settings/store types in Tasks 1-4 and existing `StageAxisCalibrationMapper`.
- Produces: stable import surface for the later UI, registration, motion, and rotation plans.

- [ ] **Step 1: Add a failing nonlinear-boundary integration test**

```python
import pytest

from probe_station_gui.coordinates import BFrameTransform, PhysicalMachinePose
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.axis_mapping import AxisCalibrationCurve


def test_frame_math_is_applied_after_universal_controller_to_physical_mapping() -> None:
    mapper = StageAxisCalibrationMapper(
        calibrations={
            "X": AxisCalibrationCurve((0.0, 10.0), (0.0, 20.0)),
            "Y": AxisCalibrationCurve((0.0, 10.0), (0.0, 10.0)),
        },
        position_reporting_mode="machine",
        active_work_coordinate_system=None,
        controller_coordinate_offsets={},
        axis_index={"X": 0, "Y": 1},
    )
    machine = PhysicalMachinePose.from_mapping(
        {
            "X": mapper.controller_to_physical("X", 5.0),
            "Y": mapper.controller_to_physical("Y", 4.0),
            "B": 0.0,
        }
    )
    transform = BFrameTransform.identity()
    assert transform.machine_xy_to_frame(
        (machine.require("X"), machine.require("Y")),
        machine_b_deg=machine.require("B"),
        pivot_machine_xy=(0.0, 0.0),
    ) == pytest.approx((10.0, 4.0))
    assert mapper.physical_to_controller("X", 10.0) == pytest.approx(5.0)
```

- [ ] **Step 2: Run the focused integration tests and observe any missing test constructor**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates -v
```

Expected: FAIL only because `BFrameTransform.identity()` is not yet exposed;
no production behavior should fail.

- [ ] **Step 3: Add the narrow test constructors and final public exports**

Add `BFrameTransform.identity()` returning all-zero planar geometry with no
Z/A origins. Export only the names consumed by later plans from
`coordinates/__init__.py`; do not add a second calibration constructor or
interpolation implementation.

```python
@classmethod
def identity(cls) -> "BFrameTransform":
    return cls(
        origin_xy_at_reference_b=(0.0, 0.0),
        reference_b_deg=0.0,
        xy_angle_at_reference_b_deg=0.0,
        b_zero_machine_deg=0.0,
    )
```

- [ ] **Step 4: Run the complete core gate and full suite**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\coordinates tests\settings -v
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

Expected: coordinate/settings tests PASS, then the full baseline PASS with no
hardware access.

- [ ] **Step 5: Commit the core boundary gate**

```powershell
git add probe_station_gui/coordinates tests/coordinates
git commit -m "test: lock software coordinate core boundary"
```
