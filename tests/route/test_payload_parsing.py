import pytest

from probe_station_gui.route.payload_parsing import (
    payload_bool,
    payload_float,
    payload_int,
    payload_optional_float,
)


def test_payload_bool_accepts_common_api_representations() -> None:
    assert payload_bool({"enabled": "yes"}, "enabled", default=False) is True
    assert payload_bool({"enabled": "0"}, "enabled", default=True) is False
    assert payload_bool({"enabled": 1}, "enabled", default=False) is True
    assert payload_bool({}, "enabled", default=True) is True


def test_payload_float_validates_finite_minimum_values() -> None:
    assert payload_float({"value": "1.5"}, "value", default=0.0) == 1.5
    assert payload_optional_float({"value": "2.5"}, "value") == 2.5
    assert payload_optional_float({}, "value") is None

    with pytest.raises(ValueError, match="value must be at least"):
        payload_float({"value": "-1"}, "value", default=0.0, minimum=0.0)

    with pytest.raises(ValueError, match="Invalid numeric value"):
        payload_float({"value": "nan"}, "value", default=0.0)


def test_payload_int_rounds_and_validates_minimum_values() -> None:
    assert payload_int({"count": "2.4"}, "count", default=1) == 2

    with pytest.raises(ValueError, match="count must be at least"):
        payload_int({"count": 0}, "count", default=1, minimum=1)
