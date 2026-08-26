from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.design_calibration import (
    design_calibration_fingerprints,
)
from probe_station_gui.coordinates.model import (
    PhysicalMachinePose,
)
from probe_station_gui.coordinates.coordinator_model import (
    CaptureMachinePoseIntent,
    CoordinateAdapterCompletion,
    CoordinateNotice,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    CustomSystemsRequest,
    DesignCalibrationObservation,
    FinishOperatorAlignmentUiEffect,
    LoadCoordinateFramesIntent,
    MachineProfileObservation,
    PersistCoordinateSelectionIntent,
    RestoreOperatorAlignmentUiEffect,
    SaveCoordinateFramesIntent,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
    CoordinateFrameStoreFailure,
)
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    MeasurementTarget,
)
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design import session_navigation
from probe_station_gui.settings.software_coordinates import (
    CustomFrameSettings,
    SoftwareCoordinateSettings,
)
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow
from probe_station_gui.views import main_window_design_workspace as design_workspace
from tests.coordinates.coordinator_registration_support import (
    _loaded_coordinator,
    _machine_snapshot,
)


def _multi_view_document(tmp_path: Path) -> DesignDocument:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"layout")
    return DesignDocument(
        path=source,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("OTHER", "TOP"),
        dbu=1e-3,
        user_unit=1e-6,
        polygons_by_layer={},
        visible_layers=frozenset({(1, 0)}),
        bounds=(0.0, 0.0, 1000.0, 1000.0),
        file_backed=True,
        available_layers=frozenset({(1, 0), (2, 0)}),
        cell_bounds={
            "TOP": (0.0, 0.0, 1000.0, 1000.0),
            "OTHER": (0.0, 0.0, 500.0, 500.0),
        },
        source_load_id="load-1",
    )


def test_coordinate_transition_submits_load_intent_to_store(monkeypatch) -> None:
    events: list[object] = []
    snapshot = CoordinateSystemSnapshot(False, (), None)
    owner = SimpleNamespace(
        _coordinate_frame_store=SimpleNamespace(
            load=lambda request_id, **kwargs: events.append(
                ("load", request_id, kwargs)
            )
        ),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, rendered: events.append(("render", rendered)),
    )

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            snapshot,
            intents=(LoadCoordinateFramesIntent(7, "rig-7"),),
        ),
    )

    assert events == [
        ("render", snapshot),
        ("load", 7, {"machine_profile_id": "rig-7"}),
    ]


def test_selection_persistence_submission_failure_is_reported_without_revert(
    monkeypatch,
) -> None:
    events: list[object] = []
    owner = SimpleNamespace(
        settings_manager=SimpleNamespace(
            set_software_coordinate_selection=lambda frame_id: (
                events.append(("settings", frame_id)) or object()
            )
        ),
        _software_coordinate_selection_store=SimpleNamespace(
            publish=lambda _snapshot: (_ for _ in ()).throw(RuntimeError("disk busy"))
        ),
        _show_status=lambda message, duration=0: events.append(
            ("status", message, duration)
        ),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, _snapshot: None,
    )

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None),
            intents=(PersistCoordinateSelectionIntent("machine"),),
        ),
    )

    assert events == [
        ("settings", "machine"),
        ("status", "Coordinate selection could not be saved.", 6000),
    ]


def test_coordinate_transition_submits_typed_machine_capture_intent(
    monkeypatch,
) -> None:
    events: list[object] = []
    owner = SimpleNamespace(
        stage_controller=SimpleNamespace(
            request_machine_coordinate_snapshot=lambda request_id, *, axes: (
                events.append(("capture", request_id, tuple(axes))) or True
            )
        ),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, rendered: events.append(("render", rendered)),
    )

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            CoordinateSystemSnapshot(True, (), None),
            intents=(CaptureMachinePoseIntent(-7),),
        ),
    )

    assert events == [
        ("render", CoordinateSystemSnapshot(True, (), None)),
        ("capture", -7, ("X", "Y", "B")),
    ]


def test_unexpected_pivot_read_failure_is_transient_but_validation_is_permanent() -> (
    None
):
    stage = SimpleNamespace(
        latest_machine_coordinate_snapshot=lambda: None,
        homed_axes=lambda: {"X", "Y"},
    )
    transient_owner = SimpleNamespace(
        stage_controller=stage,
        _rotation_geometry_snapshot=lambda: (_ for _ in ()).throw(
            RuntimeError("temporarily busy")
        ),
        _active_objective_xy_offset=lambda: (0.0, 0.0),
    )
    permanent_owner = SimpleNamespace(
        stage_controller=stage,
        _rotation_geometry_snapshot=lambda: (_ for _ in ()).throw(
            DesignModelError("Stored rotation pivot is invalid.")
        ),
        _active_objective_xy_offset=lambda: (0.0, 0.0),
    )

    transient = coordinate_flow.coordinate_authority_observation(transient_owner)
    permanent = coordinate_flow.coordinate_authority_observation(permanent_owner)

    assert transient.pivot_error == "temporarily busy"
    assert transient.pivot_error_permanent is False
    assert permanent.pivot_error_permanent is True


def test_authority_observation_prefers_live_motion_snapshot() -> None:
    stable = object()
    live = SimpleNamespace(physical_machine_pose=PhysicalMachinePose({"X": 7.0}))
    owner = SimpleNamespace(
        stage_controller=SimpleNamespace(
            latest_machine_coordinate_snapshot=lambda: stable,
            latest_motion_coordinate_snapshot=lambda: live,
            homed_axes=lambda: {"X", "Y"},
        ),
        _rotation_geometry_snapshot=lambda: SimpleNamespace(
            pivot_machine_xy=(0.0, 0.0)
        ),
        _active_objective_xy_offset=lambda: (0.0, 0.0),
    )

    observation = coordinate_flow.coordinate_authority_observation(owner)

    assert observation.machine_snapshot is live
    assert observation.physical_pose is live.physical_machine_pose


def test_coordinate_load_request_and_completion_delegate_to_coordinator(
    monkeypatch,
) -> None:
    calls: list[object] = []
    pending = CoordinateSystemSnapshot(False, (), None)
    loaded = CoordinateSystemSnapshot(True, (), CoordinateFrameDocument())

    class _Coordinator:
        current = pending

        def snapshot(self) -> CoordinateSystemSnapshot:
            return self.current

        def start(self, profile: MachineProfileObservation) -> CoordinateTransition:
            calls.append(("start", profile))
            return CoordinateTransition(
                pending,
                intents=(LoadCoordinateFramesIntent(8, profile.machine_profile_id),),
            )

        def complete(
            self, completion: CoordinateAdapterCompletion
        ) -> CoordinateTransition:
            calls.append(("complete", completion))
            self.current = loaded
            return CoordinateTransition(loaded)

        def synchronize_custom_systems(
            self,
            request: CustomSystemsRequest,
        ) -> CoordinateTransition:
            calls.append(("custom", request))
            return CoordinateTransition(loaded)

        def observe_design_calibrations(
            self,
            observation: DesignCalibrationObservation,
        ) -> CoordinateTransition:
            calls.append(("reconcile", observation))
            return CoordinateTransition(loaded)

    owner = SimpleNamespace(
        _coordinate_system_coordinator=_Coordinator(),
        _coordinate_frame_store=SimpleNamespace(
            load=lambda request_id, **kwargs: calls.append(
                ("store_load", request_id, kwargs)
            )
        ),
        settings_manager=SimpleNamespace(
            settings=SimpleNamespace(
                software_coordinates=object(),
                axis_calibrations={},
            )
        ),
        _current_machine_profile_id=lambda: "rig-8",
    )
    monkeypatch.setattr(
        coordinate_flow,
        "activate_current_design",
        lambda _owner: calls.append(("activate",)),
    )
    result = CoordinateFrameLoadResult(8, CoordinateFrameDocument())

    request_id = coordinate_flow.request_coordinate_frame_load(owner)
    coordinate_flow.handle_coordinate_frame_loaded(owner, result)

    assert request_id == 8
    assert calls == [
        ("start", MachineProfileObservation("rig-8")),
        ("store_load", 8, {"machine_profile_id": "rig-8"}),
        (
            "custom",
            CustomSystemsRequest(owner.settings_manager.settings.software_coordinates),
        ),
        ("complete", CoordinateAdapterCompletion(8, result)),
        (
            "reconcile",
            DesignCalibrationObservation(design_calibration_fingerprints({})),
        ),
        ("activate",),
    ]


def test_startup_custom_restore_is_materialized_before_selection_reconcile(
    monkeypatch,
) -> None:
    frame_id = str(uuid4())
    software_coordinates = SoftwareCoordinateSettings(
        custom_frames=(
            CustomFrameSettings(
                frame_id=frame_id,
                name="fixture",
                origin_x_mm=1.0,
                origin_y_mm=2.0,
                reference_b_deg=0.0,
                xy_angle_deg=0.0,
                b_zero_deg=0.0,
            ),
        ),
        last_selected_frame_id=frame_id,
    )
    coordinator = CoordinateSystemCoordinator(restore_frame_id=frame_id)
    load_intent = coordinator.start(MachineProfileObservation("rig-8")).intents[0]
    persisted: list[str] = []
    owner = SimpleNamespace(
        _coordinate_system_coordinator=coordinator,
        settings_manager=SimpleNamespace(
            settings=SimpleNamespace(
                software_coordinates=software_coordinates,
                axis_calibrations={},
            ),
            set_software_coordinate_selection=lambda selected: (
                persisted.append(selected) or object()
            ),
        ),
        _software_coordinate_selection_store=SimpleNamespace(
            publish=lambda _snapshot: None
        ),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, _snapshot: None,
    )
    monkeypatch.setattr(coordinate_flow, "activate_current_design", lambda _owner: None)

    coordinate_flow.handle_coordinate_frame_loaded(
        owner,
        CoordinateFrameLoadResult(
            load_intent.intent_id,
            CoordinateFrameDocument(),
        ),
    )

    snapshot = coordinator.snapshot()
    assert snapshot.selected_frame_id == frame_id
    assert snapshot.display_plan.selection_available is False
    assert persisted == []


def test_coordinate_transition_submits_save_and_presents_notices(monkeypatch) -> None:
    events: list[object] = []
    document = CoordinateFrameDocument()
    owner = SimpleNamespace(
        _coordinate_frame_store=SimpleNamespace(
            publish=lambda request_id, value: events.append(("save", request_id, value))
        ),
        _show_status=lambda message, duration=0: events.append(
            ("status", message, duration)
        ),
        _refresh_design_panel=lambda: events.append(("design_panel",)),
        _refresh_design_position=lambda: events.append(("design_position",)),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, rendered: events.append(("render", rendered)),
    )

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            CoordinateSystemSnapshot(True, (), document),
            intents=(SaveCoordinateFramesIntent(9, document),),
            notices=(CoordinateNotice("saved", duration_ms=7000),),
        ),
    )

    assert events == [
        ("render", CoordinateSystemSnapshot(True, (), document)),
        ("design_panel",),
        ("design_position",),
        ("status", "saved", 7000),
        ("save", 9, document),
    ]


def test_capture_notice_refreshes_design_views_but_stale_transition_does_not(
    monkeypatch,
) -> None:
    events: list[object] = []
    snapshot = CoordinateSystemSnapshot(True, (), None)
    owner = SimpleNamespace(
        _refresh_design_panel=lambda: events.append(("design_panel",)),
        _refresh_design_position=lambda: events.append(("design_position",)),
        _show_status=lambda message, duration=0: events.append(
            ("status", message, duration)
        ),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, rendered: events.append(("coordinate_display", rendered)),
    )

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            snapshot,
            notices=(
                CoordinateNotice(
                    "Stage source mark captured.",
                    duration_ms=4000,
                    code="registration_capture_updated",
                ),
            ),
        ),
    )
    coordinate_flow.apply_coordinate_transition(owner, CoordinateTransition(snapshot))

    assert events == [
        ("coordinate_display", snapshot),
        ("design_panel",),
        ("design_position",),
        ("status", "Stage source mark captured.", 4000),
        ("coordinate_display", snapshot),
    ]


def test_activation_view_changes_refresh_on_authority_block_and_recovery(
    monkeypatch,
) -> None:
    events: list[str] = []
    owner = SimpleNamespace(
        _refresh_design_panel=lambda: events.append("panel"),
        _refresh_design_position=lambda: events.append("position"),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, _snapshot: events.append("coordinates"),
    )
    snapshot = CoordinateSystemSnapshot(True, (), None)

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(snapshot, view_changed=True),
    )
    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(snapshot, view_changed=True),
    )

    assert events == [
        "coordinates",
        "panel",
        "position",
        "coordinates",
        "panel",
        "position",
    ]


def test_operator_alignment_effects_finish_and_restore_exact_ui_draft(
    monkeypatch,
) -> None:
    events: list[object] = []
    snapshot = CoordinateSystemSnapshot(True, (), None)
    owner = SimpleNamespace(
        _alignment_design_draft=((9.0, 9.0),),
        _alignment_stage_draft=[(8.0, 8.0)],
        _alignment_draft_fit_residuals=(1.0, 2.0),
        _set_design_snap_enabled=lambda enabled: events.append(("snap", enabled)),
        _finish_alignment_draft=lambda: events.append("finish"),
        _refresh_manual_alignment_ui=lambda: events.append("manual"),
        _update_stage_coordinate_apply_state=lambda: events.append("apply-state"),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, _snapshot: None,
    )

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            snapshot,
            ui_effects=(FinishOperatorAlignmentUiEffect(),),
        ),
    )
    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(snapshot),
    )

    assert events == [("snap", False), "finish"]

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            snapshot,
            ui_effects=(
                RestoreOperatorAlignmentUiEffect(
                    design_marks=((1.0, 2.0), (3.0, 4.0)),
                    stage_marks=((5.0, 6.0), None),
                ),
            ),
        ),
    )

    assert owner._alignment_design_draft == ((1.0, 2.0), (3.0, 4.0))
    assert owner._alignment_stage_draft == [(5.0, 6.0), None]
    assert owner._alignment_draft_fit_residuals is None
    assert events[-3:] == [("snap", True), "manual", "apply-state"]


def test_failed_design_frame_save_restores_complete_cross_document_workspace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    baseline_document = _multi_view_document(tmp_path)
    coordinator, _registry, _coordinate_session = _loaded_coordinator(
        baseline_document,
        None,
    )
    baseline_target = MeasurementTarget(
        "baseline-target",
        "Baseline target",
        (5.0, 10.0),
    )
    workspace = DesignSession(document=baseline_document)
    session_navigation.set_targets(workspace, [baseline_target])
    candidate = DesignSession()
    candidate.apply_state(workspace.snapshot_state())
    session_navigation.set_top_cell(candidate, "OTHER")
    session_navigation.set_visible_layers(candidate, {(2, 0)})
    later_target = MeasurementTarget(
        "later-target",
        "Later target",
        (25.0, 30.0),
    )
    saved: list[tuple[int, object]] = []
    refreshes: list[str] = []
    statuses: list[str] = []
    baseline_metadata = DesignFrameMetadata.from_document(baseline_document)
    baseline_markup = object()
    baseline_alignment = object()
    candidate_markup = object()
    owner = SimpleNamespace(
        _coordinate_system_coordinator=coordinator,
        _design_session=workspace,
        _active_design_frame_metadata=baseline_metadata,
        _design_markup=baseline_markup,
        _design_markup_direct_guide_ids=["baseline-guide"],
        _design_markup_pending_visibility="baseline-visibility",
        _last_selected_design_point=(1.0, 2.0),
        _pending_alignment_preparation=baseline_alignment,
        _rotation_geometry_snapshot=lambda: SimpleNamespace(
            pivot_machine_xy=(0.0, 0.0)
        ),
        _active_objective_xy_offset=lambda: (0.0, 0.0),
        _coordinate_frame_store=SimpleNamespace(
            publish=lambda intent_id, document: saved.append((intent_id, document))
        ),
        stage_controller=SimpleNamespace(
            axes_are_homed=lambda axes: bool(set(axes) <= {"X", "Y"}),
            latest_machine_coordinate_snapshot=lambda: _machine_snapshot(
                1.0,
                2.0,
                0.0,
            ),
        ),
        _refresh_design_panel=lambda: refreshes.append("panel"),
        _refresh_design_position=lambda: refreshes.append("position"),
        _show_status=lambda message, *_args: statuses.append(message),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, _snapshot: None,
    )
    candidate_metadata = replace(
        baseline_metadata,
        top_cell_name="OTHER",
    )
    workspace_after = design_workspace.capture_design_workspace(
        owner,
        session_state=candidate.snapshot_state(),
        frame_metadata=candidate_metadata,
        markup=candidate_markup,
        direct_guide_ids=("candidate-guide",),
        pending_visibility="candidate-visibility",
        last_selected_design_point=(3.0, 4.0),
        pending_alignment_preparation=None,
    )

    transition = coordinate_flow.activate_current_design(
        owner,
        session_state=candidate.snapshot_state(),
        frame_metadata=candidate_metadata,
        workspace_after=workspace_after,
    )
    assert transition is not None and transition.accepted
    session_navigation.set_targets(workspace, [later_target])
    assert workspace.document is candidate.document
    assert owner._design_markup is candidate_markup
    assert len(saved) == 1

    request_id, _document = saved[0]
    coordinate_flow.handle_coordinate_frame_failed(
        owner,
        CoordinateFrameStoreFailure(
            request_id,
            "save",
            "disk full",
        ),
    )

    assert workspace.document is baseline_document
    assert workspace.document.top_cell_name == "TOP"
    assert workspace.document.visible_layers == frozenset({(1, 0)})
    assert workspace.targets == [baseline_target]
    assert owner._active_design_frame_metadata is baseline_metadata
    assert owner._design_markup is baseline_markup
    assert owner._design_markup_direct_guide_ids == ["baseline-guide"]
    assert owner._design_markup_pending_visibility == "baseline-visibility"
    assert owner._last_selected_design_point == (1.0, 2.0)
    assert owner._pending_alignment_preparation is baseline_alignment
    assert statuses[-1] == "Design coordinate frames could not be saved."
    assert refreshes[-2:] == ["panel", "position"]
