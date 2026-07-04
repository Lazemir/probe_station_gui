import math
import types
import unittest
from pathlib import Path

import numpy as np

try:
    from .controller_test_support import (
        AxisACalibrationSettings,
        AxisZCalibrationSettings,
        MoveVector,
        StageController,
        _FakeSerial,
        _LineFakeSerial,
        _WritableFakeSerial,
    )
except ImportError:
    from controller_test_support import (
        AxisACalibrationSettings,
        AxisZCalibrationSettings,
        MoveVector,
        StageController,
        _FakeSerial,
        _LineFakeSerial,
        _WritableFakeSerial,
    )

class StageControllerAxisACalibrationTest(unittest.TestCase):
    def test_axis_a_calibration_maps_physical_lowering_to_absolute_gcode(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )

            target_a = controller.axis_a_gcode_coordinate_for_lowering(0.02)

            self.assertLess(target_a, 0.0)
            self.assertAlmostEqual(target_a, -0.0243676184, places=6)
            self.assertAlmostEqual(
                controller.axis_a_lowering_for_gcode_coordinate(0.0),
                0.0,
                places=6,
            )
        finally:
            controller.shutdown()

    def test_axis_a_calibration_falls_back_when_disabled(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(configured=False)
            )

            target_a = controller.axis_a_gcode_coordinate_for_lowering(0.02)
            lowering = controller.axis_a_lowering_for_gcode_coordinate(-1.0)

            self.assertEqual(target_a, -0.02)
            self.assertEqual(lowering, 1.0)
        finally:
            controller.shutdown()

    def test_needle_adjust_sends_absolute_calibrated_a_target(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller._position_reporting_mode = "machine"
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                display_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )
            targets = []
            controller._send_absolute_axis_move = (
                lambda axis, value, **_kwargs: targets.append((axis, value))
            )
            controller._read_current_a_position = lambda: targets[-1][1]
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

            controller._run_needles_adjust(-0.02)

            self.assertEqual(targets[0][0], "A")
            self.assertAlmostEqual(targets[0][1], -0.0243676184, places=6)
        finally:
            controller.shutdown()

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

    def test_manual_axis_a_relative_move_keeps_raw_gcode_sign(self) -> None:
        controller = StageController()
        try:
            controller._serial = _FakeSerial()
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )
            controller._query_status = lambda _serial: types.SimpleNamespace(
                state="Idle",
                position=(0.0, 0.0, 0.0, 0.0),
                work_position=(0.0, 0.0, 0.0, 0.0),
                display_position=(0.0, 0.0, 0.0, 0.0),
                homed_axes={"A"},
            )

            target = controller._manual_axis_absolute_target(
                "A",
                -0.02,
                "G91",
            )

            self.assertEqual(target, -0.02)
        finally:
            controller.shutdown()

    def test_calibrated_axis_a_display_keeps_gcode_sign(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )

            display = controller.calibrated_axis_display_value("A", -1.0)
            raw = controller.calibrated_axis_raw_value("A", display)

            self.assertLess(display, 0.0)
            self.assertAlmostEqual(raw, -1.0, places=6)
        finally:
            controller.shutdown()

    def test_calibrated_axis_a_zero_display_is_positive_zero(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                AxisACalibrationSettings(configured=True)
            )

            display = controller.calibrated_axis_display_value("A", 0.0)

            self.assertEqual(display, 0.0)
            self.assertEqual(math.copysign(1.0, display), 1.0)
        finally:
            controller.shutdown()

    def test_legacy_negative_saved_a_position_is_treated_as_raw_coordinate(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                types.SimpleNamespace(
                    configured=True,
                    model="cosine_displacement",
                    steps_per_mm=2500.0,
                    commanded_lowering_min_mm=0.0,
                    commanded_lowering_max_mm=6.0,
                    offset_mm=-0.00013272701600556085,
                    amplitude_mm=4.29496757977153,
                    angular_frequency_rad_per_mm=0.24349261926759336,
                    phase_rad=0.8994441869661569,
                )
            )

            controller.apply_needle_calibration(down_position_mm=-1.0)

            self.assertGreater(controller._needle_down_lowering_mm, 0.0)
            self.assertAlmostEqual(
                controller.axis_a_gcode_coordinate_for_lowering(
                    controller._needle_down_lowering_mm
                ),
                -1.0,
                places=6,
            )
        finally:
            controller.shutdown()


class StageControllerAxisMotionFitTest(unittest.TestCase):
    CALIBRATIONS = Path(__file__).resolve().parents[2] / "calibrations"

    @staticmethod
    def _rmse(values: np.ndarray) -> float:
        return float(np.sqrt(np.mean(values * values)))

    @staticmethod
    def _averaged_curve(gcode: np.ndarray, indicator: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        buckets: dict[float, list[float]] = {}
        for gcode_value, indicator_value in zip(np.round(gcode, 4), indicator):
            buckets.setdefault(float(gcode_value), []).append(float(indicator_value))
        keys = np.array(sorted(buckets), dtype=float)
        values = np.array([np.mean(buckets[float(key)]) for key in keys], dtype=float)
        return keys, values

    def test_default_axis_a_sine_fit_matches_measured_curves(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                AxisACalibrationSettings(configured=True)
            )
            datasets = [
                "axis_a_spm2600_pulloff0p25_start0p230_to-lowerlimit_step0p01_settle1p0_feed30_oneshot_20260504_223257.npz",
                "axis_a_spm2600_pulloff0p25_reverse_startm5p730_to0p230_step0p01_settle1p0_feed30_oneshot_nozero_20260504_225516.npz",
            ]
            residuals: list[np.ndarray] = []
            for filename in datasets:
                data = np.load(self.CALIBRATIONS / filename)
                commanded = -data["gcode"]
                predicted = np.array(
                    [
                        controller._axis_a_model_lowering_for_commanded(float(value))
                        for value in commanded
                    ],
                    dtype=float,
                )
                residuals.append(predicted - data["indicator"])
            residual = np.concatenate(residuals)

            self.assertLess(self._rmse(residual), 0.035)
            self.assertLess(float(np.percentile(np.abs(residual), 95)), 0.043)
            self.assertLess(float(np.max(np.abs(residual))), 0.05)
        finally:
            controller.shutdown()

    def test_default_axis_z_polynomial_fit_matches_stitched_center_curve(self) -> None:
        controller = StageController()
        settings = AxisZCalibrationSettings(configured=True)
        try:
            controller.apply_axis_z_calibration(settings)
            up_gcode, up_indicator, down_gcode, down_indicator = (
                self._stitched_z_indicator_curves(settings)
            )
            mask = (up_gcode >= 0.05) & (up_gcode <= 23.35)
            gcode = up_gcode[mask]
            center = (
                up_indicator[mask]
                + np.interp(gcode, down_gcode, down_indicator)
            ) * 0.5
            predicted = np.array(
                [
                    controller.calibrated_axis_display_value("Z", float(value))
                    for value in gcode
                ],
                dtype=float,
            )
            residual = predicted - center

            self.assertLess(self._rmse(residual), 0.007)
            self.assertLess(float(np.percentile(np.abs(residual), 95)), 0.013)
            self.assertLess(float(np.max(np.abs(residual))), 0.022)
        finally:
            controller.shutdown()

    def test_calibrated_axis_targets_round_trip(self) -> None:
        controller = StageController()
        try:
            controller.apply_axis_a_calibration(
                AxisACalibrationSettings(configured=True)
            )
            controller.apply_axis_z_calibration(
                AxisZCalibrationSettings(configured=True)
            )

            a_raw = controller.calibrated_axis_raw_value("A", -1.25)
            self.assertLess(a_raw, 0.0)
            self.assertAlmostEqual(
                controller.calibrated_axis_display_value("A", a_raw),
                -1.25,
                places=6,
            )

            z_display = controller.calibrated_axis_display_value(
                "Z",
                18.0,
            )
            z_raw = controller.calibrated_axis_raw_value(
                "Z",
                z_display,
            )
            self.assertAlmostEqual(z_raw, 18.0, places=6)
        finally:
            controller.shutdown()

    def _stitched_z_indicator_curves(
        self,
        settings: AxisZCalibrationSettings,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        s1_up = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_nozero_up_from0p020_until-ind9p95_step0p01_settle1p0_feed50_20260504_233107.npz"
        )
        s1_down = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_nozero_down_to0p020_from-up-end_step0p01_settle1p0_feed50_20260504_233107.npz"
        )
        s2_up = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_section2_nozero_up_step0p005_settle2p0_feed50_20260505_010155.npz"
        )
        s2_down = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_section2_nozero_down_step0p005_settle2p0_feed50_20260505_010155.npz"
        )
        s3 = np.load(
            self.CALIBRATIONS
            / "axis_z_spm6335_section3_precise_start16p5_top23p4_step0p0025_settle2p0_feed1_transition10_20260505_175825.npz"
        )

        s1_up_g, s1_up_i = self._averaged_curve(s1_up["gcode"], s1_up["indicator"])
        s1_down_g, s1_down_i = self._averaged_curve(
            s1_down["gcode"],
            s1_down["indicator"],
        )
        s2_up_g, s2_up_i = self._averaged_curve(s2_up["gcode"], s2_up["indicator"])
        s2_down_g, s2_down_i = self._averaged_curve(
            s2_down["gcode"],
            s2_down["indicator"],
        )
        s2_up_i = s2_up_i + settings.section2_indicator_offset_mm
        s2_down_i = s2_down_i + settings.section2_indicator_offset_mm

        s3_up_mask = s3["direction"] > 0
        s3_down_mask = s3["direction"] < 0
        s3_up_g, s3_up_i = self._averaged_curve(
            s3["gcode"][s3_up_mask],
            s3["indicator"][s3_up_mask],
        )
        s3_down_g, s3_down_i = self._averaged_curve(
            s3["gcode"][s3_down_mask],
            s3["indicator"][s3_down_mask],
        )
        s3_up_i = s3_up_i + settings.section3_indicator_offset_mm
        s3_down_i = s3_down_i + settings.section3_indicator_offset_mm

        up_gcode = np.concatenate(
            [
                s1_up_g[s1_up_g < 12.0],
                s2_up_g[(s2_up_g >= 12.0) & (s2_up_g <= 20.214)],
                s3_up_g[s3_up_g > 20.214],
            ]
        )
        up_indicator = np.concatenate(
            [
                s1_up_i[s1_up_g < 12.0],
                s2_up_i[(s2_up_g >= 12.0) & (s2_up_g <= 20.214)],
                s3_up_i[s3_up_g > 20.214],
            ]
        )
        down_gcode = np.concatenate(
            [
                s1_down_g[s1_down_g < 12.0],
                s2_down_g[(s2_down_g >= 12.0) & (s2_down_g <= 20.214)],
                s3_down_g[s3_down_g > 20.214],
            ]
        )
        down_indicator = np.concatenate(
            [
                s1_down_i[s1_down_g < 12.0],
                s2_down_i[(s2_down_g >= 12.0) & (s2_down_g <= 20.214)],
                s3_down_i[s3_down_g > 20.214],
            ]
        )
        order = np.argsort(down_gcode)
        return up_gcode, up_indicator, down_gcode[order], down_indicator[order]


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
            [b"\x90", b"$J=G91 G21 X0.5000 Z-0.1000 F12.3\n"],
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

if __name__ == "__main__":
    unittest.main()
