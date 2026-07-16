from __future__ import annotations

import math
from pathlib import Path

import pytest

from probe_station_gui.design.markup import (
    MARKUP_SCHEMA_VERSION,
    GuideSegment,
    MarkupLoadChoice,
    MarkupDecodeError,
    MarkupDocument,
    SourceFingerprint,
    fingerprint_source,
    normalize_source_path,
    resolve_loaded_markup,
)


def _source(tmp_path: Path, content: bytes = b"gds") -> Path:
    path = tmp_path / "design" / "chip.gds"
    path.parent.mkdir()
    path.write_bytes(content)
    return path


def test_markup_round_trip_preserves_source_visibility_ids_and_points(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    document = MarkupDocument.empty(source, visible=False).append_guide(
        (1.0, 2.0),
        (3.0, 4.0),
        guide_id="guide-1",
    )

    restored = MarkupDocument.from_dict(document.to_dict())

    assert restored == document
    assert restored.schema_version == MARKUP_SCHEMA_VERSION
    assert restored.guides == (
        GuideSegment("guide-1", (1.0, 2.0), (3.0, 4.0)),
    )


def test_markup_mutations_return_new_documents_and_preserve_source(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    empty = MarkupDocument.empty(source)
    with_first = empty.append_guide((0.0, 0.0), (2.0, 0.0), guide_id="first")
    with_both = with_first.append_guide((0.0, 1.0), (2.0, 1.0), guide_id="second")
    hidden = with_both.with_visibility(False)
    removed = hidden.remove_ids({"first"})

    assert empty.guides == () and empty.visible
    assert [guide.id for guide in with_both.guides] == ["first", "second"]
    assert not hidden.visible
    assert removed.guides == (GuideSegment("second", (0.0, 1.0), (2.0, 1.0)),)
    assert removed.source_path == empty.source_path
    assert removed.source_fingerprint == empty.source_fingerprint


def test_generated_guide_ids_are_nonempty_and_unique(tmp_path: Path) -> None:
    document = MarkupDocument.empty(_source(tmp_path))

    first = document.append_guide((0.0, 0.0), (1.0, 0.0))
    second = first.append_guide((0.0, 1.0), (1.0, 1.0))

    assert first.guides[0].id
    assert second.guides[0].id != second.guides[1].id


def test_fingerprint_detects_size_or_mtime_change(tmp_path: Path) -> None:
    source = _source(tmp_path, b"old")
    original = fingerprint_source(source)

    source.write_bytes(b"new-layout")
    changed = fingerprint_source(source)

    assert changed != original
    assert changed.size == len(b"new-layout")
    assert changed.mtime_ns == source.stat().st_mtime_ns


def test_rebind_source_fingerprint_keeps_guides(tmp_path: Path) -> None:
    source = _source(tmp_path, b"old")
    document = MarkupDocument.empty(source).append_guide(
        (0.0, 0.0),
        (1.0, 1.0),
        guide_id="kept",
    )
    replacement = SourceFingerprint(size=99, mtime_ns=123)

    rebound = document.with_source_fingerprint(replacement)

    assert rebound.source_fingerprint == replacement
    assert rebound.guides == document.guides


def test_source_path_is_absolute_normalized_and_case_normalized(tmp_path: Path) -> None:
    source = _source(tmp_path)
    mixed = source.parent / "." / "CHIP.gds"

    assert normalize_source_path(mixed) == normalize_source_path(source)
    assert Path(normalize_source_path(source)).is_absolute()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"schema_version": 99},
        {
            "schema_version": MARKUP_SCHEMA_VERSION,
            "source_path": "C:/chip.gds",
            "source_fingerprint": {"size": 1, "mtime_ns": 2},
            "visible": True,
            "guides": "not-a-list",
        },
    ],
)
def test_invalid_schema_or_shape_is_rejected(payload: object) -> None:
    with pytest.raises(MarkupDecodeError):
        MarkupDocument.from_dict(payload)


def test_duplicate_guide_ids_are_rejected(tmp_path: Path) -> None:
    document = MarkupDocument.empty(_source(tmp_path)).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="duplicate",
    )
    payload = document.to_dict()
    payload["guides"] = [payload["guides"][0], payload["guides"][0]]

    with pytest.raises(MarkupDecodeError, match="duplicate"):
        MarkupDocument.from_dict(payload)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ((1.0, 1.0), (1.0, 1.0)),
        ((math.nan, 0.0), (1.0, 1.0)),
        ((0.0, 0.0), (math.inf, 1.0)),
    ],
)
def test_degenerate_or_nonfinite_guides_are_rejected(
    tmp_path: Path,
    start: tuple[float, float],
    end: tuple[float, float],
) -> None:
    document = MarkupDocument.empty(_source(tmp_path))

    with pytest.raises(ValueError):
        document.append_guide(start, end)


def test_fingerprint_rejects_negative_values() -> None:
    with pytest.raises(MarkupDecodeError):
        SourceFingerprint.from_dict({"size": -1, "mtime_ns": 2})


def test_matching_loaded_markup_is_accepted_without_a_choice(tmp_path: Path) -> None:
    source = _source(tmp_path)
    saved = MarkupDocument.empty(source).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="kept",
    )

    decision = resolve_loaded_markup(saved, source)

    assert decision.accepted
    assert not decision.needs_choice
    assert decision.document == saved


def test_changed_source_requires_explicit_markup_choice(tmp_path: Path) -> None:
    source = _source(tmp_path, b"old")
    saved = MarkupDocument.empty(source).append_guide(
        (0.0, 0.0),
        (1.0, 0.0),
        guide_id="old-guide",
    )
    source.write_bytes(b"changed-layout")

    unresolved = resolve_loaded_markup(saved, source)
    kept = resolve_loaded_markup(saved, source, MarkupLoadChoice.KEEP)
    emptied = resolve_loaded_markup(saved, source, MarkupLoadChoice.START_EMPTY)
    cancelled = resolve_loaded_markup(saved, source, MarkupLoadChoice.CANCEL)

    assert unresolved.needs_choice and not unresolved.accepted
    assert kept.accepted and kept.publish and kept.document is not None
    assert kept.document.guides == saved.guides
    assert kept.document.matches_source(source)
    assert emptied.accepted and emptied.delete_stored
    assert emptied.document == MarkupDocument.empty(source)
    assert not cancelled.accepted and cancelled.cancel_load
