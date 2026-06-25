from probe_station_gui.route_operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    normalize_route_operation_mode,
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
