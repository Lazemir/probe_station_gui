from types import SimpleNamespace

from probe_station_gui.lcr_meter_helpers import (
    callable_accepts_keyword,
    normalize_visa_role,
    prepare_route_measurement_batch,
    read_route_measurement_batch,
    session_visa_resource_roles,
    voltage_sweep_point_to_dict,
)


def test_callable_accepts_keyword_detects_named_and_variadic_keywords() -> None:
    def named(count, *, trigger=False):
        return count, trigger

    def variadic(count, **kwargs):
        return count, kwargs

    def positional(count):
        return count

    assert callable_accepts_keyword(named, "trigger")
    assert callable_accepts_keyword(variadic, "trigger")
    assert not callable_accepts_keyword(positional, "trigger")
    assert not callable_accepts_keyword(object(), "trigger")


def test_prepare_route_measurement_batch_passes_source_list_count_when_supported() -> None:
    calls: list[tuple[object, ...]] = []

    def preparer(count, *, source_list_count=None) -> None:
        calls.append((count, source_list_count))

    prepare_route_measurement_batch(preparer, 10, source_list_count=240)

    assert calls == [(10, 240)]


def test_read_route_measurement_batch_passes_supported_options() -> None:
    calls: list[tuple[object, ...]] = []

    def reader(count, *, trigger=False, after_measurement=None):
        calls.append((count, trigger, after_measurement))
        return ("a", "b")

    result = read_route_measurement_batch(
        reader,
        2,
        after_measurement={"settle": True},
    )

    assert result == ["a", "b"]
    assert calls == [(2, True, {"settle": True})]
    assert read_route_measurement_batch(object(), 2, after_measurement=None) == []


def test_voltage_sweep_point_to_dict_accepts_known_shapes() -> None:
    class PointWithDict:
        def as_dict(self) -> dict[str, object]:
            return {"source_voltage_v": 0.1}

    assert voltage_sweep_point_to_dict(PointWithDict()) == {"source_voltage_v": 0.1}
    assert voltage_sweep_point_to_dict({"current_a": 1e-6}) == {"current_a": 1e-6}
    assert voltage_sweep_point_to_dict(
        SimpleNamespace(resistance_ohm=10.0, compliance_hit=False)
    ) == {"resistance_ohm": 10.0, "compliance_hit": False}
    assert voltage_sweep_point_to_dict(42) == {"value": 42}


def test_visa_role_helpers_normalize_and_mark_roles_available() -> None:
    class Session:
        def visa_resource_roles(self) -> dict[str, dict[str, object]]:
            return {"source-meter": {"resource": "GPIB0::1::INSTR"}}

    assert normalize_visa_role(" Source-Meter ") == "source_meter"
    assert session_visa_resource_roles(
        Session(),
        meter_type="keithley",
    ) == {
        "source-meter": {
            "role": "source-meter",
            "resource": "GPIB0::1::INSTR",
            "meter_type": "keithley",
            "available": True,
        }
    }
    assert session_visa_resource_roles(None, meter_type="keithley") == {}
