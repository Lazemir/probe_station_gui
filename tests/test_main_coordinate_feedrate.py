import sys
import unittest


def _restore_real_imports_for_main() -> None:
    for name in list(sys.modules):
        if name == "PySide6" or name.startswith("PySide6."):
            del sys.modules[name]
    serial_module = sys.modules.get("serial")
    if serial_module is not None and not hasattr(serial_module, "__path__"):
        for name in list(sys.modules):
            if name == "serial" or name.startswith("serial."):
                del sys.modules[name]
    package = sys.modules.get("probe_station_gui")
    if package is not None and not hasattr(package, "__path__"):
        for name in list(sys.modules):
            if name == "probe_station_gui" or name.startswith("probe_station_gui."):
                del sys.modules[name]


_restore_real_imports_for_main()
from main import Main
from probe_station_gui.settings_manager import Settings


class _FakeTimer:
    def __init__(self) -> None:
        self.started = False

    def isActive(self) -> bool:
        return False

    def start(self) -> None:
        self.started = True


class _FakeButton:
    def __init__(self) -> None:
        self.enabled = False

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt naming
        self.enabled = bool(enabled)


class _FakeView:
    def __init__(self) -> None:
        self.focus_count = 0

    def setFocus(self, *_args, **_kwargs) -> None:  # noqa: N802 - Qt naming
        self.focus_count += 1


class _FakeJoystick:
    def __init__(self, current_feedrate: float) -> None:
        self._current_feedrate = float(current_feedrate)
        self.common_targets: list[tuple[float, float]] = []
        self.common_cleared = 0
        self.needle_contacts: list[tuple[str, float | None]] = []

    def current_linear_feedrate(self) -> float:
        return self._current_feedrate

    def set_common_feedrate_target(self, feedrate: float, max_feedrate: float) -> None:
        self.common_targets.append((float(feedrate), float(max_feedrate)))

    def clear_common_feedrate_target(self) -> None:
        self.common_cleared += 1

    def clear_temporary_linear_feedrate_bounds(self) -> None:
        pass

    def set_needle_contact_coordinate(
        self,
        action: str,
        value: float | None,
    ) -> None:
        self.needle_contacts.append((action, value))


class _FakeStageController:
    FEED_OVERRIDE_MIN_PERCENT = 10
    FEED_OVERRIDE_MAX_PERCENT = 200

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, float], float | None]] = []
        self.jog_stops = 0
        self.next_absolute_jog_accept = True
        self.busy = False
        self.latest_state = "Idle"
        self.cancelled_tasks: list[str] = []
        self.cancelled_motions: list[str] = []
        self.needle_calibrations: list[dict[str, float | None]] = []

    def request_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        feedrate: float | None = None,
    ) -> bool:
        self.requests.append((dict(targets), feedrate))
        return True

    def queue_jog_stop(self) -> None:
        self.jog_stops += 1

    def queue_absolute_axis_targets_jog(
        self,
        targets: dict[str, float],
        *,
        feedrate: float,
    ) -> bool:
        if not self.next_absolute_jog_accept:
            return False
        self.queue_jog_stop()
        self.requests.append((dict(targets), feedrate))
        return True

    def queue_feed_override_reset(self) -> int:
        return 100

    def is_busy(self) -> bool:
        return self.busy

    def latest_stage_state(self) -> str:
        return self.latest_state

    def axis_max_feedrates(self) -> dict[str, float]:
        return {"X": 100.0, "Y": 150.0, "Z": 80.0, "A": 70.0, "B": 60.0}

    def cancel_active_task(self, reason: str) -> None:
        self.cancelled_tasks.append(reason)

    def cancel_active_motion(self, reason: str) -> None:
        self.cancelled_motions.append(reason)

    def apply_needle_calibration(
        self,
        *,
        raise_position_mm: float | None,
        down_position_mm: float | None,
        contact_zone_mm: float | None = None,
    ) -> None:
        self.needle_calibrations.append(
            {
                "raise_position_mm": raise_position_mm,
                "down_position_mm": down_position_mm,
                "contact_zone_mm": contact_zone_mm,
            }
        )

    def axis_a_configured_coordinate_for_lowering(self, lowering_mm: float) -> float:
        return float(lowering_mm)

    def calibrated_axis_display_value(self, axis: str, raw_value: float) -> float:
        self._last_display_axis = axis
        return float(raw_value)

    def axis_a_lowering_for_configured_coordinate(self, coordinate: float) -> float:
        return float(coordinate)


class _FakeSettingsManager:
    def __init__(self) -> None:
        self.settings = Settings()
        self.saved_count = 0
        self.replaced_settings: list[Settings] = []

    def replace(self, settings: Settings) -> None:
        self.settings = settings
        self.replaced_settings.append(settings)

    def save(self) -> None:
        self.saved_count += 1


def _make_main(current_feedrate: float = 120.0) -> tuple[
    Main,
    _FakeStageController,
    _FakeJoystick,
    _FakeTimer,
    list[str],
]:
    window = Main.__new__(Main)
    stage_controller = _FakeStageController()
    joystick = _FakeJoystick(current_feedrate)
    timer = _FakeTimer()
    statuses: list[str] = []
    estimates: list[tuple[float, ...]] = []

    window.stage_controller = stage_controller
    window.joystick_panel = joystick
    window._pending_stage_axis_targets = {}
    window._coordinate_move_axis = None
    window._coordinate_move_axes = set()
    window._coordinate_move_origin_position = None
    window._coordinate_move_stage_position = None
    window._coordinate_move_target_position = None
    window._coordinate_move_started_at = None
    window._coordinate_move_ends_at = None
    window._coordinate_move_programmed_feedrate = None
    window._coordinate_move_effective_feedrate = None
    window._coordinate_move_seen_active_state = False
    window._pending_homing_axes = []
    window._manual_jog_timer = timer
    window._seed_motion_prediction_position = lambda: (
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    window._stage_axis_target_limit_error = lambda _axis, _target: None

    def position_with_axis_values(
        raw_targets: dict[str, float],
        *,
        base_position: tuple[float, ...],
    ) -> tuple[float, ...]:
        values = list(base_position)
        for axis, value in raw_targets.items():
            values[Main.STAGE_AXIS_NAMES.index(axis)] = float(value)
        return tuple(values)

    window._position_with_axis_values = position_with_axis_values
    window._set_stage_motion_axes = lambda axes: setattr(
        window, "_motion_axes", set(axes)
    )
    window._update_stage_coordinate_apply_state = lambda: None
    window._refresh_stage_axis_styles = lambda: None
    window._show_status = (
        lambda message, _timeout_ms=None: statuses.append(str(message))
    )
    window._publish_stage_position_estimate = (
        lambda position: estimates.append(tuple(float(value) for value in position))
    )
    return window, stage_controller, joystick, timer, statuses


def _make_cancel_main() -> tuple[Main, _FakeStageController, _FakeButton, list[str]]:
    window = Main.__new__(Main)
    stage_controller = _FakeStageController()
    cancel_button = _FakeButton()
    statuses: list[str] = []

    window.stage_controller = stage_controller
    window._stage_coordinate_apply_button = _FakeButton()
    window._stage_coordinate_cancel_button = cancel_button
    window._pending_stage_axis_targets = {}
    window._stage_axis_fields = {}
    window._stage_axis_return_commits = set()
    window._stage_axis_base_styles = {}
    window._coordinate_move_axis = None
    window._coordinate_move_axes = set()
    window._coordinate_move_seen_active_state = False
    window._route_measurement_runner = None
    window.surface_map_window = None
    window._manual_alignment_pick_slot = None
    window._pending_click_to_move = None
    window._pending_homing_axes = []
    window._homing_active_key = None
    window._pending_alignment_preparation = None
    window._pending_quick_alignment_rotation = False
    window.design_navigator_panel = None
    window.view = _FakeView()
    window._show_status = (
        lambda message, _timeout_ms=None: statuses.append(str(message))
    )
    window._clear_stage_motion_axes = lambda: None
    window._clear_planned_move_prediction = lambda *, clear_wait_state: None
    window._schedule_status_refreshes = lambda _delays: None
    window._schedule_cancel_state_refresh = lambda: None
    window._refresh_stage_axis_styles = lambda: None
    return window, stage_controller, cancel_button, statuses


class MainCoordinateFeedrateTest(unittest.TestCase):
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

        Main._save_needle_down_position_from_lowering(window, 2.885)

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
        self.assertEqual(window._coordinate_move_programmed_feedrate, 120.0)
        self.assertEqual(window._coordinate_move_effective_feedrate, 120.0)
        self.assertTrue(timer.started)
        self.assertEqual(_joystick.common_targets, [(120.0, 150.0)])

    def test_api_move_without_feedrate_uses_current_gui_feedrate(self) -> None:
        window, _stage_controller, _joystick, _timer, _statuses = _make_main(77.0)

        self.assertEqual(Main._api_move_feedrate(window, None), 77.0)

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

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(stage_controller.jog_stops, 1)
        self.assertEqual(
            stage_controller.requests,
            [({"X": 5.0}, 120.0), ({"X": 5.0}, 180.0)],
        )
        self.assertEqual(window._coordinate_move_programmed_feedrate, 180.0)
        self.assertEqual(window._coordinate_move_effective_feedrate, 180.0)
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
        window._coordinate_move_axis = None
        window._advance_coordinate_move_prediction = lambda: None

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(stage_controller.jog_stops, 1)
        self.assertEqual(
            stage_controller.requests,
            [
                ({"X": 5.0, "Y": -2.0}, 120.0),
                ({"X": 5.0, "Y": -2.0}, 180.0),
            ],
        )
        self.assertEqual(window._coordinate_move_programmed_feedrate, 180.0)
        self.assertEqual(window._coordinate_move_effective_feedrate, 180.0)

    def test_coordinate_move_feedrate_reissue_failure_keeps_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.next_absolute_jog_accept = False
        scheduled_delays: list[tuple[int, ...]] = []
        window._schedule_status_refreshes = (
            lambda delays: scheduled_delays.append(tuple(delays))
        )
        window._advance_coordinate_move_prediction = lambda: None

        Main._apply_coordinate_move_feedrate(window, 180.0)

        self.assertEqual(window._coordinate_move_axis, "X")
        self.assertEqual(window._coordinate_move_axes, {"X"})
        self.assertEqual(window._coordinate_move_programmed_feedrate, 120.0)
        self.assertEqual(window._coordinate_move_effective_feedrate, 120.0)
        self.assertEqual(stage_controller.requests, [({"X": 5.0}, 120.0)])
        self.assertEqual(stage_controller.jog_stops, 0)
        self.assertTrue(scheduled_delays)
        self.assertTrue(
            any(
                "Unable to update coordinate move feedrate." in item
                for item in statuses
            )
        )

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

    def test_cancel_button_cancels_generic_busy_stage_task(self) -> None:
        window, stage_controller, _cancel_button, statuses = _make_cancel_main()
        stage_controller.busy = True

        Main._cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_tasks,
            ["Operation cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_motions, [])
        self.assertIn("Cancel requested.", statuses)

    def test_cancel_button_keeps_coordinate_move_on_jog_cancel_path(self) -> None:
        window, stage_controller, _cancel_button, _statuses = _make_cancel_main()
        window._coordinate_move_axis = "X"
        window._coordinate_move_axes = {"X"}
        window._clear_coordinate_move_tracking = (
            lambda *, clear_pending, reset_override: setattr(
                window,
                "_coordinate_move_axis",
                None,
            )
        )

        Main._cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_motions,
            ["Coordinate move cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_tasks, [])

    def test_cancel_button_cancels_reported_controller_motion(self) -> None:
        window, stage_controller, _cancel_button, statuses = _make_cancel_main()
        stage_controller.latest_state = "Jog"

        Main._cancel_stage_coordinate_action(window)

        self.assertEqual(
            stage_controller.cancelled_motions,
            ["Motion cancel requested."],
        )
        self.assertEqual(stage_controller.cancelled_tasks, [])
        self.assertIn("Cancel requested.", statuses)

    def test_idle_status_before_motion_does_not_clear_coordinate_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.latest_state = "Idle"
        window._coordinate_move_started_at = None

        Main._finish_coordinate_move_if_idle(window, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

        self.assertEqual(window._coordinate_move_axis, "X")
        self.assertEqual(window._coordinate_move_axes, {"X", "Y"})

    def test_idle_status_after_motion_clears_coordinate_tracking(self) -> None:
        window, stage_controller, _joystick, _timer, _statuses = _make_main(120.0)
        Main._start_coordinate_targets_move(
            window,
            {"X": (5.0, 5.0), "Y": (-2.0, -2.0)},
            feedrate_mm_min=120.0,
            source_label="coordinate fields",
        )
        stage_controller.latest_state = "Idle"
        window._coordinate_move_started_at = None
        window._coordinate_move_seen_active_state = True

        Main._finish_coordinate_move_if_idle(window, (5.0, -2.0, 0.0, 0.0, 0.0, 0.0))

        self.assertIsNone(window._coordinate_move_axis)


if __name__ == "__main__":
    unittest.main()
