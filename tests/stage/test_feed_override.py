from probe_station_gui.stage.feed_override import (
    clamp_feed_override_percent,
    feed_override_payload_for_percent_change,
    feed_override_percent_for_feedrates,
)


def test_feed_override_percent_for_feedrates_clamps_controller_range() -> None:
    assert feed_override_percent_for_feedrates(
        100.0,
        250.0,
        min_feedrate=1.0,
        min_percent=10,
        max_percent=200,
    ) == 200
    assert feed_override_percent_for_feedrates(
        100.0,
        1.0,
        min_feedrate=1.0,
        min_percent=10,
        max_percent=200,
    ) == 10


def test_feed_override_payload_uses_grbl_realtime_steps() -> None:
    payload, applied = feed_override_payload_for_percent_change(
        100,
        137,
        min_percent=10,
        max_percent=200,
        reset_payload=b"\x90",
        plus_10_payload=b"\x91",
        minus_10_payload=b"\x92",
        plus_1_payload=b"\x93",
        minus_1_payload=b"\x94",
    )

    assert applied == 137
    assert payload == b"\x91\x91\x91" + b"\x93" * 7

    payload, applied = feed_override_payload_for_percent_change(
        137,
        82,
        min_percent=10,
        max_percent=200,
        reset_payload=b"\x90",
        plus_10_payload=b"\x91",
        minus_10_payload=b"\x92",
        plus_1_payload=b"\x93",
        minus_1_payload=b"\x94",
    )

    assert applied == 82
    assert payload == b"\x92" * 5 + b"\x94" * 5


def test_feed_override_payload_can_reset_before_delta() -> None:
    payload, applied = feed_override_payload_for_percent_change(
        150,
        120,
        reset_first=True,
        min_percent=10,
        max_percent=200,
        reset_payload=b"\x90",
        plus_10_payload=b"\x91",
        minus_10_payload=b"\x92",
        plus_1_payload=b"\x93",
        minus_1_payload=b"\x94",
    )

    assert applied == 120
    assert payload == b"\x90\x91\x91"


def test_clamp_feed_override_percent_rounds_before_clamping() -> None:
    assert clamp_feed_override_percent(12.6, min_percent=10, max_percent=200) == 13
    assert clamp_feed_override_percent(1, min_percent=10, max_percent=200) == 10
    assert clamp_feed_override_percent(999, min_percent=10, max_percent=200) == 200
