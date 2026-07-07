import threading
import time
import types
import unittest

import numpy as np

try:
    from .controller_test_support import (
        AutofocusContext,
        FocusSweepResult,
        MoveVector,
        StageController,
        StageControllerError,
        _FakeSerial,
        _LineFakeSerial,
        _LockedLineFakeSerial,
        _RejectingSerial,
        _ResetTrackingLineFakeSerial,
        _SwappingLineFakeSerial,
        _TrackingLock,
        _WritableFakeSerial,
        _stage_controller_module,
    )
except ImportError:
    from controller_test_support import (
        AutofocusContext,
        FocusSweepResult,
        MoveVector,
        StageController,
        StageControllerError,
        _FakeSerial,
        _LineFakeSerial,
        _LockedLineFakeSerial,
        _RejectingSerial,
        _ResetTrackingLineFakeSerial,
        _SwappingLineFakeSerial,
        _TrackingLock,
        _WritableFakeSerial,
        _stage_controller_module,
    )

class StageControllerStartupLimitsTest(unittest.TestCase):
    def test_parse_startup_limits(self) -> None:
        lines = [
            "?<Idle|MPos:0.000,0.000,9.520,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000,0.000,0.000>",
            "[0xc]$Build/Info",
            "[VER:4.0 FluidNC v4.0.1:]",
            "[MSG:INFO: Axis X (0.000,64.000)]",
            "[MSG:INFO: Axis Y (0.000,64.000)]",
            "[MSG:INFO: Axis Z (0.000,20.000)]",
            "[MSG:INFO: Axis A (-0.100,0.000)]",
            "[MSG:INFO: Axis B (-1000.000,0.000)]",
            "ok",
        ]

        limits = StageController._parse_startup_limits(lines)

        self.assertEqual(limits.get("X"), (0.0, 64.0))
        self.assertEqual(limits.get("Y"), (0.0, 64.0))
        self.assertEqual(limits.get("Z"), (0.0, 20.0))
        self.assertEqual(limits.get("A"), (-0.1, 0.0))
        self.assertEqual(limits.get("B"), (-1000.0, 0.0))

    def test_relative_software_limits_apply_per_homed_axis(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._axis_limits = {"X": (0.0, 10.0), "Y": (0.0, 10.0)}
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=None,
            display_position=(9.0, 0.0, 0.0),
            work_position=(9.0, 0.0, 0.0),
            work_offset=(0.0, 0.0, 0.0),
            homed_axes={"X"},
        )
        controller._ensure_b_axis_zero_reference = lambda _status: None

        controller._check_relative_move_limits(MoveVector(y=15.0), allow_relative=True)

        with self.assertRaises(StageControllerError):
            controller._check_relative_move_limits(
                MoveVector(x=5.0), allow_relative=True
            )

    def test_constrain_jog_distances_clips_homed_axis_to_soft_limit(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0)}
        controller._last_stage_position = (0.0, 0.0, 1.0)
        controller._homed_axes = {"Z"}
        messages = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: messages.append(message)
        )

        constrained = controller.constrain_jog_distances((("Z", -25.0),))

        self.assertEqual(constrained, (("Z", -1.0),))
        self.assertTrue(any("soft limit" in message for message in messages))

    def test_queue_jog_command_rejects_unclipped_soft_limit_move(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0)}
        controller._last_stage_position = (0.0, 0.0, 0.0)
        controller._homed_axes = {"Z"}

        with self.assertRaises(StageControllerError):
            controller.queue_jog_command("$J=G91 G21 Z-25.000 F10")

        self.assertFalse(controller._jog_motion_active)

    def test_constrain_jog_distances_does_not_blanket_block_unknown_position(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0)}
        controller._last_stage_position = None
        controller._homed_axes = {"Z"}

        constrained = controller.constrain_jog_distances((("Z", 250.0),))

        self.assertEqual(constrained, (("Z", 250.0),))

    def test_queue_jog_command_rewrites_known_relative_jog_to_absolute_target(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            controller._position_reporting_mode = "machine"
            controller._axis_limits = {"Z": (0.0, 20.0)}
            controller._last_stage_position = (0.0, 0.0, 1.0)
            controller._homed_axes = {"Z"}

            controller.queue_jog_command("$J=G91 G21 Z5.000 F10")
            controller._async_write_queue.join()

            self.assertEqual(
                serial_connection.writes,
                [b"$J=G90 G21 G53 Z6 F10\n"],
            )
        finally:
            controller.shutdown()

    def test_queue_jog_command_allows_unknown_position_without_blanket_block(self) -> None:
        controller = StageController()
        try:
            controller._position_reporting_mode = "machine"
            controller._axis_limits = {"Z": (0.0, 20.0)}
            controller._last_stage_position = None
            controller._homed_axes = {"Z"}

            controller.queue_jog_command("$J=G91 G21 Z250.000 F10")

            self.assertTrue(controller._jog_motion_active)
        finally:
            controller.shutdown()

    def test_queue_outbound_command_routes_jog_stop(self) -> None:
        controller = StageController()
        try:
            observed: list[tuple[str, object]] = []
            controller.queue_jog_stop = lambda: observed.append(("jog_stop", None))

            handled = controller.queue_outbound_command(b"\x85")

            self.assertTrue(handled)
            self.assertEqual(observed, [("jog_stop", None)])
        finally:
            controller.shutdown()

    def test_queue_outbound_command_routes_soft_reset_with_source(self) -> None:
        controller = StageController()
        try:
            observed: list[tuple[str, object]] = []
            controller.queue_soft_reset = lambda *, source="unknown": observed.append(
                ("soft_reset", source)
            )

            handled = controller.queue_outbound_command(
                b"\x18",
                source="serial_terminal_ctrl_x",
            )

            self.assertTrue(handled)
            self.assertEqual(
                observed,
                [("soft_reset", "serial_terminal_ctrl_x")],
            )
        finally:
            controller.shutdown()

    def test_queue_outbound_command_routes_jog_command(self) -> None:
        controller = StageController()
        try:
            observed: list[tuple[str, object]] = []
            controller.queue_jog_command = lambda command: observed.append(
                ("jog_command", command)
            )

            handled = controller.queue_outbound_command("$J=G91 G21 X1.000 F10")

            self.assertTrue(handled)
            self.assertEqual(
                observed,
                [("jog_command", "$J=G91 G21 X1.000 F10")],
            )
        finally:
            controller.shutdown()

    def test_queue_outbound_command_routes_manual_terminal_command(self) -> None:
        controller = StageController()
        try:
            observed: list[tuple[str, object]] = []
            controller.queue_manual_command = lambda command: observed.append(
                ("manual_command", command)
            )

            handled = controller.queue_outbound_command("M114")

            self.assertTrue(handled)
            self.assertEqual(observed, [("manual_command", "M114")])
        finally:
            controller.shutdown()

    def test_queue_outbound_command_returns_none_for_unsupported_bytes(self) -> None:
        controller = StageController()
        try:
            handled = controller.queue_outbound_command(b"\x99")

            self.assertIsNone(handled)
        finally:
            controller.shutdown()

    def test_absolute_axis_move_respects_homed_axis_soft_limit(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "machine"
        controller._axis_limits = {"Z": (0.0, 20.0), "X": (0.0, 64.0)}
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 1.0),
            work_position=(0.0, 0.0, 1.0),
            display_position=(0.0, 0.0, 1.0),
            work_offset=(0.0, 0.0, 0.0),
            homed_axes={"Z"},
        )
        commands = []
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        with self.assertRaises(StageControllerError):
            controller._send_absolute_axis_move(
                "Z",
                -0.1,
                ignore_needle_safety=True,
                allow_unhomed=True,
            )

        self.assertFalse(any(command.startswith("G1 Z") for command in commands))

    def test_feed_override_payload_uses_realtime_grbl_steps(self) -> None:
        payload, applied = StageController._feed_override_payload_for_percent_change(
            100,
            137,
        )

        self.assertEqual(applied, 137)
        self.assertEqual(payload, b"\x91\x91\x91" + b"\x93" * 7)

        payload, applied = StageController._feed_override_payload_for_percent_change(
            137,
            82,
        )

        self.assertEqual(applied, 82)
        self.assertEqual(payload, b"\x92" * 5 + b"\x94" * 5)

    def test_feed_override_percent_is_clamped_to_controller_range(self) -> None:
        self.assertEqual(
            StageController.feed_override_percent_for_feedrates(100.0, 250.0),
            200,
        )
        self.assertEqual(
            StageController.feed_override_percent_for_feedrates(100.0, 1.0),
            10,
        )

    def test_active_needles_feedrate_queues_realtime_override(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            controller._active_needles_action = "lower"
            controller._active_needles_programmed_feedrate = 100.0
            controller._active_feed_override_percent = 100

            applied = controller.queue_active_needles_feedrate(150.0)
            controller._async_write_queue.join()

            self.assertEqual(applied, 150)
            self.assertEqual(serial_connection.writes, [b"\x91" * 5])
        finally:
            controller.shutdown()


class StageControllerAbsoluteMoveTest(unittest.TestCase):
    def test_relative_move_callback_runs_after_g1_is_accepted(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial([b"ok\n", b"ok\n", b"ok\n", b"ok\n"])
        controller._serial = serial_connection
        callback_writes: list[list[bytes]] = []

        controller._move_safety_check = lambda: None
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._check_relative_move_limits = lambda *_args, **_kwargs: None
        controller._reset_feed_override = lambda: None

        controller._send_relative_move(
            MoveVector(x=0.1),
            wait_for_completion=False,
            motion_started_callback=lambda _move, _feedrate: callback_writes.append(
                list(serial_connection.writes)
            ),
        )

        self.assertEqual(len(callback_writes), 1)
        self.assertTrue(callback_writes[0][-1].startswith(b"G1 X0.1"))
        self.assertFalse(any(command == b"G90\n" for command in callback_writes[0]))
        self.assertEqual(serial_connection.writes[-1], b"G90\n")

    def test_absolute_xy_move_sends_absolute_machine_target(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "machine"

        sent_commands = []
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                position=(10.0, 20.0, 0.0),
                display_position=(10.0, 20.0, 0.0),
                homed_axes={"X", "Y"},
            ),
            types.SimpleNamespace(
                state="Idle",
                position=(15.0, 26.0, 0.0),
                display_position=(15.0, 26.0, 0.0),
                homed_axes={"X", "Y"},
            ),
        ]

        controller._move_safety_check = lambda: None
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._wait_for_idle_at_targets = lambda _targets, timeout: None
        controller._write_current_command_and_wait = (
            lambda command: sent_commands.append(command)
        )
        controller._query_status = lambda _serial: statuses.pop(0)
        started_moves = []

        controller.absolute_xy_move_started = types.SimpleNamespace(
            emit=lambda *args: started_moves.append(args)
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )

        controller._run_move_to_xy(15.0, 26.0)

        self.assertEqual(sent_commands, ["$J=G90 G21 G53 X15 Y26 F600"])
        self.assertEqual(started_moves, [(15.0, 26.0, 600.0)])
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("Arrived", movement_results[-1][1])

    def test_absolute_xy_move_sends_target_not_delta_from_rounded_status(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "work"

        sent_commands = []
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                position=(10.123, 20.765, 0.0),
                display_position=(10.123, 20.765, 0.0),
                work_position=(10.123, 20.765, 0.0),
                homed_axes={"X", "Y"},
            ),
            types.SimpleNamespace(
                state="Idle",
                position=(15.123, 26.765, 0.0),
                display_position=(15.123, 26.765, 0.0),
                work_position=(15.123, 26.765, 0.0),
                homed_axes={"X", "Y"},
            ),
        ]

        controller._move_safety_check = lambda: None
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._wait_for_idle_at_targets = lambda _targets, timeout: None
        controller._write_current_command_and_wait = (
            lambda command: sent_commands.append(command)
        )
        controller._query_status = lambda _serial: statuses.pop(0)
        controller.absolute_xy_move_started = types.SimpleNamespace(
            emit=lambda *args: None
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )

        controller._run_move_to_xy(15.1234567, 26.7654321)

        self.assertEqual(
            sent_commands,
            ["$J=G90 G21 X15.123457 Y26.765432 F600"],
        )
        self.assertEqual(movement_results[-1][0], True)

    def test_absolute_xy_move_uses_work_basis_and_ignores_machine_position(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            32.0,
            32.0,
            0.0,
            0.0,
            0.0,
        )

        sent_commands = []
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                position=(999.0, 999.0, 9.244, 0.0, 2.17),
                display_position=(4.984, 3.705, 9.244, 0.0, 2.17),
                work_position=(4.984, 3.705, 9.244, 0.0, 2.17),
                work_offset=(32.0, 32.0, 0.0, 0.0, 0.0),
                coordinate_system="G54",
                homed_axes={"X", "Y"},
            ),
            types.SimpleNamespace(
                state="Idle",
                position=(888.0, 888.0, 9.244, 0.0, 2.17),
                display_position=(29.887, 26.689, 9.244, 0.0, 2.17),
                work_position=(29.887, 26.689, 9.244, 0.0, 2.17),
                work_offset=(32.0, 32.0, 0.0, 0.0, 0.0),
                coordinate_system="G54",
                homed_axes={"X", "Y"},
            ),
        ]

        controller._move_safety_check = lambda: None
        controller._ensure_axis_limits = lambda **_kwargs: None
        refresh_calls = []
        controller._refresh_coordinate_system_state = (
            lambda apply_preference=True: refresh_calls.append(apply_preference)
        )
        controller._wait_for_idle_at_targets = lambda _targets, timeout: None
        controller._write_current_command_and_wait = (
            lambda command: sent_commands.append(command)
        )
        controller._query_status = lambda _serial: statuses.pop(0)
        started_moves = []

        controller.absolute_xy_move_started = types.SimpleNamespace(
            emit=lambda *args: started_moves.append(args)
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )

        controller._run_move_to_xy(29.887, 26.689)

        self.assertEqual(sent_commands, ["$J=G90 G21 X29.887 Y26.689 F600"])
        self.assertEqual(started_moves, [(29.887, 26.689, 600.0)])
        self.assertEqual(movement_results[-1][0], True)
        self.assertEqual(refresh_calls, [])

    def test_modal_state_query_waits_for_payload_when_ok_arrives_first(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"ok\n",
                b"[GC:G1 G54 G17 G21 G90]\n",
            ]
        )

        controller._serial = serial_connection
        with controller._serial_session():
            tokens = controller._query_modal_state_tokens(timeout=0.2)

        self.assertIn("G54", tokens)
        self.assertEqual(serial_connection.writes, [b"$G\n"])

    def test_work_offset_query_ignores_stale_modal_response(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"[GC:G1 G54 G17 G21 G90]\n",
                b"ok\n",
                b"[G54:32.000,32.000,0.000,-2.908,0.000]\n",
                b"[G55:0.000,0.000,0.000,0.000,0.000]\n",
                b"ok\n",
            ]
        )

        controller._serial = serial_connection
        with controller._serial_session():
            offsets = controller._query_work_coordinate_offsets(timeout=0.5)

        self.assertEqual(serial_connection.writes, [b"$#\n"])
        self.assertIn("G54", offsets)
        self.assertEqual(offsets["G54"], (32.0, 32.0, 0.0, -2.908, 0.0))

    def test_modal_state_query_returns_empty_tokens_after_non_modal_ack(self) -> None:
        controller = StageController()
        serial_connection = _ResetTrackingLineFakeSerial(
            [
                b"[MSG:INFO: unrelated]\n",
                b"ok\n",
            ]
        )

        controller._serial = serial_connection
        with controller._serial_session():
            tokens = controller._query_modal_state_tokens(timeout=0.2)

        self.assertEqual(tokens, [])
        self.assertEqual(serial_connection.writes, [b"$G\n"])

    def test_work_offset_query_resets_input_after_truncated_offsets(self) -> None:
        controller = StageController()
        serial_connection = _ResetTrackingLineFakeSerial(
            [
                b"[G54:32.000,32.000,0.000,-2.908,0.000]\n",
            ]
        )

        controller._serial = serial_connection
        with controller._serial_session():
            offsets = controller._query_work_coordinate_offsets(timeout=0.2)

        self.assertEqual(
            offsets,
            {"G54": (32.0, 32.0, 0.0, -2.908, 0.0)},
        )
        self.assertEqual(serial_connection.writes, [b"$#\n"])
        self.assertEqual(serial_connection.reset_input_buffer_calls, 1)

    def test_set_current_a_work_coordinate_zeroes_active_wcs(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"<Idle|WPos:0.000,0.000,0.000,-1.000,0.000|WCO:0.000,0.000,0.000,0.000,0.000|H:A>\n",
                b"ok\n",
                b"[G54:0.000,0.000,0.000,-1.000,0.000]\n",
                b"ok\n",
                b"<Idle|WPos:0.000,0.000,0.000,0.000,0.000|WCO:0.000,0.000,0.000,-1.000,0.000|H:A>\n",
            ]
        )
        controller._serial = serial_connection
        controller._current_status_report_mask = 2
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"

        controller.set_current_axis_work_coordinate("A", 0.0)

        self.assertIn(b"G10 L20 P1 A0\n", serial_connection.writes)
        self.assertEqual(
            controller._controller_coordinate_offsets["G54"],
            (0.0, 0.0, 0.0, -1.0, 0.0),
        )
        self.assertAlmostEqual(controller.latest_a_position(), 0.0)

    def test_query_axis_max_feedrates_reads_controller_config_dump(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"axes:\n",
                b"  x:\n",
                b"    max_rate_mm_per_min: 500\n",
                b"  a:\n",
                b"    max_rate_mm_per_min: 80\n",
                b"ok\n",
            ]
        )
        controller._serial = serial_connection

        rates = controller.query_axis_max_feedrates()

        self.assertEqual(rates, {"X": 500.0, "A": 80.0})
        self.assertEqual(controller._axis_max_feedrates, rates)
        self.assertEqual(serial_connection.writes, [b"$CD\n"])

    def test_query_axis_max_feedrates_ignores_leading_ok(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"ok\n",
                b"axes:\n",
                b"  x:\n",
                b"    max_rate_mm_per_min: 500\n",
                b"  z:\n",
                b"    max_rate_mm_per_min: 100\n",
                b"  a:\n",
                b"    max_rate_mm_per_min: 80\n",
                b"ok\n",
            ]
        )
        controller._serial = serial_connection

        rates = controller.query_axis_max_feedrates()

        self.assertEqual(rates, {"X": 500.0, "Z": 100.0, "A": 80.0})

    def test_max_feedrate_for_axes_uses_limiting_axis(self) -> None:
        controller = StageController()
        controller.apply_axis_max_feedrates({"X": 500.0, "Y": 400.0, "Z": 100.0})

        self.assertEqual(controller.max_feedrate_for_axes(("X", "Y")), 400.0)
        self.assertEqual(controller.max_feedrate_for_axes(("Z",)), 100.0)
        self.assertEqual(
            controller.max_feedrate_for_axes(("C",)),
            controller.DEFAULT_FEEDRATE,
        )

    def test_ensure_axis_limits_refreshes_partial_cache(self) -> None:
        controller = StageController()
        controller._axis_limits = {"X": (0.0, 64.0)}
        serial_connection = _LineFakeSerial(
            [
                b"[MSG:INFO: Axis X (0.000,64.000)]\n",
                b"[MSG:INFO: Axis Y (0.000,64.000)]\n",
                b"[MSG:INFO: Axis Z (0.000,23.000)]\n",
                b"[MSG:INFO: Axis A (-5.500,0.000)]\n",
                b"ok\n",
            ]
        )

        controller._serial = serial_connection

        controller._ensure_axis_limits(
            required_axes=controller.CONTROLLER_LIMIT_AXES
        )

        self.assertEqual(controller._axis_limits["Z"], (0.0, 23.0))
        self.assertEqual(controller._axis_limits["A"], (-5.5, 0.0))
        self.assertEqual(serial_connection.writes, [b"$Startup/Show\n"])

    def test_absolute_xy_move_requires_serial(self) -> None:
        controller = StageController()
        controller._serial = None
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )

        controller._run_move_to_xy(1.0, 1.0)

        self.assertEqual(movement_results[-1][0], False)
        self.assertIn("Serial connection is not available", movement_results[-1][1])

    def test_absolute_xyz_move_uses_safe_transfer_z_before_xy(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._position_reporting_mode = "machine"

        sent_targets = []
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                position=(10.0, 20.0, 8.0, 0.0),
                display_position=(10.0, 20.0, 8.0, 0.0),
                homed_axes={"X", "Y", "Z"},
            ),
            types.SimpleNamespace(
                state="Idle",
                position=(30.0, 40.0, 6.0, 0.0),
                display_position=(30.0, 40.0, 6.0, 0.0),
                homed_axes={"X", "Y", "Z"},
            ),
        ]

        controller._move_safety_check = lambda: None
        controller._wait_for_idle = lambda: None
        controller._query_status = lambda _serial: statuses.pop(0)
        controller._send_absolute_axis_targets_move = (
            lambda targets, **kwargs: sent_targets.append((dict(targets), dict(kwargs)))
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )

        controller._run_move_to_xyz(30.0, 40.0, 6.0, 3.0, "stone position")

        self.assertEqual(
            sent_targets,
            [
                ({"Z": 3.0}, {"as_jog": True}),
                ({"X": 30.0, "Y": 40.0}, {"as_jog": True}),
                ({"Z": 6.0}, {"as_jog": True}),
            ],
        )
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("Arrived at stone position", movement_results[-1][1])

    def test_click_to_move_holds_serial_lock(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._pixels_to_mm = _stage_controller_module.np.eye(2) * 0.1
        controller._move_safety_check = lambda: None
        controller._ensure_calibration = lambda target_pixels=None: (
            False,
            None,
        )
        controller._get_frame_snapshot = lambda timeout=3.0: (object(), 1)
        controller._wait_for_new_frame = (
            lambda frame_counter, timeout=4.0: (object(), frame_counter + 1)
        )
        controller.movement_started = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.status_message = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        observed = []

        def _send_relative_move(_move, **_kwargs) -> None:
            def _probe() -> None:
                acquired = controller._serial_session_lock.acquire(blocking=False)
                observed.append(acquired)
                if acquired:
                    controller._serial_session_lock.release()

            thread = threading.Thread(target=_probe)
            thread.start()
            thread.join()

        controller._send_relative_move = _send_relative_move

        controller._run_move(10.0, -5.0)

        self.assertEqual(observed, [False])
        self.assertEqual(movement_results[-1], (True, "Move complete."))


class StageControllerStatusParsingTest(unittest.TestCase):
    def test_work_mode_requests_wpos_status_reports(self) -> None:
        controller = StageController()

        self.assertEqual(controller._desired_status_report_mask_for_mode("work"), 2)
        self.assertEqual(controller._desired_status_report_mask_for_mode("machine"), 3)

    def test_parse_status_line_uses_native_work_position(self) -> None:
        controller = StageController()
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            32.0,
            32.0,
            0.0,
            0.0,
            0.0,
        )

        status = controller._parse_status_line(
            "<Idle|WPos:-6.894,-6.599,9.207,0.000,2.170|Bf:15,127|FS:0,0>"
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertIsNone(status.position)
        self.assertEqual(status.work_offset[:2], (32.0, 32.0))
        self.assertAlmostEqual(status.work_position[0], -6.894)
        self.assertAlmostEqual(status.work_position[1], -6.599)

    def test_parse_status_line_captures_limit_pins(self) -> None:
        controller = StageController()

        status = controller._parse_status_line(
            "<Alarm|WPos:0.000,10.000,1.000,0.000|Pn:XY|FS:0,0>"
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.pins, {"X", "Y"})

    def test_limit_axes_update_from_status_pins(self) -> None:
        controller = StageController()
        emitted = []
        controller.limit_axes_changed = types.SimpleNamespace(
            emit=lambda axes: emitted.append(set(axes))
        )
        status = controller._parse_status_line(
            "<Alarm|WPos:0.000,10.000,1.000,0.000|Pn:Y|FS:0,0>"
        )

        assert status is not None
        controller._update_limit_axes_from_status(status)

        self.assertEqual(controller._limit_axes, {"Y"})
        self.assertEqual(emitted, [{"Y"}])

    def test_soft_limit_message_marks_axis_limited(self) -> None:
        controller = StageController()
        emitted = []
        controller.limit_axes_changed = types.SimpleNamespace(
            emit=lambda axes: emitted.append(set(axes))
        )

        controller._handle_limit_line("[MSG:INFO: Soft limit on X target:-8.000]")

        self.assertEqual(controller._limit_axes, {"X"})
        self.assertEqual(emitted, [{"X"}])

    def test_work_mode_ignores_machine_position_status_reports(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "work"
        controller._active_work_coordinate_system = "G54"
        controller._controller_coordinate_offsets["G54"] = (
            32.0,
            32.0,
            0.0,
            0.0,
            0.0,
        )

        status = controller._parse_status_line(
            "<Idle|MPos:25.106,25.401,9.207,0.000,2.170|Bf:15,127|FS:0,0>"
        )

        self.assertIsNone(status)

    def test_parse_status_line_rejects_truncated_machine_position(self) -> None:
        controller = StageController()

        status = controller._parse_status_line("<Idle|MPos:29.459,31.4|FS:0,0>")

        self.assertIsNone(status)

    def test_required_coordinate_status_retries_after_empty_status(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "work"
        statuses = [
            None,
            types.SimpleNamespace(
                state="Idle",
                position=None,
                work_position=(1.0, 2.0, 3.0),
                display_position=(1.0, 2.0, 3.0),
            ),
        ]
        calls = []

        def _query_status(_serial) -> object:
            calls.append(1)
            return statuses.pop(0)

        controller._query_status = _query_status

        status = controller._query_status_with_required_coordinates(
            _FakeSerial(),
            axes=("X", "Y"),
        )

        self.assertIsNotNone(status)
        self.assertEqual(len(calls), 2)
        self.assertEqual(status.work_position[:2], (1.0, 2.0))

    def test_required_coordinate_status_retries_short_axis_tuple(self) -> None:
        controller = StageController()
        controller._position_reporting_mode = "machine"
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                position=(1.0, 2.0, 3.0),
                work_position=(1.0, 2.0, 3.0),
                display_position=(1.0, 2.0, 3.0),
            ),
            types.SimpleNamespace(
                state="Idle",
                position=(1.0, 2.0, 3.0, -0.5),
                work_position=(1.0, 2.0, 3.0, -0.50),
                display_position=(1.0, 2.0, 3.0, -0.50),
            ),
        ]
        calls = []

        def _query_status(_serial) -> object:
            calls.append(1)
            return statuses.pop(0)

        controller._query_status = _query_status

        status = controller._query_status_with_required_coordinates(
            _FakeSerial(),
            axes=("A",),
        )

        self.assertIsNotNone(status)
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(status.position[3], -0.50)


class StageControllerAutofocusTest(unittest.TestCase):
    def test_autofocus_sweep_feedrate_uses_live_frame_rate(self) -> None:
        controller = StageController()
        start = time.monotonic() - 0.25
        with controller._frame_condition:
            for index in range(6):
                controller._frame_history.append(
                    (
                        index + 1,
                        start + index * 0.05,
                        np.zeros((2, 2), dtype=np.uint8),
                    )
                )

        feedrate = controller._autofocus_sweep_feedrate_mm_min(0.002)

        self.assertAlmostEqual(feedrate, 2.4, places=5)

    def test_autofocus_holds_serial_lock_and_refreshes_work_offsets(self) -> None:
        controller = StageController()
        lock = _TrackingLock()
        controller._serial_session_lock = lock
        serial_connection = _LockedLineFakeSerial(
            [
                b"ok\n",
                b"[GC:G54 G21 G90]\n",
                b"ok\n",
                b"[G54:32.000,32.000,0.000,0.000,0.000]\n",
                b"ok\n",
                b"<Idle|WPos:-0.842,-2.953,9.520,0.000,0.000|Bf:15,127|FS:0,0>\n",
            ],
            lock,
        )
        controller._serial = serial_connection
        controller._needles_known = True
        controller._needles_up = True
        controller._homed_axes = {"Z"}
        controller._axis_limits = {"Z": (0.0, 20.0)}
        controller._position_reporting_mode = "work"
        controller._current_status_report_mask = 2
        controller._active_work_coordinate_system = None
        controller._controller_coordinate_offsets.clear()
        controller.AUTOFOCUS_SAMPLES = 1
        frame = np.array([[1.0, 2.0], [3.0, 4.0]])
        controller._get_frame_snapshot = lambda timeout=3.0: (frame, 1)
        controller._focus_metric = lambda _frame: 10.0
        controller._autofocus_sweep_feedrate_mm_min = lambda _fine_step: 12.0
        controller.status_message = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        finished = []
        controller.autofocus_finished = types.SimpleNamespace(
            emit=lambda success, message: finished.append((success, message))
        )

        controller._run_focus_sweep_locked = (
            lambda *_args, **_kwargs: FocusSweepResult(
                best_z=9.520,
                best_score=10.0,
                sample_count=5,
                edge_peak=False,
            )
        )
        controller._run_static_focus_refinement_locked = (
            lambda *_args, **_kwargs: FocusSweepResult(
                best_z=9.515,
                best_score=12.0,
                sample_count=9,
                edge_peak=False,
            )
        )
        controller._approach_z_from_below_locked = lambda *_args, **_kwargs: None
        try:
            controller._run_autofocus()
        finally:
            controller.shutdown()

        self.assertTrue(finished[-1][0])
        self.assertEqual(controller._active_work_coordinate_system, "G54")
        self.assertIn("G54", controller._controller_coordinate_offsets)
        self.assertGreaterEqual(lock.max_depth, 1)
        self.assertEqual(
            serial_connection.writes[:3],
            [b"$G\n", b"$#\n", b"?\n"],
        )

    def test_static_autofocus_refinement_corrects_sweep_z_bias(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        controller._serial = serial_connection
        current_z = [10.040]
        true_focus_z = 9.980

        def _status() -> types.SimpleNamespace:
            position = (0.0, 0.0, current_z[0])
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"Z"},
            )

        def _send_relative_move(move, **_kwargs) -> None:
            current_z[0] += move.z

        frame_counter = [0]

        def _wait_for_new_frame(previous_counter, timeout=2.0):
            frame_counter[0] = max(frame_counter[0], previous_counter) + 1
            return np.array([[current_z[0]]], dtype=np.float32), frame_counter[0]

        controller._objective_autofocus_fine_step_mm = 0.01
        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._wait_for_new_frame = _wait_for_new_frame
        controller._focus_metric = (
            lambda frame: 1.0 - (float(frame[0, 0]) - true_focus_z) ** 2
        )

        result = controller._run_static_focus_refinement_locked(
            10.000,
            min_z=0.0,
            max_z=20.0,
            step_mm=0.01,
        )

        self.assertLess(result.best_z, 10.000)
        self.assertAlmostEqual(result.best_z, true_focus_z, places=5)
        self.assertEqual(
            result.sample_count,
            controller.AUTOFOCUS_STATIC_REFINEMENT_POINTS,
        )
        self.assertFalse(result.edge_peak)

    def test_static_autofocus_refinement_expands_from_edge_peak(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        controller._serial = serial_connection
        current_z = [10.000]
        true_focus_z = 10.060

        def _status() -> types.SimpleNamespace:
            position = (0.0, 0.0, current_z[0])
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"Z"},
            )

        def _send_relative_move(move, **_kwargs) -> None:
            current_z[0] += move.z

        frame_counter = [0]

        def _wait_for_new_frame(previous_counter, timeout=2.0):
            frame_counter[0] = max(frame_counter[0], previous_counter) + 1
            return np.array([[current_z[0]]], dtype=np.float32), frame_counter[0]

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._wait_for_new_frame = _wait_for_new_frame
        controller._focus_metric = (
            lambda frame: 1.0 - (float(frame[0, 0]) - true_focus_z) ** 2
        )

        result = controller._run_static_focus_refinement_locked(
            10.000,
            min_z=0.0,
            max_z=20.0,
            step_mm=0.01,
        )

        self.assertAlmostEqual(result.best_z, true_focus_z, places=5)
        self.assertGreater(
            result.sample_count,
            controller.AUTOFOCUS_STATIC_REFINEMENT_POINTS,
        )
        self.assertFalse(result.edge_peak)

    def test_external_local_autofocus_uses_static_refinement_window(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        controller._serial = serial_connection
        calls: list[tuple[object, ...]] = []
        finished: list[tuple[bool, str]] = []
        controller.autofocus_finished = types.SimpleNamespace(
            emit=lambda success, message: finished.append((success, message))
        )

        def _prepare(*, range_mm, step_mm):
            calls.append(("prepare", range_mm, step_mm))
            return AutofocusContext(
                objective_name="X20",
                start_z=10.000,
                min_z=0.0,
                max_z=20.0,
                lower_z=9.970,
                upper_z=10.030,
                local_range_mm=0.030,
                fine_step_mm=0.010,
            )

        def _static(center_z, *, min_z, max_z, step_mm):
            calls.append(("static", center_z, min_z, max_z, step_mm))
            return FocusSweepResult(
                best_z=10.012,
                best_score=2.5,
                sample_count=5,
                edge_peak=False,
            )

        def _approach(target_z, *, min_z, fine_step_mm):
            calls.append(("approach", target_z, min_z, fine_step_mm))

        controller._prepare_autofocus_context_locked = _prepare
        controller._run_static_focus_refinement_locked = _static
        controller._approach_z_from_below_locked = _approach

        result = controller.run_external_local_autofocus(range_mm=0.030)

        self.assertIn("local complete", result.summary())
        self.assertAlmostEqual(result.best_z_mm, 10.012)
        self.assertAlmostEqual(result.delta_um, 12.0)
        self.assertEqual(result.to_dict()["focus_best_z_mm"], 10.012)
        self.assertEqual(finished[-1][0], True)
        self.assertEqual(
            calls,
            [
                ("prepare", 0.030, None),
                ("static", 10.000, 9.970, 10.030, 0.010),
                ("approach", 10.012, 0.0, 0.010),
            ],
        )

    def test_external_local_autofocus_cancel_restores_start_z(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        controller._serial = serial_connection
        current_z = [10.025]
        moves: list[float] = []
        statuses: list[str] = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda message: statuses.append(str(message))
        )

        def _prepare(*, range_mm, step_mm):
            return AutofocusContext(
                objective_name="X20",
                start_z=10.000,
                min_z=0.0,
                max_z=20.0,
                lower_z=9.970,
                upper_z=10.030,
                local_range_mm=0.030,
                fine_step_mm=0.010,
            )

        def _static(center_z, *, min_z, max_z, step_mm):
            controller._cancel_event.set()
            raise StageControllerError("Operation cancelled.")

        def _status(_serial, *, axes):
            position = (0.0, 0.0, current_z[0])
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"Z"},
            )

        def _send_relative_move(move, **_kwargs) -> None:
            self.assertFalse(controller._cancel_event.is_set())
            current_z[0] += move.z
            moves.append(move.z)

        controller._prepare_autofocus_context_locked = _prepare
        controller._run_static_focus_refinement_locked = _static
        controller._query_status_with_required_coordinates = _status
        controller._position_for_configured_mode = (
            lambda status: status.display_position
        )
        controller._wait_for_idle = lambda timeout=10.0: None
        controller._send_relative_move = _send_relative_move

        with self.assertRaisesRegex(StageControllerError, "Operation cancelled"):
            controller.run_external_local_autofocus(range_mm=0.030)

        self.assertAlmostEqual(current_z[0], 10.000)
        self.assertEqual(len(moves), 1)
        self.assertAlmostEqual(moves[0], -0.025)
        self.assertIn(b"\x85", serial_connection.writes)
        self.assertTrue(controller._cancel_event.is_set())
        self.assertIn("returning to start Z", statuses[-1])

    def test_serial_session_pins_active_connection(self) -> None:
        controller = StageController()
        original = _FakeSerial()
        replacement = _FakeSerial()
        controller._serial = original

        with controller._serial_session():
            controller._serial = replacement
            self.assertIs(controller._current_serial(), original)

        self.assertIs(controller._current_serial(), replacement)

    def test_absolute_motion_sync_keeps_config_io_on_captured_serial(self) -> None:
        controller = StageController()
        replacement = _RejectingSerial()

        def swap_after_status_mask(payload: bytes) -> None:
            if payload == b"$10=2\n":
                controller._serial = replacement

        original = _SwappingLineFakeSerial(
            [
                b"ok\n",
                b"[GC:G1 G54 G17 G21 G90]\n",
                b"ok\n",
                b"[G54:0.000,0.000,0.000,0.000,0.000]\n",
                b"ok\n",
                b"<Idle|WPos:1.000,2.000,3.000,0.000|WCO:0.000,0.000,0.000,0.000|H:XYZA>\n",
            ],
            swap_after_status_mask,
        )
        controller._serial = original

        with controller._serial_session_lock:
            status = controller._query_synced_status_for_absolute_motion(min_axes=2)

        self.assertIsNotNone(status)
        self.assertEqual(
            original.writes,
            [b"$10=2\n", b"$G\n", b"$#\n", b"?\n"],
        )
        self.assertEqual(replacement.writes, [])


class StageControllerObjectiveTest(unittest.TestCase):
    def test_verification_moves_directly_to_click_target(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        controller._serial = serial_connection
        current = [0.0, 0.0, 0.0]
        moves: list[MoveVector] = []
        controller._pixels_to_mm = np.array(
            [[0.001, 0.0], [0.0, 0.001]],
            dtype=float,
        )
        controller._frame_counter = 10

        def _status() -> types.SimpleNamespace:
            position = tuple(current)
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"X", "Y"},
            )

        def _send_relative_move(move, **_kwargs) -> None:
            moves.append(move)
            current[0] += move.x
            current[1] += move.y

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._get_frame_snapshot = (
            lambda timeout=3.0: (np.zeros((8, 8), dtype=np.uint8), 10)
        )
        controller._wait_for_new_frame = (
            lambda frame_counter, timeout=2.0: (
                np.zeros((8, 8), dtype=np.uint8),
                frame_counter + 1,
            )
        )
        controller._estimate_shift = lambda *_args: (40.0, 0.0)
        controller.status_message = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )

        handled, before_counter = controller._verify_active_objective_calibration(
            target_pixels=np.array([10.0, -5.0], dtype=float),
        )

        self.assertTrue(handled)
        self.assertEqual(before_counter, 10)
        self.assertEqual(len(moves), 2)
        self.assertAlmostEqual(moves[0].x, 0.04)
        self.assertAlmostEqual(moves[0].y, 0.0)
        self.assertAlmostEqual(moves[1].x, -0.05)
        self.assertAlmostEqual(moves[1].y, 0.005)
        self.assertAlmostEqual(current[0], -0.01)
        self.assertAlmostEqual(current[1], 0.005)

    def test_fresh_calibration_moves_directly_to_click_target(self) -> None:
        controller = StageController()
        serial_connection = _FakeSerial()
        controller._serial = serial_connection
        current = [0.0, 0.0, 0.0]
        moves: list[MoveVector] = []
        axis_calls: list[str] = []
        controller._frame_counter = 12

        def _status() -> types.SimpleNamespace:
            position = tuple(current)
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"X", "Y"},
            )

        def _send_relative_move(move, **_kwargs) -> None:
            moves.append(move)
            current[0] += move.x
            current[1] += move.y

        def _calibrate_axis_series(_frame, _origin, axis: str):
            axis_calls.append(axis)
            if axis == "Y":
                current[0] = 0.02
                current[1] = 0.03
            return [
                (
                    np.array([1.0, 0.0], dtype=float),
                    np.array([1000.0, 0.0], dtype=float),
                ),
                (
                    np.array([0.0, 1.0], dtype=float),
                    np.array([0.0, 1000.0], dtype=float),
                ),
            ]

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._get_frame_snapshot = (
            lambda timeout=3.0: (np.zeros((8, 8), dtype=np.uint8), 12)
        )
        controller._calibrate_axis_series = _calibrate_axis_series
        controller._calibration_matrix_from_observations = (
            lambda _observations: np.eye(2, dtype=float) * 1000.0
        )
        controller.calibration_changed = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        controller.objective_calibration_updated = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *args, **kwargs: None
        )

        handled, before_counter = controller._ensure_calibration(
            target_pixels=np.array([10.0, -5.0], dtype=float),
        )

        self.assertTrue(handled)
        self.assertEqual(before_counter, 12)
        self.assertEqual(axis_calls, ["X", "Y"])
        self.assertEqual(len(moves), 1)
        self.assertAlmostEqual(moves[0].x, -0.03)
        self.assertAlmostEqual(moves[0].y, -0.025)
        self.assertAlmostEqual(current[0], -0.01)
        self.assertAlmostEqual(current[1], 0.005)

    def test_axis_calibration_uses_axis_reference_position(self) -> None:
        controller = StageController()
        controller.CALIBRATION_MAX_OBSERVATIONS_PER_AXIS = 4
        controller.CALIBRATION_MIN_OBSERVATIONS = 4
        controller.CALIBRATION_PROBE_STEP_MM = 0.1
        controller.CALIBRATION_MAX_ADAPTIVE_STEP_MM = 0.1
        controller.CALIBRATION_MAX_UNVERIFIED_STEP_MM = 0.1
        controller._objective_calibration_target_pixels = 1000.0
        current = [0.4, 0.0, 0.0]
        serial_connection = _FakeSerial()
        controller._serial = serial_connection

        def _status() -> types.SimpleNamespace:
            position = tuple(current)
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"X", "Y"},
            )

        def _send_relative_move(move) -> None:
            current[0] += move.x
            current[1] += move.y

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._wait_for_new_frame = (
            lambda frame_counter, timeout=2.0: (
                np.zeros((8, 8), dtype=np.uint8),
                frame_counter + 1,
            )
        )
        controller._estimate_shift_with_response = lambda *_args: (
            0.0,
            current[1] * 1000.0,
            1.0,
        )

        observations = controller._calibrate_axis_series(
            np.zeros((8, 8), dtype=np.uint8),
            (0.0, 0.0, 0.0),
            axis="Y",
        )

        self.assertEqual(len(observations), 4)
        for index, (mm_vector, _pixel_vector) in enumerate(observations, start=1):
            self.assertAlmostEqual(float(mm_vector[0]), 0.0)
            self.assertAlmostEqual(float(mm_vector[1]), 0.1 * index)

    def test_axis_calibration_adapts_probe_step_from_measured_pixels(self) -> None:
        controller = StageController()
        controller.CALIBRATION_MAX_OBSERVATIONS_PER_AXIS = 4
        controller.CALIBRATION_MIN_OBSERVATIONS = 4
        controller.CALIBRATION_PROBE_STEP_MM = 0.005
        controller.CALIBRATION_MAX_ADAPTIVE_STEP_MM = 0.08
        controller._objective_calibration_target_pixels = 120.0
        current = [0.0, 0.0, 0.0]
        moves: list[float] = []
        serial_connection = _FakeSerial()
        controller._serial = serial_connection

        def _status() -> types.SimpleNamespace:
            position = tuple(current)
            return types.SimpleNamespace(
                state="Idle",
                position=position,
                work_position=position,
                display_position=position,
                work_offset=(0.0, 0.0, 0.0),
                homed_axes={"X", "Y"},
            )

        def _send_relative_move(move) -> None:
            moves.append(move.x)
            current[0] += move.x

        controller._query_status = lambda _serial: _status()
        controller._send_relative_move = _send_relative_move
        controller._wait_for_new_frame = (
            lambda frame_counter, timeout=2.0: (
                np.zeros((8, 8), dtype=np.uint8),
                frame_counter + 1,
            )
        )
        controller._estimate_shift_with_response = lambda *_args: (
            current[0] * 600.0,
            0.0,
            1.0,
        )

        observations = controller._calibrate_axis_series(
            np.zeros((8, 8), dtype=np.uint8),
            (0.0, 0.0, 0.0),
            axis="X",
        )

        self.assertEqual(len(observations), 4)
        self.assertAlmostEqual(moves[0], 0.005)
        self.assertTrue(all(move < 0.1 for move in moves))
        self.assertNotIn(1.0, moves)

    def test_calibration_matrix_uses_axis_slope_when_observations_have_bias(self) -> None:
        controller = StageController()
        controller.CALIBRATION_MIN_OBSERVATIONS = 4
        observations: list[tuple[np.ndarray, np.ndarray]] = []
        for stage_x in (0.005, 0.010, 0.015, 0.020):
            observations.append(
                (
                    np.array([stage_x, 0.0], dtype=float),
                    np.array(
                        [
                            11.0 - 8400.0 * stage_x,
                            -2.0 + 25.0 * stage_x,
                        ],
                        dtype=float,
                    ),
                )
            )
        for stage_y in (0.005, 0.010, 0.015, 0.020):
            observations.append(
                (
                    np.array([0.0, stage_y], dtype=float),
                    np.array(
                        [
                            4.0 + 18.0 * stage_y,
                            -7.0 - 8700.0 * stage_y,
                        ],
                        dtype=float,
                    ),
                )
            )

        matrix = controller._calibration_matrix_from_observations(observations)

        self.assertAlmostEqual(float(matrix[0, 0]), -8400.0)
        self.assertAlmostEqual(float(matrix[1, 0]), 25.0)
        self.assertAlmostEqual(float(matrix[0, 1]), 18.0)
        self.assertAlmostEqual(float(matrix[1, 1]), -8700.0)

    def test_calibration_verify_step_uses_saved_matrix_scale(self) -> None:
        controller = StageController()
        controller.CALIBRATION_VERIFY_TARGET_PIXELS = 40.0
        controller.CALIBRATION_VERIFY_STEP_MM = 0.05
        controller.CALIBRATION_PROBE_STEP_MM = 0.005
        controller._pixels_to_mm = np.array(
            [[0.001, 0.0], [0.0, 0.001]],
            dtype=float,
        )

        self.assertAlmostEqual(controller._calibration_verify_step_mm(), 0.04)

    def test_best_objective_for_measurement_uses_saved_candidate_matrices(self) -> None:
        controller = StageController()
        controller._objective_matrices = {
            "X5": np.array([[0.002, 0.0], [0.0, 0.002]], dtype=float),
            "X20": np.array([[0.0005, 0.0], [0.0, 0.0005]], dtype=float),
        }

        best = controller._best_objective_for_measurement(
            np.array([100.0, 0.0]),
            np.array([0.05, 0.0]),
            tolerance=0.005,
        )

        self.assertEqual(best, "X20")

if __name__ == "__main__":
    unittest.main()
