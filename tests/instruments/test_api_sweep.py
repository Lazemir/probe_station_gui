from __future__ import annotations

import pytest

from probe_station_gui.instruments.api_sweep import (
    ApiRawVoltageSweepRequestError,
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


def test_contact_plan_preserves_non_dict_contact_payload() -> None:
    request = api_raw_voltage_sweep_request_from_payload(
        {"voltages_v": [0.0], "contact_number": 5}
    )

    plan = api_raw_voltage_sweep_contact_plan(
        request,
        context_result={
            "accepted": True,
            "point": "point-5",
            "contact": "Pad 5",
        },
    )

    assert plan == (
        api_raw_voltage_sweep_contact_plan(
            request,
            context_result={
                "accepted": True,
                "point": "point-5",
                "contact": "Pad 5",
            },
        )
    )
    assert plan.contact == "Pad 5"


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
