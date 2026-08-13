from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from main import Main
from probe_station_gui.coordinates import application_runtime
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAuthorityObservation,
    CoordinateMotionLease,
    RegistrationWorkflowSnapshot,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    PersistCoordinateSelectionIntent,
)
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.settings.axis_calibration_config import AxisCalibrationSettings
from probe_station_gui.settings.manager import Settings, SettingsManager
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.types import _Status
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow
from probe_station_gui.views import main_window_design_workspace as design_workspace
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)
from probe_station_gui.design import navigation_targeting


def test_main_has_no_coordinate_policy_owner_or_compatibility_helpers() -> None:
    source = inspect.getsource(Main)
    for forbidden in (
        "_coordinate_frame_registry",
        "_coordinate_frame_lifecycle",
        "_design_registration_lifecycle",
        "_coordinate_frames_loaded",
        "_coordinate_frame_authority_blocked_axes",
        "_apply_coordinate_frame_authority_blocks",
        "_snapshot_active_design_frame_usability",
        "_design_frame_usability_snapshot_is_current",
        "_active_design_frame_record",
    ):
        assert forbidden not in source

    assert "_design_session" not in inspect.getsource(
        design_workspace.maybe_restore_persisted_design
    )
    assert "_adopt_session" not in inspect.getsource(Main.__init__)
    assert "_controller_persistence_design_state" not in inspect.getsource(
        design_workspace.controller_state_with_design
    )
    assert "_design_session" not in inspect.getsource(Main._api_microscope_area_scan)
    assert "session.registration" not in inspect.getsource(
        navigation_targeting.design_panel_presentation
    )
    assert "session.source_design_marks" not in inspect.getsource(
        navigation_targeting.design_position_presentation
    )


def test_task3_fixtures_do_not_recreate_deleted_coordinate_policy_fields() -> None:
    root = Path(__file__).resolve().parents[2]
    fixture_paths = (
        "tests/app/main_coordinate_feedrate_support.py",
        "tests/app/test_main_route_measurement_session.py",
        "tests/coordinates/test_coordinator_registration_operator_cancel.py",
        "tests/coordinates/test_coordinator_registration_rollback_rendering.py",
        "tests/ui/test_main_window_connection_flow.py",
    )
    source = "\n".join((root / relative).read_text(encoding="utf-8") for relative in fixture_paths)
    for forbidden in (
        "_coordinate_frame_lifecycle",
        "_coordinate_frames_loaded",
        "_apply_coordinate_frame_authority_blocks",
    ):
        assert forbidden not in source


def test_main_seeds_gui_restore_only_into_coordinator() -> None:
    source = inspect.getsource(Main.__init__)
    assert "restore_frame_id=software_coordinates.last_selected_frame_id" in source
    assert "_api_coordinate_frame_id" not in source


def test_application_runtime_does_not_expose_or_adopt_a_mutable_design_session() -> None:
    runtime = application_runtime.create_application_coordinate_runtime()

    assert not hasattr(runtime, "design_session")
    source = inspect.getsource(application_runtime)
    assert "_adopt_session" not in source
    assert "design_session:" not in source


def test_sample_workflow_registration_gate_uses_coordinator_snapshot() -> None:
    window = Main.__new__(Main)
    window._design_session = SimpleNamespace(registration=None)
    window._coordinate_system_coordinator = SimpleNamespace(
        snapshot=lambda: CoordinateSystemSnapshot(
            True,
            (),
            None,
            registration=RegistrationWorkflowSnapshot(
                registration_valid=True,
            ),
        )
    )

    assert Main._design_registration_is_active(window)
    assert "_design_session" not in inspect.getsource(
        Main._design_registration_is_active
    )


def test_design_document_mutations_are_observed_by_the_coordinator() -> None:
    for method in (
        Main._set_design_top_cell,
        Main._set_design_layer_visibility,
        Main._rotate_design_document,
    ):
        source = inspect.getsource(method)
        assert "candidate_session = self._snapshot_design_session()" in source
        assert "session_state=candidate_session.snapshot_state()" in source
        assert "workspace_after=workspace_after" in source
        assert "self._design_session.apply_state" not in source


def test_gui_coordinate_selection_stops_active_jog_before_switching(
    monkeypatch,
) -> None:
    events: list[object] = []
    window = Main.__new__(Main)
    window.joystick_panel = SimpleNamespace(
        cancel_jog_input=lambda: events.append("jog-stopped")
    )
    window._clear_exact_step_targets = lambda: events.append("step-cleared")
    window._clear_pending_stage_coordinate_targets = lambda: (
        events.append("fields-cleared") or False
    )
    monkeypatch.setattr(
        stage_position_panel_adapter,
        "select_gui_coordinate_frame",
        lambda owner, frame_id: events.append((owner, frame_id)),
    )

    Main._on_software_coordinate_system_changed(window, "design-7")

    assert events == [
        "jog-stopped",
        "step-cleared",
        "fields-cleared",
        (window, "design-7"),
    ]


def test_selection_persistence_intent_is_fire_and_forget() -> None:
    updates: list[str] = []
    published: list[object] = []
    persisted = object()
    owner = SimpleNamespace(
        _stage_axis_display_values={},
        _stage_position_panel=None,
        settings_manager=SimpleNamespace(
            set_software_coordinate_selection=lambda frame_id: (
                updates.append(frame_id) or persisted
            )
        ),
        _software_coordinate_selection_store=SimpleNamespace(
            publish=published.append
        ),
    )
    transition = CoordinateTransition(
        CoordinateSystemSnapshot(False, (), None),
        intents=(PersistCoordinateSelectionIntent("machine"),),
    )

    coordinate_flow.apply_coordinate_transition(owner, transition)

    assert updates == ["machine"]
    assert published == [persisted]


def test_external_api_move_does_not_mutate_gui_coordinate_selection() -> None:
    source = inspect.getsource(Main._api_move_to_coordinates)
    assert "select_system" not in source
    assert "_on_software_coordinate_system_changed" not in source


def test_external_api_defaults_to_machine_values_not_gui_display() -> None:
    controller = StageController()
    try:
        settings = Settings()
        settings.axis_calibrations["X"] = AxisCalibrationSettings(
            enabled=True,
            calibration_file="X.npz",
            controller_points=[0.0, 10.0, 20.0],
            physical_points=[0.0, 12.0, 30.0],
        )
        controller.apply_axis_calibrations(settings.axis_calibrations)
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            10.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        controller._update_cached_positions(
            _Status(
                state="Idle",
                synchronized_machine_position=(15.0, 3.0, 0.0, 0.0, 0.0, 0.0),
                display_position=(5.0, 3.0, 0.0, 0.0, 0.0, 0.0),
                work_position=(5.0, 3.0, 0.0, 0.0, 0.0, 0.0),
                work_offset=(10.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                coordinate_system="G54",
            )
        )
        owner = Main.__new__(Main)
        owner._stage_axis_display_values = {"X": 102.0, "Y": 103.0}
        owner.stage_controller = controller

        snapshot = controller.latest_motion_coordinate_snapshot()
        assert snapshot is not None
        assert Main._api_machine_display_position(owner)["X"] == pytest.approx(21.0)
        raw_target = snapshot.physical_machine_to_configured_controller("X", 22.0)
        assert Main._resolve_api_stage_axis_target(owner, "X", 1.0, "G91") == (
            pytest.approx(raw_target),
            pytest.approx(22.0),
        )
        assert Main._resolve_api_stage_axis_target(owner, "X", 22.0, "G90") == (
            pytest.approx(raw_target),
            pytest.approx(22.0),
        )
        owner._stage_status_payload = lambda *_args, **_kwargs: {
            "coordinate_display": "G54"
        }
        controller.latest_stage_position = lambda: (5.0, 3.0)
        assert Main._api_stage_status(owner)["coordinate_display"] == "Machine"
        assert "_api_machine_display_position" in inspect.getsource(
            Main._api_stage_status
        )
        assert "_resolve_api_stage_axis_target" in inspect.getsource(
            Main._api_move_to_coordinates
        )
    finally:
        controller.shutdown()


def test_joystick_projection_reuses_basis_lease_and_requests_pose_rebase() -> None:
    authority = CoordinateAuthorityObservation(
        physical_pose=PhysicalMachinePose({}),
        homed_axes=frozenset(),
        machine_snapshot=None,
        pivot_machine_xy=(0.0, 0.0),
    )
    current_lease = CoordinateMotionLease(
        selected_frame_id="machine",
        available=True,
        reason=None,
        record=None,
        authority=authority,
        display_values=(("X", 0.0),),
        basis_fingerprint=("basis",),
        fingerprint=("current",),
    )
    start_lease = CoordinateMotionLease(
        selected_frame_id="machine",
        available=True,
        reason=None,
        record=None,
        authority=authority,
        display_values=(("X", -1.0),),
        basis_fingerprint=("basis",),
        fingerprint=("start",),
    )
    requests: list[object] = []
    projection = object()
    window = Main.__new__(Main)
    window._coordinate_system_coordinator = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(motion_lease=current_lease),
        project_motion=lambda request: requests.append(request) or projection,
    )

    assert (
        Main._project_gui_relative_motion(window, (("X", 1.0),), None)
        is projection
    )
    assert (
        Main._project_gui_relative_motion(window, (("X", 1.0),), start_lease)
        is projection
    )

    assert requests[0].lease is current_lease
    assert requests[0].allow_pose_rebase is False
    assert requests[1].lease is start_lease
    assert requests[1].allow_pose_rebase is True


def test_objective_persistence_immediately_refreshes_coordinate_authority(
    monkeypatch,
) -> None:
    events: list[object] = []
    plan = SimpleNamespace(
        settings=object(),
        apply_settings=False,
        refresh_design_position=False,
        refresh_calibration_ui=False,
        status="",
    )
    owner = SimpleNamespace(
        settings_manager=SimpleNamespace(
            replace_and_save=lambda settings, **kwargs: events.append(
                ("save", settings, kwargs)
            )
        ),
        _apply_objective_settings=lambda: events.append(("apply",)),
        _refresh_design_position=lambda: events.append(("position",)),
        _refresh_objective_calibration_ui=lambda: events.append(("calibration",)),
        _show_plan_status=lambda _plan: events.append(("status",)),
    )
    monkeypatch.setattr(
        coordinate_flow,
        "observe_coordinate_authority",
        lambda actual_owner: events.append(("authority", actual_owner)),
    )

    assert Main._persist_objective_plan(owner, plan, show_status=False)
    assert events[-1] == ("authority", owner)


def test_settings_dialog_refreshes_authority_after_runtime_objective_apply() -> None:
    source = inspect.getsource(Main._apply_settings_from_dialog)

    assert "objective_authority_changed" in source
    assert source.index("self._apply_settings()") < source.index(
        "coordinate_flow.observe_coordinate_authority(self)"
    )
    assert "pivot_changed" in source


def test_settings_dialog_refreshes_authority_after_axis_curve_change() -> None:
    source = inspect.getsource(Main._apply_settings_from_dialog)

    assert "axis_calibrations_changed" in source
    assert "or axis_calibrations_changed" in source


def test_curve_apply_keeps_estimated_position_authority_on_remapped_snapshot(
    monkeypatch,
) -> None:
    controller = StageController()
    try:
        settings = Settings()
        settings.coordinate_system.position_mode = "machine"
        settings.axis_calibrations["X"] = AxisCalibrationSettings(
            enabled=True,
            calibration_file="X.npz",
            controller_points=[0.0, 1.0, 3.0],
            physical_points=[0.0, 2.0, 5.0],
        )
        manager = SettingsManager.__new__(SettingsManager)
        manager._settings = settings
        controller.apply_coordinate_system_configuration(
            position_mode="machine",
            startup_mode="controller",
            preferred_system="G54",
        )
        raw_position = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        controller._update_cached_positions(
            _Status(
                state="Idle",
                position=raw_position,
                synchronized_machine_position=raw_position,
                display_position=raw_position,
            )
        )
        before = controller.latest_machine_coordinate_snapshot()
        assert before is not None
        assert before.physical_machine_pose.require("X") == 1.0

        window = Main.__new__(Main)
        window.stage_controller = controller
        window.settings_manager = manager
        window._latest_physical_machine_pose = before.physical_machine_pose
        window.joystick_panel = None
        window.design_navigator_panel = None
        window.design_layout_window = None
        window.contact_calibration_window = None
        window.serial_connection_panel = None
        window.oscillation_panel = None
        window.serial_connection = None
        window._api_bridge = None
        window.lcr_controller = SimpleNamespace(
            apply_configuration=lambda **_kwargs: None,
            request_reconfigure=lambda: None,
        )
        window._clear_exact_step_targets = lambda: None
        window._apply_objective_settings = lambda: None
        window._telegram_runtime = SimpleNamespace(configure=lambda _settings: None)
        window._update_coordinate_display = lambda **_kwargs: None
        window._can_display_design_position = lambda: False
        window._update_design_position = lambda _position: None

        Main._apply_settings(window)
        remapped = controller.latest_machine_coordinate_snapshot()
        assert remapped is not None
        assert remapped.physical_machine_pose.require("X") == 2.0

        observed_poses = []
        monkeypatch.setattr(
            stage_position_update.stage_position_panel,
            "update_stage_position_display",
            lambda _owner, _position: None,
        )
        monkeypatch.setattr(
            stage_position_update.coordinate_flow,
            "observe_coordinate_authority",
            lambda _owner, physical_pose: observed_poses.append(physical_pose),
        )

        stage_position_update.publish_stage_position_estimate(window, raw_position)

        assert window._latest_physical_machine_pose is remapped.physical_machine_pose
        assert observed_poses == [remapped.physical_machine_pose]
    finally:
        controller.shutdown()
