from __future__ import annotations

import math
import sys
import unittest

from probe_station_gui.instruments.meters.lcr_session_backend import (
    LCRMeterError,
    LCRSessionConfiguration,
    configure_live_session,
    open_configured_session,
    session_visa_operation,
    validate_session_identity,
)
from probe_station_gui.route.meter_config import (
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    ROUTE_METER_KEITHLEY_2400,
)


def _configuration(**overrides: object) -> LCRSessionConfiguration:
    values: dict[str, object] = {
        "meter_type": ROUTE_METER_GWINSTEK,
        "resource_name": " COM4 ",
        "keithley_source_resource": "",
        "keithley_voltmeter_resource": "",
        "measurement_function": " DCR ",
        "range_mode": " auto ",
        "auto_range_enabled": True,
        "impedance_range": 3,
        "dcr_range": 4,
        "frequency_hz": 1000.0,
        "level_mode": " voltage ",
        "voltage_level_v": 0.01,
        "current_level_a": 0.0001,
        "source_resistance_ohm": 30,
        "aperture_rate": " fast ",
        "aperture_averages": 1,
        "trigger_source": " int ",
        "trigger_delay_s": 0.0,
        "bias_enabled": False,
        "bias_level_v": 0.0,
        "monitor1": " off ",
        "monitor2": " off ",
        "alc_enabled": False,
        "short_threshold_ohm": 10.0,
        "poll_interval_ms": 250,
    }
    values.update(overrides)
    return LCRSessionConfiguration.normalized(**values)


class LCRSessionConfigurationTests(unittest.TestCase):
    def test_runtime_configuration_normalizes_values_and_connection_policy(
        self,
    ) -> None:
        configuration = _configuration(
            meter_type="unsupported",
            resource_name=" COM7 ",
            impedance_range=99,
            dcr_range=-2,
            frequency_hz=1.0,
            voltage_level_v=-1.0,
            current_level_a=-1.0,
            aperture_averages=999,
            bias_level_v=5.0,
            short_threshold_ohm=-1.0,
            poll_interval_ms=1,
        )

        self.assertEqual(configuration.meter_type, ROUTE_METER_GWINSTEK)
        self.assertEqual(configuration.resource_name, "COM7")
        self.assertEqual(configuration.impedance_range, 8)
        self.assertEqual(configuration.dcr_range, 0)
        self.assertEqual(configuration.frequency_hz, 10.0)
        self.assertEqual(configuration.voltage_level_v, 0.0)
        self.assertEqual(configuration.current_level_a, 0.0)
        self.assertEqual(configuration.aperture_averages, 256)
        self.assertEqual(configuration.bias_level_v, 2.5)
        self.assertEqual(configuration.short_threshold_ohm, 0.0)
        self.assertEqual(configuration.poll_interval_ms, 50)
        self.assertEqual(
            configuration.connection_key,
            f"{ROUTE_METER_GWINSTEK}|ASRL7::INSTR",
        )
        self.assertEqual(configuration.connection_label, "COM7")
        self.assertTrue(configuration.is_short_reading(0.0))
        self.assertFalse(configuration.is_short_reading(0.1))

    def test_keithley_configuration_owns_resource_and_short_policy(self) -> None:
        configuration = _configuration(
            meter_type=ROUTE_METER_KEITHLEY,
            resource_name="ignored",
            keithley_source_resource=" GPIB0::1::INSTR ",
            keithley_voltmeter_resource=" GPIB0::2::INSTR ",
            trigger_source=" bus ",
        )

        self.assertEqual(
            configuration.connection_key,
            f"{ROUTE_METER_KEITHLEY}|GPIB0::1::INSTR|GPIB0::2::INSTR",
        )
        self.assertEqual(
            configuration.connection_label,
            "Keithley 2400 GPIB0::1::INSTR; 2182A GPIB0::2::INSTR",
        )
        self.assertTrue(configuration.uses_bus_trigger)
        self.assertTrue(configuration.is_short_reading(10.0))
        self.assertFalse(configuration.is_short_reading(math.nan))

        source_only = _configuration(
            meter_type=ROUTE_METER_KEITHLEY_2400,
            keithley_source_resource="GPIB0::3::INSTR",
            keithley_voltmeter_resource="GPIB0::4::INSTR",
        )
        self.assertEqual(source_only.keithley_voltmeter_resource, "")
        self.assertEqual(
            source_only.connection_key,
            f"{ROUTE_METER_KEITHLEY_2400}|GPIB0::3::INSTR",
        )


class LCRSessionBackendTests(unittest.TestCase):
    def test_open_configured_session_uses_injected_lazy_backend(self) -> None:
        calls: list[tuple[object, ...]] = []

        class _GWSession:
            def __init__(self, resource: str, timeout_ms: int) -> None:
                calls.append(("gw", resource, timeout_ms))

        def open_keithley(source: str, voltmeter: str, timeout_ms: int) -> object:
            calls.append(("keithley", source, voltmeter, timeout_ms))
            return object()

        pyvisa_modules_before = {
            name
            for name in sys.modules
            if name == "pyvisa" or name.startswith("pyvisa.")
        }
        open_configured_session(
            _configuration(),
            timeout_ms=321,
            gwinstek_session_type=_GWSession,
            keithley_session_opener=open_keithley,
        )

        self.assertEqual(
            {
                name
                for name in sys.modules
                if name == "pyvisa" or name.startswith("pyvisa.")
            },
            pyvisa_modules_before,
        )
        open_configured_session(
            _configuration(
                meter_type=ROUTE_METER_KEITHLEY,
                keithley_source_resource="GPIB0::1::INSTR",
                keithley_voltmeter_resource="GPIB0::2::INSTR",
            ),
            timeout_ms=654,
            gwinstek_session_type=_GWSession,
            keithley_session_opener=open_keithley,
        )

        self.assertEqual(
            calls,
            [
                ("gw", "COM4", 321),
                (
                    "keithley",
                    "GPIB0::1::INSTR",
                    "GPIB0::2::INSTR",
                    654,
                ),
            ],
        )
        self.assertEqual(
            {
                name
                for name in sys.modules
                if name == "pyvisa" or name.startswith("pyvisa.")
            },
            pyvisa_modules_before,
        )

    def test_configure_and_validate_live_sessions_preserve_backend_policy(self) -> None:
        class _GWSession:
            backend_name = "gw"

            def __init__(self) -> None:
                self.configurations: list[dict[str, object]] = []

            def configure_measurement(self, **kwargs: object) -> None:
                self.configurations.append(dict(kwargs))

        gw = _GWSession()
        configuration = _configuration(trigger_source="int")
        configure_live_session(gw, configuration, gwinstek_session_type=_GWSession)
        self.assertEqual(gw.configurations[-1]["trigger_source"], "BUS")
        self.assertEqual(gw.configurations[-1]["dcr_range"], 4)
        self.assertEqual(validate_session_identity(gw, configuration), ("gw", ""))

        class _KeithleySession:
            backend_name = "keithley"

            def __init__(self) -> None:
                self.configurations: list[dict[str, object]] = []

            def identify(self) -> str:
                return "2400 KEITHLEY; 2182A fake"

            def configure_measurement(self, **kwargs: object) -> None:
                self.configurations.append(dict(kwargs))

        keithley = _KeithleySession()
        keithley_configuration = _configuration(
            meter_type=ROUTE_METER_KEITHLEY,
            keithley_source_resource="GPIB0::1::INSTR",
            keithley_voltmeter_resource="GPIB0::2::INSTR",
        )
        self.assertEqual(
            validate_session_identity(keithley, keithley_configuration),
            ("keithley", "2400 KEITHLEY; 2182A fake"),
        )
        configure_live_session(keithley, keithley_configuration)
        self.assertEqual(
            keithley.configurations[-1]["keithley_measurement_voltage_v"],
            0.03,
        )
        self.assertFalse(keithley.configurations[-1]["keithley_use_buffer"])

    def test_keithley_identity_and_visa_errors_use_canonical_error(self) -> None:
        configuration = _configuration(
            meter_type=ROUTE_METER_KEITHLEY,
            keithley_source_resource="GPIB0::1::INSTR",
        )

        class _WrongSession:
            def identify(self) -> str:
                return "2182A only"

        with self.assertRaisesRegex(LCRMeterError, r"did not respond to \*IDN"):
            validate_session_identity(_WrongSession(), configuration)
        with self.assertRaisesRegex(LCRMeterError, "not connected"):
            session_visa_operation(
                None,
                "meter.source",
                "query",
                command="*IDN?",
                timeout_ms=None,
                read_termination=None,
                write_termination=None,
            )


if __name__ == "__main__":
    unittest.main()
