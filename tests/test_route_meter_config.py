from probe_station_gui.route_meter_config import (
    GWInstekRouteMeterSettings,
    KeithleyRouteMeterSettings,
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    RouteMeterConfiguration,
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
