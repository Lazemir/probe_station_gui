"""Persisted configuration for software coordinate frames."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Final
from uuid import UUID


SOFTWARE_COORDINATE_SETTINGS_VERSION: Final = 1
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
    pivot: RotationPivotSettings = field(default_factory=RotationPivotSettings)
    last_selected_frame_id: str = "machine"
    selection_generation: int = 0
    max_rotation_segment_deg: float = 0.5
    max_rotation_chord_error_mm: float = 0.005
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError(
                "Software coordinate settings version must be a positive integer."
            )
        self.custom_frames = tuple(self.custom_frames)
        if not all(isinstance(frame, CustomFrameSettings) for frame in self.custom_frames):
            raise ValueError("Custom frames must be CustomFrameSettings instances.")
        if len({frame.frame_id for frame in self.custom_frames}) != len(
            self.custom_frames
        ):
            raise ValueError("Custom frame IDs must be unique.")
        if not isinstance(self.pivot, RotationPivotSettings):
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

    def clone(self) -> "SoftwareCoordinateSettings":
        return SoftwareCoordinateSettings(
            version=self.version,
            custom_frames=self.custom_frames,
            pivot=self.pivot.clone(),
            last_selected_frame_id=self.last_selected_frame_id,
            selection_generation=self.selection_generation,
            max_rotation_segment_deg=self.max_rotation_segment_deg,
            max_rotation_chord_error_mm=self.max_rotation_chord_error_mm,
            diagnostics=self.diagnostics,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "custom_frames": [frame.to_dict() for frame in self.custom_frames],
            "pivot": self.pivot.to_dict(),
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


def _finite_or_default(value: object, default: float, name: str) -> float:
    try:
        return _finite_float(value, name)
    except ValueError:
        return default


def _optional_finite_or_default(
    value: object,
    default: float | None,
    name: str,
) -> float | None:
    try:
        return _optional_finite_float(value, name)
    except ValueError:
        return default


def _positive_finite_or_default(value: object, default: float, name: str) -> float:
    try:
        return _positive_finite_float(value, name)
    except ValueError:
        return default


def _parse_frame(raw: object) -> CustomFrameSettings:
    if not isinstance(raw, dict):
        raise ValueError("Custom frame record must be an object.")
    return CustomFrameSettings(
        frame_id=raw.get("frame_id"),
        name=raw.get("name"),
        origin_x_mm=raw.get("origin_x_mm"),
        origin_y_mm=raw.get("origin_y_mm"),
        reference_b_deg=raw.get("reference_b_deg"),
        xy_angle_deg=raw.get("xy_angle_deg"),
        b_zero_deg=raw.get("b_zero_deg"),
        z_zero_mm=raw.get("z_zero_mm"),
        a_zero_mm=raw.get("a_zero_mm"),
    )


def _parse_pivot(raw: object) -> RotationPivotSettings:
    defaults = RotationPivotSettings()
    if not isinstance(raw, dict):
        return defaults
    source = raw.get("source", defaults.source)
    if not isinstance(source, str) or not source.strip():
        source = defaults.source
    objective_name = raw.get("objective_name", defaults.objective_name)
    if not isinstance(objective_name, str):
        objective_name = defaults.objective_name
    calibration_version = raw.get("calibration_version", defaults.calibration_version)
    if (
        not isinstance(calibration_version, int)
        or isinstance(calibration_version, bool)
        or calibration_version < 0
    ):
        calibration_version = defaults.calibration_version
    return RotationPivotSettings(
        x_mm=_finite_or_default(
            raw.get("x_mm", defaults.x_mm),
            defaults.x_mm,
            "Pivot X",
        ),
        y_mm=_finite_or_default(
            raw.get("y_mm", defaults.y_mm),
            defaults.y_mm,
            "Pivot Y",
        ),
        source=source,
        calibration_version=calibration_version,
        objective_name=objective_name,
        sampled_b_min_deg=_optional_finite_or_default(
            raw.get("sampled_b_min_deg"),
            defaults.sampled_b_min_deg,
            "Pivot sampled B minimum",
        ),
        sampled_b_max_deg=_optional_finite_or_default(
            raw.get("sampled_b_max_deg"),
            defaults.sampled_b_max_deg,
            "Pivot sampled B maximum",
        ),
        rms_error_mm=_optional_finite_or_default(
            raw.get("rms_error_mm"),
            defaults.rms_error_mm,
            "Pivot RMS error",
        ),
        max_error_mm=_optional_finite_or_default(
            raw.get("max_error_mm"),
            defaults.max_error_mm,
            "Pivot maximum error",
        ),
    )


def parse_software_coordinate_settings(raw: object) -> SoftwareCoordinateSettings:
    """Parse persisted settings, retaining valid frames and reporting bad records."""

    defaults = SoftwareCoordinateSettings()
    if not isinstance(raw, dict):
        return defaults
    diagnostics: list[str] = []
    frames: list[CustomFrameSettings] = []
    seen_frame_ids: set[str] = set()
    raw_frames = raw.get("custom_frames", ())
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
    elif raw_frames is not None:
        diagnostics.append("Custom frames ignored: expected a list.")

    version = raw.get("version", defaults.version)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        version = defaults.version
    selected = raw.get("last_selected_frame_id", defaults.last_selected_frame_id)
    if not isinstance(selected, str) or not selected.strip():
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
        selection_generation = defaults.selection_generation
    return SoftwareCoordinateSettings(
        version=version,
        custom_frames=tuple(frames),
        pivot=_parse_pivot(raw.get("pivot")),
        last_selected_frame_id=selected,
        selection_generation=selection_generation,
        max_rotation_segment_deg=_positive_finite_or_default(
            raw.get("max_rotation_segment_deg", defaults.max_rotation_segment_deg),
            defaults.max_rotation_segment_deg,
            "Maximum rotation segment",
        ),
        max_rotation_chord_error_mm=_positive_finite_or_default(
            raw.get(
                "max_rotation_chord_error_mm",
                defaults.max_rotation_chord_error_mm,
            ),
            defaults.max_rotation_chord_error_mm,
            "Maximum rotation chord error",
        ),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "SOFTWARE_COORDINATE_SETTINGS_VERSION",
    "CustomFrameSettings",
    "RotationPivotSettings",
    "SoftwareCoordinateSettings",
    "parse_software_coordinate_settings",
]
