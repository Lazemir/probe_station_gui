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


class _FakeTimer:
    def __init__(self) -> None:
        self.started = False

    def isActive(self) -> bool:
        return False

    def start(self) -> None:
        self.started = True


class _FakeJoystick:
    def __init__(self, current_feedrate: float) -> None:
        self._current_feedrate = float(current_feedrate)

    def current_linear_feedrate(self) -> float:
        return self._current_feedrate


class _FakeStageController:
    FEED_OVERRIDE_MIN_PERCENT = 10
    FEED_OVERRIDE_MAX_PERCENT = 200

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, float], float | None]] = []
        self.jog_stops = 0
        self.next_absolute_jog_accept = True

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
    window._show_status = (
        lambda message, _timeout_ms=None: statuses.append(str(message))
    )
    window._publish_stage_position_estimate = (
        lambda position: estimates.append(tuple(float(value) for value in position))
    )
    return window, stage_controller, joystick, timer, statuses


class MainCoordinateFeedrateTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
