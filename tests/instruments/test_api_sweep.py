from __future__ import annotations

import pytest

from probe_station_gui.instruments.api_sweep import (
    ApiRawVoltageSweepRequestError,
    api_configure_meter_action,
    api_prepare_route_meter_controller_action,
    api_raw_voltage_sweep_action,
    api_raw_voltage_sweep_contact_plan,
    api_raw_voltage_sweep_error_response,
    api_raw_voltage_sweep_missing_contact_response,
    api_raw_voltage_sweep_request_from_payload,
    api_raw_voltage_sweep_success_response,
)


@pytest.mark.parametrize(
    ("payload"),
    [
        {},
        {"voltages_v": "0.1"},
        {"voltages_v": []},
    ],
)
def test_request_rejects_missing_non_list_or_empty_voltages(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ApiRawVoltageSweepRequestError) as exc_info:
        api_raw_voltage_sweep_request_from_payload(payload)

    assert exc_info.value.response() == {
        "accepted": False,
        "status_code": 400,
        "message": "Provide voltages_v as a non-empty array.",
    }


def test_request_converts_voltage_values_and_applies_contact_defaults() -> None:
    request = api_raw_voltage_sweep_request_from_payload(
        {
            "voltages_v": ["-0.1", 0, 0.2],
            "contact": 7,
        }
    )

    assert request.voltage_values == [-0.1, 0.0, 0.2]
    assert request.contact_number == 7
    assert request.move_to_contact is True
    assert request.lower_needles is True
    assert request.lift_after is True
    assert request.lift_before_move is True
    assert request.contact_settle_s == 0.2


def test_request_invalid_voltage_conversion_returns_409_response() -> None:
    with pytest.raises(ApiRawVoltageSweepRequestError) as exc_info:
        api_raw_voltage_sweep_request_from_payload({"voltages_v": [0.0, "bad"]})

    assert exc_info.value.response() == {
        "accepted": False,
        "status_code": 409,
        "message": "could not convert string to float: 'bad'",
    }


def test_request_boolean_aliases_override_defaults() -> None:
    request = api_raw_voltage_sweep_request_from_payload(
        {
            "voltages_v": [0.0],
            "contact_number": 4,
            "move": "off",
            "lower": 0,
            "lift_after": False,
            "lift_before_move": "no",
        }
    )

    assert request.contact_number == 4
    assert request.move_to_contact is False
    assert request.lower_needles is False
    assert request.lift_after is False
    assert request.lift_before_move is False


def test_request_contact_settle_alias_and_default() -> None:
    assert api_raw_voltage_sweep_request_from_payload(
        {"voltages_v": [0.0]}
    ).contact_settle_s == 0.2
    assert api_raw_voltage_sweep_request_from_payload(
        {"voltages_v": [0.0], "settle_s": "0.35"}
    ).contact_settle_s == 0.35


def test_request_contact_settle_minimum_error_uses_409_response() -> None:
    with pytest.raises(ApiRawVoltageSweepRequestError) as exc_info:
        api_raw_voltage_sweep_request_from_payload(
            {"voltages_v": [0.0], "contact_settle_s": -0.1}
        )

    assert exc_info.value.response() == {
        "accepted": False,
        "status_code": 409,
        "message": "contact_settle_s must be at least 0.0.",
    }


def test_contact_plan_rejects_move_without_contact_number() -> None:
    request = api_raw_voltage_sweep_request_from_payload(
        {"voltages_v": [0.0], "move_to_contact": True}
    )

    assert api_raw_voltage_sweep_contact_plan(request) == (
        api_raw_voltage_sweep_missing_contact_response()
    )


def test_success_response_formats_iv_pairs_from_result_points() -> None:
    response = api_raw_voltage_sweep_success_response(
        voltage_values=[-0.1, 0.0, 0.1],
        result={
            "points": [
                {"measured_voltage_v": -0.1001, "current_a": -1.2e-6, "ignored": 1},
                {"measured_voltage_v": 0.0998, "current_a": 1.4e-6},
                "skip me",
            ],
            "meta": {"range": "auto"},
        },
        timestamp_utc="2026-06-26T10:00:00+00:00",
        elapsed_s=1.25,
        contact={"contact_number": 7, "label": "Pad 7"},
        meter_type="keithley_2400_2182a",
        lower_needles=True,
        needles_lowered=True,
        lift_after=True,
    )

    assert response == {
        "accepted": True,
        "message": "Raw voltage sweep complete: 3 points.",
        "timestamp_utc": "2026-06-26T10:00:00+00:00",
        "elapsed_s": 1.25,
        "contact": {"contact_number": 7, "label": "Pad 7"},
        "meter_type": "keithley_2400_2182a",
        "measurement_kind": "voltage_sweep",
        "voltages_v": [-0.1, 0.0, 0.1],
        "iv_pairs": [
            {"voltage_v": -0.1001, "current_a": -1.2e-6},
            {"voltage_v": 0.0998, "current_a": 1.4e-6},
        ],
        "result": {
            "points": [
                {"measured_voltage_v": -0.1001, "current_a": -1.2e-6, "ignored": 1},
                {"measured_voltage_v": 0.0998, "current_a": 1.4e-6},
                "skip me",
            ],
            "meta": {"range": "auto"},
        },
        "needles_lowered": True,
        "lifted_after": True,
    }


def test_success_response_uses_empty_iv_pairs_when_points_is_not_a_list() -> None:
    response = api_raw_voltage_sweep_success_response(
        voltage_values=[0.0],
        result={"points": "not-a-list"},
        timestamp_utc="2026-06-26T10:00:00+00:00",
        elapsed_s=0.5,
        contact=None,
        meter_type="mock_meter",
        lower_needles=False,
        needles_lowered=False,
        lift_after=False,
    )

    assert response["iv_pairs"] == []
    assert response["needles_lowered"] is False
    assert response["lifted_after"] is False


def test_stage_or_lcr_error_response_keeps_contact_payload() -> None:
    assert api_raw_voltage_sweep_error_response(
        "Stage is busy.",
        contact={"contact_number": 4, "label": "Pad 4"},
    ) == {
        "accepted": False,
        "status_code": 409,
        "message": "Stage is busy.",
        "contact": {"contact_number": 4, "label": "Pad 4"},
    }


def test_prepare_route_meter_controller_waits_connects_and_applies_configuration() -> None:
    calls: list[tuple[object, ...]] = []

    response = api_prepare_route_meter_controller_action(
        configuration="cfg",
        prefix="Measurement instrument setup failed",
        is_connected=lambda: False,
        wait_until_idle=lambda timeout_s: calls.append(("wait", timeout_s)) or True,
        apply_route_meter_runtime_configuration=lambda configuration: calls.append(
            ("runtime", configuration)
        ),
        ensure_measurement_instrument_connected=lambda: calls.append(("ensure",)) or None,
        apply_route_meter_configuration=lambda configuration: calls.append(
            ("apply", configuration)
        ),
        instrument_exception_response=lambda prefix, exc: {
            "accepted": False,
            "status_code": 409,
            "message": f"{prefix}: {exc}",
            "error_type": type(exc).__name__,
        },
        log_exception=lambda _message: None,
    )

    assert response is None
    assert calls == [
        ("wait", 45.0),
        ("runtime", "cfg"),
        ("ensure",),
        ("apply", "cfg"),
    ]


def test_configure_meter_action_returns_success_response() -> None:
    class _Configuration:
        meter_type = "keithley_2400_2182a"

        @staticmethod
        def nplc_label() -> str:
            return "1"

    calls: list[tuple[object, ...]] = []

    response = api_configure_meter_action(
        payload={"meter_type": "keithley_2400_2182a"},
        route_meter_configuration=lambda payload, voltages_v: calls.append(
            ("config", payload, voltages_v)
        )
        or _Configuration(),
        prepare_route_meter_controller=lambda configuration, *, prefix: calls.append(
            ("prepare", configuration.meter_type, prefix)
        )
        or None,
        timestamp_utc=lambda: "2026-06-26T12:00:00+00:00",
    )

    assert response == {
        "accepted": True,
        "message": "Measurement instrument configured.",
        "timestamp_utc": "2026-06-26T12:00:00+00:00",
        "meter_type": "keithley_2400_2182a",
        "nplc": "1",
    }
    assert calls == [
        ("config", {"meter_type": "keithley_2400_2182a"}, None),
        (
            "prepare",
            "keithley_2400_2182a",
            "Measurement instrument setup failed",
        ),
    ]


def test_raw_voltage_sweep_action_preserves_stage_side_effect_order() -> None:
    calls: list[tuple[object, ...]] = []

    class _Configuration:
        meter_type = "keithley_2400_2182a"

    monotonic_values = iter([100.0, 101.25])

    response = api_raw_voltage_sweep_action(
        payload={"voltages_v": [0.0, "0.1"], "contact_number": 7},
        route_meter_configuration=lambda payload, voltages_v: calls.append(
            ("config", payload, tuple(voltages_v or ()))
        )
        or _Configuration(),
        prepare_route_meter_controller=lambda configuration, *, prefix: calls.append(
            ("prepare", configuration.meter_type, prefix)
        )
        or None,
        contact_context=lambda contact_number: {
            "accepted": True,
            "point": f"point-{contact_number}",
            "contact": {"contact_number": contact_number, "label": "Pad 7"},
        },
        needle_feedrate=lambda payload: calls.append(("feedrate", dict(payload))) or 75.0,
        route_adjusted_stage_xy=lambda point: calls.append(("target", point)) or (1.25, 2.5),
        begin_stage_task=lambda label: calls.append(("begin", label)),
        run_needles_action=lambda action, feedrate: calls.append(
            ("needles", action, feedrate)
        ),
        run_move_to_xy=lambda x_mm, y_mm: calls.append(("move", x_mm, y_mm)),
        finish_stage_task=lambda: calls.append(("finish",)),
        read_voltage_sweep_now=lambda voltages_v: calls.append(("read", tuple(voltages_v)))
        or {
            "points": [{"measured_voltage_v": 0.01, "current_a": 2.0e-6}],
        },
        json_ready=lambda result: result,
        timestamp_utc=lambda: "2026-06-26T12:00:00+00:00",
        monotonic=lambda: next(monotonic_values),
        sleep=lambda delay: calls.append(("sleep", delay)),
        instrument_exception_response=lambda prefix, exc, **extra: {
            "accepted": False,
            "status_code": 409,
            "message": f"{prefix}: {exc}",
            "error_type": type(exc).__name__,
            **extra,
        },
        log_exception=lambda _message: None,
    )

    assert response["accepted"] is True
    assert response["contact"] == {"contact_number": 7, "label": "Pad 7"}
    assert response["voltages_v"] == [0.0, 0.1]
    assert response["iv_pairs"] == [{"voltage_v": 0.01, "current_a": 2.0e-6}]
    assert calls == [
        ("config", {}, (0.0, 0.1)),
        (
            "prepare",
            "keithley_2400_2182a",
            "Measurement instrument setup failed",
        ),
        ("feedrate", {"voltages_v": [0.0, "0.1"], "contact_number": 7}),
        ("begin", "API raw voltage sweep"),
        ("needles", "lift", 75.0),
        ("target", "point-7"),
        ("move", 1.25, 2.5),
        ("needles", "lower", 75.0),
        ("sleep", 0.2),
        ("read", (0.0, 0.1)),
        ("needles", "lift", 75.0),
        ("finish",),
    ]
