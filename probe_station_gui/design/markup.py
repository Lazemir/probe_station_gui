"""Immutable construction markup bound to one source design file."""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Collection, Mapping

from probe_station_gui.design.model import Point2D
from probe_station_gui.design.selection_geometry import SegmentGeometry


MARKUP_SCHEMA_VERSION = 1


class MarkupDecodeError(ValueError):
    """Raised when a persisted markup document is not trustworthy."""


@dataclass(frozen=True)
class SourceFingerprint:
    """Cheap identity check for source contents at a stable path."""

    size: int
    mtime_ns: int

    @classmethod
    def from_dict(cls, value: object) -> SourceFingerprint:
        mapping = _mapping(value, "source_fingerprint")
        size = _nonnegative_int(mapping.get("size"), "source_fingerprint.size")
        mtime_ns = _nonnegative_int(
            mapping.get("mtime_ns"),
            "source_fingerprint.mtime_ns",
        )
        return cls(size=size, mtime_ns=mtime_ns)

    def to_dict(self) -> dict[str, int]:
        return {"size": self.size, "mtime_ns": self.mtime_ns}


@dataclass(frozen=True)
class GuideSegment:
    """One finite construction segment with a stable ID."""

    id: str
    start: Point2D
    end: Point2D

    @classmethod
    def from_dict(cls, value: object) -> GuideSegment:
        mapping = _mapping(value, "guide")
        guide = cls(
            id=_nonempty_string(mapping.get("id"), "guide.id"),
            start=_point(mapping.get("start"), "guide.start"),
            end=_point(mapping.get("end"), "guide.end"),
        )
        _validate_guide(guide, MarkupDecodeError)
        return guide

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "start": [self.start[0], self.start[1]],
            "end": [self.end[0], self.end[1]],
        }

    def geometry(self) -> SegmentGeometry:
        return SegmentGeometry(self.start, self.end)

    def translated(
        self,
        dx: float,
        dy: float,
        *,
        guide_id: str | None = None,
    ) -> GuideSegment:
        geometry = self.geometry().translated(dx, dy)
        return GuideSegment(
            id=guide_id or uuid.uuid4().hex,
            start=geometry.start,
            end=geometry.end,
        )


@dataclass(frozen=True)
class MarkupDocument:
    """The complete app-owned markup layer for one GDS source path."""

    schema_version: ClassVar[int] = MARKUP_SCHEMA_VERSION

    source_path: str
    source_fingerprint: SourceFingerprint
    visible: bool = True
    guides: tuple[GuideSegment, ...] = ()

    @classmethod
    def empty(
        cls,
        source_path: str | os.PathLike[str],
        *,
        visible: bool = True,
    ) -> MarkupDocument:
        return cls(
            source_path=normalize_source_path(source_path),
            source_fingerprint=fingerprint_source(source_path),
            visible=bool(visible),
        )

    @classmethod
    def from_dict(cls, value: object) -> MarkupDocument:
        try:
            mapping = _mapping(value, "markup")
            schema_version = _nonnegative_int(
                mapping.get("schema_version"),
                "schema_version",
            )
            if schema_version != MARKUP_SCHEMA_VERSION:
                raise MarkupDecodeError(
                    f"Unsupported markup schema version {schema_version}."
                )
            source_path = normalize_source_path(
                _nonempty_string(mapping.get("source_path"), "source_path")
            )
            fingerprint = SourceFingerprint.from_dict(
                mapping.get("source_fingerprint")
            )
            visible = mapping.get("visible", True)
            if not isinstance(visible, bool):
                raise MarkupDecodeError("markup.visible must be a boolean.")
            raw_guides = mapping.get("guides")
            if not isinstance(raw_guides, list):
                raise MarkupDecodeError("markup.guides must be a list.")
            guides = tuple(GuideSegment.from_dict(item) for item in raw_guides)
            _validate_unique_ids(guides)
            return cls(
                source_path=source_path,
                source_fingerprint=fingerprint,
                visible=visible,
                guides=guides,
            )
        except MarkupDecodeError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise MarkupDecodeError("Markup document is invalid.") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": MARKUP_SCHEMA_VERSION,
            "source_path": self.source_path,
            "source_fingerprint": self.source_fingerprint.to_dict(),
            "visible": self.visible,
            "guides": [guide.to_dict() for guide in self.guides],
        }

    def append_guide(
        self,
        start: Point2D,
        end: Point2D,
        *,
        guide_id: str | None = None,
    ) -> MarkupDocument:
        candidate = GuideSegment(
            id=(guide_id.strip() if isinstance(guide_id, str) else uuid.uuid4().hex),
            start=_point_for_edit(start, "start"),
            end=_point_for_edit(end, "end"),
        )
        _validate_guide(candidate, ValueError)
        if any(guide.id == candidate.id for guide in self.guides):
            raise ValueError(f"Guide ID {candidate.id!r} already exists.")
        return replace(self, guides=(*self.guides, candidate))

    def append_guides(self, guides: Collection[GuideSegment]) -> MarkupDocument:
        combined = (*self.guides, *tuple(guides))
        for guide in combined:
            _validate_guide(guide, ValueError)
        try:
            _validate_unique_ids(combined)
        except MarkupDecodeError as exc:
            raise ValueError(str(exc)) from exc
        return replace(self, guides=combined)

    def remove_ids(self, guide_ids: Collection[str]) -> MarkupDocument:
        removed = frozenset(str(item) for item in guide_ids)
        return replace(
            self,
            guides=tuple(guide for guide in self.guides if guide.id not in removed),
        )

    def with_visibility(self, visible: bool) -> MarkupDocument:
        return replace(self, visible=bool(visible))

    def with_source_fingerprint(
        self,
        fingerprint: SourceFingerprint,
    ) -> MarkupDocument:
        if fingerprint.size < 0 or fingerprint.mtime_ns < 0:
            raise ValueError("Source fingerprint values must be nonnegative.")
        return replace(self, source_fingerprint=fingerprint)

    def matches_source(self, source_path: str | os.PathLike[str]) -> bool:
        return (
            self.source_path == normalize_source_path(source_path)
            and self.source_fingerprint == fingerprint_source(source_path)
        )


def normalize_source_path(source_path: str | os.PathLike[str]) -> str:
    """Return the stable absolute path used in sidecar identity and payloads."""

    raw_path = os.fspath(source_path).strip()
    if not raw_path:
        raise ValueError("Source path is empty.")
    absolute = Path(raw_path).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(absolute)))


def fingerprint_source(source_path: str | os.PathLike[str]) -> SourceFingerprint:
    stat = Path(source_path).stat()
    return SourceFingerprint(size=int(stat.st_size), mtime_ns=int(stat.st_mtime_ns))


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MarkupDecodeError(f"{label} must be an object.")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarkupDecodeError(f"{label} must be a nonempty string.")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarkupDecodeError(f"{label} must be a nonnegative integer.")
    return value


def _point(value: object, label: str) -> Point2D:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise MarkupDecodeError(f"{label} must contain two numbers.")
    try:
        point = (float(value[0]), float(value[1]))
    except (TypeError, ValueError) as exc:
        raise MarkupDecodeError(f"{label} must contain two numbers.") from exc
    if not all(math.isfinite(component) for component in point):
        raise MarkupDecodeError(f"{label} must contain finite numbers.")
    return point


def _point_for_edit(value: object, label: str) -> Point2D:
    try:
        return _point(value, label)
    except MarkupDecodeError as exc:
        raise ValueError(str(exc)) from exc


def _validate_guide(
    guide: GuideSegment,
    error_type: type[ValueError],
) -> None:
    if not isinstance(guide.id, str) or not guide.id.strip():
        raise error_type("Guide ID must be nonempty.")
    if not all(
        math.isfinite(component)
        for point in (guide.start, guide.end)
        for component in point
    ):
        raise error_type("Guide coordinates must be finite.")
    if guide.start == guide.end:
        raise error_type("Guide endpoints must be different.")


def _validate_unique_ids(guides: Collection[GuideSegment]) -> None:
    ids = [guide.id for guide in guides]
    if len(ids) != len(set(ids)):
        raise MarkupDecodeError("Markup contains duplicate guide IDs.")


__all__ = [
    "GuideSegment",
    "MARKUP_SCHEMA_VERSION",
    "MarkupDecodeError",
    "MarkupDocument",
    "SourceFingerprint",
    "fingerprint_source",
    "normalize_source_path",
]
