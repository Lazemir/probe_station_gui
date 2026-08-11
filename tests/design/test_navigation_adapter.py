from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from probe_station_gui.coordinates.coordinator_model import DesignSessionCheckpoint
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.coordinates.coordinator_model import RegistrationWorkflowSnapshot
from probe_station_gui.coordinates.provenance import (
    RUNTIME_PROVENANCE_REASON,
    RUNTIME_PROVENANCE_STATUS,
)
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    commit_xyb_registration,
    new_design_frame_draft,
)
from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    DesignRegistration,
    MeasurementTarget,
)
from probe_station_gui.design.navigation_adapter import (
    activate_design_frame_for_document,
    apply_loaded_design_document,
    add_route_array_points,
    design_panel_presentation,
    design_position_presentation,
    document_with_persisted_design_view,
    parse_persisted_visible_layers,
    plan_design_coordinate_move,
    plan_design_target_move,
    prepare_design_frame_activation,
    prepare_design_frame_publication,
    prepare_persisted_design_restore,
    select_route_point,
)
from probe_station_gui.design.session import DesignSession


class _FakeCell:
    def __init__(self, name: str, polygons_by_spec: dict[tuple[int, int], list[np.ndarray]]) -> None:
        self.name = name
        self._polygons_by_spec = polygons_by_spec

    def get_polygons(self, *args: object, **kwargs: object) -> dict[tuple[int, int], list[np.ndarray]]:
        return self._polygons_by_spec


class _FakeLibrary:
    def __init__(self, cells: list[_FakeCell]) -> None:
        self.cells = cells
        self.unit = 1e-6
        self.precision = 1e-9

    def top_level(self) -> list[_FakeCell]:
        return [self.cells[0]]


def _make_document(tmp_path: Path) -> DesignDocument:
    design_path = tmp_path / "sample.gds"
    design_path.write_bytes(b"sample-design")
    top = _FakeCell(
        "TOP",
        {
            (1, 0): [
                np.asarray([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]])
            ],
            (2, 0): [
                np.asarray([[10.0, 10.0], [20.0, 10.0], [20.0, 20.0]])
            ],
        },
    )
    alt = _FakeCell(
        "ALT",
        {
            (3, 1): [
                np.asarray([[0.0, 0.0], [30.0, 0.0], [30.0, 10.0]])
            ],
            (4, 2): [
                np.asarray([[1.0, 1.0], [5.0, 1.0], [5.0, 5.0]])
            ],
        },
    )
    return DesignDocument._from_components(
        path=design_path,
        library=_FakeLibrary([top, alt]),
        top_cell_name="TOP",
    )


def test_parse_persisted_visible_layers_ignores_malformed_entries() -> None:
    assert parse_persisted_visible_layers(
        [[1, 0], ["2", "3"], ["bad", 0], [4], [5, 6, 7], "x", None]
    ) == {(1, 0), (2, 3)}


def test_document_with_persisted_design_view_applies_cell_rotation_and_layers(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)

    restored = document_with_persisted_design_view(
        document,
        {
            "top_cell_name": "ALT",
            "rotation_quarter_turns": "1",
            "visible_layers": [[4, 2], [999, 0]],
        },
    )

    assert restored.top_cell_name == "ALT"
    assert restored.rotation_quarter_turns == 1
    assert restored.visible_layers == frozenset({(4, 2)})


def test_persisted_restore_rejects_missing_or_changed_xy_and_accepts_current_file(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    stat = document.path.stat()
    state = {
        "document_path": str(document.path),
        "document_size": stat.st_size,
        "document_mtime_ns": stat.st_mtime_ns,
    }

    missing = prepare_persisted_design_restore(
        None,
        actual_position=(1.0, 2.0, 3.0),
        document_loaded=False,
    )
    changed_xy = prepare_persisted_design_restore(
        state,
        expected_position=(1.0, 2.0, 3.0),
        actual_position=(9.0, 2.0, 3.0),
        document_loaded=False,
    )
    accepted = prepare_persisted_design_restore(
        state,
        expected_position=(1.0, 2.0, 3.0),
        actual_position=(1.0, 2.0, 3.0),
        document_loaded=False,
    )

    assert not missing.should_start_load
    assert not changed_xy.should_start_load
    assert changed_xy.clear_cached_design
    assert changed_xy.axes_to_mark_unhomed == {"X"}
    assert accepted.should_start_load
    assert accepted.design_path == str(document.path)
    assert not accepted.clear_cached_design


def test_persisted_restore_z_mismatch_requests_unhome_without_status(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    stat = document.path.stat()
    state = {
        "document_path": str(document.path),
        "document_size": stat.st_size,
        "document_mtime_ns": stat.st_mtime_ns,
    }

    decision = prepare_persisted_design_restore(
        state,
        expected_position=(1.0, 2.0, 3.0),
        actual_position=(1.0, 2.0, 9.0),
        document_loaded=False,
    )

    assert decision.should_start_load
    assert decision.design_path == str(document.path)
    assert decision.axes_to_mark_unhomed == {"Z"}
    assert decision.status_message is None
    assert not decision.clear_cached_design


def test_design_load_success_plan_uses_current_route_point_camera_center(tmp_path: Path) -> None:
    document = _make_document(tmp_path)
    session = DesignSession()
    session.load_document(document)
    point = session.add_route_point((12.0, 34.0))
    assert session.route is not None
    session.route.save(tmp_path / "route.probe-route.json")
    session.selected_route_point_index = 0
    state = session.export_persisted_state()
    assert state is not None

    restored_session = DesignSession()
    plan = apply_loaded_design_document(restored_session, document, state)

    assert plan.last_selected_design_point == point.camera_center
    assert plan.status_message == "Loaded design 'sample.gds' (TOP)."


def test_route_array_add_creates_route_and_selects_last_added_point(tmp_path: Path) -> None:
    session = DesignSession()
    session.load_document(_make_document(tmp_path))

    plan = add_route_array_points(
        session,
        1.0,
        2.0,
        10.0,
        0.0,
        2,
        0.0,
        10.0,
        2,
        False,
        False,
    )

    assert session.route is not None
    assert len(session.route.points) == 4
    assert session.selected_route_point_index == 3
    assert plan.last_selected_design_point == (11.0, 12.0)
    assert plan.status_message.startswith("Added 4 array route points")


def test_route_point_selection_is_bounds_checked_and_invalid_clears_selection(
    tmp_path: Path,
) -> None:
    session = DesignSession()
    session.load_document(_make_document(tmp_path))
    session.add_route_point((1.0, 2.0))
    session.add_route_point((3.0, 4.0))

    valid = select_route_point(session, 1)
    invalid = select_route_point(session, 99)

    assert valid.last_selected_design_point == (3.0, 4.0)
    assert invalid.last_selected_design_point is None
    assert session.selected_route_point_index == -1


def test_design_target_move_plans_unknown_missing_registration_and_accepted(
    tmp_path: Path,
) -> None:
    session = DesignSession()
    session.load_document(_make_document(tmp_path))
    session.set_targets([MeasurementTarget("a", "A", (10.0, 20.0))])

    unknown = plan_design_target_move(session, "missing", None)
    unregistered = plan_design_target_move(session, "a", None)

    session.registration = DesignRegistration.from_marks(
        [(0.0, 0.0), (10.0, 0.0)],
        [(1.0, 2.0), (21.0, 2.0)],
    )
    accepted = plan_design_target_move(session, "a", session.stage_from_design((10.0, 20.0)))

    assert not unknown.accepted
    assert unknown.status_message == "Unknown target 'missing'."
    assert not unregistered.accepted
    assert unregistered.status_message == "Design registration is required before moving to a target."
    assert accepted.accepted
    assert accepted.stage_xy == (16.0, 22.0)


def test_design_coordinate_move_plans_unloaded_busy_unregistered_and_accepted(
    tmp_path: Path,
) -> None:
    assert not plan_design_coordinate_move(False, False, (1.0, 2.0), None, "design").accepted

    busy = plan_design_coordinate_move(True, True, (1.0, 2.0), (3.0, 4.0), "design")
    missing_registration = plan_design_coordinate_move(True, False, (1.0, 2.0), None, "design")
    accepted = plan_design_coordinate_move(True, False, (1.0, 2.0), (3.0, 4.0), "design")

    assert busy.status_message == "Stage is busy. Ignoring design move request."
    assert missing_registration.status_message == "Design click-to-move requires completed registration."
    assert accepted.accepted
    assert accepted.last_selected_design_point == (1.0, 2.0)
    assert accepted.pending_planned_move_target_xy == (3.0, 4.0)


def test_panel_and_position_presentations_include_navigation_state(tmp_path: Path) -> None:
    session = DesignSession()
    document = _make_document(tmp_path)
    session.load_document(document)
    session.set_targets([MeasurementTarget("a", "A", (10.0, 20.0))])
    session.add_route_point((1.0, 2.0))
    session.source_design_marks = [(0.0, 0.0), None]
    session.check_design_marks = [(5.0, 6.0)]
    registration = RegistrationWorkflowSnapshot(
        source_design_marks=((0.0, 0.0),),
        check_design_marks=((5.0, 6.0),),
    )

    panel = design_panel_presentation(
        session,
        registration,
        route_running=True,
        pending_alignment_preparation=True,
        design_snap_enabled=False,
    )
    position = design_position_presentation(
        session,
        registration,
        stage_xy=(7.0, 8.0),
        design_xy=(9.0, 10.0),
        fov_design_size=(11.0, 12.0),
        last_selected_design_point=(1.0, 2.0),
    )

    assert panel.selected_target_id == "a"
    assert panel.selected_route_point_index == 0
    assert panel.route_measurement_running
    assert panel.calibration_prompt == "Calibration step 4/4: chip rotation is in progress."
    assert position.current_design_position == (9.0, 10.0)
    assert position.fov_design_size == (11.0, 12.0)
    assert position.selected_design_point == (1.0, 2.0)
    assert position.source_design_marks == ((0.0, 0.0),)
    assert position.check_design_marks == ((5.0, 6.0),)


def test_loaded_design_links_requested_existing_frame_and_new_registration_is_independent(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    first = registry.add(new_design_frame_draft(document, existing_names=()))
    second = registry.add(
        new_design_frame_draft(document, existing_names=(first.name,))
    )
    session = DesignSession(document=document)

    linked = activate_design_frame_for_document(
        session,
        registry,
        document,
        requested_frame_id=first.frame_id,
    )
    created = activate_design_frame_for_document(
        session,
        registry,
        document,
        create_new=True,
    )

    assert linked.record.frame_id == first.frame_id
    assert linked.created is False
    assert created.created is True
    assert created.record.frame_id not in {first.frame_id, second.frame_id}
    assert created.record.name == f"{document.path.stem} (3)"
    assert session.active_frame_id == created.record.frame_id


def test_design_frame_publication_is_prepared_without_mutating_live_session(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    draft = new_design_frame_draft(document, existing_names=())
    committed = replace(draft, name="registered", version=1)
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    checkpoint = DesignSessionCheckpoint.capture(session)

    publication = prepare_design_frame_publication(
        session,
        (draft,),
        committed,
        previous_record=draft,
        machine_point_for_navigation=lambda point: point,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
        success_message="saved",
        success_duration_ms=7000,
    )

    assert DesignSessionCheckpoint.capture(session) == checkpoint
    assert publication.records == (committed,)
    assert publication.previous_record == draft
    assert publication.previous_session == checkpoint
    assert publication.proposed_session_link is not None
    assert publication.proposed_session_link.frame_id == committed.frame_id
    assert publication.success_notice is not None
    assert publication.success_notice.message == "saved"


def test_design_frame_activation_is_prepared_on_detached_owners(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = registry.add(new_design_frame_draft(document, existing_names=()))
    session = DesignSession(document=document)
    session.link_active_frame(draft)
    registry_before = registry.snapshot()
    session_before = DesignSessionCheckpoint.capture(session)

    prepared = prepare_design_frame_activation(
        session,
        registry,
        document,
        requested_frame_id=draft.frame_id,
        machine_point_for_navigation=lambda point: point,
        machine_b_deg=0.0,
        pivot_machine_xy=(0.0, 0.0),
    )

    assert prepared.activation.record.frame_id == draft.frame_id
    assert prepared.projection.frame_id == draft.frame_id
    assert registry.snapshot() == registry_before
    assert DesignSessionCheckpoint.capture(session) == session_before


def test_requested_frame_with_blocked_provenance_cannot_link(
    tmp_path: Path,
) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    draft = new_design_frame_draft(document, existing_names=())
    blocked = registry.add(
        replace(
            draft,
            metadata={
                **draft.metadata,
                RUNTIME_PROVENANCE_STATUS: "blocked",
                RUNTIME_PROVENANCE_REASON: "Design source changed.",
            },
        )
    )
    session = DesignSession(document=document, active_frame_id=blocked.frame_id)

    with pytest.raises(DesignModelError, match="Design source changed"):
        activate_design_frame_for_document(
            session,
            registry,
            document,
            requested_frame_id=blocked.frame_id,
        )

    assert session.active_frame_id is None


def test_reconciled_frame_projection_failure_is_transactional(tmp_path: Path) -> None:
    document = _make_document(tmp_path)
    registry = CoordinateFrameRegistry()
    existing = registry.add(
        commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=((0.0, 0.0), (1000.0, 0.0)),
            physical_machine_points=((3.0, 4.0), (4.0, 4.0)),
            physical_b_deg=0.0,
            pivot_machine_xy=(0.0, 0.0),
        )
    )
    session = DesignSession(document=document)
    session.link_active_frame(existing)
    before = registry.snapshot()
    document.path.write_bytes(b"changed-design")
    changed_metadata = DesignFrameMetadata.from_document(document)

    with pytest.raises(DesignModelError, match="calibration unavailable"):
        activate_design_frame_for_document(
            session,
            registry,
            document,
            requested_frame_id=existing.frame_id,
            current_metadata=changed_metadata,
            machine_point_for_navigation=lambda _point: (_ for _ in ()).throw(
                RuntimeError("calibration unavailable")
            ),
        )

    assert registry.snapshot() == before
    assert session.active_frame_id == existing.frame_id


def test_requested_frame_for_other_top_cell_is_rejected_without_linking(
    tmp_path: Path,
) -> None:
    top_document = _make_document(tmp_path)
    alt_document = top_document.with_top_cell("ALT")
    registry = CoordinateFrameRegistry()
    top_frame = registry.add(new_design_frame_draft(top_document, existing_names=()))
    session = DesignSession(document=alt_document)

    with pytest.raises(DesignModelError, match="top cell"):
        activate_design_frame_for_document(
            session,
            registry,
            alt_document,
            requested_frame_id=top_frame.frame_id,
        )

    assert session.active_frame_id is None


def test_persisted_wrong_top_frame_id_is_cleared_when_activation_rejects_it(
    tmp_path: Path,
) -> None:
    top_document = _make_document(tmp_path)
    alt_document = top_document.with_top_cell("ALT")
    registry = CoordinateFrameRegistry()
    top_frame = registry.add(new_design_frame_draft(top_document, existing_names=()))
    session = DesignSession(document=alt_document, active_frame_id=top_frame.frame_id)

    with pytest.raises(DesignModelError, match="top cell"):
        activate_design_frame_for_document(
            session,
            registry,
            alt_document,
            requested_frame_id=top_frame.frame_id,
        )

    assert session.active_frame_id is None
    assert session.registration is None


def test_automatic_activation_selects_frame_matching_path_and_top_cell(
    tmp_path: Path,
) -> None:
    top_document = _make_document(tmp_path)
    alt_document = top_document.with_top_cell("ALT")
    registry = CoordinateFrameRegistry()
    top_frame = registry.add(new_design_frame_draft(top_document, existing_names=()))
    alt_frame = registry.add(
        new_design_frame_draft(
            alt_document,
            existing_names=(top_frame.name,),
        )
    )
    session = DesignSession(document=alt_document)

    activation = activate_design_frame_for_document(session, registry, alt_document)

    assert activation.record.frame_id == alt_frame.frame_id
    assert session.active_frame_id == alt_frame.frame_id
