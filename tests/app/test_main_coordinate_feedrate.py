import csv
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from main_coordinate_feedrate_support import (
    LCRMeterError,
    Main,
    ObjectiveCalibrationSettings,
    RouteContactHeightRecord,
    RouteContactPlacementResult,
    RouteContactQuality,
    RouteContactSeekResult,
    RouteMeasurementDialog,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RouteMeasurementRunner,
    RouteMeterConfiguration,
    RoutePhotoRecord,
    Settings,
    StageControllerError,
    _FakeAliveThread,
    _FakeButton,
    _FakeFrame,
    _FakeRouteMeasurementRunner,
    _FakeThread,
    _telegram_test_photo_bytes,
    main_module,
    request_route_measurement_for_point,
    route_measurement_dialog_module,
)

class MainCoordinateFeedrateTest(unittest.TestCase):
    def test_api_keithley_meter_configuration_accepts_code_auto_ranges(self) -> None:
        window = Main.__new__(Main)
        window.lcr_controller = types.SimpleNamespace(
            meter_type=lambda: main_module.ROUTE_METER_KEITHLEY
        )

        config = Main._api_route_meter_configuration(
            window,
            {
                "meter_type": "keithley",
                "measurement_voltage_v": 0.03,
                "ranges": {
                    "mode": "code_auto",
                    "expected_resistance_ohm": 100_000.0,
                    "max_current_a": 10e-6,
                },
                "nplc": 5,
            },
            voltages_v=None,
        )

        self.assertEqual(config.meter_type, main_module.ROUTE_METER_KEITHLEY)
        self.assertEqual(config.keithley.range_mode, "code_auto")
        self.assertEqual(config.keithley.measurement_voltage_v, 0.03)
        self.assertEqual(config.keithley.expected_resistance_ohm, 100_000.0)
        self.assertEqual(config.keithley.maximum_current_a, 10e-6)
        self.assertEqual(config.keithley.nplc, 5)

    def test_api_keithley_voltage_range_defaults_to_raw_sweep_span(self) -> None:
        window = Main.__new__(Main)
        window.lcr_controller = types.SimpleNamespace(
            meter_type=lambda: main_module.ROUTE_METER_KEITHLEY
        )

        config = Main._api_route_meter_configuration(
            window,
            {
                "meter_type": "keithley",
                "nplc": 5,
            },
            voltages_v=[-0.3, 0.1, 0.25],
        )

        self.assertEqual(config.keithley.measurement_voltage_v, 0.3)
        self.assertEqual(config.keithley.voltage_range_v, 0.3)
        self.assertEqual(config.keithley.source_voltage_range_v, 0.3)
        self.assertEqual(config.keithley.voltmeter_range_v, 0.3)

    def test_api_configure_meter_reports_unexpected_instrument_error(self) -> None:
        class _FailingLcr:
            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                _configuration: RouteMeterConfiguration,
            ) -> None:
                raise RuntimeError(
                    "VI_ERROR_TMO (-1073807339): Timeout expired before operation completed."
                )

        window = Main.__new__(Main)
        window.lcr_controller = _FailingLcr()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )

        response = Main._api_configure_meter(window, {})

        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["error_type"], "RuntimeError")
        self.assertIn("Measurement instrument setup failed", response["message"])
        self.assertIn("VI_ERROR_TMO", response["message"])

    def test_api_prepare_route_meter_connects_shared_lcr_controller(self) -> None:
        class _Lcr:
            def __init__(self) -> None:
                self.connected = False
                self.runtime_configurations: list[RouteMeterConfiguration] = []
                self.applied_configurations: list[RouteMeterConfiguration] = []
                self.connect_count = 0

            def is_connected(self) -> bool:
                return self.connected

            def apply_route_meter_runtime_configuration(
                self,
                configuration: RouteMeterConfiguration,
            ) -> None:
                self.runtime_configurations.append(configuration)

            def connect_now(self) -> None:
                self.connect_count += 1
                self.connected = True

            def apply_route_meter_configuration(
                self,
                configuration: RouteMeterConfiguration,
            ) -> None:
                self.applied_configurations.append(configuration)

        configuration = RouteMeterConfiguration()
        lcr = _Lcr()
        window = Main.__new__(Main)
        window.lcr_controller = lcr

        response = Main._api_prepare_route_meter_controller(window, configuration)

        self.assertIsNone(response)
        self.assertEqual(lcr.runtime_configurations, [configuration])
        self.assertEqual(lcr.connect_count, 1)
        self.assertEqual(lcr.applied_configurations, [configuration])

    def test_api_prepare_route_meter_reports_busy_instrument_task(self) -> None:
        class _Lcr:
            def __init__(self) -> None:
                self.wait_calls: list[float] = []
                self.applied_configurations: list[RouteMeterConfiguration] = []

            def wait_until_idle(self, timeout_s: float) -> bool:
                self.wait_calls.append(float(timeout_s))
                return False

            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                configuration: RouteMeterConfiguration,
            ) -> None:
                self.applied_configurations.append(configuration)

        configuration = RouteMeterConfiguration()
        lcr = _Lcr()
        window = Main.__new__(Main)
        window.lcr_controller = lcr

        response = Main._api_prepare_route_meter_controller(window, configuration)

        self.assertIsNotNone(response)
        assert response is not None
        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["message"], "Measurement instrument task is still running.")
        self.assertEqual(lcr.wait_calls, [45.0])
        self.assertEqual(lcr.applied_configurations, [])

    def test_resistance_standby_toggle_reflects_controller_state(self) -> None:
        class _Lcr:
            def __init__(self) -> None:
                self.requests: list[bool] = []

            def set_live_polling_enabled(self, enabled: bool) -> None:
                self.requests.append(bool(enabled))

            def live_polling_enabled(self) -> bool:
                return False

        class _Panel:
            def __init__(self) -> None:
                self.states: list[bool] = []

            def set_standby_enabled(self, enabled: bool) -> None:
                self.states.append(bool(enabled))

        lcr = _Lcr()
        panel = _Panel()
        window = Main.__new__(Main)
        window.lcr_controller = lcr
        window.resistance_panel = panel

        Main._on_resistance_standby_enabled_changed(window, True)

        self.assertEqual(lcr.requests, [True])
        self.assertEqual(panel.states, [False])

    def test_meter_auto_connect_uses_shared_lcr_controller(self) -> None:
        class _SettingsManager:
            def serial_auto_connect_enabled(self) -> bool:
                return False

            def meter_auto_connect_enabled(self) -> bool:
                return True

        class _Lcr:
            def __init__(self) -> None:
                self.request_count = 0

            def is_connected(self) -> bool:
                return False

            def request_connect(self) -> None:
                self.request_count += 1

        lcr = _Lcr()
        window = Main.__new__(Main)
        window.serial_connection_panel = None
        window.serial_connection = None
        window.settings_manager = _SettingsManager()
        window.lcr_controller = lcr

        Main._auto_connect_if_possible(window)

        self.assertEqual(lcr.request_count, 1)

    def test_api_raw_voltage_sweep_reports_unexpected_instrument_error(self) -> None:
        class _FailingLcr:
            def is_connected(self) -> bool:
                return True

            def apply_route_meter_configuration(
                self,
                _configuration: RouteMeterConfiguration,
            ) -> None:
                pass

            def read_voltage_sweep_now(self, _voltages_v: list[float]) -> dict[str, object]:
                raise RuntimeError(
                    "VI_ERROR_TMO (-1073807339): Timeout expired before operation completed."
                )

        window = Main.__new__(Main)
        window.lcr_controller = _FailingLcr()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window._api_contact_number = lambda _payload, required=False: None
        window._api_bool = lambda _payload, *names, default=False: default
        window._api_float = (
            lambda _payload, *names, default=0.0, minimum=None: default
        )
        window._api_needle_feedrate = lambda _payload: None
        window._api_timestamp_utc = lambda: "2026-06-06T12:00:00+00:00"

        response = Main._api_raw_voltage_sweep(window, {"voltages_v": [0.0]})

        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["error_type"], "RuntimeError")
        self.assertIn("Raw voltage sweep failed", response["message"])
        self.assertIn("VI_ERROR_TMO", response["message"])
        self.assertIsNone(response["contact"])

    def test_api_raw_voltage_sweep_returns_meter_setup_rejection_before_stage_side_effects(
        self,
    ) -> None:
        calls: list[tuple[object, ...]] = []
        setup_error = {
            "accepted": False,
            "status_code": 409,
            "message": "Measurement instrument setup failed: bad VISA session.",
        }
        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.lcr_controller = types.SimpleNamespace()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window._api_prepare_route_meter_controller = (
            lambda _configuration, prefix="": setup_error
        )

        response = Main._api_raw_voltage_sweep(window, {"voltages_v": [0.0]})

        self.assertEqual(response, setup_error)
        self.assertEqual(calls, [])

    def test_api_raw_voltage_sweep_returns_contact_context_rejection_unchanged(self) -> None:
        context_error = {
            "accepted": False,
            "status_code": 409,
            "message": "Route registration is not valid.",
        }
        calls: list[tuple[object, ...]] = []
        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.lcr_controller = types.SimpleNamespace()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window._api_prepare_route_meter_controller = (
            lambda _configuration, prefix="": None
        )
        window._api_contact_context = lambda _contact_number: context_error

        response = Main._api_raw_voltage_sweep(
            window,
            {
                "voltages_v": [0.0],
                "contact_number": 7,
            },
        )

        self.assertEqual(response, context_error)
        self.assertEqual(calls, [])

    def test_api_raw_voltage_sweep_contact_stage_side_effect_order(self) -> None:
        calls: list[tuple[object, ...]] = []
        point = object()
        contact = {"contact_number": 7, "label": "Pad 7"}

        class _Lcr:
            def read_voltage_sweep_now(self, voltages_v: list[float]) -> dict[str, object]:
                calls.append(("read", tuple(voltages_v)))
                return {
                    "points": [
                        {"measured_voltage_v": 0.01, "current_a": 2.0e-6},
                    ]
                }

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.lcr_controller = _Lcr()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window._api_prepare_route_meter_controller = (
            lambda _configuration, prefix="": None
        )
        window._api_contact_context = lambda _contact_number: {
            "accepted": True,
            "point": point,
            "contact": contact,
        }
        window._api_route_adjusted_stage_xy = lambda selected_point: (
            1.25,
            2.5,
        ) if selected_point is point else (0.0, 0.0)
        window._api_needle_feedrate = lambda _payload: 75.0
        window._api_timestamp_utc = lambda: "2026-06-26T12:00:00+00:00"
        window._api_json_ready = lambda result: result
        monotonic_values = iter([100.0, 101.25])
        original_monotonic = main_module.time.monotonic
        original_sleep = main_module.time.sleep
        main_module.time.monotonic = lambda: next(monotonic_values)
        main_module.time.sleep = lambda delay: calls.append(("sleep", delay))
        try:
            response = Main._api_raw_voltage_sweep(
                window,
                {
                    "voltages_v": [0.0, "0.1"],
                    "contact_number": 7,
                },
            )
        finally:
            main_module.time.monotonic = original_monotonic
            main_module.time.sleep = original_sleep

        self.assertTrue(response["accepted"], response)
        self.assertEqual(response["contact"], contact)
        self.assertEqual(response["voltages_v"], [0.0, 0.1])
        self.assertEqual(response["iv_pairs"], [{"voltage_v": 0.01, "current_a": 2.0e-6}])
        self.assertEqual(
            calls,
            [
                ("begin", "API raw voltage sweep"),
                ("needles", "lift", 75.0),
                ("move", 1.25, 2.5),
                ("needles", "lower", 75.0),
                ("sleep", 0.2),
                ("read", (0.0, 0.1)),
                ("needles", "lift", 75.0),
                ("finish",),
            ],
        )

    def test_api_raw_voltage_sweep_with_lift_after_false_leaves_needles_down(self) -> None:
        calls: list[tuple[object, ...]] = []

        class _Lcr:
            def read_voltage_sweep_now(self, voltages_v: list[float]) -> dict[str, object]:
                calls.append(("read", tuple(voltages_v)))
                return {"points": []}

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.lcr_controller = _Lcr()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window._api_prepare_route_meter_controller = (
            lambda _configuration, prefix="": None
        )
        window._api_needle_feedrate = lambda _payload: 55.0
        window._api_timestamp_utc = lambda: "2026-06-26T12:00:00+00:00"
        window._api_json_ready = lambda result: result
        monotonic_values = iter([200.0, 200.4])
        original_monotonic = main_module.time.monotonic
        main_module.time.monotonic = lambda: next(monotonic_values)
        try:
            response = Main._api_raw_voltage_sweep(
                window,
                {
                    "voltages_v": [0.0],
                    "lower_needles": True,
                    "lift_after": False,
                    "contact_settle_s": 0.0,
                },
            )
        finally:
            main_module.time.monotonic = original_monotonic

        self.assertTrue(response["accepted"], response)
        self.assertTrue(response["needles_lowered"])
        self.assertFalse(response["lifted_after"])
        self.assertEqual(
            calls,
            [
                ("begin", "API raw voltage sweep"),
                ("needles", "lower", 55.0),
                ("read", (0.0,)),
                ("finish",),
            ],
        )

    def test_api_raw_voltage_sweep_final_lift_stage_error_is_logged_and_finishes_task(
        self,
    ) -> None:
        calls: list[tuple[object, ...]] = []
        logged: list[str] = []

        class _Lcr:
            def read_voltage_sweep_now(self, voltages_v: list[float]) -> dict[str, object]:
                calls.append(("read", tuple(voltages_v)))
                return {"points": []}

        def run_needles_action(action: str, feedrate: float | None) -> None:
            calls.append(("needles", action, feedrate))
            if action == "lift":
                raise StageControllerError("lift failed")

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=run_needles_action,
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.lcr_controller = _Lcr()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window._api_prepare_route_meter_controller = (
            lambda _configuration, prefix="": None
        )
        window._api_needle_feedrate = lambda _payload: 55.0
        window._api_timestamp_utc = lambda: "2026-06-26T12:00:00+00:00"
        window._api_json_ready = lambda result: result
        monotonic_values = iter([300.0, 300.4])
        original_monotonic = main_module.time.monotonic
        original_logger_exception = main_module.logger.exception
        main_module.time.monotonic = lambda: next(monotonic_values)
        main_module.logger.exception = lambda message, *args: logged.append(
            str(message) % args if args else str(message)
        )
        try:
            response = Main._api_raw_voltage_sweep(
                window,
                {
                    "voltages_v": [0.0],
                    "lower_needles": True,
                    "lift_after": True,
                    "lift_before_move": False,
                    "contact_settle_s": 0.0,
                },
            )
        finally:
            main_module.time.monotonic = original_monotonic
            main_module.logger.exception = original_logger_exception

        self.assertTrue(response["accepted"], response)
        self.assertEqual(
            calls,
            [
                ("begin", "API raw voltage sweep"),
                ("needles", "lower", 55.0),
                ("read", (0.0,)),
                ("needles", "lift", 55.0),
                ("finish",),
            ],
        )
        self.assertEqual(
            logged,
            ["API raw voltage sweep failed to lift needles."],
        )

    def test_api_raw_voltage_sweep_final_lift_lcr_error_is_not_swallowed(self) -> None:
        class _Lcr:
            def read_voltage_sweep_now(self, _voltages_v: list[float]) -> dict[str, object]:
                return {"points": []}

        def run_needles_action(action: str, _feedrate: float | None) -> None:
            if action == "lift":
                raise LCRMeterError("unexpected lift meter error")

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda _label: None,
            run_external_needles_action=run_needles_action,
            run_external_move_to_xy=lambda _x_mm, _y_mm: None,
            finish_external_task=lambda: None,
        )
        window.lcr_controller = _Lcr()
        window._api_route_meter_configuration = (
            lambda _payload, voltages_v=None: RouteMeterConfiguration()
        )
        window._api_prepare_route_meter_controller = (
            lambda _configuration, prefix="": None
        )
        window._api_needle_feedrate = lambda _payload: 55.0
        window._api_timestamp_utc = lambda: "2026-06-26T12:00:00+00:00"
        window._api_json_ready = lambda result: result
        monotonic_values = iter([400.0, 400.4])
        original_monotonic = main_module.time.monotonic
        main_module.time.monotonic = lambda: next(monotonic_values)
        try:
            with self.assertRaisesRegex(LCRMeterError, "unexpected lift meter error"):
                Main._api_raw_voltage_sweep(
                    window,
                    {
                        "voltages_v": [0.0],
                        "lower_needles": True,
                        "lift_after": True,
                        "lift_before_move": False,
                        "contact_settle_s": 0.0,
                    },
                )
        finally:
            main_module.time.monotonic = original_monotonic

    def test_api_move_to_contact_stage_side_effect_order(self) -> None:
        calls: list[tuple[object, ...]] = []
        point = object()
        contact = {"contact_number": 7, "label": "Pad 7"}

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window._api_contact_context = lambda _contact_number: {
            "accepted": True,
            "point": point,
            "contact": contact,
        }
        window._api_route_adjusted_stage_xy = lambda selected_point: (
            1.25,
            2.5,
        ) if selected_point is point else (0.0, 0.0)
        window._api_needle_feedrate = lambda _payload: 75.0
        window._api_timestamp_utc = lambda: "2026-06-26T12:10:00+00:00"
        window._api_route_offset_xy = (0.5, -0.25)
        original_sleep = main_module.time.sleep
        main_module.time.sleep = lambda delay: calls.append(("sleep", delay))
        try:
            response = Main._api_move_to_contact(
                window,
                {
                    "contact_number": 7,
                    "lower_needles": True,
                    "lift_after": True,
                },
            )
        finally:
            main_module.time.sleep = original_sleep

        self.assertTrue(response["accepted"], response)
        self.assertEqual(response["contact"], contact)
        self.assertTrue(response["needles_lowered"])
        self.assertTrue(response["lifted_before_move"])
        self.assertTrue(response["lifted_after"])
        self.assertEqual(
            response["route_offset_xy"],
            {"dx_mm": 0.5, "dy_mm": -0.25},
        )
        self.assertEqual(
            response["target_stage_xy"],
            {"x_mm": 1.25, "y_mm": 2.5},
        )
        self.assertEqual(
            calls,
            [
                ("begin", "API contact move"),
                ("needles", "lift", 75.0),
                ("move", 1.25, 2.5),
                ("needles", "lower", 75.0),
                ("sleep", 0.2),
                ("needles", "lift", 75.0),
                ("finish",),
            ],
        )

    def test_api_move_to_contact_missing_contact_returns_400_before_default_feedrate(
        self,
    ) -> None:
        window = Main.__new__(Main)

        response = Main._api_move_to_contact(window, {})

        self.assertEqual(
            response,
            {
                "accepted": False,
                "status_code": 400,
                "message": "Provide a positive contact_number.",
            },
        )

    def test_api_move_to_contact_context_rejection_returns_before_default_feedrate(
        self,
    ) -> None:
        rejection = {
            "accepted": False,
            "status_code": 409,
            "message": "Design registration is required before using contacts.",
        }
        window = Main.__new__(Main)
        window._api_contact_context = lambda _contact_number: rejection

        response = Main._api_move_to_contact(window, {"contact_number": 7})

        self.assertIs(response, rejection)

    def test_api_move_to_contact_with_lift_after_false_leaves_needles_down(self) -> None:
        calls: list[tuple[object, ...]] = []
        point = object()

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window._api_contact_context = lambda _contact_number: {
            "accepted": True,
            "point": point,
            "contact": {"contact_number": 7},
        }
        window._api_route_adjusted_stage_xy = lambda _selected_point: (3.0, 4.0)
        window._api_needle_feedrate = lambda _payload: 55.0
        window._api_timestamp_utc = lambda: "2026-06-26T12:15:00+00:00"
        window._api_route_offset_xy = (0.0, 0.0)
        original_sleep = main_module.time.sleep
        main_module.time.sleep = lambda delay: calls.append(("sleep", delay))
        try:
            response = Main._api_move_to_contact(
                window,
                {
                    "contact_number": 7,
                    "lower_needles": True,
                    "lift_after": False,
                    "contact_settle_s": 0.0,
                },
            )
        finally:
            main_module.time.sleep = original_sleep

        self.assertTrue(response["accepted"], response)
        self.assertTrue(response["needles_lowered"])
        self.assertFalse(response["lifted_after"])
        self.assertEqual(
            calls,
            [
                ("begin", "API contact move"),
                ("needles", "lift", 55.0),
                ("move", 3.0, 4.0),
                ("needles", "lower", 55.0),
                ("finish",),
            ],
        )

    def test_api_move_to_contact_stage_error_keeps_contact_and_finishes_task(self) -> None:
        calls: list[tuple[object, ...]] = []
        point = object()
        contact = {"contact_number": 7, "label": "Pad 7"}

        def run_move(_x_mm: float, _y_mm: float) -> None:
            calls.append(("move", 1.25, 2.5))
            raise StageControllerError("Stage is busy.")

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=run_move,
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window._api_contact_context = lambda _contact_number: {
            "accepted": True,
            "point": point,
            "contact": contact,
        }
        window._api_route_adjusted_stage_xy = lambda _selected_point: (1.25, 2.5)
        window._api_needle_feedrate = lambda _payload: 40.0
        window._api_route_offset_xy = (0.0, 0.0)

        response = Main._api_move_to_contact(window, {"contact_number": 7})

        self.assertFalse(response["accepted"], response)
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(response["message"], "Stage is busy.")
        self.assertEqual(response["contact"], contact)
        self.assertEqual(
            calls,
            [
                ("begin", "API contact move"),
                ("needles", "lift", 40.0),
                ("move", 1.25, 2.5),
                ("finish",),
            ],
        )

    def test_api_move_to_contact_final_lift_stage_error_is_logged_and_finishes_task(
        self,
    ) -> None:
        calls: list[tuple[object, ...]] = []
        logged: list[str] = []
        point = object()

        def run_needles_action(action: str, feedrate: float | None) -> None:
            calls.append(("needles", action, feedrate))
            if action == "lift":
                raise StageControllerError("lift failed")

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=run_needles_action,
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window._api_contact_context = lambda _contact_number: {
            "accepted": True,
            "point": point,
            "contact": {"contact_number": 7},
        }
        window._api_route_adjusted_stage_xy = lambda _selected_point: (1.0, 2.0)
        window._api_needle_feedrate = lambda _payload: 55.0
        window._api_timestamp_utc = lambda: "2026-06-26T12:20:00+00:00"
        window._api_route_offset_xy = (0.0, 0.0)
        original_logger_exception = main_module.logger.exception
        main_module.logger.exception = lambda message, *args: logged.append(
            str(message) % args if args else str(message)
        )
        try:
            response = Main._api_move_to_contact(
                window,
                {
                    "contact_number": 7,
                    "lower_needles": True,
                    "lift_after": True,
                    "lift_before_move": False,
                    "contact_settle_s": 0.0,
                },
            )
        finally:
            main_module.logger.exception = original_logger_exception

        self.assertTrue(response["accepted"], response)
        self.assertEqual(
            calls,
            [
                ("begin", "API contact move"),
                ("move", 1.0, 2.0),
                ("needles", "lower", 55.0),
                ("needles", "lift", 55.0),
                ("finish",),
            ],
        )
        self.assertEqual(logged, ["API contact move failed to lift needles."])

    def test_api_move_to_contact_final_lift_non_stage_error_is_not_swallowed(
        self,
    ) -> None:
        point = object()

        def run_needles_action(action: str, _feedrate: float | None) -> None:
            if action == "lift":
                raise RuntimeError("unexpected final lift error")

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda _label: None,
            run_external_needles_action=run_needles_action,
            run_external_move_to_xy=lambda _x_mm, _y_mm: None,
            finish_external_task=lambda: None,
        )
        window._api_contact_context = lambda _contact_number: {
            "accepted": True,
            "point": point,
            "contact": {"contact_number": 7},
        }
        window._api_route_adjusted_stage_xy = lambda _selected_point: (1.0, 2.0)
        window._api_needle_feedrate = lambda _payload: 55.0
        window._api_route_offset_xy = (0.0, 0.0)

        with self.assertRaisesRegex(RuntimeError, "unexpected final lift error"):
            Main._api_move_to_contact(
                window,
                {
                    "contact_number": 7,
                    "lower_needles": True,
                    "lift_after": True,
                    "lift_before_move": False,
                    "contact_settle_s": 0.0,
                },
            )

    def test_api_contact_needles_stage_side_effect_order_and_response(self) -> None:
        calls: list[tuple[object, ...]] = []
        contact = {"contact_number": 8, "label": "Pad 8"}

        window = Main.__new__(Main)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window._api_contact_context = lambda _contact_number: {
            "accepted": True,
            "point": object(),
            "contact": contact,
        }
        window._api_needle_feedrate = lambda _payload: 12.0
        window._api_timestamp_utc = lambda: "2026-06-26T12:25:00+00:00"

        response = Main._api_contact_needles(
            window,
            {
                "contact_number": 8,
                "action": "up",
                "feedrate": 0.2,
            },
        )

        self.assertTrue(response["accepted"], response)
        self.assertEqual(response["contact"], contact)
        self.assertEqual(response["needle_action"], "lift")
        self.assertEqual(response["needle_feedrate_mm_min"], 1.0)
        self.assertEqual(
            calls,
            [
                ("begin", "API needle action"),
                ("needles", "lift", 1.0),
                ("finish",),
            ],
        )

    def test_api_contact_needles_invalid_action_returns_400_before_default_feedrate(
        self,
    ) -> None:
        contact = {"contact_number": 8, "label": "Pad 8"}
        window = Main.__new__(Main)
        window._api_contact_context = lambda _contact_number: {
            "accepted": True,
            "point": object(),
            "contact": contact,
        }

        response = Main._api_contact_needles(
            window,
            {
                "contact_number": 8,
                "action": "park",
            },
        )

        self.assertEqual(
            response,
            {
                "accepted": False,
                "status_code": 400,
                "message": "Needle action must be lower, lift, or raise.",
            },
        )

    def test_api_contact_needles_context_rejection_returns_before_default_feedrate(
        self,
    ) -> None:
        rejection = {
            "accepted": False,
            "status_code": 409,
            "message": "Design registration is required before using contacts.",
        }
        window = Main.__new__(Main)
        window._api_contact_context = lambda _contact_number: rejection

        response = Main._api_contact_needles(window, {"contact_number": 8})

        self.assertIs(response, rejection)

    def test_api_contact_needles_invalid_action_beats_context_rejection(self) -> None:
        rejection = {
            "accepted": False,
            "status_code": 409,
            "message": "Design registration is required before using contacts.",
        }
        window = Main.__new__(Main)
        window._api_contact_context = lambda _contact_number: rejection

        response = Main._api_contact_needles(
            window,
            {
                "contact_number": 8,
                "action": "park",
            },
        )

        self.assertEqual(
            response,
            {
                "accepted": False,
                "status_code": 400,
                "message": "Needle action must be lower, lift, or raise.",
            },
        )

    def test_api_check_contact_connection_failure_keeps_contact_payload(self) -> None:
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="Pad 107",
            design_center=(10.0, 20.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        contact = {"contact_number": 7, "label": "Pad 107"}
        window = Main.__new__(Main)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
            "contact": contact,
        }
        window._api_ensure_measurement_instrument_connected = lambda: {
            "accepted": False,
            "status_code": 409,
            "message": "Measurement instrument is not connected.",
        }

        response = Main._api_check_contact(window, {"contact_number": 7})

        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 409)
        self.assertEqual(
            response["message"],
            "Measurement instrument is not connected.",
        )
        self.assertEqual(response["contact"], contact)

    def test_api_check_contact_invalid_payload_returns_400_after_context_resolution(
        self,
    ) -> None:
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="Pad 107",
            design_center=(10.0, 20.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        contact = {"contact_number": 7, "label": "Pad 107"}
        context_calls: list[int] = []
        window = Main.__new__(Main)
        window._api_contact_context = lambda contact_number: context_calls.append(
            int(contact_number)
        ) or {
            "accepted": True,
            "point": point,
            "contact": contact,
        }
        window._api_ensure_measurement_instrument_connected = lambda: None

        response = Main._api_check_contact(
            window,
            {"contact_number": 7, "measurement_count": "bad"},
        )

        self.assertEqual(context_calls, [7])
        self.assertFalse(response["accepted"])
        self.assertEqual(response["status_code"], 400)
        self.assertEqual(
            response["message"],
            "Invalid integer value for measurement_count.",
        )
        self.assertEqual(response["contact"], contact)

    def test_api_measure_current_contact_constructs_runner_with_parsed_settings_for_check_and_seek(
        self,
    ) -> None:
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="Pad 107",
            design_center=(10.0, 20.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        contact = {"contact_number": 7, "label": "Pad 107"}
        created: list[dict[str, object]] = []
        calls: list[tuple[str, RouteMeasurementPoint]] = []

        class _FakeRunner:
            SHORT_CHECK_SAMPLE_COUNT = RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT
            AUTO_CONTACT_SEEK_MAX_TOTAL_MM = (
                RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM
            )
            AUTO_CONTACT_SEEK_STEP_MM = (
                RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM
            )
            DEFAULT_CONTACT_SETTLE_S = (
                RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S
            )

            def __init__(self, **kwargs) -> None:
                created.append(dict(kwargs))

            def check_contact(
                self,
                selected_point: RouteMeasurementPoint,
            ) -> RouteContactPlacementResult:
                calls.append(("check", selected_point))
                return RouteContactPlacementResult(
                    success=True,
                    message="Contact check complete.",
                    point=selected_point,
                    record=RouteMeasurementRecord(
                        timestamp="2026-06-26T10:00:00+00:00",
                        structure_number=107,
                        nplc="1",
                        measurement_type="resistance",
                        n_measurements=6,
                        resistance_ohm=123.4,
                        resistance_rms_ohm=0.5,
                        relative_rms=0.004,
                        status="good",
                    ),
                    contact_seek=None,
                )

            def seek_contact(
                self,
                selected_point: RouteMeasurementPoint,
            ) -> RouteContactPlacementResult:
                calls.append(("seek", selected_point))
                return RouteContactPlacementResult(
                    success=True,
                    message="Contact seek complete.",
                    point=selected_point,
                    record=RouteMeasurementRecord(
                        timestamp="2026-06-26T10:00:01+00:00",
                        structure_number=107,
                        nplc="1",
                        measurement_type="resistance",
                        n_measurements=6,
                        resistance_ohm=123.4,
                        resistance_rms_ohm=0.5,
                        relative_rms=0.004,
                        status="good",
                    ),
                    contact_seek=RouteContactSeekResult(
                        found=True,
                        status="found",
                        attempts=2,
                        initial_status="bad_contact",
                        final_status="good",
                        depth_below_down_mm=0.001,
                        axis_a_lowering_mm=1.001,
                        step_mm=0.001,
                        max_depth_mm=0.002,
                    ),
                )

        window = Main.__new__(Main)
        window.stage_controller = object()
        window.lcr_controller = object()
        window.route_measurement_status = types.SimpleNamespace(emit=lambda _message: None)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
            "contact": contact,
        }
        window._api_ensure_measurement_instrument_connected = lambda: None
        window._api_needle_feedrate = lambda _payload: 7.0
        window._api_timestamp_utc = lambda: "2026-06-26T10:00:00+00:00"

        with mock.patch.object(main_module, "RouteMeasurementRunner", _FakeRunner):
            check_response = Main._api_check_contact(
                window,
                {
                    "contact_number": 7,
                    "initial_samples": 3,
                    "samples": 6,
                    "seek_range_mm": 0.07,
                    "seek_step_mm": 0.003,
                    "settle_s": 0.2,
                    "max_relative_rms_percent": 1.5,
                },
            )
            seek_response = Main._api_contact_seek(
                window,
                {
                    "contact_number": 7,
                    "check_sample_count": 4,
                    "measurement_count": 5,
                    "contact_seek_range_mm": 0.08,
                    "contact_seek_step_mm": 0.004,
                    "contact_settle_s": 0.3,
                    "max_rel_rms": 0.02,
                },
            )

        self.assertEqual(calls, [("check", point), ("seek", point)])
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0]["points"], [point])
        self.assertEqual(created[0]["stage_controller"], window.stage_controller)
        self.assertEqual(created[0]["lcr_controller"], window.lcr_controller)
        self.assertEqual(created[0]["needle_feedrate"], 7.0)
        self.assertEqual(created[0]["measurement_count"], 6)
        self.assertEqual(created[0]["initial_measurement_count"], 3)
        self.assertEqual(created[0]["start_point_number"], 7)
        self.assertEqual(created[0]["max_relative_rms"], 0.015)
        self.assertFalse(created[0]["auto_contact_seek_on_bad_contact"])
        self.assertEqual(created[0]["auto_contact_seek_step_mm"], 0.003)
        self.assertEqual(created[0]["auto_contact_seek_max_total_mm"], 0.07)
        self.assertEqual(created[0]["contact_settle_s"], 0.2)
        self.assertEqual(created[1]["measurement_count"], 5)
        self.assertEqual(created[1]["initial_measurement_count"], 4)
        self.assertEqual(created[1]["max_relative_rms"], 0.02)
        self.assertTrue(created[1]["auto_contact_seek_on_bad_contact"])
        self.assertEqual(created[1]["auto_contact_seek_step_mm"], 0.004)
        self.assertEqual(created[1]["auto_contact_seek_max_total_mm"], 0.08)
        self.assertEqual(created[1]["contact_settle_s"], 0.3)
        self.assertTrue(check_response["accepted"])
        self.assertIsNone(check_response["contact_seek"])
        self.assertTrue(seek_response["accepted"])
        self.assertTrue(seek_response["contact_found"])

    def test_api_contact_seek_failed_seek_sends_route_attention_alert_once(self) -> None:
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="Pad 107",
            design_center=(10.0, 20.0),
            stage_xy=(1.0, 2.0),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        contact = {"contact_number": 7, "label": "Pad 107"}
        alerts: list[tuple[str, str, bool, object]] = []

        class _FakeRunner:
            SHORT_CHECK_SAMPLE_COUNT = RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT
            AUTO_CONTACT_SEEK_MAX_TOTAL_MM = (
                RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM
            )
            AUTO_CONTACT_SEEK_STEP_MM = (
                RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM
            )
            DEFAULT_CONTACT_SETTLE_S = (
                RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S
            )

            def __init__(self, **_kwargs) -> None:
                pass

            def seek_contact(
                self,
                selected_point: RouteMeasurementPoint,
            ) -> RouteContactPlacementResult:
                return RouteContactPlacementResult(
                    success=False,
                    message="Contact seek could not find contact.",
                    point=selected_point,
                    record=RouteMeasurementRecord(
                        timestamp="2026-06-26T10:05:00+00:00",
                        structure_number=107,
                        nplc="1",
                        measurement_type="resistance",
                        n_measurements=5,
                        resistance_ohm=200.0,
                        resistance_rms_ohm=1.0,
                        relative_rms=0.01,
                        status="bad_contact",
                    ),
                    contact_seek=RouteContactSeekResult(
                        found=False,
                        status="not_found",
                        attempts=3,
                        initial_status="bad_contact",
                        final_status="bad_contact",
                        depth_below_down_mm=0.002,
                        axis_a_lowering_mm=1.002,
                        step_mm=0.001,
                        max_depth_mm=0.002,
                    ),
                )

        window = Main.__new__(Main)
        window.stage_controller = object()
        window.lcr_controller = object()
        window.route_measurement_status = types.SimpleNamespace(emit=lambda _message: None)
        window._api_contact_context = lambda _contact: {
            "accepted": True,
            "point": point,
            "contact": contact,
        }
        window._api_ensure_measurement_instrument_connected = lambda: None
        window._api_needle_feedrate = lambda _payload: 7.0
        window._api_timestamp_utc = lambda: "2026-06-26T10:05:00+00:00"
        window._telegram_route_actions_markup = lambda: "actions"
        window._send_telegram_alert = (
            lambda key, text, *, attach_photo=False, reply_markup=None: alerts.append(
                (key, text, bool(attach_photo), reply_markup)
            )
        )

        with mock.patch.object(main_module, "RouteMeasurementRunner", _FakeRunner):
            response = Main._api_contact_seek(
                window,
                {
                    "contact_number": 7,
                    "telegram_on_failure": True,
                },
            )

        self.assertTrue(response["accepted"])
        self.assertFalse(response["contact_found"])
        self.assertEqual(
            alerts,
            [
                (
                    "route_attention",
                    "Probe route needs attention:\n"
                    "Contact seek failed for contact 7: "
                    "Contact seek could not find contact.",
                    True,
                    "actions",
                )
            ],
        )

    def test_combine_telegram_contact_photos_side_by_side(self) -> None:
        script = r"""
import struct
import zlib
from PySide6.QtGui import QImage
from main import Main

def png_bytes(width, height, fill):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )
    red = (fill >> 16) & 0xFF
    green = (fill >> 8) & 0xFF
    blue = fill & 0xFF
    row = b"\x00" + bytes((red, green, blue)) * int(width)
    header = struct.pack(">IIBBBBB", int(width), int(height), 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * int(height)))
        + chunk(b"IEND", b"")
    )

combined = Main._combine_telegram_contact_photos(
    png_bytes(8, 4, 0x00FF0000),
    png_bytes(2, 4, 0x0000FF00),
)
assert combined is not None
image = QImage()
assert image.loadFromData(combined[0])
assert combined[1] == "route-contact-comparison.jpg"
assert image.width() == 10
assert image.height() == 4
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            timeout=20,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_route_contact_result_sends_one_combined_telegram_photo(self) -> None:
        window = Main.__new__(Main)
        before = (
            _telegram_test_photo_bytes(8, 4, 0x00FF0000),
            "before.jpg",
            "Route contact before needle press:\nPoint 1/2, structure 3, P003.",
        )
        after = (
            _telegram_test_photo_bytes(2, 4, 0x0000FF00),
            "after.jpg",
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )
        sent: list[tuple[str, tuple[bytes, str] | None, object | None]] = []

        window._route_measurement_dialog = None
        window._take_pending_telegram_contact_photos = lambda: (before, after)
        window._telegram_contact_photo_payload = (
            lambda _before, _after: (
                (b"combined", "route-contact-comparison.jpg"),
                "Route contact check:\n"
                "Left: before needle press. Right: contact attempt.\n"
                "Point 1/2, structure 3, P003, status=bad_contact.",
            )
        )
        window._send_telegram_bot_message = (
            lambda message, *, photo=None, reply_markup=None: sent.append(
                (message, photo, reply_markup)
            )
        )
        window._telegram_default_markup = lambda: "markup"

        Main._on_route_measurement_result(
            window,
            types.SimpleNamespace(),
            1,
            2,
            True,
        )

        self.assertEqual(len(sent), 1)
        message, photo, reply_markup = sent[0]
        self.assertIn("Left: before needle press. Right: contact attempt.", message)
        self.assertIn("status=bad_contact", message)
        self.assertIsNotNone(photo)
        assert photo is not None
        self.assertEqual(photo[1], "route-contact-comparison.jpg")
        self.assertEqual(reply_markup, "markup")

    def test_route_waiting_bad_contact_sends_one_combined_contact_photo(self) -> None:
        window = Main.__new__(Main)
        before = (
            _telegram_test_photo_bytes(8, 4, 0x00FF0000),
            "before.jpg",
            "Route contact before needle press:\nPoint 1/2, structure 3, P003.",
        )
        after = (
            _telegram_test_photo_bytes(2, 4, 0x0000FF00),
            "after.jpg",
            "Route contact attempt photo:\n"
            "Point 1/2, structure 3, P003, status=bad_contact.",
        )
        alerts: list[tuple[str, str, dict[str, object]]] = []
        record = RouteMeasurementRecord(
            timestamp="2026-06-03T17:21:46",
            structure_number=3,
            nplc="1",
            measurement_type="test",
            n_measurements=250,
            resistance_ohm=298000.0,
            resistance_rms_ohm=19900.0,
            relative_rms=0.0666,
            status="bad_contact",
            contact_quality=RouteContactQuality(
                assessed=True,
                good=False,
                status="bad_contact",
                median_ohm=297000.0,
                mad_sigma_ohm=19100.0,
                p95_abs_step_ohm=48300.0,
                span_ohm=102000.0,
                compliance_hits=0,
                polarity_sign_mismatch_count=2,
                reasons=("mad_sigma_too_high", "step_noise_too_high"),
            ),
        )

        window._last_telegram_attention_message = ""
        window._last_route_measurement_result = (record, 1, 2, True)
        window._route_measurement_waiting = False
        window._pending_route_measure_point = None
        window._latest_route_contact_failure_telegram_photos = lambda: (before, after)
        window._telegram_contact_photo_payload = (
            lambda _before, _after: (
                (b"combined", "route-contact-comparison.jpg"),
                "Route contact check:\n"
                "Left: before needle press. Right: contact attempt.\n"
                "Point 1/2, structure 3, P003, status=bad_contact.",
            )
        )
        window._send_telegram_alert = (
            lambda key, text, **kwargs: alerts.append((key, text, kwargs))
        )
        window._telegram_route_actions_markup = lambda: "actions"
        window.design_navigator_panel = None
        window._route_measurement_dialog = None

        Main._on_route_measurement_waiting_changed(window, True)

        self.assertEqual(len(alerts), 1)
        key, text, kwargs = alerts[0]
        self.assertEqual(key, "route_attention")
        self.assertIn("Measured route point 1/2", text)
        self.assertIn("status=bad_contact", text)
        self.assertIn("reasons=mad_sigma_too_high, step_noise_too_high", text)
        self.assertIn("p95_step=", text)
        self.assertIn("span=", text)
        self.assertIn("compliance_hits=0", text)
        self.assertIn("polarity_mismatches=2", text)
        self.assertIn("Left: before needle press. Right: contact attempt.", text)
        self.assertFalse(kwargs["attach_photo"])
        self.assertEqual(kwargs["reply_markup"], "actions")
        photo = kwargs["photo"]
        self.assertIsNotNone(photo)
        assert photo is not None
        self.assertEqual(photo[1], "route-contact-comparison.jpg")

    def test_route_photo_autofocus_reports_status_through_signal(self) -> None:
        window = Main.__new__(Main)
        emitted: list[str] = []
        requested_ranges: list[float] = []

        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda message: emitted.append(str(message))
        )
        window.stage_controller = types.SimpleNamespace(
            run_external_local_autofocus=lambda *, range_mm: requested_ranges.append(
                float(range_mm)
            )
            or "focus-result"
        )
        configuration = types.SimpleNamespace(photo_autofocus_range_mm=0.03)

        result = Main._route_photo_autofocus(
            window,
            types.SimpleNamespace(),
            2,
            5,
            configuration=configuration,
        )

        self.assertEqual(result, "focus-result")
        self.assertEqual(requested_ranges, [0.03])
        self.assertEqual(
            emitted,
            ["Route photo autofocus: point 2/5, +/-0.030 mm."],
        )

    def test_api_stage_local_focus_runs_local_autofocus(self) -> None:
        window = Main.__new__(Main)
        calls: list[tuple[float, float | None]] = []

        class _FocusResult:
            objective_name = "X50"
            mode = "local"
            start_z_mm = 4.0
            best_z_mm = 4.002
            best_score = 0.2
            sample_count = 9
            edge_peak = False
            range_mm = 0.03
            fine_step_mm = 0.002
            lower_z_mm = 3.97
            upper_z_mm = 4.03

            @property
            def delta_um(self) -> float:
                return 2.0

            def summary(self) -> str:
                return "focused"

            def to_dict(self) -> dict[str, object]:
                return {
                    "objective_name": self.objective_name,
                    "mode": self.mode,
                    "focus_start_z_mm": self.start_z_mm,
                    "focus_best_z_mm": self.best_z_mm,
                    "focus_delta_um": self.delta_um,
                    "focus_score": self.best_score,
                    "focus_sample_count": self.sample_count,
                    "focus_edge_peak": self.edge_peak,
                    "autofocus_range_mm": self.range_mm,
                    "autofocus_fine_step_mm": self.fine_step_mm,
                    "autofocus_lower_z_mm": self.lower_z_mm,
                    "autofocus_upper_z_mm": self.upper_z_mm,
                }

        window.stage_controller = types.SimpleNamespace(
            _needles_known=True,
            _needles_up=True,
            _needles_zone="raise",
            begin_external_task=lambda _label: None,
            finish_external_task=lambda: None,
            run_external_local_autofocus=lambda *, range_mm, step_mm=None: (
                calls.append((float(range_mm), step_mm))
                or _FocusResult()
            ),
        )

        result = Main._api_stage_local_focus(
            window,
            {"range_mm": 0.03, "step_mm": 0.002},
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(calls, [(0.03, 0.002)])
        self.assertEqual(result["focus"]["objective_name"], "X50")
        self.assertEqual(result["focus"]["focus_best_z_mm"], 4.002)

    def test_wait_for_camera_frame_requires_fresh_counter_after_marker(self) -> None:
        window = Main.__new__(Main)
        window._latest_camera_frame_condition = threading.Condition()
        window._latest_camera_frame = _FakeFrame("old")
        window._latest_camera_frame_counter = 7

        frame, counter = Main._wait_for_camera_frame(
            window,
            after_counter=7,
            timeout_s=0.0,
        )

        self.assertIsNone(frame)
        self.assertEqual(counter, 7)

        frame, counter = Main._wait_for_camera_frame(window, timeout_s=0.0)

        self.assertIsNotNone(frame)
        self.assertEqual(frame.name, "old")
        self.assertEqual(counter, 7)

    def test_record_route_photo_writes_focus_map_csv(self) -> None:
        window = Main.__new__(Main)
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(name="route-a")
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            photo_path = Path(tmpdir) / "photos" / "point-001.png"
            record = RoutePhotoRecord(
                timestamp="2026-05-29T12:00:00+03:00",
                path=str(photo_path),
                structure_number=1,
                point_index=1,
                point_id="p001",
                label="P001",
                design_center=(10.0, 20.0),
                stage_xy=(1.0, 2.0),
                focus={
                    "objective_name": "X20",
                    "focus_start_z_mm": 9.98,
                    "focus_best_z_mm": 10.0,
                    "focus_delta_um": 20.0,
                    "focus_score": 12.5,
                    "focus_sample_count": 7,
                    "focus_edge_peak": False,
                    "autofocus_range_mm": 0.03,
                    "autofocus_fine_step_mm": 0.005,
                    "autofocus_lower_z_mm": 9.95,
                    "autofocus_upper_z_mm": 10.01,
                },
            )

            Main._record_route_photo(window, record, 1, 3)

            focus_map_path = photo_path.parent / "route-photo-focus-map.csv"
            with focus_map_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route_name"], "route-a")
        self.assertEqual(rows[0]["photo_path"], str(photo_path))
        self.assertEqual(rows[0]["design_x"], "10.0")
        self.assertEqual(rows[0]["stage_y"], "2.0")
        self.assertEqual(rows[0]["focus_best_z_mm"], "10.0")
        self.assertEqual(rows[0]["focus_delta_um"], "20.0")

    def test_record_route_photo_keeps_empty_route_name_when_route_name_is_none(
        self,
    ) -> None:
        window = Main.__new__(Main)
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(name=None)
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            photo_path = Path(tmpdir) / "photos" / "point-001.png"
            record = RoutePhotoRecord(
                timestamp="2026-05-29T12:00:00+03:00",
                path=str(photo_path),
                structure_number=1,
                point_index=1,
                point_id="p001",
                label="P001",
                design_center=(10.0, 20.0),
                stage_xy=(1.0, 2.0),
                focus={
                    "objective_name": "X20",
                    "focus_start_z_mm": 9.98,
                    "focus_best_z_mm": 10.0,
                    "focus_delta_um": 20.0,
                    "focus_score": 12.5,
                    "focus_sample_count": 7,
                    "focus_edge_peak": False,
                    "autofocus_range_mm": 0.03,
                    "autofocus_fine_step_mm": 0.005,
                    "autofocus_lower_z_mm": 9.95,
                    "autofocus_upper_z_mm": 10.01,
                },
            )

            Main._record_route_photo(window, record, 1, 3)

            focus_map_path = photo_path.parent / "route-photo-focus-map.csv"
            with focus_map_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route_name"], "")

    def test_route_points_keep_contact_xy_separate_from_photo_objective_xy(self) -> None:
        window = Main.__new__(Main)
        settings = Settings()
        settings.objectives.active_name = "X50"
        settings.objectives.objectives = {
            "X5": ObjectiveCalibrationSettings(
                name="X5",
                magnification=5.0,
                xy_offset_configured=True,
            ),
            "X50": ObjectiveCalibrationSettings(
                name="X50",
                magnification=50.0,
                xy_offset_x_mm=0.25,
                xy_offset_y_mm=-0.25,
                xy_offset_configured=True,
            ),
        }
        window.settings_manager = types.SimpleNamespace(
            objectives_configuration=lambda: settings.objectives
        )
        window._design_session = types.SimpleNamespace(
            stage_from_design=lambda _design_xy: (1.0, 2.0)
        )
        route_point = types.SimpleNamespace(
            enabled=True,
            camera_center=(10.0, 20.0),
            id="p001",
            label="P001",
        )
        route = types.SimpleNamespace(
            points=[route_point],
            needle_hits_for_point=lambda _point: [],
        )

        point = Main._route_measurement_points(window, route)[0]

        self.assertEqual(point.stage_xy, (1.0, 2.0))
        self.assertEqual(point.photo_stage_xy, (1.25, 1.75))

    def test_sample_load_focus_uses_active_objective_cache(self) -> None:
        window = Main.__new__(Main)
        settings = Settings()
        settings.objectives.active_name = "X20"
        window.settings_manager = types.SimpleNamespace(
            objectives_configuration=lambda: settings.objectives
        )
        window.stage_controller = types.SimpleNamespace(
            latest_stage_position=lambda: (0.0, 0.0, 9.0)
        )
        window._last_sample_focus_z_by_objective = {
            "X5": 1.25,
            "X20": 2.5,
        }

        self.assertEqual(Main._sample_load_focus_z(window), 2.5)

        settings.objectives.active_name = "X5"

        self.assertEqual(Main._sample_load_focus_z(window), 1.25)

    def test_sample_focus_memory_is_stored_per_objective(self) -> None:
        window = Main.__new__(Main)
        settings = Settings()
        latest_z = [3.0]
        window.settings_manager = types.SimpleNamespace(
            objectives_configuration=lambda: settings.objectives
        )
        window.stage_controller = types.SimpleNamespace(
            latest_stage_position=lambda: (0.0, 0.0, latest_z[0])
        )
        window._last_sample_focus_z_by_objective = {}

        settings.objectives.active_name = "X5"
        Main._remember_sample_focus_from_latest(window)
        latest_z[0] = 7.0
        settings.objectives.active_name = "X20"
        Main._remember_sample_focus_from_latest(window)

        self.assertEqual(
            window._last_sample_focus_z_by_objective,
            {"X5": 3.0, "X20": 7.0},
        )

    def test_sample_unload_cancel_keeps_registration_and_does_not_start(self) -> None:
        window = Main.__new__(Main)
        invalidations: list[str] = []
        remembered: list[bool] = []
        window._design_session = types.SimpleNamespace(
            registration=types.SimpleNamespace(valid=True)
        )
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._has_cancelable_operation = lambda: False
        window._invalidate_design_registration = lambda reason: invalidations.append(
            reason
        )
        window._remember_sample_focus_from_latest = lambda: remembered.append(True)
        window._current_needle_feedrate = lambda: 7.0
        window.stage_controller = types.SimpleNamespace(
            max_feedrate_for_axes=lambda _axes: 900.0
        )

        original_box = main_module.QMessageBox
        original_thread = main_module.threading.Thread
        _FakeThread.instances = []
        main_module.QMessageBox = types.SimpleNamespace(
            Yes=1,
            No=2,
            question=lambda *_args, **_kwargs: 2,
        )
        main_module.threading.Thread = _FakeThread
        try:
            Main._request_sample_unload(window)
        finally:
            main_module.QMessageBox = original_box
            main_module.threading.Thread = original_thread

        self.assertEqual(invalidations, [])
        self.assertEqual(remembered, [])
        self.assertEqual(_FakeThread.instances, [])

    def test_sample_unload_confirm_clears_registration_before_start(self) -> None:
        window = Main.__new__(Main)
        events: list[object] = []
        window._design_session = types.SimpleNamespace(
            registration=types.SimpleNamespace(valid=True)
        )
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._has_cancelable_operation = lambda: False
        window._invalidate_design_registration = lambda reason: events.append(
            ("invalidate", reason)
        )
        window._remember_sample_focus_from_latest = lambda: events.append("remember")
        window._current_needle_feedrate = lambda: 7.0
        window._run_sample_unload = lambda *_args: None
        window.stage_controller = types.SimpleNamespace(
            max_feedrate_for_axes=lambda axes: 900.0
            if tuple(axes) == ("X", "Y")
            else 1.0
        )

        original_box = main_module.QMessageBox
        original_thread = main_module.threading.Thread
        _FakeThread.instances = []
        main_module.QMessageBox = types.SimpleNamespace(
            Yes=1,
            No=2,
            question=lambda *_args, **_kwargs: 1,
        )
        main_module.threading.Thread = _FakeThread
        try:
            Main._request_sample_unload(window)
        finally:
            main_module.QMessageBox = original_box
            main_module.threading.Thread = original_thread

        self.assertEqual(
            events,
            [
                (
                    "invalidate",
                    "Design registration cleared before sample unload.",
                ),
                "remember",
            ],
        )
        self.assertEqual(len(_FakeThread.instances), 1)
        self.assertTrue(_FakeThread.instances[0].started)
        self.assertEqual(_FakeThread.instances[0].args, (900.0, 7.0))

    def test_sample_unload_without_registration_skips_prompt_and_reset(self) -> None:
        window = Main.__new__(Main)
        invalidations: list[str] = []
        remembered: list[bool] = []
        window._design_session = types.SimpleNamespace(registration=None)
        window._stage_serial_ready = lambda: True
        window._sample_handling_active = lambda: False
        window._has_cancelable_operation = lambda: False
        window._invalidate_design_registration = lambda reason: invalidations.append(
            reason
        )
        window._remember_sample_focus_from_latest = lambda: remembered.append(True)
        window._current_needle_feedrate = lambda: 7.0
        window._run_sample_unload = lambda *_args: None
        window.stage_controller = types.SimpleNamespace(
            max_feedrate_for_axes=lambda _axes: 900.0
        )

        original_box = main_module.QMessageBox
        original_thread = main_module.threading.Thread
        _FakeThread.instances = []

        def _unexpected_question(*_args, **_kwargs):
            raise AssertionError("confirmation should not be shown")

        main_module.QMessageBox = types.SimpleNamespace(
            Yes=1,
            No=2,
            question=_unexpected_question,
        )
        main_module.threading.Thread = _FakeThread
        try:
            Main._request_sample_unload(window)
        finally:
            main_module.QMessageBox = original_box
            main_module.threading.Thread = original_thread

        self.assertEqual(invalidations, [])
        self.assertEqual(remembered, [True])
        self.assertEqual(len(_FakeThread.instances), 1)
        self.assertTrue(_FakeThread.instances[0].started)

    def test_route_progress_updates_dialog_progress_bar(self) -> None:
        window = Main.__new__(Main)
        resumed: list[int] = []
        progress: list[tuple[int, int, int]] = []

        window._set_route_measurement_resume_point = resumed.append
        window._route_measurement_dialog = types.SimpleNamespace(
            set_progress=lambda position, total, point_number: progress.append(
                (position, total, point_number)
            )
        )

        Main._on_route_measurement_progress(window, 3, 7, 12)

        self.assertEqual(resumed, [12])
        self.assertEqual(progress, [(3, 7, 12)])

    def test_route_progress_eta_uses_current_run_baseline(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        dialog._progress_started_at = 100.0
        dialog._progress_baseline_completed = 187
        original_monotonic = route_measurement_dialog_module.time.monotonic
        route_measurement_dialog_module.time.monotonic = lambda: 160.0
        try:
            text = RouteMeasurementDialog._progress_eta_text(dialog, 188, 292)
        finally:
            route_measurement_dialog_module.time.monotonic = original_monotonic

        self.assertIn("Remaining: 1:44:00 | Finish:", text)
        self.assertNotIn("Remaining: 00:33", text)

    def test_route_contact_move_raises_needles_and_moves_only(self) -> None:
        window = Main.__new__(Main)
        calls: list[object] = []
        statuses: list[str] = []
        finished: list[tuple[bool, str]] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(
                ("move", x_mm, y_mm)
            ),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.route_measurement_status = types.SimpleNamespace(
            emit=lambda message: statuses.append(str(message))
        )
        window.route_contact_move_finished = types.SimpleNamespace(
            emit=lambda success, message: finished.append((bool(success), str(message)))
        )

        Main._run_route_contact_move(window, point, 75.0)

        self.assertEqual(
            calls,
            [
                ("begin", "route contact move"),
                ("needles", "raise", 75.0),
                ("move", 1.25, 2.5),
                ("finish",),
            ],
        )
        self.assertTrue(statuses)
        self.assertEqual(
            finished,
            [(True, "Route contact move complete: point 7 P007.")],
        )

    def test_route_contact_move_applies_saved_api_route_shift(self) -> None:
        window = Main.__new__(Main)
        calls: list[object] = []
        point = RouteMeasurementPoint(
            index=7,
            point_id="p007",
            label="P007",
            design_center=(10.0, 20.0),
            stage_xy=(1.25, 2.5),
            needle_1_design=(11.0, 21.0),
            needle_2_design=(9.0, 19.0),
        )
        window._api_route_offset_xy = (0.1, -0.2)
        window.stage_controller = types.SimpleNamespace(
            begin_external_task=lambda label: calls.append(("begin", label)),
            run_external_needles_action=lambda action, feedrate: calls.append(
                ("needles", action, feedrate)
            ),
            run_external_move_to_xy=lambda x_mm, y_mm: calls.append(
                ("move", x_mm, y_mm)
            ),
            finish_external_task=lambda: calls.append(("finish",)),
        )
        window.route_measurement_status = types.SimpleNamespace(emit=lambda _message: None)
        window.route_contact_move_finished = types.SimpleNamespace(
            emit=lambda _success, _message: None
        )

        Main._run_route_contact_move(window, point, 75.0)

        self.assertIn(("move", 1.35, 2.3), calls)

    def test_measure_selected_route_point_while_running_queues_interrupt(self) -> None:
        window = Main.__new__(Main)
        runner = _FakeRouteMeasurementRunner()
        statuses: list[str] = []

        window._route_measurement_thread = _FakeAliveThread()
        window._route_measurement_runner = runner
        window._route_measurement_waiting = False
        window._pending_route_measure_point = None
        window._route_measurement_dialog = None
        window.design_navigator_panel = None
        cancellations: list[str] = []
        window.stage_controller = types.SimpleNamespace(
            cancel_active_task=lambda reason: cancellations.append(str(reason))
        )
        window._clear_stage_motion_axes = lambda: None
        window._schedule_status_refreshes = lambda _delays: None
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        request_route_measurement_for_point(
            point_number=91,
            thread_active=True,
            waiting=False,
            open_dialog=window._open_route_measurement_dialog,
            current_dialog=lambda: window._route_measurement_dialog,
            submit_confirmation=window._submit_route_measurement_confirmation,
            request_point_correction=window._request_route_measurement_point_correction,
            start_measurement=window._start_route_measurement,
            set_pending_point=lambda point: setattr(
                window, "_pending_route_measure_point", point
            ),
            clear_pending_point=lambda: setattr(
                window, "_pending_route_measure_point", None
            ),
        )

        self.assertTrue(runner.correction_requested)
        self.assertEqual(cancellations, ["Route measurement interrupt requested."])
        self.assertEqual(window._pending_route_measure_point, 91)
        self.assertEqual(
            statuses,
            [
                "Stopping contact measurement, then measuring point 91."
            ],
        )
        self.assertEqual(runner.confirmations, [])

    def test_contact_seek_cancel_does_not_abort_measurement_instrument(self) -> None:
        window = Main.__new__(Main)
        aborts: list[str] = []
        cancelled_motions: list[str] = []
        statuses: list[str] = []

        window._contact_seek_stop_requested = threading.Event()
        window.lcr_controller = types.SimpleNamespace(
            abort_current_measurement=lambda: aborts.append("lcr")
        )
        window.stage_controller = types.SimpleNamespace(
            cancel_active_motion=lambda reason: cancelled_motions.append(str(reason))
        )
        window._show_status = (
            lambda message, _timeout_ms=None: statuses.append(str(message))
        )

        Main._cancel_contact_seek(window)

        self.assertTrue(window._contact_seek_stop_requested.is_set())
        self.assertEqual(aborts, [])
        self.assertEqual(cancelled_motions, ["Contact seek cancel requested."])
        self.assertEqual(statuses, ["Contact seek cancel requested."])

    def test_waiting_dialog_measure_confirms_current_contact(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        confirmed: list[bool] = []
        remeasured: list[bool] = []
        measured: list[bool] = []

        dialog._running = True
        dialog._waiting = True
        dialog.measure_current_requested = types.SimpleNamespace(
            emit=lambda: confirmed.append(True)
        )
        dialog.remeasure_requested = types.SimpleNamespace(
            emit=lambda: remeasured.append(True)
        )
        dialog._emit_measure_requested = lambda: measured.append(True)

        RouteMeasurementDialog._emit_next_or_measure_requested(dialog)

        self.assertEqual(confirmed, [True])
        self.assertEqual(remeasured, [])
        self.assertEqual(measured, [])

    def test_waiting_dialog_enables_runtime_output_and_meter_fields(self) -> None:
        class _FakeEnabledWidget:
            def __init__(self, *, checked: bool = False) -> None:
                self.enabled = False
                self._checked = bool(checked)

            def setEnabled(self, enabled: bool) -> None:  # noqa: N802
                self.enabled = bool(enabled)

            def isChecked(self) -> bool:  # noqa: N802
                return self._checked

        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        dialog._running = True
        dialog._waiting = False
        dialog._operation_combo = _FakeEnabledWidget()
        dialog._operation_combo.currentData = lambda: (
            route_measurement_dialog_module.ROUTE_OPERATION_PHOTO_THEN_MEASURE
        )
        dialog._photo_dir_edit = _FakeEnabledWidget()
        dialog._photo_browse_button = _FakeEnabledWidget()
        dialog._photo_settle_spin = _FakeEnabledWidget()
        dialog._photo_autofocus_checkbox = _FakeEnabledWidget(checked=True)
        dialog._photo_autofocus_range_spin = _FakeEnabledWidget()
        dialog._csv_path_edit = _FakeEnabledWidget()
        dialog._csv_browse_button = _FakeEnabledWidget()
        dialog._previous_ok_only_checkbox = _FakeEnabledWidget(checked=True)
        dialog._previous_csv_path_edit = _FakeEnabledWidget()
        dialog._previous_csv_browse_button = _FakeEnabledWidget()
        dialog._meter_combo = _FakeEnabledWidget()
        dialog._gwinstek_page = _FakeEnabledWidget()
        dialog._keithley_page = _FakeEnabledWidget()
        dialog._initial_measurement_count_spin = _FakeEnabledWidget()
        dialog._followup_measurement_count_spin = _FakeEnabledWidget()
        dialog._max_relative_rms_spin = _FakeEnabledWidget()
        dialog._contact_settle_spin = _FakeEnabledWidget()
        dialog._contact_seek_range_spin = _FakeEnabledWidget()
        dialog._contact_seek_step_spin = _FakeEnabledWidget()
        dialog._contact_max_mad_sigma_spin = _FakeEnabledWidget()
        dialog._contact_max_p95_step_spin = _FakeEnabledWidget()
        dialog._contact_max_relative_mad_spin = _FakeEnabledWidget()
        dialog._contact_max_relative_p95_step_spin = _FakeEnabledWidget()
        dialog._pause_button = _FakeButton()
        dialog._interrupt_button = _FakeButton()
        dialog._stop_button = _FakeButton()
        dialog._save_shift_button = _FakeButton()
        dialog._remeasure_button = _FakeButton()
        dialog._skip_button = _FakeButton()
        dialog._next_button = _FakeButton()
        dialog._jump_point_spin = _FakeEnabledWidget()
        dialog._move_button = _FakeButton()
        dialog._jump_button = _FakeButton()
        dialog._set_runtime_settings_enabled = lambda _enabled: None
        dialog._update_session_buttons = lambda: None

        RouteMeasurementDialog.set_waiting(dialog, True)

        self.assertTrue(dialog._csv_path_edit.enabled)
        self.assertTrue(dialog._operation_combo.enabled)
        self.assertTrue(dialog._photo_dir_edit.enabled)
        self.assertTrue(dialog._photo_autofocus_checkbox.enabled)
        self.assertTrue(dialog._photo_autofocus_range_spin.enabled)
        self.assertTrue(dialog._previous_ok_only_checkbox.enabled)
        self.assertTrue(dialog._previous_csv_path_edit.enabled)
        self.assertTrue(dialog._meter_combo.enabled)
        self.assertTrue(dialog._gwinstek_page.enabled)
        self.assertTrue(dialog._keithley_page.enabled)

    def test_running_dialog_close_is_ignored(self) -> None:
        dialog = RouteMeasurementDialog.__new__(RouteMeasurementDialog)
        statuses: list[str] = []

        class _CloseEvent:
            def __init__(self) -> None:
                self.ignored = False

            def ignore(self) -> None:
                self.ignored = True

        event = _CloseEvent()
        dialog._running = True
        dialog.set_status = lambda message: statuses.append(str(message))

        RouteMeasurementDialog.closeEvent(dialog, event)

        self.assertTrue(event.ignored)
        self.assertEqual(statuses, ["Stop route measurement before closing."])

    def test_main_shutdown_forces_route_dialog_close(self) -> None:
        window = Main.__new__(Main)
        calls: list[tuple[str, bool | None]] = []
        window._route_measurement_dialog = types.SimpleNamespace(
            set_running=lambda value: calls.append(("running", bool(value))),
            close=lambda: calls.append(("close", None)),
        )
        window.design_layout_window = None
        window.contact_calibration_window = None
        window.surface_map_window = None
        window.microscope_scan_dialog = None

        Main._close_auxiliary_windows(window, force_route_dialog=True)

        self.assertEqual(calls, [("running", False), ("close", None)])

    def test_record_route_contact_height_writes_height_map_csv(self) -> None:
        window = Main.__new__(Main)
        window._design_session = types.SimpleNamespace(
            route=types.SimpleNamespace(name="route-a")
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "measurements" / "route.csv"
            record = RouteContactHeightRecord(
                timestamp="2026-05-29T12:00:00+03:00",
                structure_number=1,
                point_index=1,
                point_id="p001",
                label="P001",
                design_center=(10.0, 20.0),
                stage_xy=(1.0, 2.0),
                measurement_status="ok",
                resistance_ohm=1000.5,
                resistance_rms_ohm=0.5,
                relative_rms=0.0005,
                contact_quality=RouteContactQuality(
                    assessed=True,
                    good=True,
                    status="good",
                    median_ohm=1000.5,
                    mad_sigma_ohm=1.2,
                    p95_abs_step_ohm=2.0,
                    span_ohm=3.0,
                ),
                contact_found=True,
                contact_depth_below_down_mm=0.002,
                contact_axis_a_lowering_mm=1.002,
                contact_seek=RouteContactSeekResult(
                    found=True,
                    status="found",
                    attempts=3,
                    initial_status="bad_contact",
                    final_status="good",
                    depth_below_down_mm=0.002,
                    axis_a_lowering_mm=1.002,
                    step_mm=0.001,
                    max_depth_mm=0.002,
                ),
            )

            Main._record_route_contact_height(
                window,
                record,
                1,
                3,
                csv_path=csv_path,
            )

            height_map_path = csv_path.parent / "route-contact-height-map.csv"
            with height_map_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["route_name"], "route-a")
        self.assertEqual(rows[0]["design_x"], "10.0")
        self.assertEqual(rows[0]["stage_y"], "2.0")
        self.assertEqual(rows[0]["measurement_status"], "ok")
        self.assertEqual(rows[0]["contact_found"], "true")
        self.assertEqual(rows[0]["contact_depth_below_down_mm"], "0.002")
        self.assertEqual(rows[0]["contact_axis_a_lowering_mm"], "1.002")
        self.assertEqual(rows[0]["contact_seek_status"], "found")
        self.assertEqual(rows[0]["contact_seek_attempts"], "3")

if __name__ == "__main__":
    unittest.main()
