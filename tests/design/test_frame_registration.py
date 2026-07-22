from __future__ import annotations

from pathlib import Path

import numpy as np

from probe_station_gui.coordinates.model import ReadinessStatus
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    commit_xyb_registration,
    design_frame_for_loaded_document,
    find_equivalent_migrated_frame,
    migrate_legacy_design_state,
    new_design_frame_draft,
)
from probe_station_gui.design.model import DesignDocument


def _document(tmp_path: Path, *, content: bytes = b"gds-a") -> DesignDocument:
    path = tmp_path / "chip.gds"
    path.write_bytes(content)
    return DesignDocument(
        path=path,
        library=object(),
        top_cell=object(),
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 1.0, 1.0),
        polygons_by_layer={
            (1, 0): (np.asarray([[0.0, 0.0], [1.0, 0.0]]),),
        },
        visible_layers=frozenset({(1, 0)}),
    )


def test_same_design_can_have_two_independent_registration_ids(tmp_path: Path) -> None:
    document = _document(tmp_path)
    registry = CoordinateFrameRegistry()

    first = registry.add(new_design_frame_draft(document, existing_names=()))
    second = registry.add(
        new_design_frame_draft(document, existing_names=(first.name,))
    )

    assert first.frame_id != second.frame_id
    assert first.name == document.path.stem
    assert second.name == f"{document.path.stem} (2)"
    assert all(
        state.status is ReadinessStatus.MISSING
        for state in first.readiness.values()
    )


def test_mark_commit_makes_only_xyb_ready_and_anchors_physical_b(
    tmp_path: Path,
) -> None:
    draft = new_design_frame_draft(_document(tmp_path), existing_names=())

    registered = commit_xyb_registration(
        draft,
        design_points=((0.0, 0.0), (1000.0, 0.0)),
        physical_machine_points=((3.0, 4.0), (3.0, 5.0)),
        physical_b_deg=12.0,
        pivot_machine_xy=(10.0, -2.0),
    )

    assert all(registered.readiness[axis].available for axis in ("X", "Y", "B"))
    assert registered.readiness["Z"].status is ReadinessStatus.MISSING
    assert registered.readiness["A"].status is ReadinessStatus.MISSING
    assert registered.transform is not None
    assert registered.transform.reference_b_deg == 12.0
    assert registered.transform.origin_xy_at_reference_b == (3.0, 4.0)
    assert registered.transform.xy_angle_at_reference_b_deg == 90.0
    assert registered.transform.b_zero_machine_deg == -78.0
    mapped = registered.transform.frame_xy_to_machine(
        (1.0, 0.0),
        machine_b_deg=12.0,
        pivot_machine_xy=(10.0, -2.0),
    )
    assert mapped == (3.0, 5.0)


def test_metadata_serializer_uses_exact_durable_fields(tmp_path: Path) -> None:
    draft = new_design_frame_draft(_document(tmp_path), existing_names=())
    metadata = DesignFrameMetadata.from_mapping(draft.metadata)

    assert tuple(metadata.to_dict()) == (
        "source_path",
        "source_size",
        "source_mtime_ns",
        "source_sha256",
        "top_cell_name",
        "design_unit_mm",
        "source_design_marks",
        "source_machine_marks",
        "check_design_marks",
        "check_machine_marks",
        "scale_ratio",
        "rms_residual_mm",
        "max_residual_mm",
        "machine_profile_id",
        "calibration_fingerprints",
    )
    assert metadata.source_sha256


def test_legacy_registration_migrates_without_z_or_a(tmp_path: Path) -> None:
    document = _document(tmp_path)
    legacy = {
        "source_design_marks": [[0.0, 0.0], [1000.0, 0.0]],
        "source_stage_marks": [[3.0, 4.0], [4.0, 4.0]],
        "check_design_marks": [],
        "check_stage_marks": [],
        "registration_valid": True,
    }

    migrated = migrate_legacy_design_state(
        legacy,
        design_document=document,
        physical_b_deg=0.0,
    )

    assert migrated is not None
    assert migrated.readiness["X"].available
    assert migrated.readiness["Y"].available
    assert migrated.readiness["B"].available
    assert not migrated.readiness["Z"].available
    assert not migrated.readiness["A"].available


def test_repeated_legacy_migration_reuses_equivalent_published_frame(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    legacy = {
        "source_design_marks": [[0.0, 0.0], [1000.0, 0.0]],
        "source_stage_marks": [[3.0, 4.0], [4.0, 4.0]],
        "registration_valid": True,
    }
    published = migrate_legacy_design_state(
        legacy,
        design_document=document,
        physical_b_deg=7.0,
    )
    repeated = migrate_legacy_design_state(
        legacy,
        design_document=document,
        physical_b_deg=7.0,
    )

    assert published is not None
    assert repeated is not None
    assert find_equivalent_migrated_frame((published,), repeated) is published


def test_changed_design_file_marks_frame_stale_instead_of_deleting_it(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    registered = commit_xyb_registration(
        new_design_frame_draft(document, existing_names=()),
        design_points=((0.0, 0.0), (1000.0, 0.0)),
        physical_machine_points=((3.0, 4.0), (4.0, 4.0)),
        physical_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )
    document.path.write_bytes(b"changed-design")
    changed_document = _document(tmp_path, content=b"changed-design")

    result = design_frame_for_loaded_document(registered, changed_document)

    assert result.frame_id == registered.frame_id
    assert not result.readiness["X"].available
    assert "changed" in result.readiness["X"].reason.lower()
