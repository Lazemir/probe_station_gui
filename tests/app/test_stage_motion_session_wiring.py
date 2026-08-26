from __future__ import annotations

import ast
import importlib
import inspect
import types
from pathlib import Path

from probe_station_gui.application import stage_motion_session as motion_session
from probe_station_gui.application.stage_motion_types import StageMotionPresentation
from probe_station_gui.coordinates.model import PhysicalMachinePose
from probe_station_gui.stage import coordinate_targets
from probe_station_gui.stage import types as stage_types
from tests.app.import_reset import restore_real_imports_for_main
from tests.app.test_stage_motion_session_planned_position import (
    SESSION_PATH,
    _observe,
    _session,
)


def test_main_composes_one_session_without_raw_planned_or_position_state() -> None:
    restore_real_imports_for_main()
    main_module = importlib.import_module("main")
    source = inspect.getsource(main_module.Main.__init__)

    assert "self._stage_motion = _StageMotionSession(" in source
    assert source.index(
        "self.stage_controller = self._create_stage_controller()"
    ) < source.index("self._stage_motion = _StageMotionSession(")
    for removed_name in (
        "_planned_move_origin_xy",
        "_planned_move_stage_xy",
        "_planned_move_target_xy",
        "_planned_move_started_at",
        "_planned_move_ends_at",
        "_planned_move_waiting_for_fresh_status",
        "_planned_move_stop_status_timestamp",
        "_pending_planned_move_target_xy",
        "_pending_planned_move_source_label",
        "_latest_physical_machine_pose",
        "_last_reported_b_position",
        "_manual_jog_prediction",
        "_manual_jog_timer",
        "_exact_step_accumulator",
        "_exact_step_pending_axes",
        "_exact_step_motion_lease",
        "_exact_step_pose_rebase_allowed",
        "_exact_step_window_elapsed",
        "_exact_step_timer",
    ):
        assert removed_name not in source


def test_controller_motion_signals_connect_directly_to_session_slots() -> None:
    restore_real_imports_for_main()
    main_module = importlib.import_module("main")
    source = inspect.getsource(main_module.Main.__init__)

    for signal_name, slot_name in (
        ("tracked_absolute_xy_move_finished", "on_tracked_absolute_xy_move_finished"),
        ("tracked_absolute_xy_move_started", "on_tracked_absolute_xy_move_started"),
        ("stage_position_observed", "on_stage_position_changed"),
        ("movement_finished", "on_movement_finished"),
    ):
        assert (
            f"{signal_name}.connect(\n"
            f"            self._stage_motion.{slot_name},\n"
            "            Qt.ConnectionType.QueuedConnection,\n"
            "        )"
        ) in source
    assert "self._stage_motion.on_absolute_xy_move_started" not in source


def test_typed_presentation_renders_stage_authority_and_design_in_order(
    monkeypatch,
) -> None:
    restore_real_imports_for_main()
    main_module = importlib.import_module("main")
    from probe_station_gui.application import stage_design_position
    from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow
    from probe_station_gui.views import main_window_design_workspace as design_workspace

    window = main_module.Main.__new__(main_module.Main)
    calls: list[object] = []
    window._manual_jog_prediction = types.SimpleNamespace(
        prediction_available=lambda: False,
    )
    window.contact_calibration_window = types.SimpleNamespace(
        set_current_stage_position=lambda value: calls.append(("contact", value))
    )
    window._can_display_design_position = lambda: True
    window._update_coordinate_display = lambda **kwargs: calls.append(
        ("coordinate", kwargs["center_xy"])
    )
    window._update_design_position = lambda value: calls.append(("design", value))
    monkeypatch.setattr(
        design_workspace,
        "maybe_restore_persisted_design",
        lambda _owner, value: calls.append(("restore", value)),
    )
    monkeypatch.setattr(
        stage_design_position.stage_position_panel_adapter,
        "update_stage_position_display",
        lambda _owner, value: calls.append(("stage", value)),
    )
    monkeypatch.setattr(
        stage_design_position.stage_position_panel_adapter,
        "set_stage_motion_axes",
        lambda _owner, value: calls.append(("axes", frozenset(value))),
    )
    monkeypatch.setattr(
        coordinate_flow,
        "observe_coordinate_authority",
        lambda _owner, value, **_facts: calls.append(("authority", value)),
    )
    pose = PhysicalMachinePose.from_mapping({"X": 11.0, "Y": 22.0})
    presentation = StageMotionPresentation(
        reported_position=(1.0, 2.0, 3.0),
        presented_position=(4.0, 5.0, 3.0),
        raw_stage_xy=(1.0, 2.0),
        presented_stage_xy=(4.0, 5.0),
        physical_machine_pose=pose,
        contact_calibration_position=(1.0, 2.0, 3.0),
        b_position=None,
        active_axes=frozenset({"X", "Y"}),
        unhomed_fallback=False,
        clear_motion_axes=False,
    )

    main_module.Main._apply_stage_motion_presentation(window, presentation)

    assert calls == [
        ("axes", frozenset({"X", "Y"})),
        ("restore", (1.0, 2.0, 3.0)),
        ("contact", (1.0, 2.0, 3.0)),
        ("stage", (4.0, 5.0, 3.0)),
        ("authority", pose),
        ("coordinate", (4.0, 5.0)),
        ("design", (4.0, 5.0)),
    ]


def test_typed_renderer_uses_exact_observation_snapshot_without_controller_reread(
    monkeypatch,
) -> None:
    restore_real_imports_for_main()
    main_module = importlib.import_module("main")
    from probe_station_gui.application import stage_design_position
    from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow

    cache_reads: list[str] = []

    def cache_read(name: str):
        def read():
            cache_reads.append(name)
            raise RuntimeError(f"unexpected mutable cache read: {name}")

        return read

    window = main_module.Main.__new__(main_module.Main)
    window.stage_controller = types.SimpleNamespace(
        latest_motion_coordinate_snapshot=cache_read("motion snapshot"),
        latest_machine_coordinate_snapshot=cache_read("machine snapshot"),
        homed_axes=cache_read("homed axes"),
    )
    window._manual_jog_prediction = types.SimpleNamespace(
        prediction_available=lambda: False,
    )
    window.contact_calibration_window = None
    window._can_display_design_position = lambda: True
    window._update_coordinate_display = lambda **_kwargs: None
    window._update_design_position = lambda _value: None
    window._rotation_geometry_snapshot = lambda: types.SimpleNamespace(
        pivot_machine_xy=(0.0, 0.0)
    )
    window._active_objective_xy_offset = lambda: (0.0, 0.0)
    authority_observations = []
    window._coordinate_system_coordinator = types.SimpleNamespace(
        observe_authority=lambda value: authority_observations.append(value),
    )
    monkeypatch.setattr(
        coordinate_flow, "apply_coordinate_transition", lambda *_args: None
    )
    monkeypatch.setattr(
        stage_design_position.design_workspace,
        "maybe_restore_persisted_design",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        stage_design_position.stage_position_panel_adapter,
        "update_stage_position_display",
        lambda *_args: None,
    )
    snapshots = (object(), object())
    poses = (
        PhysicalMachinePose.from_mapping({"X": 11.0}),
        PhysicalMachinePose.from_mapping({"X": 22.0}),
    )
    homed = (
        frozenset({"X", "Y"}),
        frozenset({"X", "Y", "Z"}),
    )

    for index in range(2):
        position = (float(index + 1), 2.0, 3.0)
        main_module.Main._apply_stage_motion_presentation(
            window,
            StageMotionPresentation(
                reported_position=position,
                presented_position=position,
                raw_stage_xy=(position[0], position[1]),
                presented_stage_xy=(position[0], position[1]),
                physical_machine_pose=poses[index],
                contact_calibration_position=position,
                b_position=None,
                active_axes=frozenset(),
                unhomed_fallback=False,
                clear_motion_axes=False,
                motion_coordinate_snapshot=snapshots[index],
                stage_state="Run",
                homed_axes=homed[index],
                status_timestamp=float(index + 1),
                last_jog_write_timestamp=None,
            ),
        )

    assert cache_reads == []
    assert [value.machine_snapshot for value in authority_observations] == list(
        snapshots
    )
    assert [value.physical_pose for value in authority_observations] == list(poses)
    assert [value.homed_axes for value in authority_observations] == list(homed)


def test_unhomed_presentation_hides_coordinate_xy_but_keeps_baseline_raw_outputs(
    monkeypatch,
) -> None:
    session, controller = _session()
    controller.homed.clear()
    controller.state = "run"
    presentations = []
    session.presentation_changed.connect(presentations.append)
    _observe(session, controller, (7.0, 8.0, 3.0, 4.0, 5.0))

    restore_real_imports_for_main()
    main_module = importlib.import_module("main")
    from probe_station_gui.application import stage_design_position

    calls: list[tuple[str, object]] = []
    window = main_module.Main.__new__(main_module.Main)
    window._manual_jog_prediction = types.SimpleNamespace(
        prediction_available=lambda: False,
    )
    window.contact_calibration_window = None
    window._can_display_design_position = lambda: True
    window._update_coordinate_display = lambda **kwargs: calls.append(
        ("coordinate", kwargs["center_xy"])
    )
    window._update_design_position = lambda value: calls.append(("design", value))
    monkeypatch.setattr(
        stage_design_position.design_workspace,
        "maybe_restore_persisted_design",
        lambda _owner, value: calls.append(("restore", value)),
    )
    monkeypatch.setattr(
        stage_design_position.stage_position_panel_adapter,
        "update_stage_position_display",
        lambda _owner, value: calls.append(("stage", value)),
    )
    monkeypatch.setattr(
        stage_design_position.coordinate_flow,
        "observe_coordinate_authority",
        lambda *_args, **_kwargs: None,
    )

    main_module.Main._apply_stage_motion_presentation(window, presentations[-1])

    assert calls == [
        ("restore", (7.0, 8.0, 3.0, 4.0, 5.0)),
        ("stage", (7.0, 8.0, 3.0, 4.0, 5.0)),
        ("coordinate", None),
        ("design", (7.0, 8.0)),
    ]


def test_timestamp_only_presentation_skips_idle_renderer_rebuild(monkeypatch) -> None:
    restore_real_imports_for_main()
    main_module = importlib.import_module("main")
    from probe_station_gui.application import stage_design_position

    calls: list[str] = []
    window = main_module.Main.__new__(main_module.Main)
    window._manual_jog_prediction = types.SimpleNamespace(
        prediction_available=lambda: False,
    )
    window.contact_calibration_window = types.SimpleNamespace(
        set_current_stage_position=lambda _value: calls.append("contact")
    )
    window._can_display_design_position = lambda: calls.append("display") or True
    window._update_coordinate_display = lambda **_kwargs: calls.append("coordinate")
    window._update_design_position = lambda _value: calls.append("design")
    monkeypatch.setattr(
        stage_design_position.stage_position_update,
        "on_stage_position_changed",
        lambda *_args, **_kwargs: calls.append("deferred"),
    )
    monkeypatch.setattr(
        stage_design_position.design_workspace,
        "maybe_restore_persisted_design",
        lambda *_args: calls.append("restore"),
    )
    monkeypatch.setattr(
        stage_design_position.stage_position_panel_adapter,
        "update_stage_position_display",
        lambda *_args: calls.append("stage"),
    )
    monkeypatch.setattr(
        stage_design_position.coordinate_flow,
        "observe_coordinate_authority",
        lambda *_args, **_kwargs: calls.append("authority"),
    )
    presentation = StageMotionPresentation(
        reported_position=(1.0, 2.0, 3.0),
        presented_position=(1.0, 2.0, 3.0),
        raw_stage_xy=(1.0, 2.0),
        presented_stage_xy=(1.0, 2.0),
        physical_machine_pose=PhysicalMachinePose.from_mapping({}),
        contact_calibration_position=(1.0, 2.0, 3.0),
        b_position=None,
        active_axes=frozenset(),
        unhomed_fallback=False,
        clear_motion_axes=True,
        stage_state="Idle",
        material_change=False,
    )

    main_module.Main._apply_stage_motion_presentation(window, presentation)

    assert calls == []


def test_typed_presentation_never_delegates_back_to_legacy_position_workflow(
    monkeypatch,
) -> None:
    restore_real_imports_for_main()
    main_module = importlib.import_module("main")
    from probe_station_gui.application import stage_design_position

    window = main_module.Main.__new__(main_module.Main)
    window.contact_calibration_window = None
    window._can_display_design_position = lambda: True
    window._update_coordinate_display = lambda **_kwargs: direct_render_calls.append(
        "coordinate"
    )
    window._update_design_position = lambda _value: direct_render_calls.append("design")
    delegated: list[tuple[object, object, PhysicalMachinePose]] = []
    direct_render_calls: list[str] = []
    monkeypatch.setattr(
        stage_design_position.stage_position_update,
        "on_stage_position_changed",
        lambda owner, position, *, physical_machine_pose, **_facts: delegated.append(
            (owner, position, physical_machine_pose)
        ),
    )
    monkeypatch.setattr(
        stage_design_position.design_workspace,
        "maybe_restore_persisted_design",
        lambda *_args: direct_render_calls.append("restore"),
    )
    monkeypatch.setattr(
        stage_design_position.stage_position_panel_adapter,
        "update_stage_position_display",
        lambda *_args: direct_render_calls.append("stage"),
    )
    monkeypatch.setattr(
        stage_design_position.coordinate_flow,
        "observe_coordinate_authority",
        lambda *_args: direct_render_calls.append("authority"),
    )
    presentation = StageMotionPresentation(
        reported_position=(1.0, 2.0, 3.0),
        presented_position=(4.0, 5.0, 3.0),
        raw_stage_xy=(1.0, 2.0),
        presented_stage_xy=(4.0, 5.0),
        physical_machine_pose=PhysicalMachinePose.from_mapping({}),
        contact_calibration_position=(1.0, 2.0, 3.0),
        b_position=None,
        active_axes=frozenset(),
        unhomed_fallback=False,
        clear_motion_axes=False,
        material_change=False,
    )

    main_module.Main._apply_stage_motion_presentation(window, presentation)

    assert delegated == []
    assert direct_render_calls == []


def test_application_consumers_use_typed_session_boundary() -> None:
    from probe_station_gui.application import api_stage_contact
    from probe_station_gui.application import camera_pipeline
    from probe_station_gui.application import registration_focus
    from probe_station_gui.application import stage_design_position
    from probe_station_gui.stage import move_lifecycle
    from probe_station_gui.views import main_window_docks

    assert not hasattr(
        camera_pipeline._MainCameraPipelineMixin, "_on_absolute_xy_move_started"
    )
    move_source = inspect.getsource(
        registration_focus._MainRegistrationFocusMixin._move_to_design_coordinate
    )
    assert "PlannedXYMoveRequest(" in move_source
    assert "self._stage_motion.request_planned_xy_move(" in move_source
    assert "_pending_planned_move" not in move_source
    presentation_source = inspect.getsource(
        stage_design_position._MainStageDesignPositionMixin._apply_stage_motion_presentation
    )
    assert "StageMotionPresentation" in presentation_source
    assert (
        "stage_position_panel_adapter.update_stage_position_display"
        in presentation_source
    )
    assert "coordinate_flow.observe_coordinate_authority" in presentation_source
    manual_wiring_source = inspect.getsource(
        main_window_docks._connect_joystick_manual_motion
    )
    api_source = inspect.getsource(
        api_stage_contact._MainApiStageContactMixin._interrupt_api_route_controlled_operation
    )
    cancel_source = inspect.getsource(move_lifecycle._cancel_controller_activity)
    assert "owner._stage_motion.on_manual_jog_command" in manual_wiring_source
    assert "owner._stage_motion.on_manual_jog_stopped" in manual_wiring_source
    assert "self._stage_motion.cancel_planned_xy_move()" in api_source
    assert "owner._stage_motion.cancel_planned_xy_move()" in cancel_source
    assert "_clear_planned_move_prediction" not in (
        manual_wiring_source + api_source + cancel_source
    )


def test_deleted_main_motion_state_has_no_production_consumer() -> None:
    forbidden_attributes = {
        "_planned_move_origin_xy",
        "_planned_move_stage_xy",
        "_planned_move_target_xy",
        "_planned_move_started_at",
        "_planned_move_ends_at",
        "_planned_move_waiting_for_fresh_status",
        "_planned_move_stop_status_timestamp",
        "_pending_planned_move_target_xy",
        "_pending_planned_move_source_label",
        "_latest_physical_machine_pose",
        "_last_reported_b_position",
        "_manual_jog_timer",
        "_exact_step_pending_axes",
        "_exact_step_motion_lease",
        "_exact_step_pose_rebase_allowed",
        "_exact_step_window_elapsed",
    }
    root = Path(__file__).resolve().parents[2]
    production_files = [root / "main.py"]
    production_files.extend((root / "probe_station_gui").rglob("*.py"))
    offenders: list[str] = []
    for path in production_files:
        if path == SESSION_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}:{node.attr}")
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_clear_planned_move_prediction"
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}:{node.name}")

    assert offenders == []


def test_coordinate_contracts_live_below_application_session() -> None:
    coordinate_contracts = (
        "CoordinateMoveRequest",
        "CoordinateMoveCompletion",
        "CoordinateMoveDisposition",
        "CoordinatePendingEdits",
    )
    for name in coordinate_contracts:
        assert getattr(coordinate_targets, name, None) is not None, name
        assert not hasattr(motion_session, name), name
    assert getattr(stage_types, "UnclaimedMovementCompletion", None) is not None
    assert not hasattr(motion_session, "UnclaimedMovementCompletion")

    root = Path(__file__).resolve().parents[2] / "probe_station_gui"
    offenders: list[str] = []
    for package in (root / "stage", root / "views"):
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    == "probe_station_gui.application.stage_motion_session"
                ):
                    offenders.append(
                        f"{path.relative_to(root.parent).as_posix()}:{node.lineno}"
                    )
    assert offenders == []


def test_migrated_coordinate_state_has_no_owner_outside_session() -> None:
    root = Path(__file__).resolve().parents[2]
    session_path = (
        root / "probe_station_gui" / "application" / "stage_motion_session.py"
    )
    forbidden = {
        "_coordinate_targets",
        "_pending_stage_axis_targets",
        "_pending_coordinate_motion_lease",
    }
    offenders: list[str] = []
    for path in [root / "main.py", *(root / "probe_station_gui").rglob("*.py")]:
        if path == session_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append(
                    f"{path.relative_to(root).as_posix()}:{node.lineno}:{node.attr}"
                )
    assert offenders == []


def test_deleted_coordinate_lifecycle_wrappers_have_no_production_definition() -> None:
    root = Path(__file__).resolve().parents[2]
    forbidden = {
        "_on_stage_axis_editing_finished",
        "_apply_pending_stage_coordinate_targets",
        "_start_coordinate_axis_move",
        "_start_coordinate_targets_move",
        "_apply_coordinate_common_feedrate_plan",
        "_apply_coordinate_move_feedrate",
        "_start_next_pending_stage_axis_move",
        "_advance_coordinate_move_prediction",
        "_clear_pending_stage_coordinate_targets",
    }
    offenders: list[str] = []
    for path in [root / "main.py", *(root / "probe_station_gui").rglob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in forbidden
            ):
                offenders.append(
                    f"{path.relative_to(root).as_posix()}:{node.lineno}:{node.name}"
                )
    assert offenders == []


def test_main_wires_global_completion_and_homing_directly_to_session() -> None:
    root = Path(__file__).resolve().parents[2]
    main_source = (root / "main.py").read_text(encoding="utf-8")

    assert (
        "self.stage_controller.movement_finished.connect(\n"
        "            self._stage_motion.on_movement_finished"
    ) in main_source
    assert main_source.count("self.stage_controller.movement_finished.connect(") == 1
    assert (
        main_source.count("self._stage_motion.unclaimed_movement_finished.connect(")
        == 1
    )
    assert (
        "lambda completion: stage_move_lifecycle.on_move_finished(\n"
        "                self,\n"
        "                completion.success,\n"
        "                completion.message,"
    ) in main_source
    assert "self._stage_motion.continue_homing_requested.connect(" in main_source
