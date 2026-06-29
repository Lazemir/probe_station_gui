import types
import unittest

try:
    from .controller_test_support import (
        StageController,
        StageControllerError,
        _BufferedFakeSerial,
        _BufferedLineFakeSerial,
        _FakeSerial,
        _LineFakeSerial,
        _RejectingSerial,
        _SwappingLineFakeSerial,
        _SwapOnFirstAcquireLock,
    )
except ImportError:
    from controller_test_support import (
        StageController,
        StageControllerError,
        _BufferedFakeSerial,
        _BufferedLineFakeSerial,
        _FakeSerial,
        _LineFakeSerial,
        _RejectingSerial,
        _SwappingLineFakeSerial,
        _SwapOnFirstAcquireLock,
    )

_stage_controller_module = __import__(StageController.__module__, fromlist=["time"])

class StageControllerStatusRefreshTest(unittest.TestCase):
    def test_poll_status_once_uses_captured_serial_for_status_mask(self) -> None:
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

            controller._poll_status_once()

            self.assertEqual(replacement.writes, [])
            self.assertEqual(original.writes, [b"$10=2\n", b"?\n"])
            self.assertEqual(positions[-1], (0.0, 0.0, 3.84, 2.967, 0.0))
        finally:
            controller.shutdown()

    def test_status_query_discards_stale_buffered_status_before_query(self) -> None:
        controller = StageController()
        controller._current_status_report_mask = (
            controller._desired_status_report_mask_for_mode("work")
        )
        stale = b"<Idle|WPos:0.000,0.000,0.000,-0.004,0.000|Bf:15,127|FS:0,0>\n"
        serial_connection = _BufferedLineFakeSerial(
            stale,
            [
                b"<Idle|WPos:0.000,0.000,0.000,-0.010,0.000|Bf:15,127|FS:0,0>\n",
            ],
        )

        status = controller._query_status(serial_connection)

        self.assertIsNotNone(status)
        self.assertEqual(status.work_position[3], -0.010)
        self.assertEqual(bytes(serial_connection.discarded), stale)
        self.assertEqual(serial_connection.writes, [b"?\n"])

    def test_status_query_detects_controller_reboot_in_discarded_input(self) -> None:
        controller = StageController()
        controller._current_status_report_mask = (
            controller._desired_status_report_mask_for_mode("work")
        )
        controller._homed_axes = {"A", "X", "Y", "Z"}
        controller._needles_up = True
        controller._needles_known = True
        controller._controller_state_stale = False
        controller._controller_session_marker = 800
        controller._last_stage_position = (1.0, 2.0, 3.0, 4.0)
        controller._axis_limits = {"Z": (0.0, 23.0)}
        controller._axis_max_feedrates = {"Z": 100.0}
        positions = []
        reboot_events = []
        ready_events = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )
        controller.controller_reboot_detected = types.SimpleNamespace(
            emit=lambda: reboot_events.append(True)
        )
        controller.controller_reboot_ready = types.SimpleNamespace(
            emit=lambda: ready_events.append(True)
        )
        boot_text = (
            b"ok\r\n\r\n"
            b"Brownout detector was triggered\r\n\r\n"
            b"ets Jul 29 2019 12:21:46\r\n\r\n"
            b"rst:0xc (SW_CPU_RESET),boot:0x12 (SPI_FAST_FLASH_BOOT)\r\n"
        )
        serial_connection = _BufferedLineFakeSerial(
            boot_text,
            [
                b"<Idle|WPos:-32.000,-32.000,0.000,2.968,0.000|Bf:15,127|FS:0,0>\n",
            ],
        )

        status = controller._query_status(serial_connection)

        self.assertIsNotNone(status)
        self.assertEqual(status.work_position[:4], (-32.0, -32.0, 0.0, 2.968))
        self.assertEqual(bytes(serial_connection.discarded), boot_text)
        self.assertEqual(serial_connection.writes, [b"?\n"])
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertIsNone(controller._controller_session_marker)
        self.assertEqual(controller._axis_limits, {})
        self.assertEqual(controller._axis_max_feedrates, {})
        self.assertEqual(positions[0], None)
        self.assertEqual(reboot_events, [True])
        self.assertEqual(ready_events, [True])

    def test_target_idle_wait_ignores_stale_idle_before_target(self) -> None:
        controller = StageController()
        statuses = [
            types.SimpleNamespace(
                state="Idle",
                work_position=(0.0, 0.0, 0.0, -0.004, 0.0),
                position=None,
                display_position=(0.0, 0.0, 0.0, -0.004, 0.0),
            ),
            types.SimpleNamespace(
                state="Jog",
                work_position=(0.0, 0.0, 0.0, -0.007, 0.0),
                position=None,
                display_position=(0.0, 0.0, 0.0, -0.007, 0.0),
            ),
            types.SimpleNamespace(
                state="Idle",
                work_position=(0.0, 0.0, 0.0, -0.010, 0.0),
                position=None,
                display_position=(0.0, 0.0, 0.0, -0.010, 0.0),
            ),
        ]
        seen = []

        def _query_status(_serial):
            status = statuses.pop(0)
            seen.append(status)
            return status

        controller._query_status = _query_status
        original_sleep = _stage_controller_module.time.sleep
        _stage_controller_module.time.sleep = lambda _seconds: None
        try:
            controller._serial = _FakeSerial()
            with controller._serial_session():
                controller._wait_for_idle_at_targets({"A": -0.010}, timeout=1.0)
        finally:
            _stage_controller_module.time.sleep = original_sleep

        self.assertEqual(len(seen), 3)

    def test_status_refresh_is_suppressed_while_jog_is_active(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        started = []
        controller._run_status_refresh = lambda: started.append(True)

        controller._jog_motion_active = True
        controller.request_status_refresh()

        self.assertEqual(started, [])

        controller._jog_motion_active = False
        controller.request_status_refresh()
        thread = controller._status_refresh_thread
        if thread is not None:
            thread.join(timeout=1.0)

        self.assertEqual(started, [True])

    def test_status_refresh_reads_idle_when_motion_cancel_flag_is_set(self) -> None:
        controller = StageController()
        controller._current_status_report_mask = (
            controller._desired_status_report_mask_for_mode("work")
        )
        serial_connection = _LineFakeSerial(
            [
                b"<Idle|WPos:0.000,0.000,3.840,2.967,0.000|Bf:15,127|FS:0,0>\n",
            ]
        )
        positions = []
        controller._serial = serial_connection
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )
        controller._cancel_event.set()

        try:
            controller._poll_status_once()
        finally:
            controller.shutdown()

        self.assertTrue(controller._cancel_event.is_set())
        self.assertEqual(controller._last_stage_state, "Idle")
        self.assertEqual(controller._last_stage_position, (0.0, 0.0, 3.84, 2.967, 0.0))
        self.assertEqual(positions, [(0.0, 0.0, 3.84, 2.967, 0.0)])
        self.assertEqual(serial_connection.writes, [b"?\n"])


class StageControllerStartupSyncTest(unittest.TestCase):
    def test_startup_sync_keeps_config_io_on_captured_serial(self) -> None:
        controller = StageController()
        replacement = _RejectingSerial()

        def swap_after_config_dump(payload: bytes) -> None:
            if payload == b"$CD\n":
                controller._serial = replacement

        original = _SwappingLineFakeSerial(
            [
                b"axes:\n",
                b"  x:\n",
                b"    max_rate_mm_per_min: 500\n",
                b"  z:\n",
                b"    max_rate_mm_per_min: 100\n",
                b"  a:\n",
                b"    max_rate_mm_per_min: 80\n",
                b"ok\n",
                b"[MSG:INFO: Axis X (0.000,64.000)]\n",
                b"[MSG:INFO: Axis Y (0.000,64.000)]\n",
                b"[MSG:INFO: Axis Z (0.000,23.000)]\n",
                b"[MSG:INFO: Axis A (-5.500,0.000)]\n",
                b"ok\n",
                b"ok\n",
                b"[GC:G1 G54 G17 G21 G90]\n",
                b"ok\n",
                b"[G54:0.000,0.000,0.000,0.000,0.000]\n",
                b"ok\n",
                b"<Idle|WPos:1.000,2.000,3.000,0.000|WCO:0.000,0.000,0.000,0.000|H:XYZA>\n",
            ],
            swap_after_config_dump,
        )
        controller._serial = original
        controller._ensure_controller_session_marker = lambda: None
        positions = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(
            original.writes,
            [b"$CD\n", b"$Startup/Show\n", b"$10=2\n", b"$G\n", b"$#\n", b"?\n"],
        )
        self.assertEqual(replacement.writes, [])
        self.assertEqual(controller._axis_max_feedrates["X"], 500.0)
        self.assertEqual(controller._axis_limits["Z"], (0.0, 23.0))
        self.assertEqual(positions[-1], (1.0, 2.0, 3.0, 0.0))

    def test_startup_sync_homes_a_when_not_reported_homed(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._refresh_coordinate_system_state = (
            lambda apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._ensure_controller_session_marker = lambda: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 0.0, 0.0),
            homed_axes={"X", "Y"},
        )
        performed = []
        controller._perform_home_command = (
            lambda command: performed.append(command)
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(performed, ["$HA"])

    def test_startup_sync_skips_a_homing_when_axis_is_homed(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._refresh_coordinate_system_state = (
            lambda apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._ensure_controller_session_marker = lambda: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 0.0, 0.0),
            homed_axes={"X", "Y", "A"},
        )
        performed = []
        controller._perform_home_command = (
            lambda command: performed.append(command)
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(performed, [])

    def test_startup_sync_uses_cached_homing_when_status_does_not_report_it(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._homed_axes = {"X", "Y", "A"}
        controller._refresh_coordinate_system_state = (
            lambda apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._ensure_controller_session_marker = lambda: None
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(1.0, 2.0, 3.0, 0.0),
            work_position=(1.0, 2.0, 3.0, 0.0),
            display_position=(1.0, 2.0, 3.0, 0.0),
            homed_axes=None,
            coordinate_system="G54",
        )
        performed = []
        controller._perform_home_command = (
            lambda command: performed.append(command)
        )
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.movement_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_started = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_action_finished = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(performed, [])

    def test_startup_sync_uses_cached_axis_feedrates_without_config_dump(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._axis_max_feedrates = {"X": 500.0, "Z": 100.0, "A": 80.0}
        controller._refresh_coordinate_system_state = (
            lambda apply_preference=True: None
        )
        controller._ensure_axis_limits = lambda **_kwargs: None
        controller._ensure_controller_session_marker = lambda: None
        controller._query_axis_max_feedrates_locked = (
            lambda: (_ for _ in ()).throw(AssertionError("$CD should be skipped"))
        )
        controller._query_status = lambda _serial: types.SimpleNamespace(
            state="Idle",
            position=(1.0, 2.0, 3.0, 0.0),
            work_position=(1.0, 2.0, 3.0, 0.0),
            display_position=(1.0, 2.0, 3.0, 0.0),
            homed_axes={"X", "Y", "Z", "A"},
            coordinate_system="G54",
        )
        emitted_feedrates = []
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.axis_max_feedrates_changed = types.SimpleNamespace(
            emit=lambda rates: emitted_feedrates.append(dict(rates))
        )
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller._run_startup_sync(auto_home_a=True)

        self.assertEqual(
            emitted_feedrates,
            [{"X": 500.0, "Z": 100.0, "A": 80.0}],
        )


class StageControllerReconnectStateTest(unittest.TestCase):
    def test_disconnect_clears_unverified_controller_state(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        controller._axis_a_ready = True
        axis_ready = []
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda ready: axis_ready.append(bool(ready))
        )

        controller.set_serial(None)

        self.assertIsNone(controller._last_stage_position)
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertFalse(controller._axis_a_ready)
        self.assertEqual(axis_ready[-1], False)

    def test_reconnect_without_reboot_clears_unverified_state_until_sync(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        fake_serial = _FakeSerial()

        controller.set_serial(fake_serial)

        self.assertIsNone(controller._last_stage_position)
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertFalse(controller._axis_a_ready)

    def test_set_serial_allows_position_signal_slot_to_query_busy(self) -> None:
        controller = StageController()
        busy_values = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda _position: busy_values.append(controller.is_busy())
        )

        controller.set_serial(_FakeSerial())

        self.assertEqual(busy_values, [False])

    def test_reconnect_with_reboot_clears_cached_state(self) -> None:
        controller = StageController()
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._homed_axes = {"X", "Y", "A"}
        controller._needles_up = True
        controller._needles_known = True
        state_changes = []
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda raised, known: state_changes.append((raised, known))
        )
        controller.homing_status_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        fake_serial = _FakeSerial()
        fake_serial.probe_station_reboot_detected = True

        controller.set_serial(fake_serial)

        self.assertIsNone(controller._last_stage_position)
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertEqual(state_changes[-1], (False, False))

    def test_import_cached_state_restores_homing_but_not_coordinates(self) -> None:
        controller = StageController()
        positions = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )
        controller.homing_status_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )

        controller.import_cached_controller_state(
            {
                "last_stage_position": [1.0, 2.0, 3.0, 0.0],
                "last_stage_state": "Idle",
                "active_work_coordinate_system": "G54",
                "controller_coordinate_offsets": {
                    "G54": [32.0, 32.0, 0.0, -2.885, 0.0],
                },
                "homed_axes": ["X", "Y", "A"],
                "needles_up": True,
                "needles_known": True,
                "controller_session_marker": 321,
                "axis_limits": {
                    "X": [0.0, 64.0],
                    "Y": [0.0, 64.0],
                    "Z": [0.0, 23.0],
                    "A": [-5.5, 0.0],
                },
                "axis_max_feedrates": {"X": 500.0, "Z": 100.0, "A": 80.0},
            }
        )

        self.assertIsNone(controller._last_stage_position)
        self.assertEqual(controller._homed_axes, {"X", "Y", "A"})
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
        self.assertEqual(controller._controller_session_marker, 321)
        self.assertTrue(controller._controller_state_stale)
        self.assertFalse(controller._axis_a_ready)
        self.assertEqual(positions, [])
        self.assertEqual(controller._axis_limits["Z"], (0.0, 23.0))
        self.assertEqual(controller._axis_max_feedrates["X"], 500.0)
        self.assertEqual(controller._active_work_coordinate_system, "G54")
        self.assertEqual(
            controller._controller_coordinate_offsets["G54"],
            (32.0, 32.0, 0.0, -2.885, 0.0),
        )

    def test_cached_session_marker_retries_after_truncated_modal_response(self) -> None:
        controller = StageController()
        serial_connection = _LineFakeSerial(
            [
                b"T600 F0 S0]\n",
                b"ok\n",
                b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T600 F0 S0]\n",
                b"ok\n",
            ]
        )
        controller._serial = serial_connection

        try:
            current = controller.cached_controller_session_is_current(
                {"controller_session_marker": 600}
            )
        finally:
            controller.shutdown()

        self.assertTrue(current)
        self.assertEqual(serial_connection.writes, [b"$G\n", b"$G\n"])

    def test_status_reader_detects_live_controller_reboot_and_clears_homing(self) -> None:
        controller = StageController()
        controller._homed_axes = {"A", "Y"}
        controller._needles_up = True
        controller._needles_known = True
        controller._controller_state_stale = False
        controller._controller_session_marker = 321
        controller._last_stage_position = (1.0, 2.0, 3.0, 0.0)
        controller._axis_limits = {"Z": (0.0, 23.0)}
        controller._axis_max_feedrates = {"Z": 100.0}
        positions = []
        reboot_events = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )
        controller.controller_reboot_detected = types.SimpleNamespace(
            emit=lambda: reboot_events.append(True)
        )
        serial_connection = _LineFakeSerial(
            [b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n"]
        )

        with self.assertRaises(StageControllerError):
            controller._read_status_frame(serial_connection, timeout=0.1)

        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_up)
        self.assertFalse(controller._needles_known)
        self.assertTrue(controller._controller_state_stale)
        self.assertIsNone(controller._controller_session_marker)
        self.assertEqual(controller._axis_limits, {})
        self.assertEqual(controller._axis_max_feedrates, {})
        self.assertEqual(positions[-1], None)
        self.assertEqual(reboot_events, [True])

    def test_repeated_live_controller_reboot_lines_emit_one_recovery_event(self) -> None:
        controller = StageController()
        reboot_events = []
        controller.controller_reboot_detected = types.SimpleNamespace(
            emit=lambda: reboot_events.append(True)
        )
        serial_connection = _LineFakeSerial(
            [
                b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n",
                b"[MSG:RST]\n",
            ]
        )

        for _ in range(2):
            with self.assertRaises(StageControllerError):
                controller._read_status_frame(serial_connection, timeout=0.1)

        self.assertEqual(reboot_events, [True])

    def test_valid_status_after_live_reboot_emits_recovery_ready_once(self) -> None:
        controller = StageController()
        controller._controller_reboot_recovery_pending = True
        controller._current_status_report_mask = 2
        ready_events = []
        controller.controller_reboot_ready = types.SimpleNamespace(
            emit=lambda: ready_events.append(True)
        )
        serial_connection = _LineFakeSerial(
            [
                b"<Idle|WPos:0.000,0.000,0.000,0.000,0.000|Bf:15,127|FS:0,0>\n",
                b"<Idle|WPos:0.000,0.000,0.000,0.000,0.000|Bf:15,127|FS:0,0>\n",
            ]
        )

        self.assertIsNotNone(controller._query_status(serial_connection))
        self.assertIsNotNone(controller._query_status(serial_connection))

        self.assertEqual(ready_events, [True])

    def test_terminal_pending_read_detects_live_controller_reboot(self) -> None:
        controller = StageController()
        serial_connection = _BufferedFakeSerial(
            b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
        )
        controller._serial = serial_connection
        controller._homed_axes = {"A"}
        controller._needles_up = True
        controller._needles_known = True
        controller._controller_state_stale = False
        controller._controller_session_marker = 321
        positions = []
        controller.stage_position_changed = types.SimpleNamespace(
            emit=lambda position: positions.append(position)
        )

        data = controller.read_pending_serial_output()

        self.assertIn(b"POWERON_RESET", data)
        self.assertEqual(controller._homed_axes, set())
        self.assertFalse(controller._needles_known)
        self.assertIsNone(controller._controller_session_marker)
        self.assertEqual(positions[-1], None)

    def test_perform_home_command_marks_a_ready_and_updates_homing(self) -> None:
        controller = StageController()
        controller._serial = _FakeSerial()
        controller._controller_state_stale = True
        controller.status_message = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.axis_a_ready_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.needles_state_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller.homing_status_changed = types.SimpleNamespace(
            emit=lambda *_args, **_kwargs: None
        )
        controller._write_command = lambda *_args, **_kwargs: None
        controller._wait_for_ok = lambda *_args, **_kwargs: None
        controller._wait_for_idle = lambda *_args, **_kwargs: None

        controller.set_serial(_FakeSerial())
        controller._perform_home_command("$HA")

        self.assertIn("A", controller._homed_axes)
        self.assertFalse(controller._controller_state_stale)
        self.assertTrue(controller._needles_up)
        self.assertTrue(controller._needles_known)
        self.assertTrue(controller._axis_a_ready)
        self.assertIsNotNone(controller._controller_session_marker)

if __name__ == "__main__":
    unittest.main()
