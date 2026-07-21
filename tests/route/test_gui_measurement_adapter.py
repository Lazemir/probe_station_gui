from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from probe_station_gui.instruments.meters.lcr import LCRMeterError
from probe_station_gui.route.gui_measurement_adapter import (
    GuiRouteEventBindings,
    setup_gui_route_meter,
)
from probe_station_gui.route.measurement_records import (
    RouteContactHeightRecord,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)
from probe_station_gui.route.meter_config import RouteMeterConfiguration
from probe_station_gui.route.point_execution import PointPhotoSettings
from probe_station_gui.route.runtime_presenter import RouteRuntimePresentationSink


def test_gui_route_events_keep_one_point_snapshot_and_use_next_point_update(
    tmp_path: Path,
) -> None:
    first = PointPhotoSettings(
        autofocus_enabled=True,
        autofocus_range_mm=0.03,
        output_dir=str(tmp_path / "first"),
        csv_path=str(tmp_path / "first.csv"),
    )
    second = PointPhotoSettings(
        autofocus_enabled=False,
        autofocus_range_mm=0.07,
        output_dir=str(tmp_path / "second"),
        csv_path=str(tmp_path / "second.csv"),
    )
    calls: list[tuple[object, ...]] = []

    def capture_photo(
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        settings: PointPhotoSettings,
        focus_result: object | None = None,
    ) -> str:
        calls.append(("photo", settings, focus_result, position, total))
        return settings.output_dir

    def autofocus(
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        settings: PointPhotoSettings,
    ) -> object:
        calls.append(("focus", settings, position, total))
        return point

    def contact_height(
        record: RouteContactHeightRecord,
        position: int,
        total: int,
        *,
        csv_path: str | Path,
    ) -> None:
        calls.append(("height", record, str(csv_path), position, total))

    bindings = GuiRouteEventBindings(
        status=lambda message: calls.append(("status", message)),
        progress=lambda position, total, point: calls.append(
            ("progress", position, total, point)
        ),
        record=lambda record, position, total: calls.append(
            ("record", record, position, total)
        ),
        capture_photo=capture_photo,
        autofocus=autofocus,
        photo_record=lambda record, position, total: calls.append(
            ("photo_record", record, position, total)
        ),
        contact_height=contact_height,
        contact_photo=lambda point, record, position, total, saved: calls.append(
            ("contact_photo", point, record, position, total, saved)
        ),
        pre_contact_photo=lambda point, position, total: calls.append(
            ("pre_contact", point, position, total)
        ),
        result=lambda record, position, total, saved: calls.append(
            ("result", record, position, total, saved)
        ),
        waiting=lambda waiting: calls.append(("waiting", waiting)),
    )
    events = bindings.events()
    point = cast(RouteMeasurementPoint, object())
    record = cast(RouteMeasurementRecord, object())
    height = cast(RouteContactHeightRecord, object())

    assert events.status is not None
    assert events.point_photo is not None
    assert events.point_photo_focus is not None
    assert events.point_contact_height is not None
    assert events.result is not None
    events.status("start")
    assert events.point_photo(point, 1, 3, "focus-1", first) == first.output_dir
    assert events.point_photo_focus(point, 1, 3, first) is point
    events.point_contact_height(height, 1, 3, first)
    assert events.point_photo(point, 2, 3, None, second) == second.output_dir
    events.result(record, 1, 3, True)

    assert calls == [
        ("status", "start"),
        ("photo", first, "focus-1", 1, 3),
        ("focus", first, 1, 3),
        ("height", height, first.csv_path, 1, 3),
        ("photo", second, None, 2, 3),
        ("result", record, 1, 3, True),
    ]


class _Meter:
    def __init__(self, *, connected: bool, error: bool = False) -> None:
        self.connected = connected
        self.error = error
        self.calls: list[str] = []

    def is_connected(self) -> bool:
        return self.connected

    def apply_route_meter_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        self.calls.append("connected")
        if self.error:
            raise LCRMeterError("boom")

    def apply_route_meter_runtime_configuration(
        self,
        configuration: RouteMeterConfiguration,
    ) -> None:
        self.calls.append("runtime")
        if self.error:
            raise LCRMeterError("boom")


class _Presenter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def set_running(self, active: bool) -> bool:
        self.calls.append(("running", active))
        return True

    def set_status(self, message: str) -> bool:
        self.calls.append(("status", message))
        return True


@pytest.mark.parametrize(
    ("connected", "expected"),
    [(True, "connected"), (False, "runtime")],
)
def test_gui_route_meter_uses_connection_appropriate_configuration(
    connected: bool,
    expected: str,
) -> None:
    meter = _Meter(connected=connected)
    configuration = cast(RouteMeterConfiguration, object())

    result = setup_gui_route_meter(
        controller=meter,
        configuration=configuration,
        measure_enabled=True,
        show_status=lambda message, timeout: None,
        presenter=cast(RouteRuntimePresentationSink, _Presenter()),
    )

    assert result is meter
    assert meter.calls == [expected]


def test_gui_route_meter_failure_rejects_start_and_updates_presentation() -> None:
    meter = _Meter(connected=True, error=True)
    presenter = _Presenter()
    status: list[tuple[str, int]] = []

    result = setup_gui_route_meter(
        controller=meter,
        configuration=cast(RouteMeterConfiguration, object()),
        measure_enabled=True,
        show_status=lambda message, timeout: status.append((message, timeout)),
        presenter=cast(RouteRuntimePresentationSink, presenter),
    )

    message = "Route measurement instrument setup failed: boom"
    assert result is None
    assert status == [(message, 8000)]
    assert presenter.calls == [("running", False), ("status", message)]
