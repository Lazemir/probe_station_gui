from __future__ import annotations

from pathlib import Path

import numpy as np

from probe_station_gui.design.model import (
    DesignDocument,
    DesignRegistration,
    MeasurementTarget,
)
from probe_station_gui.design.navigation_adapter import (
    apply_loaded_design_document,
    add_route_array_points,
    design_panel_presentation,
    design_position_presentation,
    document_with_persisted_design_view,
    parse_persisted_visible_layers,
    plan_design_coordinate_move,
    plan_design_target_move,
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

    panel = design_panel_presentation(
        session,
        route_running=True,
        pending_alignment_preparation=True,
        design_snap_enabled=False,
    )
    position = design_position_presentation(
        session,
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
