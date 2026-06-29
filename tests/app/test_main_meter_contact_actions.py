import types
import unittest
from unittest import mock

from tests.app.main_coordinate_feedrate_support import (
    LCRMeterError,
    Main,
    RouteContactPlacementResult,
    RouteContactSeekResult,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RouteMeasurementRunner,
    RouteMeterConfiguration,
    StageControllerError,
    main_module,
)


class MainMeterContactActionsTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
