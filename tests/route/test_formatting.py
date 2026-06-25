import math

from probe_station_gui.route.formatting import (
    csv_bool,
    csv_float,
    format_route_ohm,
    format_route_percent,
)


def test_route_formatting_helpers_preserve_status_text_units() -> None:
    assert csv_float("1.25") == "1.25"
    assert csv_float(math.nan) == ""
    assert csv_bool(1) == "true"
    assert csv_bool(0) == "false"
    assert format_route_percent(0.1234) == "12.3%"
    assert format_route_percent(math.nan) == "nan%"
    assert format_route_ohm(1200.0) == "1.2 kOhm"
    assert format_route_ohm(math.nan) == "nan Ohm"
