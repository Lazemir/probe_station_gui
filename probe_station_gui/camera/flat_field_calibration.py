"""Durable storage for objective-specific flat-field calibrations."""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtGui import QImage

from probe_station_gui.camera.imaging import (
    FlatFieldProfile,
    build_flat_field_profile,
    median_flat_field_reference,
)


_MANIFEST_VERSION = 1
_COORDINATE_SPACE = "raw camera frame, before lens distortion correction"
_APPLICATION_ORDER = ["flat_field", "lens_distortion", "mosaic"]
_AUTO_BLUR_SHORT_SIDE_DIVISOR = 40.0
_AUTO_BLUR_RADIUS_MIN_PX = 5
_AUTO_BLUR_RADIUS_MAX_PX = 101
_AUTO_MAX_GAIN_FLOOR = 4.0
_AUTO_MAX_GAIN_CEILING = 16.0
_AUTO_MAX_GAIN_MARGIN = 1.1
_SAFE_OBJECTIVE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


@dataclass(frozen=True)
class StoredFlatFieldCalibration:
    """A loaded flat-field reference and its correction profile."""

    objective_name: str
    current_manifest: Path
    reference_image: Path
    profile: FlatFieldProfile
    metadata: dict[str, object]


class FlatFieldCalibrationStore:
    """Read and atomically install flat-field profiles below a config directory."""

    def __init__(self, config_dir: str | Path) -> None:
        self._config_dir = Path(config_dir).expanduser()
        self._cached_signature: tuple[object, ...] | None = None
        self._cached_calibration: StoredFlatFieldCalibration | None = None

    def current_manifest_path(self, objective_name: str) -> Path:
        """Return the active manifest path for a validated objective name."""

        objective = _safe_objective_name(objective_name)
        return self._objective_directory(objective) / "current.json"

    def load(self, objective_name: str) -> StoredFlatFieldCalibration:
        """Load the active profile for an objective from its current manifest."""

        objective = _safe_objective_name(objective_name)
        current_manifest = self.current_manifest_path(objective)
        try:
            payload = json.loads(current_manifest.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError("flat-field current.json is invalid") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("flat-field current.json must contain an object")
        version = payload.get("version")
        if type(version) is not int or version != _MANIFEST_VERSION:
            raise ValueError(
                f"flat-field current.json version must be exactly {_MANIFEST_VERSION}"
            )

        declared_objective = str(payload.get("objective") or objective).strip()
        if declared_objective.casefold() != objective.casefold():
            raise ValueError("flat-field objective does not match the active objective")
        reference_image = _reference_path(payload, current_manifest.parent)
        signature = (
            str(current_manifest),
            *_file_signature(current_manifest),
            str(reference_image),
            *_file_signature(reference_image),
        )
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("flat-field metadata must contain an object")
        if signature == self._cached_signature and self._cached_calibration is not None:
            return self._cached_calibration
        reference = QImage(str(reference_image))
        if reference.isNull():
            raise ValueError(f"unable to load flat-field reference {reference_image}")
        declared_size = payload.get("frame_size_px")
        if declared_size is not None:
            try:
                size = tuple(int(value) for value in declared_size)
            except (TypeError, ValueError) as exc:
                raise ValueError("flat-field frame_size_px is invalid") from exc
            if size != (reference.width(), reference.height()):
                raise ValueError("flat-field reference size does not match current.json")
        profile = build_flat_field_profile(
            reference,
            blur_radius_px=int(payload.get("blur_radius_px", 401)),
            max_gain=float(payload.get("max_gain", 4.0)),
            source=f"{objective} live flat-field",
        )
        stored = StoredFlatFieldCalibration(
            objective_name=objective,
            current_manifest=current_manifest,
            reference_image=reference_image,
            profile=profile,
            metadata=dict(metadata),
        )
        self._cached_signature = signature
        self._cached_calibration = stored
        return stored

    def install(
        self,
        objective_name: str,
        frames: Sequence[QImage],
        *,
        blur_radius_px: int | None = None,
        max_gain: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StoredFlatFieldCalibration:
        """Persist a new profile and atomically make it the active objective profile."""

        objective = _safe_objective_name(objective_name)
        reference = median_flat_field_reference(frames)
        selected_blur_radius, selected_max_gain = _profile_parameters(
            reference,
            blur_radius_px=blur_radius_px,
            max_gain=max_gain,
        )
        profile = build_flat_field_profile(
            reference,
            blur_radius_px=selected_blur_radius,
            max_gain=selected_max_gain,
            source=f"{objective} flat-field",
        )
        current_manifest = self.current_manifest_path(objective)
        profile_dir = self._new_profile_directory(objective)
        reference_image = profile_dir / "reference.png"
        profile_manifest = profile_dir / "profile.json"
        metadata_dict = dict(metadata or {})
        manifest = _manifest_payload(
            objective=objective,
            reference_image=reference_image.relative_to(current_manifest.parent),
            profile=profile,
            metadata=metadata_dict,
        )

        profile_dir.mkdir(parents=True, exist_ok=False)
        if not reference.save(str(reference_image), "PNG"):
            raise RuntimeError(f"Unable to save flat-field reference to {reference_image}.")
        _write_json(profile_manifest, manifest)
        temporary_manifest = current_manifest.with_name(
            f".{current_manifest.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            _write_json(temporary_manifest, manifest)
            temporary_manifest.replace(current_manifest)
        finally:
            if temporary_manifest.exists():
                temporary_manifest.unlink()

        stored = StoredFlatFieldCalibration(
            objective_name=objective,
            current_manifest=current_manifest,
            reference_image=reference_image,
            profile=profile,
            metadata=metadata_dict,
        )
        self._cached_signature = (
            str(current_manifest),
            *_file_signature(current_manifest),
            str(reference_image),
            *_file_signature(reference_image),
        )
        self._cached_calibration = stored
        return stored

    def _objective_directory(self, objective_name: str) -> Path:
        return self._config_dir / "calibrations" / "flat-field" / objective_name

    def _new_profile_directory(self, objective_name: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self._objective_directory(objective_name) / "profiles" / f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _safe_objective_name(value: str) -> str:
    objective = str(value or "")
    path = Path(objective)
    device_stem = objective.split(".", 1)[0].casefold()
    if (
        not objective
        or objective != objective.strip()
        or objective in {".", ".."}
        or objective.endswith((".", " "))
        or path.is_absolute()
        or path.name != objective
        or "/" in objective
        or "\\" in objective
        or _SAFE_OBJECTIVE_COMPONENT.fullmatch(objective) is None
        or device_stem in _WINDOWS_RESERVED_DEVICE_NAMES
    ):
        raise ValueError("objective name must be a single safe path component")
    return objective


def _reference_path(payload: Mapping[str, object], base_dir: Path) -> Path:
    value = str(payload.get("reference_image") or "").strip()
    if not value:
        raise ValueError("flat-field reference_image is missing")
    reference = Path(value).expanduser()
    return reference if reference.is_absolute() else base_dir / reference


def _profile_parameters(
    reference: QImage,
    *,
    blur_radius_px: int | None,
    max_gain: float | None,
) -> tuple[int, float]:
    if blur_radius_px is None:
        short_side = min(int(reference.width()), int(reference.height()))
        radius = int(round(float(short_side) / _AUTO_BLUR_SHORT_SIDE_DIVISOR))
        radius = min(
            _AUTO_BLUR_RADIUS_MAX_PX,
            max(_AUTO_BLUR_RADIUS_MIN_PX, radius),
        )
        if radius % 2 == 0:
            radius += 1
    else:
        radius = int(blur_radius_px)

    if max_gain is not None:
        return radius, float(max_gain)

    import numpy as np

    provisional = build_flat_field_profile(
        reference,
        blur_radius_px=radius,
        max_gain=_AUTO_MAX_GAIN_CEILING,
        source="flat-field parameter selection",
    )
    illumination = provisional.illumination_rgb.astype(np.float32, copy=False)
    mean = np.asarray(provisional.mean_rgb, dtype=np.float32).reshape((1, 1, 3))
    required_gain = float(np.max(mean / np.maximum(illumination, 1.0)))
    selected_gain = math.ceil(required_gain * _AUTO_MAX_GAIN_MARGIN)
    return radius, min(
        _AUTO_MAX_GAIN_CEILING,
        max(_AUTO_MAX_GAIN_FLOOR, float(selected_gain)),
    )


def _manifest_payload(
    *,
    objective: str,
    reference_image: Path,
    profile: FlatFieldProfile,
    metadata: Mapping[str, object],
) -> dict[str, object]:
    return {
        "version": _MANIFEST_VERSION,
        "objective": objective,
        "reference_image": str(reference_image),
        "frame_size_px": list(profile.image_size_px),
        "blur_radius_px": profile.blur_radius_px,
        "max_gain": profile.max_gain,
        "coordinate_space": _COORDINATE_SPACE,
        "application_order": _APPLICATION_ORDER,
        "metadata": dict(metadata),
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_mtime_ns), int(stat.st_size)


__all__ = [
    "FlatFieldCalibrationStore",
    "StoredFlatFieldCalibration",
    "median_flat_field_reference",
]
