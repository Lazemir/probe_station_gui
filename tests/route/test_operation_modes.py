from probe_station_gui.route.operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    normalize_route_operation_mode,
    route_operation_measure_enabled,
    route_operation_photo_enabled,
)


def test_normalize_route_operation_mode_accepts_legacy_aliases() -> None:
    assert normalize_route_operation_mode("measure") == ROUTE_OPERATION_MEASURE
    assert normalize_route_operation_mode("measurement") == ROUTE_OPERATION_MEASURE
    assert normalize_route_operation_mode("measure_only") == ROUTE_OPERATION_MEASURE
    assert normalize_route_operation_mode("photo") == ROUTE_OPERATION_PHOTO
    assert normalize_route_operation_mode("photo_only") == ROUTE_OPERATION_PHOTO
    assert normalize_route_operation_mode("image") == ROUTE_OPERATION_PHOTO
    assert normalize_route_operation_mode("capture") == ROUTE_OPERATION_PHOTO
    assert (
        normalize_route_operation_mode("photo_measure")
        == ROUTE_OPERATION_PHOTO_THEN_MEASURE
    )
    assert (
        normalize_route_operation_mode("photo+measure")
        == ROUTE_OPERATION_PHOTO_THEN_MEASURE
    )
    assert normalize_route_operation_mode("unknown") == ROUTE_OPERATION_MEASURE


def test_route_operation_flags_match_modes() -> None:
    assert route_operation_measure_enabled(ROUTE_OPERATION_MEASURE) is True
    assert route_operation_photo_enabled(ROUTE_OPERATION_MEASURE) is False

    assert route_operation_measure_enabled(ROUTE_OPERATION_PHOTO) is False
    assert route_operation_photo_enabled(ROUTE_OPERATION_PHOTO) is True

    assert route_operation_measure_enabled(ROUTE_OPERATION_PHOTO_THEN_MEASURE) is True
    assert route_operation_photo_enabled(ROUTE_OPERATION_PHOTO_THEN_MEASURE) is True
