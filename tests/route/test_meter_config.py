from probe_station_gui.route.meter_config import (
    GWInstekRouteMeterSettings,
    KeithleyRouteMeterSettings,
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    RouteMeterConfiguration,
    route_meter_configuration_from_payload,
    route_meter_type_from_payload,
)


def test_route_meter_configuration_describes_keithley_voltage_sweep() -> None:
    configuration = RouteMeterConfiguration(
        meter_type=ROUTE_METER_KEITHLEY,
        keithley=KeithleyRouteMeterSettings(
            measurement_voltage_v=0.03,
            nplc=7.5,
        ),
    )

    assert configuration.measurement_type_label() == "Keithley voltage sweep +/-0.03 V"
    assert configuration.nplc_label() == "7.5"


def test_route_meter_configuration_describes_gwinstek_mode() -> None:
    assert (
        RouteMeterConfiguration(
            meter_type=ROUTE_METER_GWINSTEK,
            gwinstek=GWInstekRouteMeterSettings(measurement_function="DCR"),
        ).measurement_type_label()
        == "GW Instek DCR"
    )
    assert (
        RouteMeterConfiguration(
            meter_type=ROUTE_METER_GWINSTEK,
            gwinstek=GWInstekRouteMeterSettings(measurement_function="Cp-D"),
        ).measurement_type_label()
        == "GW Instek Cp-D"
    )


def test_route_meter_type_from_payload_normalizes_api_aliases() -> None:
    assert route_meter_type_from_payload(None) is None
    assert route_meter_type_from_payload("configured") is None
    assert route_meter_type_from_payload("keithley") == ROUTE_METER_KEITHLEY
    assert route_meter_type_from_payload("2400_2182a") == ROUTE_METER_KEITHLEY
    assert route_meter_type_from_payload("lcr") == ROUTE_METER_GWINSTEK
    assert route_meter_type_from_payload("custom-meter") == "custom-meter"


def test_route_meter_configuration_from_payload_accepts_keithley_code_auto_ranges() -> None:
    config = route_meter_configuration_from_payload(
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
        current_meter_type=ROUTE_METER_GWINSTEK,
        default_gwinstek_resource_name="COM9",
    )

    assert config.meter_type == ROUTE_METER_KEITHLEY
    assert config.keithley.range_mode == "code_auto"
    assert config.keithley.measurement_voltage_v == 0.03
    assert config.keithley.expected_resistance_ohm == 100_000.0
    assert config.keithley.maximum_current_a == 10e-6
    assert config.keithley.nplc == 5


def test_route_meter_configuration_from_payload_uses_raw_sweep_span_for_ranges() -> None:
    config = route_meter_configuration_from_payload(
        {
            "meter_type": "keithley",
            "nplc": 5,
        },
        voltages_v=[-0.3, 0.1, 0.25],
        current_meter_type=ROUTE_METER_GWINSTEK,
        default_gwinstek_resource_name="COM9",
    )

    assert config.keithley.measurement_voltage_v == 0.3
    assert config.keithley.voltage_range_v == 0.3
    assert config.keithley.source_voltage_range_v == 0.3
    assert config.keithley.voltmeter_range_v == 0.3


def test_route_meter_configuration_from_payload_uses_current_meter_and_gwinstek_defaults() -> None:
    config = route_meter_configuration_from_payload(
        {
            "measurement_function": "DCR",
            "bias_enabled": "yes",
            "alc_enabled": "0",
        },
        voltages_v=None,
        current_meter_type=ROUTE_METER_GWINSTEK,
        default_gwinstek_resource_name="COM8",
    )

    assert config.meter_type == ROUTE_METER_GWINSTEK
    assert config.gwinstek.resource_name == "COM8"
    assert config.gwinstek.bias_enabled is True
    assert config.gwinstek.alc_enabled is False
