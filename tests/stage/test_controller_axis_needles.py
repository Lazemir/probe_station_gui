import types
import unittest

from probe_station_gui.settings.precision_approach import (
    PrecisionApproachProfile,
    PrecisionApproachSettings,
)

try:
    from .controller_test_support import (
        MoveVector,
        StageController,
        _FakeSerial,
        _LineFakeSerial,
        _WritableFakeSerial,
    )
except ImportError:
    from controller_test_support import (
        MoveVector,
        StageController,
        _FakeSerial,
        _LineFakeSerial,
        _WritableFakeSerial,
    )


class StageControllerNeedlesMotionTest(unittest.TestCase):
    def test_needles_lower_uses_requested_feedrate_while_active(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "machine"
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(down_position_mm=1.0)
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                display_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )
            observed = []

            def _send_absolute_axis_move(axis, value, **kwargs) -> None:
                observed.append(
                    (
                        axis,
                        value,
                        kwargs.get("feedrate"),
                        controller._active_needles_action,
                        controller._active_needles_programmed_feedrate,
                    )
                )

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("lower", feedrate=80.0)

            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], -0.95)
            self.assertEqual(observed[0][2:], (500.0, None, None))
            self.assertEqual(observed[1][0], "A")
            self.assertAlmostEqual(observed[1][1], -1.0)
            self.assertEqual(observed[1][2:], (80.0, "lower", 80.0))
            self.assertIsNone(controller._active_needles_action)
            self.assertIsNone(controller._active_needles_programmed_feedrate)
        finally:
            controller.shutdown()

    def test_needles_raise_moves_slow_out_of_contact_zone_then_full_up(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "machine"
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(
                raise_position_mm=0.5,
                down_position_mm=1.0,
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, -1.0),
                work_position=(0.0, 0.0, 0.0, -1.0),
                display_position=(0.0, 0.0, 0.0, -1.0),
                homed_axes={"A"},
            )
            observed = []
            states = []

            def _send_absolute_axis_move(axis, value, **kwargs) -> None:
                observed.append(
                    (
                        axis,
                        value,
                        kwargs.get("feedrate"),
                        controller._active_needles_action,
                        controller._active_needles_programmed_feedrate,
                    )
                )

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda raised, known: states.append((raised, known))
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("raise", feedrate=70.0)

            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], -0.95)
            self.assertEqual(observed[0][2:], (70.0, "raise", 70.0))
            self.assertEqual(observed[1][0], "A")
            self.assertAlmostEqual(observed[1][1], 0.0)
            self.assertEqual(observed[1][2:], (500.0, None, None))
            self.assertEqual(states[-1], (True, True))
        finally:
            controller.shutdown()

    def test_needles_raise_refreshes_work_offset_before_deciding_target(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "work"
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(down_position_mm=1.0)
            refreshes = []

            def _refresh_coordinate_system_state(
                *,
                apply_preference: bool = False,
            ) -> None:
                refreshes.append(apply_preference)
                controller._active_work_coordinate_system = "G54"
                controller._controller_coordinate_offsets["G54"] = (
                    32.0,
                    32.0,
                    0.0,
                    -2.028,
                    0.0,
                )

            controller._refresh_coordinate_system_state = (
                _refresh_coordinate_system_state
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=None,
                work_position=(0.0, 0.0, 0.664, 2.028, 0.0),
                display_position=(0.0, 0.0, 0.664, 2.028, 0.0),
                work_offset=None,
                coordinate_system=None,
                homed_axes={"A"},
            )
            observed = []
            messages = []

            def _send_absolute_axis_move(axis, value, **kwargs) -> None:
                observed.append((axis, value, kwargs.get("feedrate")))

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda success, message, action: messages.append(
                    (success, message, action)
                )
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("raise", feedrate=600.0)

            self.assertEqual(refreshes, [True])
            self.assertEqual(observed, [])
            self.assertEqual(messages[-1], (True, "Needles already raised.", "raise"))
        finally:
            controller.shutdown()

    def test_needles_lift_moves_only_to_contact_zone_boundary(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "machine"
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(
                raise_position_mm=0.5,
                down_position_mm=1.0,
                contact_zone_mm=0.1,
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, -1.0),
                work_position=(0.0, 0.0, 0.0, -1.0),
                display_position=(0.0, 0.0, 0.0, -1.0),
                homed_axes={"A"},
            )
            observed = []

            def _send_absolute_axis_move(axis, value, **kwargs) -> None:
                observed.append(
                    (
                        axis,
                        value,
                        kwargs.get("feedrate"),
                        controller._active_needles_action,
                        controller._active_needles_programmed_feedrate,
                    )
                )

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("lift", feedrate=70.0)

            self.assertEqual(observed, [("A", -0.9, 70.0, "lift", 70.0)])
            self.assertIsNone(controller._active_needles_action)
            self.assertIsNone(controller._active_needles_programmed_feedrate)
        finally:
            controller.shutdown()

    def test_needles_lift_moves_to_contact_zone_boundary_from_above(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "machine"
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(
                raise_position_mm=0.5,
                down_position_mm=1.0,
                contact_zone_mm=0.1,
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, -0.5),
                work_position=(0.0, 0.0, 0.0, -0.5),
                display_position=(0.0, 0.0, 0.0, -0.5),
                homed_axes={"A"},
            )
            observed = []
            messages = []

            def _send_absolute_axis_move(axis, value, **kwargs) -> None:
                observed.append(
                    (
                        axis,
                        value,
                        kwargs.get("feedrate"),
                        controller._active_needles_action,
                        controller._active_needles_programmed_feedrate,
                    )
                )

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda success, message, action: messages.append(
                    (success, message, action)
                )
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("lift", feedrate=70.0)

            self.assertEqual(observed, [("A", -0.9, 500.0, None, None)])
            self.assertEqual(messages[-1], (True, "Needles lifted.", "lift"))
        finally:
            controller.shutdown()

    def test_needles_lower_targets_contact_zero_after_a_work_offset(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "work"
            controller._active_work_coordinate_system = "G54"
            controller._controller_coordinate_offsets["G54"] = (
                0.0,
                0.0,
                0.0,
                -1.0,
                0.0,
            )
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(down_position_mm=1.0)
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=None,
                work_position=(0.0, 0.0, 0.0, 1.0),
                display_position=(0.0, 0.0, 0.0, 1.0),
                work_offset=(0.0, 0.0, 0.0, -1.0, 0.0),
                coordinate_system="G54",
                homed_axes={"A"},
            )
            observed = []

            def _send_absolute_axis_move(axis, value, **kwargs) -> None:
                observed.append((axis, value, kwargs.get("feedrate")))

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            controller._run_needles_action("lower", feedrate=80.0)

            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], 0.05)
            self.assertEqual(observed[0][2], 500.0)
            self.assertEqual(observed[1][0], "A")
            self.assertAlmostEqual(observed[1][1], 0.0)
            self.assertEqual(observed[1][2], 80.0)
        finally:
            controller.shutdown()

    def test_needles_lower_to_depth_below_down_goes_directly_to_target(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "machine"
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(down_position_mm=1.0)
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, -0.95),
                work_position=(0.0, 0.0, 0.0, -0.95),
                display_position=(0.0, 0.0, 0.0, -0.95),
                homed_axes={"A"},
            )
            observed = []

            def _send_absolute_axis_move(axis, value, **kwargs) -> None:
                observed.append((axis, value, kwargs.get("feedrate")))

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            message = controller.run_external_needles_lower_to_depth_below_down(
                0.001,
                feedrate=80.0,
            )

            self.assertEqual(message, "Needles lowered to 0.0010 mm below saved down.")
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], -1.001)
            self.assertEqual(observed[0][2], 80.0)
        finally:
            controller.shutdown()

    def test_needles_lower_to_depth_retries_missing_a_status(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "machine"
            controller.apply_axis_max_feedrates({"A": 500.0})
            controller.apply_needle_calibration(down_position_mm=1.0)
            statuses = [
                None,
                types.SimpleNamespace(
                    state="Idle",
                    position=(0.0, 0.0, 0.0, -0.95),
                    work_position=(0.0, 0.0, 0.0, -0.95),
                    display_position=(0.0, 0.0, 0.0, -0.95),
                    homed_axes={"A"},
                ),
            ]
            status_reads = []

            def _query_status(_serial):
                status_reads.append(1)
                return statuses.pop(0)

            controller._query_status = _query_status
            observed = []

            def _send_absolute_axis_move(axis, value, **kwargs) -> None:
                observed.append((axis, value, kwargs.get("feedrate")))

            controller._send_absolute_axis_move = _send_absolute_axis_move
            controller.needles_action_started = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_action_finished = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needle_height_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.needles_state_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )
            controller.axis_a_ready_changed = types.SimpleNamespace(
                emit=lambda *_args, **_kwargs: None
            )

            message = controller.run_external_needles_lower_to_depth_below_down(
                0.001,
                feedrate=80.0,
            )

            self.assertEqual(message, "Needles lowered to 0.0010 mm below saved down.")
            self.assertEqual(len(status_reads), 2)
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], "A")
            self.assertAlmostEqual(observed[0][1], -1.001)
            self.assertEqual(observed[0][2], 80.0)
        finally:
            controller.shutdown()

class StageControllerNeedlesStateTest(unittest.TestCase):
    def test_status_without_a_homing_keeps_needles_unknown(self) -> None:
        controller = StageController()
        emitted = []
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes=None,
            )
        )

        self.assertEqual(emitted, [])
        self.assertFalse(controller._needles_known)
        self.assertFalse(controller._needles_up)

    def test_status_with_a_homing_marks_needles_up_when_a_is_zero(self) -> None:
        controller = StageController()
        emitted = []
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (True, True))

    def test_status_marks_needles_unknown_inside_contact_zone(self) -> None:
        controller = StageController()
        emitted = []
        controller.apply_needle_calibration(
            raise_position_mm=0.5,
            down_position_mm=1.0,
        )
        controller._needles_up = True
        controller._needles_known = True
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, -0.975),
                work_position=(0.0, 0.0, 0.0, -0.975),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (False, False))

    def test_status_marks_needles_lifted_above_contact_zone(self) -> None:
        controller = StageController()
        emitted = []
        zones = []
        controller.apply_needle_calibration(
            raise_position_mm=0.5,
            down_position_mm=1.0,
        )
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.needles_zone_changed = types.SimpleNamespace(
            emit=lambda zone: zones.append(zone)
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, -0.75),
                work_position=(0.0, 0.0, 0.0, -0.75),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (False, True))
        self.assertEqual(zones[-1], "lift")

    def test_status_marks_needles_down_at_or_below_saved_lower(self) -> None:
        controller = StageController()
        emitted = []
        controller.apply_needle_calibration(
            raise_position_mm=0.5,
            down_position_mm=1.0,
        )
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: emitted.append((raised, known))
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needle_height_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._update_needles_from_status(
            types.SimpleNamespace(
                state="Idle",
                position=None,
                display_position=(0.0, 0.0, 0.0, -1.1),
                work_position=(0.0, 0.0, 0.0, -1.1),
                homed_axes={"A"},
            )
        )

        self.assertEqual(emitted[-1], (False, True))

    def test_latest_a_position_reads_cached_stage_position(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, -0.25)

        self.assertEqual(controller.latest_a_position(), -0.25)


class StageControllerPriorityNeedlesActionTest(unittest.TestCase):
    def test_mark_axes_unhomed_removes_only_requested_homed_axes(self) -> None:
        controller = StageController()
        controller._homed_axes = {"X", "Y", "Z", "A"}
        emitted = []
        controller.homing_status_changed = types.SimpleNamespace(
            emit=lambda axes: emitted.append(set(axes))
        )

        removed = controller.mark_axes_unhomed({"z", "B"})

        self.assertEqual(removed, {"Z"})
        self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
        self.assertEqual(emitted, [{"X", "Y", "A"}])

    def test_cancel_active_motion_sends_jog_cancel_without_invalidating_state(self) -> None:
        controller = StageController()
        controller._serial = _WritableFakeSerial()
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )

        controller.cancel_active_motion("Coordinate move cancel requested.")

        self.assertIn(b"\x85", controller._serial.writes)
        self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
        self.assertEqual(messages[-1], "Coordinate move cancel requested.")

    def test_cancel_active_task_preserves_homing_without_soft_reset(self) -> None:
        controller = StageController()
        controller._serial = _WritableFakeSerial()
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = False
        controller._needles_known = True
        controller._active_needles_action = "lower"
        controller._active_needles_programmed_feedrate = 80.0
        controller._queued_needles_actions.append(("raise", None, None))
        controller._oscillation_needles_actions.append(("lower", None, None))
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )
        try:
            controller.cancel_active_task("Needle move cancel requested.")
            controller._async_write_queue.join()

            self.assertTrue(controller._cancel_event.is_set())
            self.assertIn(b"\x85", controller._serial.writes)
            self.assertNotIn(b"\x18", controller._serial.writes)
            self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
            self.assertFalse(controller._needles_known)
            self.assertIsNone(controller._active_needles_action)
            self.assertIsNone(controller._active_needles_programmed_feedrate)
            self.assertEqual(list(controller._queued_needles_actions), [])
            self.assertEqual(list(controller._oscillation_needles_actions), [])
            self.assertEqual(messages[0], "Needle move cancel requested.")
        finally:
            controller.shutdown()

    def test_cancel_active_task_without_needles_preserves_needle_state(self) -> None:
        controller = StageController()
        controller._serial = _WritableFakeSerial()
        controller._homed_axes = {"X", "Y", "Z", "A"}
        controller._needles_up = True
        controller._needles_known = True
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )
        try:
            controller.cancel_active_task("Autofocus cancel requested.")
            controller._async_write_queue.join()

            self.assertTrue(controller._cancel_event.is_set())
            self.assertEqual(controller._serial.writes, [b"\x85"])
            self.assertEqual(controller._homed_axes, {"X", "Y", "Z", "A"})
            self.assertTrue(controller._needles_up)
            self.assertTrue(controller._needles_known)
            self.assertEqual(messages[-1], "Autofocus cancel requested.")
        finally:
            controller.shutdown()

    def test_reset_controller_soft_resets_and_clears_unverified_state(self) -> None:
        controller = StageController()
        controller._serial = _WritableFakeSerial()
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )
        try:
            controller.reset_controller(source="test", reason="Reset requested.")
            controller._async_write_queue.join()

            self.assertTrue(controller._cancel_event.is_set())
            self.assertIn(b"\x18", controller._serial.writes)
            self.assertEqual(controller._homed_axes, set())
            self.assertFalse(controller._needles_known)
            self.assertEqual(messages[0], "Reset requested.")
        finally:
            controller.shutdown()

    def test_relative_move_can_be_sent_as_cancelable_jog(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial([b"ok\n"])
        controller._serial = serial_connection
        controller.set_motion_safety_disabled(True)
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        controller._send_relative_move(
            MoveVector(x=0.5, z=-0.1),
            feedrate=12.3,
            as_jog=True,
        )

        self.assertEqual(
            serial_connection.writes,
            [b"\x90", b"$J=G91 G21 X0.5 Z-0.1 F12.3\n"],
        )

    def test_needles_lower_queues_during_oscillation(self) -> None:
        controller = StageController()
        controller._oscillation_active = True
        controller._active_thread = types.SimpleNamespace(is_alive=lambda: True)
        messages = []
        controller.needles_action_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )

        controller.request_needles_lower()

        self.assertFalse(controller._cancel_event.is_set())
        self.assertEqual(
            list(controller._oscillation_needles_actions), [("lower", None, None)]
        )
        self.assertIn("queued during oscillation", messages[-1])

class StageControllerPrecisionNeedleTargetTest(unittest.TestCase):
    def test_final_profile_segment_uses_shared_precision_executor(self) -> None:
        controller = StageController()
        profiles = PrecisionApproachSettings()
        profiles.profiles["A"] = PrecisionApproachProfile(True, 0.1, -1)
        controller.apply_precision_approach_configuration(profiles)
        controller._axis_a_configured_target_for_lowering = (
            lambda lowering, _status=None: float(lowering)
        )
        controller._needle_motion_profile_segments = (
            lambda *_args, **_kwargs: [(1.0, 5.0, False)]
        )
        controller._update_needles_from_a_position = lambda _position: None
        moves = []
        controller._execute_precision_axis_targets_locked = (
            lambda targets, **kwargs: moves.append((dict(targets), dict(kwargs)))
        )
        controller._send_absolute_axis_move = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("final needle target bypassed precision executor")
            )
        )

        moved = controller._send_needle_motion_profile_locked(
            action="lower",
            current_a=0.0,
            target_lowering=1.0,
            feedrate=5.0,
            status=None,
        )

        self.assertTrue(moved)
        self.assertEqual(
            moves,
            [
                (
                    {"A": 1.0},
                    {
                        "feedrate": 5.0,
                        "allow_unhomed": False,
                        "ignore_needle_safety": True,
                    },
                )
            ],
        )
        controller.shutdown()


if __name__ == "__main__":
    unittest.main()
