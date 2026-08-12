import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import numpy as np

from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.design.frame_registration import (
    commit_xyb_registration,
    new_design_frame_draft,
)
from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    MeasurementTarget,
)
from probe_station_gui.design.rigid_registration import DesignRegistration
from probe_station_gui.design.session import (
    DesignSession,
    MeasurementRoute,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeCell:
    def __init__(self, name, polygons_by_spec):
        self.name = name
        self._polygons_by_spec = polygons_by_spec

    def get_polygons(self, *args, **kwargs):
        return self._polygons_by_spec


class _FakeLibrary:
    def __init__(self, cells):
        self.cells = cells
        self.unit = 1e-6
        self.precision = 1e-9

    def top_level(self):
        return [self.cells[0]]


class DesignRegistrationTest(unittest.TestCase):
    def test_rigid_registration_roundtrip(self) -> None:
        registration = DesignRegistration.from_marks(
            [(0.0, 0.0), (10.0, 0.0)],
            [(1.0, 2.0), (21.0, 2.0)],
            check_design_marks=[(5.0, 5.0)],
            check_stage_marks=[(11.0, 7.0)],
        )

        self.assertTrue(registration.valid)
        self.assertEqual(registration.residual_summary.count, 1)
        self.assertAlmostEqual(registration.residual_summary.rms, 0.0)
        self.assertEqual(registration.design_to_stage((5.0, 5.0)), (11.0, 7.0))
        self.assertEqual(registration.stage_to_design((11.0, 7.0)), (5.0, 5.0))

    def test_registration_requires_two_marks(self) -> None:
        with self.assertRaises(DesignModelError):
            DesignRegistration.from_marks([(0.0, 0.0)], [(1.0, 1.0)])


class DesignDocumentTest(unittest.TestCase):
    def test_build_in_memory_document_and_switch_layers(self) -> None:
        cell_main = _FakeCell(
            "TOP",
            {
                (1, 0): [np.asarray([[0.0, 0.0], [10.0, 0.0], [10.0, 5.0]])],
                (2, 0): [np.asarray([[20.0, 20.0], [25.0, 20.0], [25.0, 30.0]])],
            },
        )
        cell_alt = _FakeCell(
            "ALT",
            {
                (3, 1): [np.asarray([[1.0, 1.0], [2.0, 1.0], [2.0, 2.0]])],
            },
        )
        fake_library = _FakeLibrary([cell_main, cell_alt])

        document = DesignDocument._from_components(
            path=REPO_ROOT / "tests" / "fixtures" / "synthetic.gds",
            library=fake_library,
            top_cell_name="TOP",
        )

        self.assertEqual(document.top_cell_name, "TOP")
        self.assertEqual(document.layer_keys(), ((1, 0), (2, 0)))
        self.assertEqual(document.bounds, (0.0, 0.0, 25.0, 30.0))
        self.assertEqual(document.visible_layers, frozenset({(1, 0), (2, 0)}))

        filtered = document.with_visible_layers({(2, 0)})
        self.assertEqual(filtered.visible_layers, frozenset({(2, 0)}))

        alt_document = document.with_top_cell("ALT")
        self.assertEqual(alt_document.top_cell_name, "ALT")
        self.assertEqual(alt_document.layer_keys(), ((3, 1),))

    def test_in_memory_document_uses_requested_cell_with_geometry(self) -> None:
        cell_empty = _FakeCell("EMPTY_TOP", {})
        cell_main = _FakeCell(
            "MAIN",
            {
                (1, 0): [np.asarray([[0.0, 0.0], [10.0, 0.0], [10.0, 5.0]])],
            },
        )
        fake_library = _FakeLibrary([cell_empty, cell_main])

        document = DesignDocument._from_components(
            path=REPO_ROOT / "tests" / "fixtures" / "synthetic.gds",
            library=fake_library,
            top_cell_name="MAIN",
        )

        self.assertEqual(document.top_cell_name, "MAIN")
        self.assertEqual(document.layer_keys(), ((1, 0),))

    def test_snap_point_info_prefers_vertex_or_segment_from_cached_geometry(
        self,
    ) -> None:
        document = DesignDocument(
            path=REPO_ROOT / "tests" / "fixtures" / "synthetic.gds",
            library=object(),
            top_cell=object(),
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 10.0, 10.0),
            polygons_by_layer={
                (1, 0): (
                    np.asarray([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]),
                )
            },
            visible_layers=frozenset({(1, 0)}),
            snap_vertices=np.asarray(
                [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
                dtype=float,
            ),
            snap_segment_starts=np.asarray(
                [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
                dtype=float,
            ),
            snap_segment_ends=np.asarray(
                [[10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
                dtype=float,
            ),
        )

        vertex_snap = document.snap_point_info((9.95, 9.95))
        segment_snap = document.snap_point_info((4.0, 1.0))

        self.assertEqual(vertex_snap.mode, "vertex")
        self.assertEqual(vertex_snap.point, (10.0, 10.0))
        self.assertEqual(segment_snap.mode, "segment")
        self.assertAlmostEqual(segment_snap.point[0], 4.0)
        self.assertAlmostEqual(segment_snap.point[1], 0.0)

    def test_rotate_document_by_quarter_turns(self) -> None:
        cell_main = _FakeCell(
            "TOP",
            {
                (1, 0): [
                    np.asarray(
                        [
                            [0.0, 0.0],
                            [10.0, 0.0],
                            [10.0, 20.0],
                            [0.0, 20.0],
                        ]
                    )
                ],
            },
        )
        document = DesignDocument._from_components(
            path=REPO_ROOT / "tests" / "fixtures" / "synthetic.gds",
            library=_FakeLibrary([cell_main]),
            top_cell_name="TOP",
        )

        rotated = document.with_rotation_delta(1)

        self.assertEqual(rotated.rotation_quarter_turns, 1)
        self.assertEqual(rotated.bounds, (-5.0, 5.0, 15.0, 15.0))
        self.assertEqual(rotated.rotate_vector((2.0, 3.0), 1), (-3.0, 2.0))
        self.assertEqual(document.rotate_point((10.0, 0.0), 1), (15.0, 15.0))

        restored = rotated.with_rotation_delta(-1)
        self.assertEqual(restored.rotation_quarter_turns, 0)
        self.assertEqual(restored.bounds, document.bounds)


class DesignSessionTest(unittest.TestCase):
    def _make_document(self) -> DesignDocument:
        return DesignDocument(
            path=REPO_ROOT / "tests" / "fixtures" / "synthetic.gds",
            library=object(),
            top_cell=object(),
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 100.0, 100.0),
            polygons_by_layer={(1, 0): (np.asarray([[0.0, 0.0], [1.0, 0.0]]),)},
            visible_layers=frozenset({(1, 0)}),
        )

    def test_snapshot_state_detaches_all_mutable_session_data(self) -> None:
        document = self._make_document()
        session = DesignSession(document=document)
        session.source_design_marks = ((0.0, 0.0), (10.0, 0.0))
        session.source_stage_marks = ((1.0, 2.0), (21.0, 2.0))
        session.check_design_marks = [(5.0, 0.0)]
        session.check_stage_marks = [(11.0, 2.0)]
        session._rebuild_registration()
        session.targets = [
            MeasurementTarget(
                id="target",
                label="Target",
                design_center=(3.0, 4.0),
                metadata={"groups": ["source"]},
            )
        ]
        session.selected_target_index = 0
        route = MeasurementRoute.default_for_document(document, name="Snapshot")
        route_point = route.add_point((8.0, 9.0))
        route.points[0] = replace(route_point, metadata={"tags": ["original"]})
        session.route = route
        session.selected_route_point_index = 0
        session.registration_status = "Ready."
        session.active_frame_id = "design:frame-1"
        session._runtime_blocked_persisted_state = {"route": {"point_ids": ["p001"]}}
        session._legacy_stage_coordinate_provenance = {"axes": ["X", "Y", "B"]}
        session._legacy_stage_coordinate_provenance_present = True

        state = session.snapshot_state()

        self.assertIs(state.document, document)
        self.assertEqual(state.registration, session.registration)
        self.assertEqual(state.source_design_marks, ((0.0, 0.0), (10.0, 0.0)))
        self.assertEqual(state.source_stage_marks, ((1.0, 2.0), (21.0, 2.0)))
        self.assertEqual(state.check_design_marks, ((5.0, 0.0),))
        self.assertEqual(state.check_stage_marks, ((11.0, 2.0),))
        self.assertEqual(state.targets[0].metadata, {"groups": ["source"]})
        self.assertEqual(state.selected_target_index, 0)
        self.assertEqual(state.route.points[0].metadata, {"tags": ["original"]})
        self.assertEqual(state.selected_route_point_index, 0)
        self.assertEqual(state.registration_status, "Ready.")
        self.assertEqual(state.active_frame_id, "design:frame-1")
        self.assertEqual(
            state._runtime_blocked_persisted_state,
            {"route": {"point_ids": ["p001"]}},
        )
        self.assertEqual(
            state._legacy_stage_coordinate_provenance,
            {"axes": ["X", "Y", "B"]},
        )
        self.assertTrue(state._legacy_stage_coordinate_provenance_present)
        with self.assertRaises(FrozenInstanceError):
            state.active_frame_id = "changed"  # type: ignore[misc]

        session.check_design_marks.append((6.0, 0.0))
        session.check_stage_marks.append((12.0, 2.0))
        session.targets[0].metadata["groups"].append("changed")
        session.route.points[0].metadata["tags"].append("changed")
        session._runtime_blocked_persisted_state["route"]["point_ids"].append("p002")
        session._legacy_stage_coordinate_provenance["axes"].append("A")

        self.assertEqual(state.check_design_marks, ((5.0, 0.0),))
        self.assertEqual(state.check_stage_marks, ((11.0, 2.0),))
        self.assertEqual(state.targets[0].metadata, {"groups": ["source"]})
        self.assertEqual(state.route.points[0].metadata, {"tags": ["original"]})
        self.assertEqual(
            state._runtime_blocked_persisted_state,
            {"route": {"point_ids": ["p001"]}},
        )
        self.assertEqual(
            state._legacy_stage_coordinate_provenance,
            {"axes": ["X", "Y", "B"]},
        )

    def test_apply_state_restores_every_field_without_replacing_session(self) -> None:
        document = self._make_document()
        source = DesignSession(document=document)
        source.source_design_marks = ((0.0, 0.0), (10.0, 0.0))
        source.source_stage_marks = ((1.0, 2.0), (21.0, 2.0))
        source.check_design_marks = [(5.0, 0.0)]
        source.check_stage_marks = [(11.0, 2.0)]
        source._rebuild_registration()
        source.targets = [
            MeasurementTarget(
                id="target",
                label="Target",
                design_center=(3.0, 4.0),
                metadata={"groups": ["source"]},
            )
        ]
        source.selected_target_index = 0
        route = MeasurementRoute.default_for_document(document, name="Applied")
        route_point = route.add_point((8.0, 9.0))
        route.points[0] = replace(route_point, metadata={"tags": ["original"]})
        source.route = route
        source.selected_route_point_index = 0
        source.registration_status = "Ready."
        source.active_frame_id = "design:frame-1"
        source._runtime_blocked_persisted_state = {"blocked": ["registration"]}
        source._legacy_stage_coordinate_provenance = {"axes": ["X", "Y", "B"]}
        source._legacy_stage_coordinate_provenance_present = True
        state = source.snapshot_state()
        session = DesignSession(
            check_design_marks=[(-1.0, -1.0)],
            registration_status="Previous state.",
        )
        observer = session
        session_identity = id(session)

        session.apply_state(state)

        self.assertIs(observer, session)
        self.assertEqual(id(session), session_identity)
        self.assertIs(session.document, document)
        self.assertEqual(session.registration, source.registration)
        self.assertEqual(session.source_design_marks, ((0.0, 0.0), (10.0, 0.0)))
        self.assertEqual(session.source_stage_marks, ((1.0, 2.0), (21.0, 2.0)))
        self.assertEqual(session.check_design_marks, [(5.0, 0.0)])
        self.assertEqual(session.check_stage_marks, [(11.0, 2.0)])
        self.assertEqual(session.targets[0].metadata, {"groups": ["source"]})
        self.assertEqual(session.selected_target_index, 0)
        self.assertEqual(session.route.points[0].metadata, {"tags": ["original"]})
        self.assertEqual(session.selected_route_point_index, 0)
        self.assertEqual(session.registration_status, "Ready.")
        self.assertEqual(session.active_frame_id, "design:frame-1")
        self.assertEqual(
            session._runtime_blocked_persisted_state,
            {"blocked": ["registration"]},
        )
        self.assertEqual(
            session._legacy_stage_coordinate_provenance,
            {"axes": ["X", "Y", "B"]},
        )
        self.assertTrue(session._legacy_stage_coordinate_provenance_present)
        self.assertIsNot(session.targets, state.targets)
        self.assertIsNot(session.route, state.route)
        self.assertIsNot(
            session._runtime_blocked_persisted_state,
            state._runtime_blocked_persisted_state,
        )
        self.assertIsNot(
            session._legacy_stage_coordinate_provenance,
            state._legacy_stage_coordinate_provenance,
        )

        state.targets[0].metadata["groups"].append("changed")
        state.route.points[0].metadata["tags"].append("changed")
        state._runtime_blocked_persisted_state["blocked"].append("changed")
        state._legacy_stage_coordinate_provenance["axes"].append("A")

        self.assertEqual(session.targets[0].metadata, {"groups": ["source"]})
        self.assertEqual(session.route.points[0].metadata, {"tags": ["original"]})
        self.assertEqual(
            session._runtime_blocked_persisted_state,
            {"blocked": ["registration"]},
        )
        self.assertEqual(
            session._legacy_stage_coordinate_provenance,
            {"axes": ["X", "Y", "B"]},
        )

    def test_apply_state_failure_leaves_existing_state_untouched(self) -> None:
        class ExplodingDeepcopy:
            def __deepcopy__(self, _memo):
                raise RuntimeError("copy failed")

        incoming = DesignSession(
            document=self._make_document(),
            source_design_marks=((0.0, 0.0),),
            check_design_marks=[(1.0, 1.0)],
            registration_status="Incoming state.",
        ).snapshot_state()
        failing_state = replace(
            incoming,
            _legacy_stage_coordinate_provenance=ExplodingDeepcopy(),
        )
        session = DesignSession(
            source_design_marks=((99.0, 98.0),),
            check_design_marks=[(97.0, 96.0)],
            targets=[
                MeasurementTarget(
                    id="kept",
                    label="Kept",
                    design_center=(95.0, 94.0),
                )
            ],
            registration_status="Existing state.",
        )
        session._runtime_blocked_persisted_state = {"kept": ["runtime"]}
        session._legacy_stage_coordinate_provenance = {"kept": ["provenance"]}
        session._legacy_stage_coordinate_provenance_present = True
        before = session.snapshot_state()
        check_marks = session.check_design_marks
        targets = session.targets
        runtime_state = session._runtime_blocked_persisted_state

        with self.assertRaisesRegex(RuntimeError, "copy failed"):
            session.apply_state(failing_state)

        self.assertEqual(session.snapshot_state(), before)
        self.assertIs(session.check_design_marks, check_marks)
        self.assertIs(session.targets, targets)
        self.assertIs(session._runtime_blocked_persisted_state, runtime_state)

    def test_registration_invalidation_marks_registration_stale(self) -> None:
        session = DesignSession()
        session.source_design_marks = [(0.0, 0.0), (10.0, 0.0)]
        session.source_stage_marks = [(1.0, 2.0), (21.0, 2.0)]
        session._rebuild_registration()

        self.assertIsNotNone(session.registration)
        self.assertTrue(session.registration.valid)

        session.invalidate_registration("Controller reset.")

        assert session.registration is not None
        self.assertFalse(session.registration.valid)
        self.assertEqual(session.registration_status, "Controller reset.")
        self.assertEqual(session.registration.stale_reason, "Controller reset.")

    def test_active_frame_link_is_session_state_and_unload_keeps_registry_record(
        self,
    ) -> None:
        document = self._make_document()
        registry = CoordinateFrameRegistry()
        frame = registry.add(
            commit_xyb_registration(
                new_design_frame_draft(document, existing_names=()),
                design_points=((0.0, 0.0), (1000.0, 0.0)),
                physical_machine_points=((1.0, 2.0), (2.0, 2.0)),
                physical_b_deg=5.0,
                pivot_machine_xy=(0.0, 0.0),
            )
        )
        session = DesignSession(document=document)

        session.link_active_frame(frame)
        state = session.export_persisted_state()
        session.unload_document()

        assert state is not None
        self.assertEqual(state["version"], 3)
        self.assertEqual(state["active_frame_id"], frame.frame_id)
        self.assertNotIn("source_stage_marks", state)
        self.assertIsNone(session.active_frame_id)
        self.assertEqual(registry.get(frame.frame_id), frame)

    def test_active_frame_marks_roundtrip_through_rotated_view_in_canonical_coordinates(
        self,
    ) -> None:
        document = replace(
            self._make_document(),
            bounds=(0.0, 0.0, 1.0, 0.0),
        )
        canonical_marks = ((0.0, 0.0), (1.0, 0.0))
        frame = commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=canonical_marks,
            physical_machine_points=((1.0, 2.0), (1.001, 2.0)),
            physical_b_deg=5.0,
            pivot_machine_xy=(0.0, 0.0),
        )
        rotated = document.with_rotation_delta(1)
        expected_marks = tuple(
            document.rotate_point(point, 1) for point in canonical_marks
        )
        session = DesignSession(document=rotated)
        session.link_active_frame(frame)
        state = session.export_persisted_state()
        restored = DesignSession()

        assert state is not None
        restored_document = document.with_rotation_delta(
            int(state["rotation_quarter_turns"])
        )
        restored.restore_persisted_state(restored_document, state)
        restored.link_active_frame(frame)

        self.assertEqual(tuple(session.source_design_marks_compact()), expected_marks)
        self.assertEqual(tuple(restored.source_design_marks_compact()), expected_marks)
        self.assertEqual(restored.active_frame_id, frame.frame_id)

    def test_active_frame_link_rejects_matching_file_with_wrong_top_cell(self) -> None:
        top_document = self._make_document()
        frame = new_design_frame_draft(top_document, existing_names=())
        session = DesignSession(
            document=replace(
                top_document,
                top_cell_name="ALT",
                cell_names=("TOP", "ALT"),
            )
        )

        with self.assertRaisesRegex(DesignModelError, "top cell"):
            session.link_active_frame(frame)

        self.assertIsNone(session.active_frame_id)

    def test_active_frame_preserves_measured_marks_at_current_physical_b(self) -> None:
        document = self._make_document()
        pivot = (10.0, -2.0)
        source_design = ((0.0, 0.0), (1000.0, 0.0), (0.0, 1000.0))
        source_machine = ((3.0, 4.0), (4.02, 4.01), (2.98, 5.04))
        frame = commit_xyb_registration(
            new_design_frame_draft(document, existing_names=()),
            design_points=source_design,
            physical_machine_points=source_machine,
            physical_b_deg=12.0,
            pivot_machine_xy=pivot,
        )
        durable = frame
        session = DesignSession(document=document)

        session.link_active_frame(
            frame,
            machine_b_deg=42.0,
            pivot_machine_xy=pivot,
        )

        angle_rad = np.deg2rad(30.0)
        rotation = np.asarray(
            (
                (np.cos(angle_rad), -np.sin(angle_rad)),
                (np.sin(angle_rad), np.cos(angle_rad)),
            )
        )
        expected = tuple(
            tuple(
                np.asarray(pivot) + rotation @ (np.asarray(point) - np.asarray(pivot))
            )
            for point in source_machine
        )
        np.testing.assert_allclose(session.source_stage_marks_compact(), expected)
        self.assertTrue(session.registration.valid)
        self.assertGreater(session.registration.source_residual_summary.max_error, 0.0)
        self.assertEqual(frame, durable)

    def test_persisted_state_roundtrip_restores_registration(self) -> None:
        document = self._make_document()
        session = DesignSession()
        session.load_document(document)
        session.source_design_marks = [(0.0, 0.0), (10.0, 0.0)]
        session.source_stage_marks = [(1.0, 2.0), (21.0, 2.0)]
        session.check_design_marks = [(5.0, 0.0)]
        session.check_stage_marks = [(11.0, 2.0)]
        session._rebuild_registration()

        state = session.export_persisted_state()
        assert state is not None
        restored = DesignSession()
        restored.restore_persisted_state(document, state)

        self.assertIs(restored.document, document)
        self.assertEqual(restored.source_design_marks, session.source_design_marks)
        self.assertEqual(restored.source_stage_marks, session.source_stage_marks)
        self.assertEqual(restored.check_design_marks, session.check_design_marks)
        self.assertEqual(restored.check_stage_marks, session.check_stage_marks)
        assert restored.registration is not None
        self.assertTrue(restored.registration.valid)
        self.assertEqual(restored.registration_status, session.registration_status)

    def test_persisted_state_preserves_stale_registration(self) -> None:
        document = self._make_document()
        session = DesignSession()
        session.load_document(document)
        session.source_design_marks = [(0.0, 0.0), (10.0, 0.0)]
        session.source_stage_marks = [(1.0, 2.0), (21.0, 2.0)]
        session._rebuild_registration()
        session.invalidate_registration("Controller reset.")

        state = session.export_persisted_state()
        assert state is not None
        restored = DesignSession()
        restored.restore_persisted_state(document, state)

        assert restored.registration is not None
        self.assertFalse(restored.registration.valid)
        self.assertEqual(restored.registration.stale_reason, "Controller reset.")
        self.assertEqual(restored.registration_status, "Controller reset.")

    def test_persisted_state_restores_saved_route(self) -> None:
        document = self._make_document()
        with tempfile.TemporaryDirectory() as tmpdir:
            route = MeasurementRoute.default_for_document(document, name="Array A")
            route.add_point((10.0, 20.0))
            route.add_point((30.0, 40.0))
            route_path = Path(tmpdir) / "array-a.probe-route.json"
            route.save(route_path)
            session = DesignSession()
            session.load_document(document)
            session.set_route(route)
            session.selected_route_point_index = 1

            state = session.export_persisted_state()
            assert state is not None
            restored = DesignSession()
            restored.restore_persisted_state(document, state)

        assert restored.route is not None
        self.assertEqual(restored.route.name, "Array A")
        self.assertEqual(len(restored.route.points), 2)
        self.assertEqual(restored.selected_route_point_index, 1)
        self.assertEqual(restored.current_route_point().camera_center, (30.0, 40.0))

    def test_persisted_state_ignores_missing_route_file(self) -> None:
        document = self._make_document()
        with tempfile.TemporaryDirectory() as tmpdir:
            route = MeasurementRoute.default_for_document(document, name="Array A")
            route.add_point((10.0, 20.0))
            route_path = Path(tmpdir) / "array-a.probe-route.json"
            route.save(route_path)
            session = DesignSession()
            session.load_document(document)
            session.set_route(route)
            state = session.export_persisted_state()
            assert state is not None
            route_path.unlink()
            restored = DesignSession()
            restored.restore_persisted_state(document, state)

        self.assertIs(restored.document, document)
        self.assertIsNone(restored.route)

    def test_target_navigation(self) -> None:
        session = DesignSession()
        session.set_targets(
            [
                MeasurementTarget(id="a", label="A", design_center=(0.0, 0.0)),
                MeasurementTarget(id="b", label="B", design_center=(1.0, 1.0)),
            ]
        )

        self.assertEqual(session.current_target().id, "a")
        self.assertEqual(session.select_next_target().id, "b")
        self.assertEqual(session.select_previous_target().id, "a")

    def test_prepare_and_apply_source_alignment(self) -> None:
        session = DesignSession()
        session.document = self._make_document()
        session.capture_source_pair((0.0, 0.0), (10.0, 10.0))
        session.capture_source_pair((1000.0, 0.0), (10.0, 11.0))

        preparation = session.prepare_source_alignment()

        self.assertAlmostEqual(preparation.rotation_deg, -90.0)
        self.assertAlmostEqual(preparation.design_distance_mm, 1.0)
        self.assertAlmostEqual(preparation.stage_distance_mm, 1.0)
        self.assertAlmostEqual(preparation.distance_ratio, 1.0)
        self.assertEqual(preparation.pivot_stage, (0.0, 0.0))
        self.assertAlmostEqual(preparation.stage_marks_after_rotation[0][0], 10.0)
        self.assertAlmostEqual(preparation.stage_marks_after_rotation[0][1], -10.0)
        self.assertAlmostEqual(preparation.stage_marks_after_rotation[1][0], 11.0)
        self.assertAlmostEqual(preparation.stage_marks_after_rotation[1][1], -10.0)

        session.apply_prepared_alignment(preparation)

        assert session.registration is not None
        self.assertTrue(session.registration.valid)
        mapped = session.stage_from_design((500.0, 0.0))
        assert mapped is not None
        self.assertAlmostEqual(mapped[0], 10.5)
        self.assertAlmostEqual(mapped[1], -10.0)

    def test_prepare_alignment_uses_all_pairs_and_rotates_every_stage_mark(
        self,
    ) -> None:
        session = DesignSession()
        session.document = self._make_document()
        design_marks = ((0.0, 0.0), (1000.0, 0.0), (0.0, 2000.0))
        stage_marks = ((10.0, 10.0), (10.0, 11.0), (8.0, 10.0))
        session.source_design_marks = design_marks
        session.source_stage_marks = stage_marks

        preparation = session.prepare_source_alignment()

        self.assertEqual(preparation.design_marks, design_marks)
        self.assertEqual(preparation.stage_marks_before_rotation, stage_marks)
        self.assertEqual(len(preparation.stage_marks_after_rotation), 3)
        self.assertAlmostEqual(preparation.rotation_deg, -90.0)
        self.assertAlmostEqual(preparation.rms_residual_mm, 0.0, places=12)
        self.assertAlmostEqual(preparation.max_residual_mm, 0.0, places=12)

        session.apply_prepared_alignment(preparation)

        self.assertIsInstance(session.source_design_marks, tuple)
        self.assertIsInstance(session.source_stage_marks, tuple)
        assert session.registration is not None
        self.assertEqual(session.registration.source_residual_summary.count, 3)
        self.assertAlmostEqual(session.registration.rotation_deg, 0.0, places=9)

    def test_persisted_state_v2_roundtrip_and_v1_slot_migration(self) -> None:
        document = self._make_document()
        session = DesignSession()
        session.load_document(document)
        session.source_design_marks = (
            (0.0, 0.0),
            (10.0, 0.0),
            (0.0, 10.0),
        )
        session.source_stage_marks = (
            (1.0, 2.0),
            (21.0, 2.0),
            (1.0, 22.0),
        )
        session._rebuild_registration()

        state = session.export_persisted_state()
        assert state is not None
        self.assertEqual(state["version"], 2)
        restored = DesignSession()
        restored.restore_persisted_state(document, state)
        self.assertEqual(restored.source_design_marks, session.source_design_marks)
        self.assertEqual(restored.source_stage_marks, session.source_stage_marks)

        legacy = dict(state)
        legacy["version"] = 1
        legacy["source_design_marks"] = [[0.0, 0.0], [10.0, 0.0]]
        legacy["source_stage_marks"] = [[1.0, 2.0], [21.0, 2.0]]
        migrated = DesignSession()
        migrated.restore_persisted_state(document, legacy)
        self.assertEqual(migrated.source_design_marks, ((0.0, 0.0), (10.0, 0.0)))
        self.assertEqual(migrated.source_stage_marks, ((1.0, 2.0), (21.0, 2.0)))

    def test_rotate_document_transforms_design_state(self) -> None:
        document = DesignDocument(
            path=REPO_ROOT / "tests" / "fixtures" / "synthetic.gds",
            library=object(),
            top_cell=object(),
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 100.0, 200.0),
            polygons_by_layer={
                (1, 0): (
                    np.asarray(
                        [
                            [0.0, 0.0],
                            [100.0, 0.0],
                            [100.0, 200.0],
                            [0.0, 200.0],
                        ]
                    ),
                )
            },
            visible_layers=frozenset({(1, 0)}),
        )
        session = DesignSession()
        session.load_document(document)
        session.source_design_marks = [(0.0, 0.0), (100.0, 0.0)]
        session.source_stage_marks = [(1.0, 2.0), (3.0, 2.0)]
        session._rebuild_registration()
        session.set_targets(
            [MeasurementTarget(id="target", label="Target", design_center=(20.0, 30.0))]
        )
        route = session.create_route()
        route.add_point((10.0, 20.0))
        route.set_needle_offsets(1.0, 2.0, -3.0, 4.0)
        old_target_stage = session.stage_from_design((20.0, 30.0))

        rotated = session.rotate_document(1)

        self.assertIs(session.document, rotated)
        self.assertEqual(rotated.bounds, (-50.0, 50.0, 150.0, 150.0))
        self.assertEqual(session.source_design_marks[0], (150.0, 50.0))
        self.assertEqual(session.source_design_marks[1], (150.0, 150.0))
        self.assertEqual(session.targets[0].design_center, (120.0, 70.0))
        assert session.route is not None
        self.assertEqual(session.route.design.bounds, rotated.bounds)
        self.assertEqual(session.route.points[0].camera_center, (130.0, 60.0))
        self.assertEqual(
            [(offset.dx, offset.dy) for offset in session.route.needle_offsets],
            [(-2.0, 1.0), (-4.0, -3.0)],
        )
        new_target_stage = session.stage_from_design(session.targets[0].design_center)
        assert old_target_stage is not None and new_target_stage is not None
        self.assertAlmostEqual(new_target_stage[0], old_target_stage[0])
        self.assertAlmostEqual(new_target_stage[1], old_target_stage[1])


if __name__ == "__main__":
    unittest.main()
