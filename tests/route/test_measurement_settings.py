import json
import types
from pathlib import Path

from probe_station_gui.route.measurement_settings import RouteMeasurementSettingsStore


def test_load_returns_empty_for_missing_invalid_or_non_object_settings(tmp_path: Path) -> None:
    store = RouteMeasurementSettingsStore(tmp_path)

    assert store.load() == {}

    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == {}

    store.path.write_text("{broken", encoding="utf-8")
    assert store.load() == {}


def test_current_point_and_session_active_preserve_legacy_settings_rules() -> None:
    assert RouteMeasurementSettingsStore.current_point({"current_point": "3"}) == 3
    assert RouteMeasurementSettingsStore.current_point({"start_point": 4}) == 4
    assert RouteMeasurementSettingsStore.current_point({"current_point": 0}) is None
    assert RouteMeasurementSettingsStore.current_point({"current_point": "bad"}) is None

    assert RouteMeasurementSettingsStore.session_active(
        {"measurement_session_active": True}
    )
    assert RouteMeasurementSettingsStore.session_active({"measurement_pending": True})
    assert not RouteMeasurementSettingsStore.session_active(
        {
            "measurement_session_active": False,
            "measurement_pending": True,
        }
    )


def test_settings_match_route_uses_count_path_and_name() -> None:
    route_path = Path("route.json").resolve()
    route = types.SimpleNamespace(
        name="route-a",
        path=route_path,
        points=[object(), object()],
    )

    assert RouteMeasurementSettingsStore.settings_match_route(
        {"session_route_point_count": 2, "session_route_path": str(route_path)},
        route,
    )
    assert not RouteMeasurementSettingsStore.settings_match_route(
        {"session_route_point_count": 3},
        route,
    )
    assert not RouteMeasurementSettingsStore.settings_match_route(
        {"session_route_path": str(route_path.with_name("other.json"))},
        route,
    )
    assert RouteMeasurementSettingsStore.settings_match_route(
        {"session_route_name": "route-a"},
        route,
    )
    assert not RouteMeasurementSettingsStore.settings_match_route(
        {"session_route_name": "route-b"},
        route,
    )


def test_save_current_point_and_pending_merge_existing_profile_data(tmp_path: Path) -> None:
    store = RouteMeasurementSettingsStore(tmp_path)
    store.path.write_text(
        json.dumps({"csv_path": "old.csv", "operation_mode": "photo"}),
        encoding="utf-8",
    )

    store.save_current_point(7, session_active=True)
    data = store.load()

    assert data["csv_path"] == "old.csv"
    assert data["operation_mode"] == "photo"
    assert data["current_point"] == 7
    assert data["start_point"] == 7
    assert data["measurement_session_active"] is True
    assert data["measurement_pending"] is True

    store.save_pending(False)
    data = store.load()

    assert data["current_point"] == 7
    assert data["measurement_session_active"] is False
    assert data["measurement_pending"] is False


def test_save_session_metadata_merges_route_and_configuration(tmp_path: Path) -> None:
    store = RouteMeasurementSettingsStore(tmp_path)
    route = types.SimpleNamespace(
        name="route-a",
        path=tmp_path / "route.json",
        points=[object(), object()],
    )
    configuration = types.SimpleNamespace(
        csv_path="new.csv",
        operation_mode="photo_then_measure",
        photo_output_dir="photos",
        current_point=5,
    )

    store.save_session_metadata(
        route=route,
        configuration=configuration,
        session_active=True,
    )
    data = store.load()

    assert data["session_route_name"] == "route-a"
    assert data["session_route_point_count"] == 2
    assert data["session_route_path"] == str(tmp_path / "route.json")
    assert data["csv_path"] == "new.csv"
    assert data["operation_mode"] == "photo_then_measure"
    assert data["photo_output_dir"] == "photos"
    assert data["current_point"] == 5
    assert data["start_point"] == 5
    assert data["measurement_session_active"] is True
    assert data["measurement_pending"] is True
