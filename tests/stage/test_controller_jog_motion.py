import threading
import time
import types
import unittest

try:
    from .controller_test_support import (
        MoveVector,
        QueuedSerialWrite,
        StageController,
        StageControllerError,
        _FakeSerial,
        _LineFakeSerial,
        _RejectingSerial,
        _SwapOnFirstAcquireLock,
        _WritableFakeSerial,
    )
except ImportError:
    from controller_test_support import (
        MoveVector,
        QueuedSerialWrite,
        StageController,
        StageControllerError,
        _FakeSerial,
        _LineFakeSerial,
        _RejectingSerial,
        _SwapOnFirstAcquireLock,
        _WritableFakeSerial,
    )

class StageControllerJogQueueTest(unittest.TestCase):
    def test_cached_jog_status_uses_captured_serial_for_status_mask(self) -> None:
        controller = StageController()
        original = _LineFakeSerial(
            [
                b"ok\n",
                b"<Idle|WPos:0.000,0.000,3.840,2.967,0.000|Bf:15,127|FS:0,0>\n",
            ]
        )
        replacement = _RejectingSerial()
        positions = []
        try:
            controller._serial = original
            controller._serial_session_lock = _SwapOnFirstAcquireLock(
                lambda: setattr(controller, "_serial", replacement)
            )
            controller.stage_position_changed = types.SimpleNamespace(
                emit=lambda position: positions.append(position)
            )

            controller._refresh_cached_jog_status_if_missing()

            self.assertEqual(replacement.writes, [])
            self.assertEqual(original.writes, [b"$10=2\n", b"?\n"])
            self.assertEqual(positions[-1], (0.0, 0.0, 3.84, 2.967, 0.0))
        finally:
            controller.shutdown()

    def test_jog_stop_writes_immediately_when_serial_lock_is_free(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection

            controller.queue_jog_stop()

            self.assertEqual(serial_connection.writes, [b"\x85"])
            self.assertTrue(controller._async_write_queue.empty())
        finally:
            controller.shutdown()

    def test_force_jog_stop_writes_synchronously_before_serial_shutdown(self) -> None:
        controller = StageController()
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            controller._jog_motion_active = True

            written = controller.force_jog_stop(timeout=0.1)

            self.assertTrue(written)
            self.assertEqual(serial_connection.writes, [b"\x85"])
            self.assertFalse(controller._jog_motion_active)
            self.assertTrue(controller._async_write_queue.empty())
        finally:
            controller.shutdown()

    def test_jog_command_superseded_while_waiting_for_serial_lock_is_not_written(
        self,
    ) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            controller._serial_session_lock.acquire()
            try:
                controller.queue_jog_command("$J=G91 G21 X250.000 F10")
                deadline = time.monotonic() + 1.0
                while (
                    not controller._async_write_queue.empty()
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.005)
                controller.queue_jog_stop()
            finally:
                controller._serial_session_lock.release()

            controller._async_write_queue.join()

            self.assertEqual(serial_connection.writes, [b"\x85"])
        finally:
            controller.shutdown()

    def test_absolute_jog_reissue_queues_stop_before_new_feedrate_command(self) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        serial_connection = _WritableFakeSerial()
        try:
            controller._serial = serial_connection
            lock_acquired = threading.Event()
            release_lock = threading.Event()

            def _hold_serial_lock() -> None:
                with controller._serial_session_lock:
                    lock_acquired.set()
                    release_lock.wait(timeout=1.0)

            holder = threading.Thread(target=_hold_serial_lock)
            holder.start()
            self.assertTrue(lock_acquired.wait(timeout=1.0))
            accepted = controller.queue_absolute_axis_targets_jog(
                {"Y": -2.0, "X": 1.5},
                feedrate=180.0,
            )
            self.assertTrue(accepted)
            self.assertFalse(controller._jog_motion_active)
            self.assertEqual(serial_connection.writes, [])
            release_lock.set()
            holder.join(timeout=1.0)

            controller._async_write_queue.join()

            self.assertEqual(
                serial_connection.writes,
                [b"\x85", b"$J=G90 G21 X1.5000 Y-2.0000 F180\n"],
            )
        finally:
            controller.shutdown()

    def test_absolute_jog_reissue_rejects_busy_stage_without_replace(self) -> None:
        controller = StageController()
        release_thread = threading.Event()
        active_thread = threading.Thread(target=lambda: release_thread.wait(timeout=1.0))
        active_thread.start()
        try:
            controller._active_thread = active_thread

            with self.assertRaises(StageControllerError):
                controller.queue_absolute_axis_targets_jog(
                    {"X": 1.0},
                    feedrate=180.0,
                )

            self.assertFalse(controller._cancel_event.is_set())
        finally:
            release_thread.set()
            active_thread.join(timeout=1.0)
            controller.shutdown()

    def test_absolute_jog_reissue_can_replace_active_coordinate_waiter(self) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        serial_connection = _WritableFakeSerial()
        lock_acquired = threading.Event()
        release_thread = threading.Event()

        def _hold_serial_lock() -> None:
            with controller._serial_session_lock:
                lock_acquired.set()
                release_thread.wait(timeout=1.0)

        active_thread = threading.Thread(target=_hold_serial_lock)
        active_thread.start()
        try:
            controller._serial = serial_connection
            controller._active_thread = active_thread
            self.assertTrue(lock_acquired.wait(timeout=1.0))
            accepted = controller.queue_absolute_axis_targets_jog(
                {"Y": -2.0, "X": 1.5},
                feedrate=180.0,
                replace_active=True,
            )

            self.assertTrue(accepted)
            self.assertTrue(controller._cancel_event.is_set())
            self.assertEqual(serial_connection.writes, [])

            release_thread.set()
            active_thread.join(timeout=1.0)
            controller._async_write_queue.join()

            self.assertEqual(
                serial_connection.writes,
                [b"\x85", b"$J=G90 G21 X1.5000 Y-2.0000 F180\n"],
            )
        finally:
            release_thread.set()
            active_thread.join(timeout=1.0)
            controller.shutdown()

    def test_superseded_jog_command_is_dropped_before_write(self) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        controller._queued_jog_generation = 2
        job = QueuedSerialWrite(
            priority=controller.SERIAL_PRIORITY_JOG_COMMAND,
            sequence=1,
            kind="jog_command",
            payload=b"$J=G91 G21 X250.000 F10.0\n",
            description="$J=G91 G21 X250.000 F10.0",
            generation=1,
        )

        ready = controller._await_current_jog_command(job)

        self.assertFalse(ready)

    def test_current_jog_command_survives_settle_window(self) -> None:
        controller = StageController()
        controller.SERIAL_JOG_COMMAND_SETTLE_S = 0.0
        controller._queued_jog_generation = 3
        job = QueuedSerialWrite(
            priority=controller.SERIAL_PRIORITY_JOG_COMMAND,
            sequence=1,
            kind="jog_command",
            payload=b"$J=G91 G21 Y250.000 F10.0\n",
            description="$J=G91 G21 Y250.000 F10.0",
            generation=3,
        )

        ready = controller._await_current_jog_command(job)

        self.assertTrue(ready)


class StageControllerMotionSafetyBypassTest(unittest.TestCase):
    def test_disabled_motion_safety_bypasses_needle_safety_and_axis_limits(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        commands = []
        controller._ensure_axis_limits = (
            lambda: (_ for _ in ()).throw(AssertionError("limits checked"))
        )
        controller._check_relative_move_limits = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("relative limits checked")
            )
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        controller._move_safety_check()
        controller._send_relative_move(MoveVector(a=0.25))

        self.assertIn("G1 A0.2500 F600", commands)

    def test_disabled_motion_safety_allows_absolute_manual_axis_move(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        commands = []
        controller._ensure_axis_limits = (
            lambda: (_ for _ in ()).throw(AssertionError("limits checked"))
        )
        controller._query_status = (
            lambda _serial: (_ for _ in ()).throw(AssertionError("status queried"))
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        controller._send_absolute_axis_move(
            "B",
            0.25,
            ignore_needle_safety=True,
            feedrate=123.4,
        )

        self.assertIn("G90", commands)
        self.assertIn("G1 B0.2500 F123.4", commands)

    def test_multi_axis_absolute_move_uses_single_g90_command(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._needles_known = True
        controller._needles_up = True
        controller._axis_limits = {"X": (0.0, 64.0), "Y": (0.0, 64.0)}
        controller._active_work_coordinate_system = "G54"
        commands = []
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=None,
            work_position=(0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 0.0),
            work_offset=(32.0, 32.0, 0.0),
            coordinate_system="G54",
            homed_axes={"X", "Y"},
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None
        controller._reset_feed_override = lambda: None

        controller._send_absolute_axis_targets_move(
            {"X": 10.0, "Y": -5.0},
            feedrate=123.4,
        )

        self.assertIn("G90", commands)
        self.assertIn("G1 X10.0000 Y-5.0000 F123.4", commands)

    def test_coordinate_task_uses_cancelable_absolute_jog(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        commands = []
        target_idle_calls = []
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )
        controller._wait_for_idle_at_targets = (
            lambda targets, **kwargs: target_idle_calls.append(
                (dict(targets), dict(kwargs))
            )
        )

        controller._run_absolute_axis_targets_move(
            {"X": 1.5, "Y": -2.0},
            25.0,
        )

        self.assertIn("$J=G90 G21 X1.5000 Y-2.0000 F25", commands)
        self.assertFalse(any(command.startswith("G1 ") for command in commands))
        self.assertEqual(target_idle_calls[0][0], {"X": 1.5, "Y": -2.0})
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("complete", movement_results[-1][1])

    def test_external_absolute_targets_waits_for_completion(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        movement_results = []
        idle_calls = []
        commands = []
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for plain idle")
        )
        controller._wait_for_idle_at_targets = (
            lambda targets, **kwargs: idle_calls.append(
                (dict(targets), dict(kwargs))
            )
        )
        controller._query_status = lambda _serial: None
        try:
            message = controller.run_external_absolute_axis_targets_move(
                {"z": 3.25},
                feedrate=12.5,
            )
        finally:
            controller.shutdown()

        self.assertIn("$J=G90 G21 Z3.2500 F12.5", commands)
        self.assertTrue(idle_calls)
        self.assertEqual(idle_calls[0][0], {"Z": 3.25})
        self.assertIn("Z+3.250", message)
        self.assertEqual(movement_results[-1][0], True)

    def test_machine_coordinate_jog_uses_g53(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._position_reporting_mode = "machine"
        controller._serial = _FakeSerial()
        commands = []
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._send_absolute_axis_targets_move(
            {"X": 4.0},
            ignore_needle_safety=True,
            feedrate=50.0,
            wait_for_completion=False,
            as_jog=True,
        )

        self.assertIn("$J=G90 G21 G53 X4.0000 F50", commands)

    def test_homed_work_xy_target_uses_reported_wco_limits(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._needles_known = True
        controller._needles_up = True
        controller._axis_limits = {"X": (0.0, 64.0), "Y": (0.0, 64.0)}
        controller._position_reporting_mode = "work"
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=None,
            work_position=(0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 0.0),
            work_offset=(32.0, 32.0, 0.0),
            coordinate_system="G54",
            homed_axes={"X", "Y"},
        )

        with self.assertRaises(StageControllerError):
            controller._send_absolute_axis_targets_move(
                {"X": 37.0},
                feedrate=100.0,
            )

    def test_unhomed_work_xy_target_does_not_apply_software_limits(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._needles_known = True
        controller._needles_up = True
        controller._axis_limits = {"X": (0.0, 64.0)}
        commands = []
        controller._position_reporting_mode = "work"
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=None,
            work_position=(0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 0.0),
            work_offset=None,
            coordinate_system=None,
            homed_axes=set(),
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None
        controller._reset_feed_override = lambda: None

        controller._send_absolute_axis_targets_move(
            {"X": 37.0},
            feedrate=100.0,
            allow_unhomed=True,
        )

        self.assertIn("G1 X37.0000 F100", commands)

    def test_relative_manual_axis_move_is_resolved_to_absolute_g90(
        self,
    ) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        commands = []
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 0.0, -0.1),
            work_position=(0.0, 0.0, 0.0, -0.1),
            display_position=(0.0, 0.0, 0.0, -0.1),
            homed_axes={"A"},
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._run_manual_axis_move("A", -0.02, "G91", 1.0)

        self.assertEqual(commands, ["$J=G90 G21 A-0.1200 F1"])
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("accepted", movement_results[-1][1])

    def test_relative_manual_axis_move_can_use_unhomed_current_position(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._needles_known = True
        controller._needles_up = True
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        commands = []
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(1.0, 0.0, 0.0),
            work_position=(1.0, 0.0, 0.0),
            display_position=(1.0, 0.0, 0.0),
            homed_axes=set(),
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._run_manual_axis_move("X", 0.25, "G91", 10.0)

        self.assertEqual(commands, ["$J=G90 G21 X1.2500 F10"])
        self.assertEqual(movement_results[-1][0], True)

    def test_absolute_manual_axis_zero_target_is_sent(self) -> None:
        controller = StageController()
        controller.set_motion_safety_disabled(True)
        controller._serial = _FakeSerial()
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        movement_results = []
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda success, message: movement_results.append((success, message))
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        commands = []
        controller._query_status = lambda _serial: (_ for _ in ()).throw(
            AssertionError("status queried")
        )
        controller._write_command = lambda _serial, command: commands.append(command)
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("waited for idle")
        )

        controller._run_manual_axis_move("A", 0.0, "G90", 5.0)

        self.assertEqual(commands, ["$J=G90 G21 A0.0000 F5"])
        self.assertEqual(movement_results[-1][0], True)
        self.assertIn("accepted", movement_results[-1][1])

    def test_idle_timeout_scales_with_slow_feedrate(self) -> None:
        controller = StageController()

        self.assertEqual(controller._idle_timeout_for_distance(0.25, 1.0), 20.0)

if __name__ == "__main__":
    unittest.main()
