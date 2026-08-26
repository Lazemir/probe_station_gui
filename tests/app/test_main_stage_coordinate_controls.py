import time
import types
import unittest
from unittest import mock

from tests.app.main_coordinate_feedrate_support import (
    Main,
    _FakeJoystick,
    _FakeLineEdit,
    _FakeSettingsManager,
    _FakeStageController,
    _FakeStagePositionPanel,
    _make_main,
    api_move_feedrate,
    main_module,
)
from tests.app.main_route_session_support import (
    _make_cancel_main,
    _make_stage_position_display_main,
)
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.application.stage_motion_types import (
    StageMotionActionState,
    StageMotionConfig,
)
from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    default_axis_calibrations,
)
from probe_station_gui.stage.controller import StageController
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateSystemSnapshot,
)
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetCommonFeedratePlan,
)
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import (
    main_window_coordinate_step as coordinate_step,
    main_window_coordinate_entry as coordinate_entry,
    main_window_needle_calibration as needle_calibration_ui,
)
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)
from probe_station_gui.views.microscope_interaction import (
    ClickMoveBindings,
    ClickMoveConfig,
    MicroscopeInteraction,
)


class MainStageCoordinateControlsTest(unittest.TestCase):
    @staticmethod
    def _apply_curve(
        controller: StageController,
        axis: str,
        controller_points: list[float],
        physical_points: list[float],
    ) -> None:
        settings = default_axis_calibrations()
        settings[axis] = AxisCalibrationSettings(
            enabled=True,
            calibration_file=f"{axis}.npz",
            controller_points=controller_points,
            physical_points=physical_points,
        )
        controller.apply_axis_calibrations(settings)

    def test_manual_terminal_command_invalidates_needles_and_coordinate_confidence(
        self,
    ) -> None:
        events = []
        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            invalidate_needles_state=lambda reason="": events.append(
                ("needles", reason)
            ),
            invalidate_coordinate_confidence=lambda reason="": events.append(
                ("coordinates", reason)
            ),
        )

        with mock.patch.object(coordinate_step, "clear_exact_steps") as clear_exact:
            Main._on_manual_terminal_command(window, "G1 X1")

        self.assertEqual([event[0] for event in events], ["needles", "coordinates"])
        clear_exact.assert_called_once()

    @staticmethod
    def _select_gui_coordinate_system(window: Main, frame_id: str) -> object:
        motion_lease = types.SimpleNamespace(basis_fingerprint=(frame_id,))
        snapshot = CoordinateSystemSnapshot(
            frames_loaded=True,
            records=(),
            document=None,
            selected_frame_id=frame_id,
            display_plan=types.SimpleNamespace(selection_available=True),
            motion_lease=motion_lease,
        )
        window._coordinate_system_coordinator = types.SimpleNamespace(
            snapshot=lambda: snapshot
        )
        return motion_lease

    def test_stage_position_display_updates_caches_while_panel_applies_ui_state(
        self,
    ) -> None:
        window, stage_controller = _make_stage_position_display_main()
        panel = _FakeStagePositionPanel(window._stage_axis_fields)
        window._stage_position_panel = panel
        window._stage_axis_base_styles = panel.base_styles
        window._stage_axis_return_commits = panel.return_commits

        stage_position_panel_adapter.update_stage_position_display(
            window,
            (1.0, 2.0, 3.0),
        )

        self.assertEqual(window._stage_axis_raw_values, {"X": 1.0, "Y": 2.0, "Z": 3.0})
        self.assertEqual(
            window._stage_axis_display_values,
            {"X": 1.0, "Y": 2.0, "Z": 3.0},
        )
        self.assertEqual(window._stage_axis_homed, {"X", "Y"})
        self.assertEqual(len(panel.display_plans), 1)
        self.assertEqual(
            window._stage_axis_fields["X"].tool_tip,
            "X coordinate. Enter targets and press Apply. Move feedrate: 123.0 mm/min.",
        )

    def test_unavailable_calibrated_coordinate_does_not_break_status_update(
        self,
    ) -> None:
        window, stage_controller = _make_stage_position_display_main()
        stage_controller.calibrated_axis_display_value = lambda axis, value: (
            (_ for _ in ()).throw(RuntimeError(f"{axis} unavailable at {value}"))
            if axis == "X"
            else value
        )

        stage_position_panel_adapter.update_stage_position_display(
            window,
            (1.0, 2.0, 3.0),
        )

        self.assertNotIn("X", window._stage_axis_display_values)
        self.assertEqual(window._stage_axis_display_values["Y"], 2.0)

    def test_stage_position_display_preserves_focused_pending_coordinate_edit(
        self,
    ) -> None:
        window, _stage_controller = _make_stage_position_display_main()
        x_field = window._stage_axis_fields["X"]
        x_field.has_focus = True
        x_field.setText("7.777")
        x_field.setModified(True)
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 7.5, 7.777, motion_lease=None
        )
        window._stage_position_panel.pending_targets["X"] = (7.5, 7.777)

        stage_position_panel_adapter.update_stage_position_display(
            window,
            (1.0, 2.0, 3.0),
        )

        self.assertEqual(x_field.text(), "7.777")
        self.assertTrue(x_field.isModified())
        self.assertEqual(window._stage_axis_display_values["X"], 1.0)
        self.assertEqual(
            x_field.tool_tip,
            "X coordinate. Enter targets and press Apply. Move feedrate: 123.0 mm/min.",
        )

    def test_stage_position_display_limit_style_overrides_homed_style(self) -> None:
        window, stage_controller = _make_stage_position_display_main()
        stage_controller.homed_axes = lambda: {"X", "Y", "Z"}
        window._stage_limit_axes = {"X"}

        stage_position_panel_adapter.update_stage_position_display(
            window,
            (1.0, 2.0, 3.0),
        )

        self.assertEqual(window._stage_axis_base_styles["X"], ("#c62828", "#ffffff"))
        self.assertEqual(
            window._stage_axis_fields["X"].styles[-1],
            ("#c62828", "#ffffff"),
        )

    def test_stage_coordinate_mode_change_clears_pending_targets_and_reports_status(
        self,
    ) -> None:
        window = Main.__new__(Main)
        latest_position = (4.0, 5.0, 6.0)
        statuses: list[str] = []
        display_updates: list[tuple[float, ...]] = []
        panel = _FakeStagePositionPanel({"X": _FakeLineEdit()})
        panel.pending_targets["X"] = (1.0, 1.5)
        window._stage_position_panel = panel
        window._stage_axis_return_commits = panel.return_commits
        window._stage_motion_axes = set()
        window._stage_motion_blink_dimmed = False
        window._stage_motion = mock.Mock()
        window._stage_motion.snapshot.return_value = types.SimpleNamespace(
            exact_step_display_targets=()
        )
        window._stage_motion.clear_pending_coordinate_edits.return_value = True
        window._stage_axis_display_values = {"X": 9.0}
        window.stage_controller = types.SimpleNamespace(
            latest_stage_position=lambda: latest_position,
        )
        window._show_status = lambda message, timeout_ms=None: statuses.append(
            f"{message}|{timeout_ms}"
        )
        window._update_stage_coordinate_apply_state = lambda: None

        with mock.patch.object(
            main_module.stage_position_panel_adapter,
            "update_stage_position_display",
            side_effect=lambda _owner, position: display_updates.append(
                tuple(float(value) for value in position)
            ),
        ):
            Main._on_stage_coordinate_mode_changed(window)

        self.assertEqual(panel.pending_targets, {})
        self.assertEqual(panel.pending_only_clear_count, 1)
        self.assertEqual(panel.cleared_display_values, [])
        self.assertEqual(display_updates, [latest_position])
        self.assertEqual(
            statuses,
            ["Cleared pending coordinate edits after input mode change.|2000"],
        )

    def test_stage_coordinate_mode_change_preserves_focused_uncommitted_edit(
        self,
    ) -> None:
        window, stage_controller = _make_stage_position_display_main()
        panel = _FakeStagePositionPanel(window._stage_axis_fields)
        window._stage_position_panel = panel
        window._stage_axis_return_commits = panel.return_commits
        window._stage_axis_base_styles = panel.base_styles
        stage_controller.latest_position = (4.0, 5.0, 6.0, 0.0, 0.0, 0.0)
        statuses: list[str] = []
        window._show_status = lambda message, timeout_ms=None: statuses.append(
            f"{message}|{timeout_ms}"
        )
        x_field = window._stage_axis_fields["X"]
        y_field = window._stage_axis_fields["Y"]
        x_field.setText("12.5")
        panel.pending_targets["X"] = (12.5, 12.5)
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 12.5, 12.5, motion_lease=None
        )
        y_field.has_focus = True
        y_field.setText("7.777")
        y_field.setModified(True)

        Main._on_stage_coordinate_mode_changed(window)

        self.assertEqual(panel.pending_targets, {})
        self.assertEqual(panel.pending_only_clear_count, 1)
        self.assertEqual(
            statuses, ["Cleared pending coordinate edits after input mode change.|2000"]
        )
        self.assertEqual(y_field.text(), "7.777")
        self.assertTrue(y_field.isModified())

    def test_mode_change_restores_selected_system_before_relative_step(self) -> None:
        frame_id = "11111111-1111-4111-8111-111111111111"
        window, _controller, _joystick, _timer, _statuses = _make_main(120.0)
        motion_lease = self._select_gui_coordinate_system(window, frame_id)
        window._stage_axis_display_values["X"] = 1.0
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 10.5, 1.5, motion_lease=motion_lease
        )
        window._stage_position_panel.pending_targets["X"] = (10.5, 1.5)
        render_events: list[str] = []
        projection_calls: list[tuple[tuple[str, float], ...]] = []

        def render_raw(_owner: object, _position: object) -> None:
            render_events.append("raw")
            window._stage_axis_display_values["X"] = 10.0

        def render_selected(_owner: object) -> None:
            render_events.append("selected")
            window._stage_axis_display_values["X"] = 1.0

        def project(axis_values, *, mode, lease, allow_pose_rebase=False):
            self.assertEqual(mode, "G90")
            self.assertIs(lease, motion_lease)
            self.assertFalse(allow_pose_rebase)
            projection_calls.append(tuple(axis_values))
            return types.SimpleNamespace(
                accepted=True,
                lease=motion_lease,
                raw_targets=(("X", 10.001),),
                raw_distances=(("X", 0.001),),
                display_targets=(("X", 1.001),),
                machine_targets=(("X", 10.001),),
                reason="",
            )

        window._project_gui_coordinate_motion = project
        with (
            mock.patch.object(
                main_module.stage_position_panel_adapter,
                "update_stage_position_display",
                side_effect=render_raw,
            ),
            mock.patch.object(
                main_module.stage_position_panel_adapter,
                "refresh_coordinate_frame_display",
                side_effect=render_selected,
            ),
        ):
            Main._on_stage_coordinate_mode_changed(window)
            coordinate_step.on_manual_axis_move_requested(
                window, "X", 0.001, "G91", 120.0
            )

        self.assertEqual(render_events, ["raw", "selected"])
        self.assertEqual(projection_calls, [(("X", 1.001),)])

    def test_saving_needle_down_target_does_not_reapply_full_settings(self) -> None:
        window = Main.__new__(Main)
        stage_controller = _FakeStageController()
        joystick = _FakeJoystick(120.0)
        settings_manager = _FakeSettingsManager()
        statuses: list[str] = []
        full_apply_called: list[bool] = []

        window.stage_controller = stage_controller
        window.joystick_panel = joystick
        window.settings_manager = settings_manager
        window.contact_calibration_window = None
        window._apply_settings = lambda: full_apply_called.append(True)
        window._show_status = lambda message, _timeout_ms=None: statuses.append(
            str(message)
        )

        needle_calibration_ui.save_needle_down_position_from_lowering(window, 2.885)

        self.assertEqual(full_apply_called, [])
        self.assertEqual(settings_manager.saved_count, 1)
        self.assertEqual(
            settings_manager.settings.needle_calibration.down_position_mm,
            2.885,
        )
        self.assertTrue(
            settings_manager.settings.needle_calibration.down_position_configured
        )
        self.assertEqual(
            stage_controller.needle_calibrations[-1]["down_position_mm"],
            2.885,
        )
        self.assertIn(("lower", 2.885), joystick.needle_contacts)
        self.assertIn(
            "Saved needle down target and set current A position to A0.",
            statuses,
        )

    def test_display_needle_target_outside_curve_is_rejected_without_save(self) -> None:
        controller = StageController()
        try:
            controller._position_reporting_mode = "machine"
            self._apply_curve(
                controller,
                "A",
                [-2.0, -1.0, 0.0],
                [-2.0, -1.0, 0.0],
            )
            settings_manager = _FakeSettingsManager()
            statuses: list[str] = []
            owner = types.SimpleNamespace(
                stage_controller=controller,
                settings_manager=settings_manager,
                joystick_panel=None,
                contact_calibration_window=None,
                _display_a_for_needle_lowering=lambda value: -float(value),
                _show_status=lambda message: statuses.append(str(message)),
            )

            needle_calibration_ui.save_needle_position_from_display_a_coordinate(
                owner,
                "lower",
                -3.0,
            )

            self.assertEqual(settings_manager.saved_count, 0)
            self.assertEqual(settings_manager.replaced_settings, [])
            self.assertIn("outside the coordinate calibration range", statuses[-1])
        finally:
            controller.shutdown()

    def test_display_needle_target_at_curve_endpoint_is_saved(self) -> None:
        controller = StageController()
        try:
            controller._position_reporting_mode = "machine"
            self._apply_curve(
                controller,
                "A",
                [-2.0, -1.0, 0.0],
                [-2.0, -1.0, 0.0],
            )
            settings_manager = _FakeSettingsManager()
            statuses: list[str] = []
            owner = types.SimpleNamespace(
                stage_controller=controller,
                settings_manager=settings_manager,
                joystick_panel=None,
                contact_calibration_window=None,
                _display_a_for_needle_lowering=lambda value: -float(value),
                _show_status=lambda message: statuses.append(str(message)),
            )

            needle_calibration_ui.save_needle_position_from_display_a_coordinate(
                owner,
                "lower",
                -2.0,
            )

            self.assertEqual(settings_manager.saved_count, 1)
            self.assertEqual(len(settings_manager.replaced_settings), 1)
            self.assertEqual(
                settings_manager.settings.needle_calibration.down_position_mm,
                2.0,
            )
            self.assertIn("Saved needle lower target", statuses[-1])
        finally:
            controller.shutdown()

    def test_api_calibrated_display_target_outside_curve_is_rejected_before_send(
        self,
    ) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        calibrated_controller = StageController()
        try:
            self._apply_curve(
                calibrated_controller,
                "Z",
                [0.0, 1.0, 3.0],
                [0.0, 2.0, 5.0],
            )
            stage_controller.calibrated_axis_raw_value = (
                calibrated_controller.calibrated_axis_raw_value
            )
            stage_controller.calibrated_axis_raw_target_value = (
                calibrated_controller.calibrated_axis_raw_target_value
            )
            stage_controller.coordinate_display_name = lambda: "Machine"
            window._stage_axis_raw_values["Z"] = 1.0
            window._stage_axis_display_values["Z"] = 2.0

            response = Main._api_move_to_coordinates(
                window,
                {"Z": 6.0},
                mode="G90",
            )

            self.assertFalse(response["accepted"])
            self.assertEqual(response["status_code"], 409)
            self.assertEqual(stage_controller.requests, [])
        finally:
            calibrated_controller.shutdown()

    def test_common_feedrate_projection_updates_joystick(self) -> None:
        window, _stage_controller, joystick, _timer, _statuses = _make_main(120.0)
        window._stage_motion.coordinate_common_feedrate = (
            CoordinateTargetCommonFeedratePlan(
                feedrate_mm_min=120.0,
                max_feedrate_mm_min=100.0,
                clear_common_target=False,
            )
        )

        stage_position_panel_adapter.apply_coordinate_common_feedrate(window)

        self.assertEqual(joystick.common_targets, [(120.0, 100.0)])

    def test_api_move_without_feedrate_uses_current_gui_feedrate(self) -> None:
        window, _stage_controller, _joystick, _timer, _statuses = _make_main(77.0)

        self.assertEqual(
            api_move_feedrate(
                None,
                current_feedrate=window._current_linear_feedrate(),
                min_feedrate=window.MIN_FEEDRATE_MM_MIN,
            ),
            77.0,
        )

    def test_api_move_to_coordinates_rejects_active_coordinate_move_before_stage_busy_check(
        self,
    ) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(77.0)
        window._stage_motion.coordinate_active = True
        window._resolve_api_stage_axis_target = (
            lambda axis, display_target, input_mode: (
                display_target,
                display_target,
            )
        )
        stage_controller.is_busy = mock.Mock(
            side_effect=AssertionError("stage busy check should not run")
        )
        window._stage_motion.start_coordinate_move = mock.Mock(
            side_effect=AssertionError("start should not run")
        )

        response = Main._api_move_to_coordinates(window, {"X": 1.0})

        self.assertEqual(
            response,
            {
                "accepted": False,
                "status_code": 409,
                "message": "Stage is busy. Ignoring API coordinate target.",
            },
        )

    def test_api_move_to_coordinates_rejects_stage_busy_before_start(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(77.0)
        window._resolve_api_stage_axis_target = (
            lambda axis, display_target, input_mode: (
                display_target,
                display_target,
            )
        )
        stage_controller.is_busy = mock.Mock(return_value=True)
        window._stage_motion.start_coordinate_move = mock.Mock(
            side_effect=AssertionError("start should not run")
        )

        response = Main._api_move_to_coordinates(window, {"X": 1.0})

        self.assertEqual(
            response,
            {
                "accepted": False,
                "status_code": 409,
                "message": "Stage is busy. Ignoring API coordinate target.",
            },
        )
        self.assertEqual(stage_controller.is_busy.call_count, 2)

    def test_api_move_to_coordinates_returns_start_failure_response(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(77.0)
        window._resolve_api_stage_axis_target = (
            lambda axis, display_target, input_mode: (
                display_target + 10.0,
                display_target + 0.25,
            )
        )
        stage_controller.is_busy = mock.Mock(return_value=False)
        window._stage_motion.start_coordinate_move = mock.Mock(return_value=False)

        response = Main._api_move_to_coordinates(window, {"X": 1.0})

        self.assertEqual(
            response,
            {
                "accepted": False,
                "status_code": 409,
                "message": "Unable to start coordinate move.",
            },
        )
        request = window._stage_motion.start_coordinate_move.call_args.args[0]
        self.assertEqual(request.targets, (("X", 11.0, 1.25),))
        self.assertEqual(request.feedrate_mm_min, 77.0)
        self.assertEqual(request.source_label, "API")
        self.assertEqual(request.physical_limit_targets, (("X", 1.25),))

    def test_api_move_to_coordinates_starts_with_raw_and_display_targets(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(77.0)

        def resolve_api_axis_target(
            _axis: str,
            display_target: float,
            input_mode: str,
        ) -> tuple[float | None, float]:
            raw_offset = 100.0 if input_mode == "G91" else 0.0
            return display_target + raw_offset, display_target + 0.5

        window._resolve_api_stage_axis_target = resolve_api_axis_target
        stage_controller.is_busy = mock.Mock(return_value=False)
        stage_controller.coordinate_display_name = mock.Mock(return_value="Work")
        window._stage_motion.start_coordinate_move = mock.Mock(return_value=True)

        response = Main._api_move_to_coordinates(
            window,
            {"Y": 2.0, "X": 1.0},
            mode="relative",
        )

        request = window._stage_motion.start_coordinate_move.call_args.args[0]
        self.assertEqual(
            request.targets,
            (("X", 101.0, 1.5), ("Y", 102.0, 2.5)),
        )
        self.assertEqual(request.feedrate_mm_min, 77.0)
        self.assertEqual(request.source_label, "API")
        self.assertEqual(
            request.physical_limit_targets,
            (("X", 1.5), ("Y", 2.5)),
        )
        self.assertEqual(
            response,
            {
                "accepted": True,
                "message": "API coordinate move accepted: X, Y.",
                "started_axes": ["X", "Y"],
                "queued_axes": [],
                "mode": "G91",
                "current_feedrate_mm_min": 77.0,
                "coordinate_display": "Machine",
                "targets": {"X": 1.5, "Y": 2.5},
            },
        )

    def test_common_feedrate_projection_clears_joystick_target(self) -> None:
        window, _stage_controller, joystick, _timer, _statuses = _make_main(120.0)

        stage_position_panel_adapter.apply_coordinate_common_feedrate(window)

        self.assertEqual(joystick.common_targets, [])
        self.assertEqual(joystick.common_cleared, 1)

    def test_pending_xy_coordinate_move_uses_xy_feedrate(self) -> None:
        window, stage_controller, joystick, _timer, _statuses = _make_main(999.0)
        joystick.coordinate_feedrate = 42.0
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 5.0, 5.0, motion_lease=None
        )
        window._stage_motion.upsert_pending_coordinate_edit(
            "Y", -2.0, -2.0, motion_lease=None
        )

        coordinate_entry.apply_pending_stage_coordinate_targets(window)

        self.assertEqual(joystick.coordinate_axes, [("X", "Y")])
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0, "Y": -2.0}, 42.0)],
        )
        self.assertEqual(joystick.common_targets, [])

    def test_coordinate_apply_switches_step_mode_to_jog_before_feedrate(self) -> None:
        window, stage_controller, joystick, _timer, _statuses = _make_main(99.0)
        joystick.mode = "step"
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 5.0, 5.0, motion_lease=None
        )

        coordinate_entry.apply_pending_stage_coordinate_targets(window)

        self.assertEqual(joystick.mode_changes, [("jog", True)])
        self.assertEqual(joystick.coordinate_modes, ["jog"])
        self.assertEqual(stage_controller.requests, [({"X": 5.0}, 99.0)])

    def test_non_machine_apply_projects_compensated_axes_with_display_lease(
        self,
    ) -> None:
        frame_id = "11111111-1111-4111-8111-111111111111"
        window, stage_controller, _joystick, _timer, _statuses = _make_main(99.0)
        motion_lease = self._select_gui_coordinate_system(window, frame_id)
        projection_calls: list[tuple[tuple[tuple[str, float], ...], str, object]] = []
        limit_calls: list[tuple[str, float]] = []
        window._machine_axis_target_limit_error = lambda axis, target: (
            limit_calls.append((axis, target)) or None
        )

        def project(axis_values, *, mode, lease, allow_pose_rebase=False):
            projection_calls.append((tuple(axis_values), mode, lease))
            return types.SimpleNamespace(
                accepted=True,
                lease=motion_lease,
                raw_targets=(("X", 10.0), ("Y", 20.0)),
                raw_distances=(("X", 9.0), ("Y", 18.0)),
                display_targets=(("X", 1.0), ("Y", 2.0)),
                machine_targets=(("X", 101.0), ("Y", 102.0)),
                reason="",
            )

        window._project_gui_coordinate_motion = project
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 99.0, 1.0, motion_lease=motion_lease
        )

        with mock.patch.object(
            stage_position_update,
            "publish_stage_position_estimate",
        ):
            coordinate_entry.apply_pending_stage_coordinate_targets(window)

        self.assertEqual(
            projection_calls,
            [((("X", 1.0),), "G90", motion_lease)],
        )
        self.assertIs(projection_calls[0][2], motion_lease)
        self.assertEqual(
            stage_controller.requests,
            [({"X": 10.0, "Y": 20.0}, 99.0)],
        )
        self.assertEqual(
            limit_calls,
            [
                ("X", 101.0),
                ("Y", 102.0),
            ],
        )

    def test_rejected_coordinate_reprojection_clears_session_and_panel_pending(
        self,
    ) -> None:
        window, _controller, _joystick, _timer, statuses = _make_main(99.0)
        motion_lease = self._select_gui_coordinate_system(window, "design-a")
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 10.0, 1.0, motion_lease=motion_lease
        )
        window._stage_position_panel.set_pending_target("X", 10.0, 1.0)
        window._project_gui_coordinate_motion = lambda *_args, **_kwargs: (
            types.SimpleNamespace(
                accepted=False,
                reason="Coordinate System authority changed before movement.",
            )
        )

        coordinate_entry.apply_pending_stage_coordinate_targets(window)

        self.assertEqual(window._stage_motion.pending_coordinate_edits().targets, ())
        self.assertEqual(window._stage_position_panel.pending_targets, {})
        self.assertIn("Coordinate System authority changed", statuses[-1])

    def test_apply_validation_rejection_preserves_session_and_panel_pending(
        self,
    ) -> None:
        window, _controller, _joystick, _timer, _statuses = _make_main(99.0)
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 10.0, 1.0, motion_lease=None
        )
        window._stage_position_panel.set_pending_target("X", 10.0, 1.0)
        window._stage_motion.start_result = False

        coordinate_entry.apply_pending_stage_coordinate_targets(window)

        self.assertEqual(
            window._stage_motion.pending_coordinate_edits().targets,
            (("X", 10.0, 1.0),),
        )
        self.assertEqual(
            window._stage_position_panel.pending_targets,
            {"X": (10.0, 1.0)},
        )

    def test_apply_controller_rejection_clears_planned_session_and_panel_axes(
        self,
    ) -> None:
        window, controller, _joystick, _timer, _statuses = _make_main(99.0)
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 10.0, 1.0, motion_lease=None
        )
        window._stage_position_panel.set_pending_target("X", 10.0, 1.0)
        controller.request_absolute_axis_targets_move = mock.Mock(return_value=False)

        coordinate_entry.apply_pending_stage_coordinate_targets(window)

        self.assertEqual(window._stage_motion.pending_coordinate_edits().targets, ())
        self.assertEqual(window._stage_position_panel.pending_targets, {})

    def test_machine_apply_uses_rendered_physical_snapshot_lease(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(99.0)
        motion_lease = self._select_gui_coordinate_system(window, "machine")
        projection_calls: list[tuple[tuple[tuple[str, float], ...], object]] = []

        def project(axis_values, *, mode, lease, allow_pose_rebase=False):
            self.assertEqual(mode, "G90")
            self.assertFalse(allow_pose_rebase)
            projection_calls.append((tuple(axis_values), lease))
            return types.SimpleNamespace(
                accepted=True,
                lease=motion_lease,
                raw_targets=(("X", 5.5),),
                raw_distances=(("X", 0.5),),
                display_targets=(("X", 21.0),),
                machine_targets=(("X", 21.0),),
                reason="",
            )

        window._project_gui_coordinate_motion = project
        window._stage_motion.upsert_pending_coordinate_edit(
            "X", 999.0, 21.0, motion_lease=motion_lease
        )

        with mock.patch.object(
            stage_position_update,
            "publish_stage_position_estimate",
        ):
            coordinate_entry.apply_pending_stage_coordinate_targets(window)

        self.assertEqual(projection_calls, [((("X", 21.0),), motion_lease)])
        self.assertEqual(stage_controller.requests, [({"X": 5.5}, 99.0)])

    def test_non_machine_step_reuses_lease_and_projects_compensated_axes(
        self,
    ) -> None:
        frame_id = "22222222-2222-4222-8222-222222222222"
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        motion_lease = self._select_gui_coordinate_system(window, frame_id)
        projection_calls: list[tuple[tuple[tuple[str, float], ...], str, object]] = []

        def project(axis_values, *, mode, lease, allow_pose_rebase=False):
            projection_calls.append((tuple(axis_values), mode, lease))
            return types.SimpleNamespace(
                accepted=True,
                lease=motion_lease,
                raw_targets=(("X", 10.001), ("Y", 20.0)),
                raw_distances=(("X", 0.001), ("Y", 0.0)),
                display_targets=(("X", 1.001), ("Y", 2.0)),
                machine_targets=(("X", 1.001), ("Y", 2.0)),
                reason="",
            )

        window._project_gui_coordinate_motion = project
        window._stage_axis_display_values["X"] = 1.0

        coordinate_step.on_manual_axis_move_requested(window, "X", 0.001, "G91", 120.0)

        self.assertEqual(len(projection_calls), 1)
        self.assertEqual(
            [call[:2] for call in projection_calls],
            [((("X", 1.001),), "G90")],
        )
        self.assertTrue(all(call[2] is motion_lease for call in projection_calls))
        self.assertEqual(
            window._stage_motion.exact_requests[-1].move_request.targets,
            (("X", 10.001, 1.001), ("Y", 20.0, 2.0)),
        )
        self.assertEqual(stage_controller.requests, [])

    def test_api_machine_target_is_not_reused_as_selected_design_step_baseline(
        self,
    ) -> None:
        frame_id = "33333333-3333-4333-8333-333333333333"
        window, _stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        window._stage_motion.coordinate_active = True
        window._stage_motion.active_axes = frozenset({"X"})
        window._stage_motion.coordinate_display_targets = (("X", 50.0),)
        window._stage_motion.coordinate_display_basis = None
        motion_lease = self._select_gui_coordinate_system(window, frame_id)
        window._stage_axis_display_values["X"] = 1.0
        self.assertIsNone(window._stage_motion.snapshot().coordinate_display_basis)
        projection_calls: list[tuple[tuple[str, float], ...]] = []

        def project(axis_values, *, mode, lease, allow_pose_rebase=False):
            self.assertEqual(mode, "G90")
            self.assertIs(lease, motion_lease)
            self.assertTrue(allow_pose_rebase)
            projection_calls.append(tuple(axis_values))
            return types.SimpleNamespace(
                accepted=True,
                lease=motion_lease,
                raw_targets=(("X", 10.001),),
                raw_distances=(("X", 0.001),),
                display_targets=(("X", 1.001),),
                machine_targets=(("X", 1.001),),
                reason="",
            )

        window._project_gui_coordinate_motion = project

        coordinate_step.on_manual_axis_move_requested(window, "X", 0.001, "G91", 120.0)

        self.assertEqual(projection_calls, [(("X", 1.001),)])

    def test_non_machine_step_builds_frozen_lease_then_allows_pose_rebase(
        self,
    ) -> None:
        frame_id = "33333333-3333-4333-8333-333333333333"
        window, _controller, _joystick, _timer, _statuses = _make_main(120.0)
        start_lease = self._select_gui_coordinate_system(window, frame_id)
        current_lease = {"value": start_lease}
        window._coordinate_system_coordinator.snapshot = lambda: types.SimpleNamespace(
            selected_frame_id=frame_id,
            display_plan=types.SimpleNamespace(selection_available=True),
            motion_lease=current_lease["value"],
        )
        calls: list[tuple[object, bool]] = []

        def project(axis_values, *, mode, lease, allow_pose_rebase=False):
            calls.append((lease, bool(allow_pose_rebase)))
            display_x = dict(axis_values)["X"]
            return types.SimpleNamespace(
                accepted=True,
                lease=current_lease["value"],
                raw_targets=(("X", display_x + 9.0),),
                raw_distances=(("X", 0.001),),
                display_targets=(("X", display_x),),
                machine_targets=(("X", display_x),),
                reason="",
            )

        window._project_gui_coordinate_motion = project
        window._stage_axis_display_values["X"] = 1.0
        coordinate_step.on_manual_axis_move_requested(window, "X", 0.001, "G91", 120.0)
        next_lease = types.SimpleNamespace(basis_fingerprint=(frame_id,))
        current_lease["value"] = next_lease
        window._stage_motion.coordinate_active = True
        coordinate_step.on_manual_axis_move_requested(window, "X", 0.001, "G91", 120.0)

        self.assertEqual(calls, [(start_lease, False), (start_lease, True)])
        self.assertIs(window._stage_motion.exact_step_motion_lease, next_lease)

    def test_rejected_step_preserves_previous_valid_projected_request(self) -> None:
        window, _controller, _joystick, _timer, statuses = _make_main(120.0)
        window._stage_axis_display_values["X"] = 1.0
        window._stage_axis_target_limit_error = lambda _axis, target: (
            "outside" if float(target) > 1.0015 else None
        )

        coordinate_step.on_manual_axis_move_requested(window, "X", 0.001, "G91", 120.0)
        coordinate_step.on_manual_axis_move_requested(window, "X", 0.001, "G91", 120.0)

        self.assertEqual(len(window._stage_motion.exact_requests), 1)
        self.assertEqual(
            window._stage_motion.exact_requests[0].pending_targets,
            (("X", 1.001, 1.001),),
        )
        self.assertIn("outside", statuses)

    def test_step_adapter_coalesces_multiple_axes_in_resolved_request(self) -> None:
        window, _controller, _joystick, _timer, _statuses = _make_main(120.0)
        window._stage_axis_display_values.update({"X": 1.0, "Y": 2.0})

        coordinate_step.on_manual_axis_move_requested(window, "X", 0.001, "G91", 120.0)
        coordinate_step.on_manual_axis_move_requested(window, "Y", -0.002, "G91", 120.0)

        self.assertEqual(
            window._stage_motion.exact_requests[-1].move_request.targets,
            (("X", 1.001, 1.001), ("Y", 1.998, 1.998)),
        )

    def test_leaving_step_mode_calls_semantic_session_clear(self) -> None:
        window, _controller, _joystick, _timer, _statuses = _make_main(120.0)
        window.settings_manager = _FakeSettingsManager()
        window.settings_manager.settings.jog.mode = "step"
        window._stage_axis_display_values["X"] = 1.0
        coordinate_step.on_manual_axis_move_requested(window, "X", 0.001, "G91", 120.0)

        Main._save_jog_control_mode(window, "jog")

        self.assertEqual(window._stage_motion.snapshot().exact_step_display_targets, ())
        self.assertEqual(window._stage_motion.pending_coordinate_edits().targets, ())

    def test_cancel_button_is_enabled_for_generic_busy_stage_task(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.busy = True

        Main._update_stage_coordinate_apply_state(window)

        self.assertTrue(cancel_button.enabled)

    def test_pending_click_refreshes_global_cancel_enabled_state(self) -> None:
        window, _stage_controller, cancel_button, _statuses = _make_cancel_main()
        interaction = MicroscopeInteraction(
            ClickMoveBindings(
                request_move=lambda _dx, _dy: False,
                stage_connected=lambda: False,
                motion_blocked=lambda: False,
                mark_motion_axes=lambda _axes: None,
                show_status=lambda _message, _timeout_ms=0: None,
                repaint=lambda: None,
                preview_hover=lambda _dx, _dy: None,
                present_coordinates=lambda **_coordinates: None,
                manual_alignment_active=lambda: False,
                capture_manual_alignment=lambda _dx, _dy: None,
                pending_state_changed=lambda _pending: (
                    Main._update_stage_coordinate_apply_state(window)
                ),
            ),
            ClickMoveConfig(pending_timeout_s=lambda: 2.0),
        )
        window._microscope_interaction = interaction

        interaction.try_start_move(2.0, -1.0, 0.6, 0.4)
        self.assertTrue(cancel_button.enabled)

        interaction.cancel_pending(clear_target=True)
        self.assertFalse(cancel_button.enabled)
        interaction.shutdown()

    def test_cancel_button_is_enabled_for_reported_controller_motion(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"

        Main._update_stage_coordinate_apply_state(window)

        self.assertTrue(cancel_button.enabled)

    def test_stale_reported_controller_motion_does_not_enable_cancel(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"
        stage_controller.last_status_time = (
            time.monotonic() - StageMotionConfig.controller_active_state_stale_s - 0.5
        )

        Main._update_stage_coordinate_apply_state(window)

        self.assertFalse(cancel_button.enabled)

    def test_apply_cancel_state_uses_panel_pending_and_cancelable_operation_flags(
        self,
    ) -> None:
        window = Main.__new__(Main)
        panel = _FakeStagePositionPanel(
            {"X": _FakeLineEdit(enabled=True, modified=True)}
        )
        panel.pending_targets["X"] = (5.0, 5.0)
        window._stage_position_panel = panel
        window.stage_controller = types.SimpleNamespace(is_busy=lambda: False)
        window._stage_motion = mock.Mock()
        window._stage_motion.snapshot.return_value = types.SimpleNamespace(
            coordinate_active=False,
            coordinate_common_feedrate=CoordinateTargetCommonFeedratePlan(
                clear_common_target=True
            ),
        )

        with mock.patch.object(
            stage_move_lifecycle,
            "has_application_cancelable_operation",
            return_value=True,
        ):
            Main._update_stage_coordinate_apply_state(window)

        self.assertEqual(panel.action_button_states[-1], (True, True))

    def test_pending_edit_action_state_keeps_apply_and_cancel_enabled(self) -> None:
        window, _controller, _cancel_button, _statuses = _make_cancel_main()
        window._stage_position_panel.pending_targets["X"] = (5.0, 5.0)

        Main._update_stage_coordinate_apply_state(
            window,
            StageMotionActionState(
                active_axes=frozenset(),
                cancelable=True,
                coordinate_active=False,
                planned_pending=False,
                planned_active=False,
            ),
        )

        self.assertEqual(
            window._stage_position_panel.action_button_states[-1],
            (True, True),
        )

    def test_cancel_button_cancels_generic_busy_stage_task(self) -> None:
        window, stage_controller, _cancel_button, statuses = _make_cancel_main()
        stage_controller.busy = True

        stage_position_panel_adapter.cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_tasks,
            ["Operation cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_motions, [])
        self.assertIn("Cancel requested.", statuses)

    def test_cancel_button_keeps_coordinate_move_on_jog_cancel_path(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        window._stage_motion.coordinate_active = True
        window._stage_motion.active_axes = frozenset({"X"})

        stage_position_panel_adapter.cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_motions,
            ["Coordinate move cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_tasks, [])

    def test_cancel_button_cancels_reported_controller_motion(self) -> None:
        window, stage_controller, _cancel_button, statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"

        stage_position_panel_adapter.cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_motions,
            ["Motion cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_tasks, [])
        self.assertIn("Cancel requested.", statuses)

    def test_home_all_ignores_stale_jog_state_after_cancel(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"
        stage_controller.last_status_time = (
            time.monotonic() - StageMotionConfig.controller_active_state_stale_s - 0.5
        )
        window._refresh_pending_homing_ui = lambda: None

        homing_ui.request_home_all_from_ui(window)

        self.assertEqual(stage_controller.home_all_requests, 1)
        self.assertEqual(stage_controller.status_message.messages, [])

    def test_home_all_sends_all_home_during_fresh_jog_state(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"
        stage_controller.last_status_time = time.monotonic()
        window._refresh_pending_homing_ui = lambda: None

        homing_ui.request_home_all_from_ui(window)

        self.assertEqual(stage_controller.home_all_requests, 1)
        self.assertEqual(stage_controller.home_axis_requests, [])
        self.assertEqual(window._pending_homing_axes, [])
        self.assertEqual(stage_controller.status_message.messages, [])

    def test_manual_axis_home_queues_second_axis_while_first_is_active(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        window._refresh_pending_homing_ui = lambda: None

        homing_ui.request_home_axis_from_ui(window, "X")
        window._homing_active_key = "X"
        homing_ui.request_home_axis_from_ui(window, "Z")

        self.assertEqual(stage_controller.home_all_requests, 0)
        self.assertEqual(stage_controller.home_axis_requests, ["X"])
        self.assertEqual(window._pending_homing_axes, ["Z"])


if __name__ == "__main__":
    unittest.main()
