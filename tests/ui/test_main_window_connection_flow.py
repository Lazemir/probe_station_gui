from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    PhysicalMachinePose,
    ReadinessStatus,
)
from probe_station_gui.coordinates.coordinator_model import (
    CaptureMachinePoseIntent,
    CoordinateAdapterCompletion,
    CoordinateNotice,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    CustomSystemsRequest,
    DesignActivationRequest,
    FinishOperatorAlignmentUiEffect,
    LegacyDesignStateRewriteResult,
    LoadCoordinateFramesIntent,
    MachineProfileObservation,
    PersistCoordinateSelectionIntent,
    RegistrationWorkflowSnapshot,
    RestoreOperatorAlignmentUiEffect,
    RewriteLegacyDesignStateIntent,
    SaveCoordinateFramesIntent,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
    CoordinateFrameStoreFailure,
)
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    MeasurementTarget,
)
from probe_station_gui.design.session import DesignSession
from probe_station_gui.settings.software_coordinates import (
    CustomFrameSettings,
    SoftwareCoordinateSettings,
)
from probe_station_gui.views import main_window_connection_flow as connection_flow
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


def test_unexpected_pivot_read_failure_is_transient_but_validation_is_permanent() -> None:
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

        def complete(self, completion: CoordinateAdapterCompletion) -> CoordinateTransition:
            calls.append(("complete", completion))
            self.current = loaded
            return CoordinateTransition(loaded)

        def synchronize_custom_systems(
            self,
            request: CustomSystemsRequest,
        ) -> CoordinateTransition:
            calls.append(("custom", request))
            return CoordinateTransition(loaded)

    owner = SimpleNamespace(
        _coordinate_system_coordinator=_Coordinator(),
        _coordinate_frame_store=SimpleNamespace(
            load=lambda request_id, **kwargs: calls.append(
                ("store_load", request_id, kwargs)
            )
        ),
        settings_manager=SimpleNamespace(
            settings=SimpleNamespace(software_coordinates=object())
        ),
        _current_machine_profile_id=lambda: "rig-8",
        _reconcile_design_calibration_fingerprints=lambda: calls.append(
            ("reconcile",)
        ),
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
        ("custom", CustomSystemsRequest(owner.settings_manager.settings.software_coordinates)),
        ("complete", CoordinateAdapterCompletion(8, result)),
        ("reconcile",),
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
            settings=SimpleNamespace(software_coordinates=software_coordinates),
            set_software_coordinate_selection=lambda selected: (
                persisted.append(selected) or object()
            ),
        ),
        _software_coordinate_selection_store=SimpleNamespace(
            publish=lambda _snapshot: None
        ),
        _reconcile_design_calibration_fingerprints=lambda: None,
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
            publish=lambda request_id, value: events.append(
                ("save", request_id, value)
            )
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
    workspace.set_targets([baseline_target])
    candidate = DesignSession()
    candidate.apply_state(workspace.snapshot_state())
    candidate.set_top_cell("OTHER")
    candidate.set_visible_layers({(2, 0)})
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
            publish=lambda intent_id, document: saved.append(
                (intent_id, document)
            )
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
    workspace.set_targets([later_target])
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


class _Serial:
    def __init__(self, events: list[object], *, port: str = "COM7") -> None:
        self.events = events
        self.port = port
        self.baudrate = "115200"
        self.is_open = True

    def close(self) -> None:
        self.events.append(("serial_close", self.port))
        self.is_open = False


class _StageController:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.owner = None
        self._axis_rates = {"X": 1200.0}

    def set_serial(self, serial_connection: object | None) -> None:
        assert self.owner._controller_state_persistence_suspended is True
        port = getattr(serial_connection, "port", None)
        self.events.append(("stage_set_serial", port))

    def request_stop_oscillation(self) -> None:
        self.events.append(("stop_oscillation",))

    def force_jog_stop(self, *, timeout: float) -> None:
        self.events.append(("force_jog_stop", timeout))

    def request_startup_sync(self, **kwargs: object) -> None:
        self.events.append(("startup_sync", kwargs))

    def apply_axis_max_feedrates(self, rates: object) -> None:
        self.events.append(("apply_rates", rates))
        self._axis_rates = dict(rates)

    def axis_max_feedrates(self) -> dict[str, float]:
        return dict(self._axis_rates)


class _RestoreStageController:
    def __init__(self, events: list[object], *, current: bool) -> None:
        self.events = events
        self.current = current

    def cached_controller_session_is_current(self, cached_state: dict) -> bool:
        self.events.append(("session_current", cached_state))
        return self.current

    def clear_cached_controller_state(self) -> None:
        self.events.append(("clear_stage_cache",))

    def import_cached_controller_state(self, cached_state: dict) -> None:
        self.events.append(("import_cache", cached_state))

    def apply_axis_max_feedrates(self, rates: object) -> None:
        self.events.append(("apply_rates", rates))

    def axis_max_feedrates(self) -> dict[str, float]:
        return {"X": 900.0}


class _Settings:
    def __init__(self, events: list[object], cached_state: dict | None = None) -> None:
        self.events = events
        self.cached_state = cached_state or {"cached": True}
        self.serial_auto = False
        self.meter_auto = False

    def load_controller_state(self) -> dict:
        self.events.append(("load_controller_state",))
        return dict(self.cached_state)

    def serial_auto_connect_enabled(self) -> bool:
        return self.serial_auto

    def meter_auto_connect_enabled(self) -> bool:
        return self.meter_auto

    def save_serial_connection_state(
        self,
        connected: bool,
        *,
        port: str,
        baud_rate: int,
    ) -> None:
        self.events.append(("save_serial", connected, port, baud_rate))

    def save_meter_connection_state(
        self,
        connected: bool,
        *,
        meter_type: str,
        description: str,
    ) -> None:
        self.events.append(("save_meter", connected, meter_type, description))


class _Panel:
    def __init__(self, events: list[object], name: str) -> None:
        self.events = events
        self.name = name

    def set_serial(self, serial_connection: object | None) -> None:
        self.events.append((self.name, getattr(serial_connection, "port", None)))

    def handle_external_disconnect(self, *, auto_retry: bool) -> None:
        self.events.append((self.name, "external_disconnect", auto_retry))

    def auto_connect(self) -> None:
        self.events.append((self.name, "auto_connect"))

    def stop_jog(self) -> None:
        self.events.append(("stop_jog", "serial disconnect"))

    def set_current_stage_position(self, value: object) -> None:
        self.events.append((self.name, "stage_position", value))

    def set_current_needle_lowering(self, value: object) -> None:
        self.events.append((self.name, "needle_lowering", value))

    def set_axis_feedrate_limits(self, rates: dict[str, float]) -> None:
        self.events.append((self.name, "axis_rates", rates))


class _Dock:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt naming
        self.events.append(("dock_visible", visible))

    def raise_(self) -> None:
        self.events.append(("dock_raise",))

    def isFloating(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def activateWindow(self) -> None:  # noqa: N802 - Qt naming
        self.events.append(("dock_activate",))


def _owner(events: list[object]) -> SimpleNamespace:
    stage_controller = _StageController(events)
    owner = SimpleNamespace(
        serial_connection=None,
        serial_port_name="",
        serial_baud_rate=0,
        settings_manager=_Settings(events),
        stage_controller=stage_controller,
        joystick_panel=_Panel(events, "joystick"),
        joystick_dock=_Dock(events),
        serial_terminal_panel=_Panel(events, "terminal"),
        serial_connection_panel=_Panel(events, "serial_panel"),
        contact_calibration_window=_Panel(events, "contact"),
        oscillation_panel=SimpleNamespace(
            set_running=lambda running, axis: events.append(("oscillation", running, axis))
        ),
        _stage_unhomed_display_origins={"X": 1.0},
        _last_reported_b_position=12.0,
        _controller_state_persistence_suspended=False,
        _controller_reboot_recovery_scheduled=False,
        _pending_persisted_design_state={"design": True},
        _pending_persisted_design_position=(1.0, 2.0, 3.0),
        _pending_homing_axes=[],
        _homing_active_key=None,
        _stage_motion_axes=set(),
        _stage_motion_blink_dimmed=False,
        _stage_motion_blink_timer=SimpleNamespace(isActive=lambda: False),
    )
    stage_controller.owner = owner
    owner._restore_persisted_controller_state = (
        lambda state, **kwargs: events.append(("restore", state, kwargs))
    )
    owner._run_serial_startup_sync = lambda: events.append(("startup_callback",))
    owner._run_controller_reboot_recovery = lambda: events.append(
        ("reboot_recovery_callback",)
    )
    owner._refresh_design_position = lambda: events.append(("refresh_design",))
    owner._persist_serial_connection_state = (
        lambda connected: events.append(
            ("persist_serial_wrapper", connected, owner.serial_connection)
        )
    )
    owner._manual_jog_timer = SimpleNamespace(
        stop=lambda: events.append(("manual_timer_stop",))
    )
    owner._manual_jog_prediction = SimpleNamespace(
        reset_tracking=lambda: events.append(("manual_prediction_reset",))
    )
    owner._clear_coordinate_move_tracking = (
        lambda **kwargs: events.append(("clear_coordinate_tracking", kwargs))
    )
    owner._clear_planned_move_prediction = (
        lambda **kwargs: events.append(("clear_prediction", kwargs))
    )
    owner._update_stage_coordinate_apply_state = lambda: events.append(("apply_state",))
    owner._update_stage_position_display = lambda value: events.append(
        ("stage_display", value)
    )
    owner.sender = lambda: object()
    owner._reset_manual_alignment = lambda **kwargs: events.append(
        ("reset_alignment", kwargs)
    )
    owner._invalidate_design_registration = lambda message: events.append(
        ("invalidate_design", message)
    )
    owner._update_design_position = lambda value: events.append(
        ("update_design_position", value)
    )
    owner._schedule_cancel_state_refresh = lambda: events.append(("schedule_refresh",))
    owner._apply_axis_feedrate_limits = lambda rates: connection_flow.apply_axis_feedrate_limits(
        owner,
        rates,
    )
    owner._apply_joystick_feedrate_preferences = lambda: events.append(
        ("apply_joystick_preferences",)
    )
    return owner


def _design_record(*, name: str = "valid") -> CoordinateFrameRecord:
    return CoordinateFrameRecord(
        frame_id="45e78a25-3c24-48ad-a028-8e2c989d6adf",
        kind=FrameKind.DESIGN,
        name=name,
        version=0,
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=0.0,
            a_zero_machine_mm=0.0,
        ),
        readiness={
            axis: AxisReadiness(ReadinessStatus.READY)
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata={},
    )


def test_on_serial_connected_preserves_attach_order(monkeypatch) -> None:
    events: list[object] = []
    timer_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        connection_flow,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: timer_calls.append((delay, callback))),
    )
    monkeypatch.setattr(
        connection_flow,
        "restore_persisted_controller_state",
        lambda _owner, state, **kwargs: events.append(("restore", state, kwargs)),
    )
    owner = _owner(events)
    owner.serial_connection = _Serial(events, port="OLD")
    serial_port = _Serial(events, port="COM9")

    connection_flow.on_serial_connected(owner, serial_port)

    assert events[:4] == [
        ("serial_close", "OLD"),
        ("load_controller_state",),
        ("stage_set_serial", "COM9"),
        ("restore", {"cached": True}, {"cache_already_loaded": True}),
    ]
    assert owner.serial_connection is serial_port
    assert owner.serial_port_name == "COM9"
    assert owner.serial_baud_rate == 115200
    assert ("joystick", "COM9") in events
    assert ("terminal", "COM9") in events
    assert ("refresh_design",) in events
    assert len(timer_calls) == 1
    assert timer_calls[0][0] == 0
    assert callable(timer_calls[0][1])
    assert owner._controller_state_persistence_suspended is False


def test_on_serial_disconnected_preserves_detach_cleanup_order(monkeypatch) -> None:
    events: list[object] = []
    owner = _owner(events)
    owner.serial_connection = _Serial(events, port="COM9")
    original_clear = connection_flow.stage_move_lifecycle.clear_coordinate_move_tracking
    original_display = coordinate_flow.stage_position_panel.update_stage_position_display
    connection_flow.stage_move_lifecycle.clear_coordinate_move_tracking = (
        lambda _owner, **kwargs: events.append(("clear_coordinate_tracking", kwargs))
    )
    coordinate_flow.stage_position_panel.update_stage_position_display = (
        lambda _owner, value: events.append(("stage_display", value))
    )
    monkeypatch.setattr(
        coordinate_flow,
        "activate_current_design",
        lambda _owner: events.append(("activate_design",)),
    )

    try:
        connection_flow.on_serial_disconnected(owner)
    finally:
        connection_flow.stage_move_lifecycle.clear_coordinate_move_tracking = (
            original_clear
        )
        coordinate_flow.stage_position_panel.update_stage_position_display = (
            original_display
        )

    assert events[:4] == [
        ("stop_jog", "serial disconnect"),
        ("force_jog_stop", 0.8),
        ("serial_close", "COM9"),
        ("save_serial", False, "", 0),
    ]
    assert ("manual_timer_stop",) in events
    assert ("stage_set_serial", None) in events
    assert ("serial_panel", "external_disconnect", True) in events
    assert ("joystick", None) in events
    assert ("terminal", None) in events
    assert ("contact", "stage_position", None) in events
    assert ("contact", "needle_lowering", None) in events
    assert ("oscillation", False, "") in events
    assert not any(event[0] == "invalidate_design" for event in events)
    assert ("activate_design",) in events
    assert owner.serial_connection is None
    assert owner._controller_state_persistence_suspended is False


def test_startup_sync_and_reboot_recovery_are_guarded(monkeypatch) -> None:
    events: list[object] = []
    timer_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        connection_flow,
        "QTimer",
        SimpleNamespace(singleShot=lambda delay, callback: timer_calls.append((delay, callback))),
    )
    owner = _owner(events)

    owner.serial_connection = SimpleNamespace(is_open=False)
    connection_flow.run_serial_startup_sync(owner)
    assert events == []

    owner.serial_connection = SimpleNamespace(is_open=True)
    connection_flow.run_serial_startup_sync(owner)
    assert events == [
        (
            "startup_sync",
            {"auto_home_a": True, "clear_unverified_state": False},
        ),
        ("schedule_refresh",),
    ]

    connection_flow.on_controller_reboot_ready(owner)
    connection_flow.on_controller_reboot_ready(owner)
    assert owner._controller_reboot_recovery_scheduled is True
    assert len(timer_calls) == 1

    connection_flow.run_controller_reboot_recovery(owner)
    assert owner._controller_reboot_recovery_scheduled is False
    assert events.count(
        (
            "startup_sync",
            {"auto_home_a": True, "clear_unverified_state": False},
        )
    ) == 2


def test_feedrate_limit_and_auto_connect_decisions() -> None:
    events: list[object] = []
    owner = _owner(events)

    connection_flow.apply_axis_feedrate_limits(owner, object())
    assert events == []

    connection_flow.apply_axis_feedrate_limits(owner, {"X": 900.0})
    assert ("apply_rates", {"X": 900.0}) in events
    assert ("joystick", "axis_rates", {"X": 900.0}) in events

    owner.settings_manager.serial_auto = True
    owner.settings_manager.meter_auto = True
    owner.serial_connection = None
    owner.lcr_controller = SimpleNamespace(
        is_connected=lambda: False,
        request_connect=lambda: events.append(("lcr", "request_connect")),
    )
    connection_flow.auto_connect_if_possible(owner)

    assert ("serial_panel", "auto_connect") in events
    assert ("lcr", "request_connect") in events


def test_request_lcr_disconnect_persists_before_disconnect() -> None:
    events: list[object] = []
    owner = SimpleNamespace(
        settings_manager=SimpleNamespace(
            save_meter_connection_state=lambda connected, **kwargs: events.append(
                ("save_meter", connected, kwargs)
            )
        ),
        lcr_controller=SimpleNamespace(
            meter_type=lambda: "gwinstek",
            connection_label=lambda: "LCR",
            request_disconnect=lambda: events.append(("disconnect_lcr",))
        ),
    )

    connection_flow.request_lcr_disconnect(owner)

    assert events == [
        ("save_meter", False, {"meter_type": "gwinstek", "description": "LCR"}),
        ("disconnect_lcr",),
    ]


def test_restore_persisted_controller_state_clears_stale_cache() -> None:
    events: list[object] = []
    owner = SimpleNamespace(
        serial_connection=SimpleNamespace(is_open=True),
        settings_manager=SimpleNamespace(
            clear_controller_state=lambda: events.append(("clear_settings_cache",))
        ),
        stage_controller=_RestoreStageController(events, current=False),
        _pending_persisted_design_state={"design": True},
        _pending_persisted_design_position=(1.0, 2.0, 3.0),
        _show_status=lambda message: events.append(("status", message)),
    )

    connection_flow.restore_persisted_controller_state(
        owner,
        {"session": "old"},
        cache_already_loaded=True,
    )

    assert events == [
        ("session_current", {"session": "old"}),
        ("clear_settings_cache",),
        ("clear_stage_cache",),
        ("status", "Controller session changed. Cleared cached homing state."),
    ]
    assert owner._pending_persisted_design_state is None
    assert owner._pending_persisted_design_position is None


def test_restore_persisted_controller_state_imports_current_cache(monkeypatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(
        design_workspace,
        "prepare_persisted_design_restore",
        lambda _owner, state: events.append(("prepare_design_restore", state)),
    )
    owner = SimpleNamespace(
        serial_connection=SimpleNamespace(is_open=True),
        settings_manager=SimpleNamespace(load_controller_state=lambda: {"session": "ok"}),
        stage_controller=_RestoreStageController(events, current=True),
        joystick_panel=SimpleNamespace(
            set_axis_feedrate_limits=lambda rates: events.append(
                ("joystick_rates", rates)
            )
        ),
        _show_status=lambda message: events.append(("status", message)),
    )

    connection_flow.restore_persisted_controller_state(owner)

    assert events == [
        ("session_current", {"session": "ok"}),
        ("prepare_design_restore", {"session": "ok"}),
        ("import_cache", {"session": "ok"}),
        ("apply_rates", {"X": 900.0}),
        ("joystick_rates", {"X": 900.0}),
        ("status", "Restored cached homing state; reading live coordinates."),
    ]


def test_legacy_controller_state_rewrite_reports_typed_success(
    monkeypatch,
) -> None:
    events: list[object] = []
    snapshot = CoordinateSystemSnapshot(True, (), CoordinateFrameDocument())
    persisted = {"version": 3, "active_frame_id": "frame-7"}

    class _Coordinator:
        def complete(self, completion: CoordinateAdapterCompletion) -> CoordinateTransition:
            events.append(
                (
                    "complete",
                    completion.intent_id,
                    completion.result,
                )
            )
            return CoordinateTransition(snapshot)

    owner = SimpleNamespace(
        _coordinate_system_coordinator=_Coordinator(),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, _snapshot: None,
    )
    monkeypatch.setattr(
        coordinate_flow,
        "complete_legacy_design_migration",
        lambda _owner, state: events.append(("rewrite", state)),
    )

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            snapshot,
            intents=(RewriteLegacyDesignStateIntent(7, persisted),),
        ),
    )

    assert events[0] == ("rewrite", persisted)
    _, intent_id, result = events[1]
    assert intent_id == 7
    assert isinstance(result, LegacyDesignStateRewriteResult)
    assert result.succeeded
    assert result.message == ""


def test_legacy_controller_state_rewrite_reports_typed_failure(
    monkeypatch,
) -> None:
    completions: list[CoordinateAdapterCompletion] = []
    statuses: list[str] = []
    snapshot = CoordinateSystemSnapshot(True, (), CoordinateFrameDocument())

    def complete(completion: CoordinateAdapterCompletion) -> CoordinateTransition:
        completions.append(completion)
        return CoordinateTransition(
            snapshot,
            notices=(
                CoordinateNotice(
                    "Design registration migration could not be finalized.",
                    severity="warning",
                    duration_ms=6000,
                ),
            ),
        )

    owner = SimpleNamespace(
        _coordinate_system_coordinator=SimpleNamespace(complete=complete),
        _show_status=lambda message, _timeout: statuses.append(message),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, _snapshot: None,
    )
    monkeypatch.setattr(
        coordinate_flow,
        "complete_legacy_design_migration",
        lambda _owner, _state: (_ for _ in ()).throw(OSError("disk full")),
    )

    coordinate_flow.apply_coordinate_transition(
        owner,
        CoordinateTransition(
            snapshot,
            intents=(
                RewriteLegacyDesignStateIntent(
                    4,
                    {"version": 3, "active_frame_id": "frame-4"},
                ),
            ),
        ),
    )

    assert len(completions) == 1
    result = completions[0].result
    assert isinstance(result, LegacyDesignStateRewriteResult)
    assert not result.succeeded
    assert result.message == "disk full"
    assert statuses == ["Design registration migration could not be finalized."]


def test_controller_state_persistence_preserves_legacy_registration_until_frame_save() -> None:
    legacy = {
        "version": 2,
        "source_design_marks": [[0.0, 0.0], [1.0, 0.0]],
        "source_stage_marks": [[2.0, 3.0], [3.0, 3.0]],
    }
    owner = SimpleNamespace(
        _coordinate_system_coordinator=SimpleNamespace(
            snapshot=lambda: CoordinateSystemSnapshot(
                True,
                (),
                CoordinateFrameDocument(),
                registration=RegistrationWorkflowSnapshot(
                    legacy_migration_state=legacy,
                ),
            )
        ),
        stage_controller=SimpleNamespace(
            export_cached_controller_state=lambda: {"last_stage_position": [1.0, 2.0]}
        ),
    )

    state = design_workspace.controller_state_with_design(owner)

    assert state == {
        "last_stage_position": [1.0, 2.0],
        "design_session": legacy,
    }


def test_controller_state_persistence_reads_detached_application_projection() -> None:
    persisted = {
        "version": 3,
        "active_frame_id": "frame-3",
    }
    owner = SimpleNamespace(
        _coordinate_system_coordinator=SimpleNamespace(
            snapshot=lambda: CoordinateSystemSnapshot(
                True,
                (),
                CoordinateFrameDocument(),
            ),
        ),
        _coordinate_runtime=SimpleNamespace(
            controller_persistence_state=lambda workspace_state: (
                persisted
                if workspace_state == "workspace-state"
                else None
            )
        ),
        _design_session=SimpleNamespace(
            snapshot_state=lambda: "workspace-state"
        ),
        stage_controller=SimpleNamespace(
            export_cached_controller_state=lambda: {"last_stage_position": [1.0, 2.0]}
        ),
    )

    state = design_workspace.controller_state_with_design(owner)

    assert state == {
        "last_stage_position": [1.0, 2.0],
        "design_session": persisted,
    }


def test_activate_current_design_does_not_read_session_owner(monkeypatch) -> None:
    calls: list[object] = []
    snapshot = CoordinateSystemSnapshot(False, (), None)

    class _Coordinator:
        def activate_design(self, request: DesignActivationRequest) -> CoordinateTransition:
            calls.append(request)
            return CoordinateTransition(snapshot)

    owner = SimpleNamespace(
        _coordinate_system_coordinator=_Coordinator(),
        _rotation_geometry_snapshot=lambda: SimpleNamespace(
            pivot_machine_xy=(0.0, 0.0)
        ),
        _active_objective_xy_offset=lambda: (0.0, 0.0),
        _active_design_frame_metadata=None,
        stage_controller=SimpleNamespace(
            axes_are_homed=lambda axes: bool(set(axes) <= {"X", "Y"}),
            latest_machine_coordinate_snapshot=lambda: None,
        ),
    )
    monkeypatch.setattr(
        coordinate_flow.stage_position_panel,
        "render_coordinate_system_snapshot",
        lambda _owner, _snapshot: None,
    )

    transition = coordinate_flow.activate_current_design(owner)

    assert transition == CoordinateTransition(snapshot)
    assert len(calls) == 1
    assert calls[0].session_state is None
