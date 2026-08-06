"""Persisted configuration for software coordinate frames."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import math
from typing import Final
from uuid import UUID


SOFTWARE_COORDINATE_SETTINGS_VERSION: Final = 1
_SOFTWARE_COORDINATE_ROOT_FIELDS: Final = frozenset(
    {
        "version",
        "custom_frames",
        "pivot",
        "last_selected_frame_id",
        "selection_generation",
        "max_rotation_segment_deg",
        "max_rotation_chord_error_mm",
    }
)
_UNSET = object()


def _finite_float(value: object, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite.")
    return parsed


def _optional_finite_float(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _finite_float(value, name)


def _json_finite_number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a JSON number.")
    return _finite_float(value, name)


def _optional_json_finite_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _json_finite_number(value, name)


def _positive_json_finite_number(value: object, name: str) -> float:
    parsed = _json_finite_number(value, name)
    if parsed <= 0.0:
        raise ValueError(f"{name} must be positive.")
    return parsed


@dataclass(frozen=True)
class CustomFrameSettings:
    """User-defined coordinate-frame geometry and optional vertical origins."""

    frame_id: str
    name: str
    origin_x_mm: float
    origin_y_mm: float
    reference_b_deg: float
    xy_angle_deg: float
    b_zero_deg: float
    z_zero_mm: float | None = None
    a_zero_mm: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.frame_id, str):
            raise ValueError("Custom frame ID must be a UUID string.")
        try:
            UUID(self.frame_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Custom frame ID must be a UUID string.") from exc
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Custom frame name must not be empty.")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(
            self,
            "origin_x_mm",
            _finite_float(self.origin_x_mm, "Frame X origin"),
        )
        object.__setattr__(
            self,
            "origin_y_mm",
            _finite_float(self.origin_y_mm, "Frame Y origin"),
        )
        object.__setattr__(
            self,
            "reference_b_deg",
            _finite_float(self.reference_b_deg, "Reference B angle"),
        )
        object.__setattr__(
            self,
            "xy_angle_deg",
            _finite_float(self.xy_angle_deg, "Frame XY angle"),
        )
        object.__setattr__(
            self,
            "b_zero_deg",
            _finite_float(self.b_zero_deg, "Frame B origin"),
        )
        object.__setattr__(
            self,
            "z_zero_mm",
            _optional_finite_float(self.z_zero_mm, "Frame Z origin"),
        )
        object.__setattr__(
            self,
            "a_zero_mm",
            _optional_finite_float(self.a_zero_mm, "Frame A origin"),
        )
        if self.a_zero_mm is not None and self.z_zero_mm is None:
            raise ValueError("Frame A origin requires a Z origin.")

    def clone(self) -> "CustomFrameSettings":
        return self

    def to_dict(self) -> dict[str, object]:
        return {
            "frame_id": self.frame_id,
            "name": self.name,
            "origin_x_mm": self.origin_x_mm,
            "origin_y_mm": self.origin_y_mm,
            "reference_b_deg": self.reference_b_deg,
            "xy_angle_deg": self.xy_angle_deg,
            "b_zero_deg": self.b_zero_deg,
            "z_zero_mm": self.z_zero_mm,
            "a_zero_mm": self.a_zero_mm,
        }

    def apply_geometry_edit(
        self,
        *,
        origin_x_mm: object = _UNSET,
        origin_y_mm: object = _UNSET,
        reference_b_deg: object = _UNSET,
        xy_angle_deg: object = _UNSET,
        b_zero_deg: object = _UNSET,
        z_zero_mm: object = _UNSET,
        a_zero_mm: object = _UNSET,
    ) -> "CustomFrameSettings":
        """Return an edited frame, invalidating dependent vertical origins."""

        x = (
            self.origin_x_mm
            if origin_x_mm is _UNSET
            else _finite_float(origin_x_mm, "Frame X origin")
        )
        y = (
            self.origin_y_mm
            if origin_y_mm is _UNSET
            else _finite_float(origin_y_mm, "Frame Y origin")
        )
        reference_b = (
            self.reference_b_deg
            if reference_b_deg is _UNSET
            else _finite_float(reference_b_deg, "Reference B angle")
        )
        xy_angle = (
            self.xy_angle_deg
            if xy_angle_deg is _UNSET
            else _finite_float(xy_angle_deg, "Frame XY angle")
        )
        b_zero = (
            self.b_zero_deg
            if b_zero_deg is _UNSET
            else _finite_float(b_zero_deg, "Frame B origin")
        )
        z = (
            self.z_zero_mm
            if z_zero_mm is _UNSET
            else _optional_finite_float(z_zero_mm, "Frame Z origin")
        )
        a = (
            self.a_zero_mm
            if a_zero_mm is _UNSET
            else _optional_finite_float(a_zero_mm, "Frame A origin")
        )

        xy_or_b_changed = (
            x != self.origin_x_mm
            or y != self.origin_y_mm
            or reference_b != self.reference_b_deg
            or xy_angle != self.xy_angle_deg
            or b_zero != self.b_zero_deg
        )
        z_changed = z != self.z_zero_mm
        if xy_or_b_changed:
            z = None
            a = None
        elif z_changed:
            a = None
        return replace(
            self,
            origin_x_mm=x,
            origin_y_mm=y,
            reference_b_deg=reference_b,
            xy_angle_deg=xy_angle,
            b_zero_deg=b_zero,
            z_zero_mm=z,
            a_zero_mm=a,
        )


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

    def __post_init__(self) -> None:
        self.x_mm = _finite_float(self.x_mm, "Pivot X")
        self.y_mm = _finite_float(self.y_mm, "Pivot Y")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("Pivot source must not be empty.")
        self.source = self.source.strip()
        if (
            not isinstance(self.calibration_version, int)
            or isinstance(self.calibration_version, bool)
            or self.calibration_version < 0
        ):
            raise ValueError(
                "Pivot calibration version must be a non-negative integer."
            )
        if not isinstance(self.objective_name, str):
            raise ValueError("Pivot objective name must be a string.")
        self.objective_name = self.objective_name.strip()
        self.sampled_b_min_deg = _optional_finite_float(
            self.sampled_b_min_deg,
            "Pivot sampled B minimum",
        )
        self.sampled_b_max_deg = _optional_finite_float(
            self.sampled_b_max_deg,
            "Pivot sampled B maximum",
        )
        self.rms_error_mm = _optional_finite_float(
            self.rms_error_mm,
            "Pivot RMS error",
        )
        self.max_error_mm = _optional_finite_float(
            self.max_error_mm,
            "Pivot maximum error",
        )

    def clone(self) -> "RotationPivotSettings":
        return replace(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "x_mm": self.x_mm,
            "y_mm": self.y_mm,
            "source": self.source,
            "calibration_version": self.calibration_version,
            "objective_name": self.objective_name,
            "sampled_b_min_deg": self.sampled_b_min_deg,
            "sampled_b_max_deg": self.sampled_b_max_deg,
            "rms_error_mm": self.rms_error_mm,
            "max_error_mm": self.max_error_mm,
        }


@dataclass
class SoftwareCoordinateSettings:
    version: int = SOFTWARE_COORDINATE_SETTINGS_VERSION
    custom_frames: tuple[CustomFrameSettings, ...] = ()
    pivot: RotationPivotSettings | None = field(default_factory=RotationPivotSettings)
    last_selected_frame_id: str = "machine"
    selection_generation: int = 0
    max_rotation_segment_deg: float = 0.5
    max_rotation_chord_error_mm: float = 0.005
    diagnostics: tuple[str, ...] = ()
    _raw_custom_frames: tuple[object, ...] = field(
        default=(),
        compare=False,
        repr=False,
    )
    _accepted_custom_frame_slots: tuple[tuple[int, str], ...] = field(
        default=(),
        compare=False,
        repr=False,
    )
    _raw_pivot_present: bool = field(default=False, compare=False, repr=False)
    _raw_pivot: object = field(default=None, compare=False, repr=False)
    _preserved_raw_section_present: bool = field(
        default=False,
        compare=False,
        repr=False,
    )
    _preserved_raw_section: object = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version != SOFTWARE_COORDINATE_SETTINGS_VERSION
        ):
            raise ValueError(
                "Software coordinate settings must use supported version "
                f"{SOFTWARE_COORDINATE_SETTINGS_VERSION}."
            )
        self.custom_frames = tuple(self.custom_frames)
        if not all(isinstance(frame, CustomFrameSettings) for frame in self.custom_frames):
            raise ValueError("Custom frames must be CustomFrameSettings instances.")
        if len({frame.frame_id for frame in self.custom_frames}) != len(
            self.custom_frames
        ):
            raise ValueError("Custom frame IDs must be unique.")
        if self.pivot is not None and not isinstance(
            self.pivot,
            RotationPivotSettings,
        ):
            raise ValueError("Pivot must be RotationPivotSettings.")
        if (
            self.pivot is None
            and not self._raw_pivot_present
            and not self._preserved_raw_section_present
            and self._preserved_raw_section is None
        ):
            raise ValueError("Pivot must be RotationPivotSettings.")
        if not isinstance(self.last_selected_frame_id, str) or not self.last_selected_frame_id.strip():
            raise ValueError("Selected frame ID must not be empty.")
        self.last_selected_frame_id = self.last_selected_frame_id.strip()
        if (
            not isinstance(self.selection_generation, int)
            or isinstance(self.selection_generation, bool)
            or self.selection_generation < 0
        ):
            raise ValueError("Selection generation must be a non-negative integer.")
        self.max_rotation_segment_deg = _positive_finite_float(
            self.max_rotation_segment_deg,
            "Maximum rotation segment",
        )
        self.max_rotation_chord_error_mm = _positive_finite_float(
            self.max_rotation_chord_error_mm,
            "Maximum rotation chord error",
        )
        self.diagnostics = tuple(str(message) for message in self.diagnostics)
        self._raw_custom_frames = tuple(deepcopy(self._raw_custom_frames))
        self._accepted_custom_frame_slots = tuple(
            (int(index), str(frame_id))
            for index, frame_id in self._accepted_custom_frame_slots
        )
        self._raw_pivot = deepcopy(self._raw_pivot)
        if self._preserved_raw_section is not None:
            self._preserved_raw_section_present = True
        self._preserved_raw_section = deepcopy(self._preserved_raw_section)

    @property
    def degraded(self) -> bool:
        """Return whether persisted data was retained but could not be used safely."""

        return bool(self.diagnostics)

    @property
    def materialization_blocked(self) -> bool:
        """Return whether this section is unsafe to apply to the frame registry."""

        return self._preserved_raw_section_present or self.pivot is None

    def clone(self) -> "SoftwareCoordinateSettings":
        return SoftwareCoordinateSettings(
            version=self.version,
            custom_frames=self.custom_frames,
            pivot=None if self.pivot is None else self.pivot.clone(),
            last_selected_frame_id=self.last_selected_frame_id,
            selection_generation=self.selection_generation,
            max_rotation_segment_deg=self.max_rotation_segment_deg,
            max_rotation_chord_error_mm=self.max_rotation_chord_error_mm,
            diagnostics=self.diagnostics,
            _raw_custom_frames=self._raw_custom_frames,
            _accepted_custom_frame_slots=self._accepted_custom_frame_slots,
            _raw_pivot_present=self._raw_pivot_present,
            _raw_pivot=self._raw_pivot,
            _preserved_raw_section_present=self._preserved_raw_section_present,
            _preserved_raw_section=self._preserved_raw_section,
        )

    def to_dict(self) -> object:
        if self._preserved_raw_section_present:
            return deepcopy(self._preserved_raw_section)
        if not self._raw_custom_frames:
            serialized_frames = [frame.to_dict() for frame in self.custom_frames]
        else:
            current_by_id = {frame.frame_id: frame for frame in self.custom_frames}
            accepted_by_slot = dict(self._accepted_custom_frame_slots)
            emitted: set[str] = set()
            serialized_frames: list[object] = []
            for index, raw_frame in enumerate(self._raw_custom_frames):
                accepted_id = accepted_by_slot.get(index)
                if accepted_id is None:
                    serialized_frames.append(deepcopy(raw_frame))
                    continue
                current = current_by_id.get(accepted_id)
                if current is not None:
                    serialized_frames.append(current.to_dict())
                    emitted.add(accepted_id)
            serialized_frames.extend(
                frame.to_dict()
                for frame in self.custom_frames
                if frame.frame_id not in emitted
            )
        if self.pivot is not None:
            serialized_pivot = self.pivot.to_dict()
        elif self._raw_pivot_present:
            serialized_pivot = deepcopy(self._raw_pivot)
        else:
            raise ValueError("B-axis rotation pivot settings are unavailable.")
        return {
            "version": self.version,
            "custom_frames": serialized_frames,
            "pivot": serialized_pivot,
            "last_selected_frame_id": self.last_selected_frame_id,
            "selection_generation": self.selection_generation,
            "max_rotation_segment_deg": self.max_rotation_segment_deg,
            "max_rotation_chord_error_mm": self.max_rotation_chord_error_mm,
        }


def _positive_finite_float(value: object, name: str) -> float:
    parsed = _finite_float(value, name)
    if parsed <= 0.0:
        raise ValueError(f"{name} must be positive.")
    return parsed


def _parse_frame(raw: object) -> CustomFrameSettings:
    if not isinstance(raw, dict):
        raise ValueError("Custom frame record must be an object.")
    supported_fields = {
        "frame_id",
        "name",
        "origin_x_mm",
        "origin_y_mm",
        "reference_b_deg",
        "xy_angle_deg",
        "b_zero_deg",
        "z_zero_mm",
        "a_zero_mm",
    }
    unsupported = set(raw).difference(supported_fields)
    if unsupported:
        raise ValueError(f"Unsupported custom frame fields: {sorted(unsupported)!r}.")
    return CustomFrameSettings(
        frame_id=raw.get("frame_id"),
        name=raw.get("name"),
        origin_x_mm=_json_finite_number(raw.get("origin_x_mm"), "Frame X origin"),
        origin_y_mm=_json_finite_number(raw.get("origin_y_mm"), "Frame Y origin"),
        reference_b_deg=_json_finite_number(
            raw.get("reference_b_deg"),
            "Reference B angle",
        ),
        xy_angle_deg=_json_finite_number(raw.get("xy_angle_deg"), "Frame XY angle"),
        b_zero_deg=_json_finite_number(raw.get("b_zero_deg"), "Frame B origin"),
        z_zero_mm=_optional_json_finite_number(
            raw.get("z_zero_mm"),
            "Frame Z origin",
        ),
        a_zero_mm=_optional_json_finite_number(
            raw.get("a_zero_mm"),
            "Frame A origin",
        ),
    )


def _parse_pivot(raw: object) -> RotationPivotSettings:
    if not isinstance(raw, dict):
        raise ValueError("Rotation pivot must be an object.")
    supported_fields = set(RotationPivotSettings().to_dict())
    if set(raw) != supported_fields:
        missing = sorted(supported_fields.difference(raw))
        unsupported = sorted(set(raw).difference(supported_fields))
        detail = []
        if missing:
            detail.append(f"missing {missing!r}")
        if unsupported:
            detail.append(f"unsupported {unsupported!r}")
        raise ValueError(f"Rotation pivot schema is invalid ({', '.join(detail)}).")
    return RotationPivotSettings(
        x_mm=_json_finite_number(raw["x_mm"], "Pivot X"),
        y_mm=_json_finite_number(raw["y_mm"], "Pivot Y"),
        source=raw["source"],
        calibration_version=raw["calibration_version"],
        objective_name=raw["objective_name"],
        sampled_b_min_deg=_optional_json_finite_number(
            raw["sampled_b_min_deg"],
            "Pivot sampled B minimum",
        ),
        sampled_b_max_deg=_optional_json_finite_number(
            raw["sampled_b_max_deg"],
            "Pivot sampled B maximum",
        ),
        rms_error_mm=_optional_json_finite_number(
            raw["rms_error_mm"],
            "Pivot RMS error",
        ),
        max_error_mm=_optional_json_finite_number(
            raw["max_error_mm"],
            "Pivot maximum error",
        ),
    )


def _preserved_settings(
    raw: object,
    diagnostic: str,
) -> SoftwareCoordinateSettings:
    return SoftwareCoordinateSettings(
        pivot=None,
        diagnostics=(diagnostic,),
        _preserved_raw_section_present=True,
        _preserved_raw_section=deepcopy(raw),
    )


def parse_software_coordinate_settings(
    raw: object,
    *,
    section_present: bool = False,
) -> SoftwareCoordinateSettings:
    """Parse persisted settings, retaining valid frames and reporting bad records."""

    defaults = SoftwareCoordinateSettings()
    if not isinstance(raw, dict):
        if section_present:
            return _preserved_settings(
                raw,
                "Software coordinate settings root must be an object.",
            )
        return defaults
    current_schema = bool(section_present or "version" in raw)
    if current_schema and "version" not in raw:
        return _preserved_settings(
            raw,
            "Software coordinate settings schema is invalid (missing ['version']).",
        )
    if "version" in raw and (
        not isinstance(raw["version"], int)
        or isinstance(raw["version"], bool)
        or raw["version"] != SOFTWARE_COORDINATE_SETTINGS_VERSION
    ):
        return _preserved_settings(
            raw,
            f"Unsupported software coordinate settings version: {raw['version']!r}.",
        )
    if current_schema and set(raw) != _SOFTWARE_COORDINATE_ROOT_FIELDS:
        missing = sorted(_SOFTWARE_COORDINATE_ROOT_FIELDS.difference(raw))
        unsupported = sorted(set(raw).difference(_SOFTWARE_COORDINATE_ROOT_FIELDS))
        detail: list[str] = []
        if missing:
            detail.append(f"missing {missing!r}")
        if unsupported:
            detail.append(f"unsupported {unsupported!r}")
        return _preserved_settings(
            raw,
            f"Software coordinate settings schema is invalid ({', '.join(detail)}).",
        )
    unsupported_root_fields = set(raw).difference(
        _SOFTWARE_COORDINATE_ROOT_FIELDS
    )
    if unsupported_root_fields:
        return _preserved_settings(
            raw,
            "Unsupported software coordinate settings fields: "
            f"{sorted(unsupported_root_fields)!r}.",
        )
    try:
        max_rotation_segment_deg = (
            defaults.max_rotation_segment_deg
            if "max_rotation_segment_deg" not in raw
            else _positive_json_finite_number(
                raw["max_rotation_segment_deg"],
                "Maximum rotation segment",
            )
        )
        max_rotation_chord_error_mm = (
            defaults.max_rotation_chord_error_mm
            if "max_rotation_chord_error_mm" not in raw
            else _positive_json_finite_number(
                raw["max_rotation_chord_error_mm"],
                "Maximum rotation chord error",
            )
        )
    except ValueError as exc:
        return _preserved_settings(
            raw,
            f"Software coordinate settings unavailable: {exc}",
        )
    diagnostics: list[str] = []
    frames: list[CustomFrameSettings] = []
    accepted_slots: list[tuple[int, str]] = []
    seen_frame_ids: set[str] = set()
    raw_frames = raw.get("custom_frames", ())
    expected_frame_containers = (list,) if current_schema else (list, tuple)
    if "custom_frames" in raw and not isinstance(
        raw_frames,
        expected_frame_containers,
    ):
        return _preserved_settings(
            raw,
            "Custom frames unavailable: expected a list.",
        )
    if isinstance(raw_frames, (list, tuple)):
        for index, record in enumerate(raw_frames):
            try:
                frame = _parse_frame(record)
                if frame.frame_id in seen_frame_ids:
                    raise ValueError("Custom frame ID is duplicated.")
            except ValueError as exc:
                diagnostics.append(f"Custom frame {index + 1} ignored: {exc}")
                continue
            seen_frame_ids.add(frame.frame_id)
            frames.append(frame)
            accepted_slots.append((index, frame.frame_id))

    selected = raw.get("last_selected_frame_id", defaults.last_selected_frame_id)
    if not isinstance(selected, str) or not selected.strip():
        if current_schema:
            return _preserved_settings(
                raw,
                "Software coordinate settings unavailable: selected frame ID "
                "must be a string.",
            )
        selected = defaults.last_selected_frame_id
    selection_generation = raw.get(
        "selection_generation",
        defaults.selection_generation,
    )
    if (
        not isinstance(selection_generation, int)
        or isinstance(selection_generation, bool)
        or selection_generation < 0
    ):
        if current_schema:
            return _preserved_settings(
                raw,
                "Software coordinate settings unavailable: selection "
                "generation must be a non-negative integer.",
            )
        selection_generation = defaults.selection_generation
    raw_pivot_present = "pivot" in raw
    raw_pivot = raw.get("pivot")
    if raw_pivot_present:
        try:
            pivot = _parse_pivot(raw_pivot)
        except (TypeError, ValueError) as exc:
            if current_schema:
                return _preserved_settings(
                    raw,
                    f"Rotation pivot unavailable: {exc}",
                )
            pivot = None
            diagnostics.append(f"Rotation pivot unavailable: {exc}")
    else:
        pivot = defaults.pivot.clone()
    return SoftwareCoordinateSettings(
        version=SOFTWARE_COORDINATE_SETTINGS_VERSION,
        custom_frames=tuple(frames),
        pivot=pivot,
        last_selected_frame_id=selected,
        selection_generation=selection_generation,
        max_rotation_segment_deg=max_rotation_segment_deg,
        max_rotation_chord_error_mm=max_rotation_chord_error_mm,
        diagnostics=tuple(diagnostics),
        _raw_custom_frames=(
            tuple(deepcopy(raw_frames))
            if isinstance(raw_frames, (list, tuple))
            else ()
        ),
        _accepted_custom_frame_slots=tuple(accepted_slots),
        _raw_pivot_present=raw_pivot_present and pivot is None,
        _raw_pivot=raw_pivot if pivot is None else None,
    )


__all__ = [
    "SOFTWARE_COORDINATE_SETTINGS_VERSION",
    "CustomFrameSettings",
    "RotationPivotSettings",
    "SoftwareCoordinateSettings",
    "parse_software_coordinate_settings",
]
