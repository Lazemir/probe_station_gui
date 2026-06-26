import pytest

from probe_station_gui.design.contact_navigation import (
    ApiContactNeedlesPlan,
    ApiContactNeedlesRequest,
    ApiMoveToContactPlan,
    ApiMoveToContactRequest,
    api_contact_needles_plan,
    api_contact_needles_request,
    api_contact_needles_stage_error_response,
    api_contact_needles_success_response,
    api_move_to_contact_plan,
    api_move_to_contact_request,
    api_move_to_contact_stage_error_response,
    api_move_to_contact_success_response,
)


def test_move_and_contact_needles_require_positive_contact_number() -> None:
    expected = {
        "accepted": False,
        "status_code": 400,
        "message": "Provide a positive contact_number.",
    }

    move_response = api_move_to_contact_request(
        {},
        default_needle_feedrate=30.0,
        min_feedrate=5.0,
    )
    needles_response = api_contact_needles_request(
        {"contact_number": 0},
        default_needle_feedrate=30.0,
        min_feedrate=5.0,
    )

    assert move_response == expected
    assert needles_response == expected


def test_move_plan_returns_context_rejection_as_is() -> None:
    rejection = {
        "accepted": False,
        "status_code": 409,
        "message": "Design registration is required before using contacts.",
    }

    result = api_move_to_contact_plan(
        {"contact_number": 7},
        contact_context=lambda _contact_number: rejection,
        default_needle_feedrate=25.0,
        min_feedrate=4.0,
    )

    assert result is rejection


def test_move_request_uses_boolean_aliases_and_defaults() -> None:
    request = api_move_to_contact_request(
        {
            "contact": "7",
            "lower": "yes",
            "lift_before_move": "0",
            "lift_after": 1,
        },
        default_needle_feedrate=18.0,
        min_feedrate=4.0,
    )

    assert request == ApiMoveToContactRequest(
        contact_number=7,
        lower_needles=True,
        lift_before_move=False,
        lift_after=True,
        contact_settle_s=0.2,
        needle_feedrate_mm_min=18.0,
    )


def test_move_request_parses_contact_settle_aliases_and_defaults() -> None:
    default_request = api_move_to_contact_request(
        {"contact_number": 7},
        default_needle_feedrate=18.0,
        min_feedrate=4.0,
    )
    alias_request = api_move_to_contact_request(
        {"contact_number": 7, "settle_s": "0.5"},
        default_needle_feedrate=18.0,
        min_feedrate=4.0,
    )

    assert default_request.contact_settle_s == pytest.approx(0.2)
    assert alias_request.contact_settle_s == pytest.approx(0.5)


def test_move_request_propagates_contact_settle_parser_errors() -> None:
    with pytest.raises(ValueError, match=r"contact_settle_s must be at least 0.0\."):
        api_move_to_contact_request(
            {"contact_number": 7, "contact_settle_s": -0.1},
            default_needle_feedrate=18.0,
            min_feedrate=4.0,
        )

    with pytest.raises(ValueError, match=r"Invalid numeric value for contact_settle_s\."):
        api_move_to_contact_request(
            {"contact_number": 7, "settle_s": "bad"},
            default_needle_feedrate=18.0,
            min_feedrate=4.0,
        )


def test_move_request_applies_default_and_minimum_needle_feedrate() -> None:
    default_request = api_move_to_contact_request(
        {"contact_number": 7},
        default_needle_feedrate=33.0,
        min_feedrate=8.0,
    )
    bounded_request = api_move_to_contact_request(
        {"contact_number": 7, "feedrate": 3.0},
        default_needle_feedrate=33.0,
        min_feedrate=8.0,
    )

    assert default_request.needle_feedrate_mm_min == pytest.approx(33.0)
    assert bounded_request.needle_feedrate_mm_min == pytest.approx(8.0)


def test_move_plan_preserves_point_contact_and_request() -> None:
    point = object()
    contact = {"contact_number": 7, "label": "Pad 7"}

    result = api_move_to_contact_plan(
        {"contact_number": 7, "lower_needles": True},
        contact_context=lambda _contact_number: {
            "accepted": True,
            "point": point,
            "contact": contact,
        },
        default_needle_feedrate=22.0,
        min_feedrate=4.0,
    )

    assert result == ApiMoveToContactPlan(
        point=point,
        contact=contact,
        request=ApiMoveToContactRequest(
            contact_number=7,
            lower_needles=True,
            lift_before_move=True,
            lift_after=False,
            contact_settle_s=0.2,
            needle_feedrate_mm_min=22.0,
        ),
    )


def test_move_success_response_reports_lowered_needles_and_target() -> None:
    plan = ApiMoveToContactPlan(
        point=object(),
        contact={"contact_number": 7, "label": "Pad 7"},
        request=ApiMoveToContactRequest(
            contact_number=7,
            lower_needles=True,
            lift_before_move=True,
            lift_after=True,
            contact_settle_s=0.2,
            needle_feedrate_mm_min=55.0,
        ),
    )

    response = api_move_to_contact_success_response(
        plan,
        timestamp_utc="2026-06-26T12:00:00+00:00",
        route_offset_xy=(0.25, -0.5),
        target_stage_xy=(1.5, 2.5),
        needles_lowered=True,
    )

    assert response == {
        "accepted": True,
        "message": "Moved to contact 7 and lowered needles.",
        "timestamp_utc": "2026-06-26T12:00:00+00:00",
        "contact": {"contact_number": 7, "label": "Pad 7"},
        "needles_lowered": True,
        "lifted_before_move": True,
        "lifted_after": True,
        "needle_feedrate_mm_min": 55.0,
        "route_offset_xy": {"dx_mm": 0.25, "dy_mm": -0.5},
        "target_stage_xy": {"x_mm": 1.5, "y_mm": 2.5},
    }


def test_move_success_response_without_lowering_needles_ends_with_period() -> None:
    plan = ApiMoveToContactPlan(
        point=object(),
        contact={"contact_number": 9},
        request=ApiMoveToContactRequest(
            contact_number=9,
            lower_needles=False,
            lift_before_move=False,
            lift_after=True,
            contact_settle_s=0.0,
            needle_feedrate_mm_min=40.0,
        ),
    )

    response = api_move_to_contact_success_response(
        plan,
        timestamp_utc="2026-06-26T12:00:00+00:00",
        route_offset_xy=(0.0, 0.0),
        target_stage_xy=(3.0, 4.0),
        needles_lowered=False,
    )

    assert response["message"] == "Moved to contact 9."
    assert response["needles_lowered"] is False
    assert response["lifted_after"] is False


def test_move_stage_error_response_keeps_contact() -> None:
    contact = {"contact_number": 7, "label": "Pad 7"}

    assert api_move_to_contact_stage_error_response(
        "Stage is busy.",
        contact=contact,
    ) == {
        "accepted": False,
        "status_code": 409,
        "message": "Stage is busy.",
        "contact": contact,
    }


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("raise", "raise"),
        (" lift ", "lift"),
        ("up", "lift"),
        ("lower", "lower"),
        ("down", "lower"),
    ],
)
def test_contact_needles_request_normalizes_action(
    action: str,
    expected: str,
) -> None:
    request = api_contact_needles_request(
        {"contact_number": 7, "action": action},
        default_needle_feedrate=12.0,
        min_feedrate=4.0,
    )

    assert request == ApiContactNeedlesRequest(
        contact_number=7,
        action=expected,
        needle_feedrate_mm_min=12.0,
    )


def test_contact_needles_request_rejects_unknown_action() -> None:
    response = api_contact_needles_request(
        {"contact_number": 7, "action": "park"},
        default_needle_feedrate=12.0,
        min_feedrate=4.0,
    )

    assert response == {
        "accepted": False,
        "status_code": 400,
        "message": "Needle action must be lower, lift, or raise.",
    }


def test_contact_needles_plan_success_and_error_helpers() -> None:
    contact = {"contact_number": 8, "label": "Pad 8"}
    plan = api_contact_needles_plan(
        {"contact_number": 8, "action": "up", "feedrate_mm_min": 2.0},
        contact_context=lambda _contact_number: {
            "accepted": True,
            "point": object(),
            "contact": contact,
        },
        default_needle_feedrate=12.0,
        min_feedrate=4.0,
    )

    assert plan == ApiContactNeedlesPlan(
        contact=contact,
        request=ApiContactNeedlesRequest(
            contact_number=8,
            action="lift",
            needle_feedrate_mm_min=4.0,
        ),
    )
    assert api_contact_needles_success_response(
        plan,
        timestamp_utc="2026-06-26T10:05:00+00:00",
    ) == {
        "accepted": True,
        "message": "Needle action 'lift' completed.",
        "timestamp_utc": "2026-06-26T10:05:00+00:00",
        "contact": contact,
        "needle_action": "lift",
        "needle_feedrate_mm_min": 4.0,
    }
    assert api_contact_needles_stage_error_response(
        "Limit switch active.",
        contact=contact,
    ) == {
        "accepted": False,
        "status_code": 409,
        "message": "Limit switch active.",
        "contact": contact,
    }
