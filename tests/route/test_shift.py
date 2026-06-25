from probe_station_gui.route.shift import (
    route_shift_from_stage_xy,
    route_shift_saved_message,
)


def test_route_shift_from_stage_xy_preserves_offset_and_status_message() -> None:
    offset_xy, message = route_shift_from_stage_xy((1.75, 2.25), (1.25, 2.5))

    assert offset_xy == (0.5, -0.25)
    assert message == "Route shift saved: dX=+0.5000 mm, dY=-0.2500 mm."


def test_route_shift_saved_message_preserves_signed_four_decimal_format() -> None:
    assert (
        route_shift_saved_message((-0.125, 0.0))
        == "Route shift saved: dX=-0.1250 mm, dY=+0.0000 mm."
    )
