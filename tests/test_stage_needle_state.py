from probe_station_gui.stage_needle_state import (
    axis_a_ready_from_state,
    needle_contact_boundary_lowering,
    needle_zone_for_lowering,
    normalized_needles_zone,
)


def test_axis_a_ready_requires_known_raised_fresh_open_serial() -> None:
    assert axis_a_ready_from_state(
        needles_up=True,
        needles_known=True,
        controller_state_stale=False,
        serial_is_open=True,
    )
    assert not axis_a_ready_from_state(
        needles_up=True,
        needles_known=False,
        controller_state_stale=False,
        serial_is_open=True,
    )
    assert not axis_a_ready_from_state(
        needles_up=True,
        needles_known=True,
        controller_state_stale=True,
        serial_is_open=True,
    )


def test_normalized_needles_zone_preserves_known_zone_or_falls_back_to_raise_flag() -> None:
    assert normalized_needles_zone(True, known=True, zone=" lift ") == "lift"
    assert normalized_needles_zone(True, known=True, zone="bad") == "raise"
    assert normalized_needles_zone(False, known=True, zone=None) == "lower"
    assert normalized_needles_zone(True, known=False, zone="raise") is None


def test_needle_contact_boundary_lowering_requires_positive_contact_zone() -> None:
    assert needle_contact_boundary_lowering(
        down_lowering_mm=1.0,
        contact_zone_mm=0.1,
    ) == 0.9
    assert needle_contact_boundary_lowering(
        down_lowering_mm=1.0,
        contact_zone_mm=0.0,
    ) is None
    assert needle_contact_boundary_lowering(
        down_lowering_mm=None,
        contact_zone_mm=0.1,
    ) is None


def test_needle_zone_for_lowering_classifies_raise_lift_lower() -> None:
    kwargs = {
        "raise_lowering_mm": 0.0,
        "down_lowering_mm": 1.0,
        "contact_zone_mm": 0.1,
        "tolerance": 1e-3,
    }

    assert needle_zone_for_lowering(0.0, **kwargs) == "raise"
    assert needle_zone_for_lowering(0.5, **kwargs) == "lift"
    assert needle_zone_for_lowering(1.0, **kwargs) == "lower"
    assert needle_zone_for_lowering(0.95, **kwargs) is None
