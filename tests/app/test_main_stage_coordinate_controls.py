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
    _coordinate_target_state,
    _make_cancel_main,
    _make_main,
    _make_stage_position_display_main,
    api_move_feedrate,
    main_module,
)
from probe_station_gui.views import main_window_homing as homing_ui
from probe_station_gui.views import main_window_needle_calibration as needle_calibration_ui
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)


class MainStageCoordinateControlsTest(unittest.TestCase):
    def test_stage_position_display_updates_caches_while_panel_applies_ui_state(
        self,
    ) -> None:
        window, stage_controller = _make_stage_position_display_main()
        panel = _FakeStagePositionPanel(window._stage_axis_fields)
        window._stage_position_panel = panel
        window._stage_axis_base_styles = panel.base_styles
        window._pending_stage_axis_targets = panel.pending_targets
        window._stage_axis_return_commits = panel.return_commits

        Main._update_stage_position_display(window, (1.0, 2.0, 3.0))

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

    def test_stage_position_display_preserves_focused_pending_coordinate_edit(self) -> None:
        window, _stage_controller = _make_stage_position_display_main()
        x_field = window._stage_axis_fields["X"]
        x_field.has_focus = True
        x_field.setText("7.777")
        x_field.setModified(True)
        window._pending_stage_axis_targets["X"] = (7.5, 7.777)

        Main._update_stage_position_display(window, (1.0, 2.0, 3.0))

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

        Main._update_stage_position_display(window, (1.0, 2.0, 3.0))

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
        window._pending_stage_axis_targets = panel.pending_targets
        window._stage_axis_return_commits = panel.return_commits
        window._stage_axis_display_values = {"X": 9.0}
        window.stage_controller = types.SimpleNamespace(
            latest_stage_position=lambda: latest_position,
        )
        window._update_stage_position_display = lambda position: display_updates.append(
            tuple(float(value) for value in position)
        )
        window._show_status = lambda message, timeout_ms=None: statuses.append(
            f"{message}|{timeout_ms}"
        )
        window._update_stage_coordinate_apply_state = lambda: None

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
        window._pending_stage_axis_targets = panel.pending_targets
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
        y_field.has_focus = True
        y_field.setText("7.777")
        y_field.setModified(True)

        Main._on_stage_coordinate_mode_changed(window)

        self.assertEqual(panel.pending_targets, {})
        self.assertEqual(panel.pending_only_clear_count, 1)
        self.assertEqual(statuses, ["Cleared pending coordinate edits after input mode change.|2000"])
        self.assertEqual(y_field.text(), "7.777")
        self.assertTrue(y_field.isModified())

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
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
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

    def test_coordinate_move_records_programmed_feedrate(self) -> None:
        window, stage_controller, _joystick, timer, _statuses = _make_main(120.0)

        accepted = Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )

        self.assertTrue(accepted)
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0, "Y": -2.0}, 120.0)],
        )
        self.assertEqual(window._coordinate_targets.programmed_feedrate, 120.0)
        self.assertEqual(window._coordinate_targets.effective_feedrate, 120.0)
        self.assertTrue(timer.started)
        self.assertEqual(_joystick.common_targets, [])
        self.assertEqual(_joystick.common_cleared, 1)

    def test_mixed_axis_coordinate_move_shows_common_feedrate(self) -> None:
        window, _stage_controller, joystick, _timer, _statuses = _make_main(120.0)

        accepted = Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Z": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )

        self.assertTrue(accepted)
        self.assertEqual(joystick.common_targets, [(120.0, 100.0)])

    def test_api_move_without_feedrate_uses_current_gui_feedrate(self) -> None:
        window, _stage_controller, _joystick, _timer, _statuses = _make_main(77.0)

        self.assertEqual(
            api_move_feedrate(None, current_feedrate=window._current_linear_feedrate(), min_feedrate=window.MIN_FEEDRATE_MM_MIN),
            77.0,
        )

    def test_api_move_to_coordinates_rejects_active_coordinate_move_before_stage_busy_check(
        self,
    ) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(77.0)
        window._coordinate_targets.active_axis = "X"
        window._resolve_stage_axis_target = (
            lambda axis, display_target, input_mode: (
                display_target,
                display_target,
            )
        )
        stage_controller.is_busy = mock.Mock(
            side_effect=AssertionError("stage busy check should not run")
        )
        window._start_coordinate_targets_move = mock.Mock(
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
        window._resolve_stage_axis_target = (
            lambda axis, display_target, input_mode: (
                display_target,
                display_target,
            )
        )
        stage_controller.is_busy = mock.Mock(return_value=True)
        window._start_coordinate_targets_move = mock.Mock(
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
        stage_controller.is_busy.assert_called_once_with()

    def test_api_move_to_coordinates_returns_start_failure_response(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(77.0)
        window._resolve_stage_axis_target = (
            lambda axis, display_target, input_mode: (
                display_target + 10.0,
                display_target + 0.25,
            )
        )
        stage_controller.is_busy = mock.Mock(return_value=False)
        window._start_coordinate_targets_move = mock.Mock(return_value=False)

        response = Main._api_move_to_coordinates(window, {"X": 1.0})

        self.assertEqual(
            response,
            {
                "accepted": False,
                "status_code": 409,
                "message": "Unable to start coordinate move.",
            },
        )
        window._start_coordinate_targets_move.assert_called_once_with(
            {"X": (11.0, 1.25)},
            feedrate_mm_min=77.0,
            source_label="API",
        )

    def test_api_move_to_coordinates_starts_with_raw_and_display_targets(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(77.0)

        def resolve_axis_target(
            _axis: str,
            display_target: float,
            input_mode: str,
        ) -> tuple[float | None, float]:
            raw_offset = 100.0 if input_mode == "G91" else 0.0
            return display_target + raw_offset, display_target + 0.5

        window._resolve_stage_axis_target = resolve_axis_target
        stage_controller.is_busy = mock.Mock(return_value=False)
        stage_controller.coordinate_display_name = mock.Mock(return_value="Work")
        window._start_coordinate_targets_move = mock.Mock(return_value=True)

        response = Main._api_move_to_coordinates(
            window,
            {"Y": 2.0, "X": 1.0},
            mode="relative",
        )

        window._start_coordinate_targets_move.assert_called_once_with(
            {"X": (101.0, 1.5), "Y": (102.0, 2.5)},
            feedrate_mm_min=77.0,
            source_label="API",
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
                "coordinate_display": "Work",
                "targets": {"X": 1.5, "Y": 2.5},
            },
        )

    def test_single_axis_coordinate_move_does_not_show_common_feedrate(self) -> None:
        window, _stage_controller, joystick, _timer, _statuses = _make_main(120.0)

        accepted = Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )

        self.assertTrue(accepted)
        self.assertEqual(joystick.common_targets, [])
        self.assertEqual(joystick.common_cleared, 1)

    def test_pending_xy_coordinate_move_uses_xy_feedrate(self) -> None:
        window, stage_controller, joystick, _timer, _statuses = _make_main(999.0)
        joystick.coordinate_feedrate = 42.0
        window._pending_stage_axis_targets = {
            "X": (5.0, 5.0),
            "Y": (-2.0, -2.0),
        }

        Main._apply_pending_stage_coordinate_targets(window)

        self.assertEqual(joystick.coordinate_axes, [("X", "Y")])
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0, "Y": -2.0}, 42.0)],
        )
        self.assertEqual(joystick.common_targets, [])

    def test_coordinate_apply_switches_step_mode_to_jog_before_feedrate(self) -> None:
        window, stage_controller, joystick, _timer, _statuses = _make_main(99.0)
        joystick.mode = "step"
        window._pending_stage_axis_targets = {"X": (5.0, 5.0)}

        Main._apply_pending_stage_coordinate_targets(window)

        self.assertEqual(joystick.mode_changes, [("jog", True)])
        self.assertEqual(joystick.coordinate_modes, ["jog"])
        self.assertEqual(stage_controller.requests, [({"X": 5.0}, 99.0)])

    def test_coordinate_move_feedrate_change_reissues_absolute_jog(self) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        advances = []
        window._advance_coordinate_move_prediction = lambda: advances.append(True)
        stage_controller.busy = True

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(stage_controller.jog_stops, 1)
        self.assertEqual(stage_controller.absolute_jog_replace_flags, [True])
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0}, 120.0), ({"X": 5.0}, 180.0)],
        )
        self.assertEqual(window._coordinate_targets.programmed_feedrate, 180.0)
        self.assertEqual(window._coordinate_targets.effective_feedrate, 180.0)
        self.assertTrue(advances)
        self.assertTrue(
            any(
                "Active coordinate move feedrate: F180.0." in item
                for item in statuses
            )
        )

    def test_coordinate_move_feedrate_change_uses_active_axis_set(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        window._coordinate_targets.active_axis = None
        window._advance_coordinate_move_prediction = lambda: None
        stage_controller.busy = True

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(stage_controller.jog_stops, 1)
        self.assertEqual(stage_controller.absolute_jog_replace_flags, [True])
        self.assertEqual(
            stage_controller.requests,
            [
                ({"X": 5.0, "Y": -2.0}, 120.0),
                ({"X": 5.0, "Y": -2.0}, 180.0),
            ],
        )
        self.assertEqual(window._coordinate_targets.programmed_feedrate, 180.0)
        self.assertEqual(window._coordinate_targets.effective_feedrate, 180.0)

    def test_coordinate_move_feedrate_change_keeps_tracking_after_busy_reissue_cancel(
        self,
    ) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.busy = True
        window._advance_coordinate_move_prediction = lambda: None

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertTrue(window._coordinate_targets.reissue_cancel_pending)
        self.assertEqual(stage_controller.absolute_jog_replace_flags, [True])
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0}, 120.0), ({"X": 5.0}, 180.0)],
        )

        Main.on_move_finished(window, False, "Operation cancelled.")

        self.assertFalse(window._coordinate_targets.reissue_cancel_pending)
        self.assertEqual(window._coordinate_targets.active_axis, "X")
        self.assertEqual(window._coordinate_targets.active_axes, {"X"})
        self.assertEqual(window._coordinate_targets.programmed_feedrate, 180.0)
        self.assertFalse(
            any("Operation cancelled." in item for item in statuses)
        )

    def test_coordinate_move_feedrate_reissue_failure_keeps_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.next_absolute_jog_accept = False
        stage_controller.busy = True
        scheduled_delays: list[tuple[int, ...]] = []
        window._schedule_status_refreshes = (
            lambda delays: scheduled_delays.append(tuple(delays))
        )
        window._advance_coordinate_move_prediction = lambda: None

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(window._coordinate_targets.active_axis, "X")
        self.assertEqual(window._coordinate_targets.active_axes, {"X"})
        self.assertEqual(window._coordinate_targets.programmed_feedrate, 120.0)
        self.assertEqual(window._coordinate_targets.effective_feedrate, 120.0)
        self.assertEqual(stage_controller.requests, [({"X": 5.0}, 120.0)])
        self.assertEqual(stage_controller.jog_stops, 0)
        self.assertTrue(scheduled_delays)
        self.assertTrue(
            any(
                "Unable to update coordinate move feedrate." in item
                for item in statuses
            )
        )

    def test_manual_jog_clears_active_coordinate_move_tracking(self) -> None:
        window, _stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Z": (3.84, 3.84)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )

        Main._on_manual_jog_command_changed(window, (("X", -250.0),), 60.0)

        self.assertIsNone(window._coordinate_targets.active_axis)
        self.assertEqual(window._coordinate_targets.active_axes, set())
        self.assertEqual(window._stage_motion_axes, {"X"})

    def test_manual_jog_start_pauses_terminal_poll_and_publishes_seeded_position(
        self,
    ) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        paused: list[bool] = []
        published: list[tuple[float, ...]] = []
        window.serial_terminal_panel = types.SimpleNamespace(
            set_live_poll_paused=lambda paused_state: paused.append(bool(paused_state))
        )
        window._publish_stage_position_estimate = lambda position: published.append(
            tuple(float(value) for value in position)
        )
        stage_controller.latest_position = (1.0, 2.0, 3.0, 0.0, 0.0, 0.0)

        Main._on_manual_jog_command_changed(window, (("X", 2.0),), 60.0)

        self.assertEqual(paused, [True])
        self.assertEqual(published, [(1.0, 2.0, 3.0, 0.0, 0.0, 0.0)])

    def test_manual_jog_stop_resumes_terminal_poll_and_schedules_refreshes(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        paused: list[bool] = []
        refreshes: list[tuple[int, ...]] = []
        single_shots: list[int] = []
        window.serial_terminal_panel = types.SimpleNamespace(
            set_live_poll_paused=lambda paused_state: paused.append(bool(paused_state))
        )
        window._manual_jog_prediction.axis_velocities = {"X": 1.0}
        window._manual_jog_prediction.stage_position = (
            1.0,
            2.0,
            3.0,
            0.0,
            0.0,
            0.0,
        )
        window._manual_jog_prediction.stage_xy = (1.0, 2.0)
        window._schedule_status_refreshes = lambda delays: refreshes.append(tuple(delays))
        stage_controller.last_status_time = 12.0

        def run_single_shot(delay: int, callback) -> None:
            single_shots.append(int(delay))
            callback()

        with mock.patch.object(main_module.QTimer, "singleShot", side_effect=run_single_shot):
            Main._on_manual_jog_stopped(window)

        self.assertEqual(single_shots, [Main.TERMINAL_RESUME_AFTER_JOG_MS])
        self.assertEqual(paused, [False])
        self.assertEqual(refreshes, [tuple(Main.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)])

    def test_feedrate_change_does_not_reissue_stale_idle_coordinate_move(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Z": (3.84, 3.84)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.busy = False
        stage_controller.latest_state = "Idle"

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertIsNone(window._coordinate_targets.active_axis)
        self.assertEqual(window._coordinate_targets.active_axes, set())
        self.assertEqual(stage_controller.requests, [({"X": 5.0, "Z": 3.84}, 120.0)])
        self.assertEqual(stage_controller.jog_stops, 0)

    def test_cancel_button_is_enabled_for_generic_busy_stage_task(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.busy = True

        Main._update_stage_coordinate_apply_state(window)

        self.assertTrue(cancel_button.enabled)

    def test_cancel_button_is_enabled_for_reported_controller_motion(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"

        Main._update_stage_coordinate_apply_state(window)

        self.assertTrue(cancel_button.enabled)

    def test_stale_reported_controller_motion_does_not_enable_cancel(self) -> None:
        window, stage_controller, cancel_button, _statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"
        stage_controller.last_status_time = (
            time.monotonic() - Main.CONTROLLER_ACTIVE_STATE_STALE_S - 0.5
        )

        Main._update_stage_coordinate_apply_state(window)

        self.assertFalse(cancel_button.enabled)

    def test_apply_cancel_state_uses_panel_pending_and_cancelable_operation_flags(
        self,
    ) -> None:
        window = Main.__new__(Main)
        panel = _FakeStagePositionPanel({"X": _FakeLineEdit(enabled=True, modified=True)})
        panel.pending_targets["X"] = (5.0, 5.0)
        window._stage_position_panel = panel
        window._pending_stage_axis_targets = panel.pending_targets
        window.stage_controller = types.SimpleNamespace(is_busy=lambda: False)
        window._coordinate_targets = _coordinate_target_state()
        window._has_cancelable_operation = lambda: True

        Main._update_stage_coordinate_apply_state(window)

        self.assertEqual(panel.action_button_states[-1], (True, True))

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
        window._coordinate_targets.active_axis = "X"
        window._coordinate_targets.active_axes = {"X"}
        window._clear_coordinate_move_tracking = (
            lambda *, clear_pending, reset_override: window._coordinate_targets.clear_tracking()
        )

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
            time.monotonic() - Main.CONTROLLER_ACTIVE_STATE_STALE_S - 0.5
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

    def test_idle_status_before_motion_does_not_clear_coordinate_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.latest_state = "Idle"
        window._coordinate_targets.started_at = None

        Main._finish_coordinate_move_if_idle(window, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

        self.assertEqual(window._coordinate_targets.active_axis, "X")
        self.assertEqual(window._coordinate_targets.active_axes, {"X", "Y"})

    def test_idle_status_after_motion_clears_coordinate_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.latest_state = "Idle"
        window._coordinate_targets.started_at = None
        window._coordinate_targets.seen_active_state = True

        Main._finish_coordinate_move_if_idle(window, (5.0, -2.0, 0.0, 0.0, 0.0, 0.0))

        self.assertIsNone(window._coordinate_targets.active_axis)

    def test_previous_micron_step_status_does_not_clear_coordinate_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"A": (-0.010, -0.010)},
            feedrate_mm_min=1.0,
            source_label="coordinate field",
        )
        stage_controller.latest_state = "Idle"
        window._coordinate_targets.started_at = None
        window._coordinate_targets.seen_active_state = True

        Main._finish_coordinate_move_if_idle(
            window,
            (0.0, 0.0, 0.0, -0.004, 0.0, 0.0),
        )

        self.assertEqual(window._coordinate_targets.active_axis, "A")

if __name__ == "__main__":
    unittest.main()
