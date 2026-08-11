"""Public ownership contracts for probe-station client domains."""

from __future__ import annotations

import ast
import inspect
import textwrap

import probe_station_client as public_api
from probe_station_client import client as core_client
from probe_station_client.camera import CameraFrame, ProbeStationCameraClient
from probe_station_client.meter import ProbeStationMeterClient
from probe_station_client.route import (
    ProbeStationApiRouteControlClient,
    ProbeStationRouteClient,
    ProbeStationRouteSession,
    RouteReadyContact,
)
from probe_station_client.visa import RemoteVisaInstrument


def test_camera_domain_owns_its_public_types() -> None:
    assert public_api.CameraFrame is CameraFrame
    assert public_api.ProbeStationCameraClient is ProbeStationCameraClient
    assert CameraFrame.__module__ == "probe_station_client.camera"
    assert ProbeStationCameraClient.__module__ == "probe_station_client.camera"
    assert not hasattr(core_client, "CameraFrame")
    assert not hasattr(core_client, "ProbeStationCameraClient")


def test_composition_root_builds_the_canonical_camera_client() -> None:
    client = core_client.ProbeStationClient(
        api_key="secret",
        transport=lambda *_args: (200, {}, b"{}"),
    )

    assert type(client.camera) is ProbeStationCameraClient


def test_meter_domain_owns_its_public_type() -> None:
    assert public_api.ProbeStationMeterClient is ProbeStationMeterClient
    assert ProbeStationMeterClient.__module__ == "probe_station_client.meter"
    assert not hasattr(core_client, "ProbeStationMeterClient")


def test_composition_root_builds_typed_meter_and_visa_clients() -> None:
    client = core_client.ProbeStationClient(
        api_key="secret",
        timeout_s=2.5,
        transport=lambda *_args: (200, {}, b"{}"),
    )

    visa = client.meter.visa("meter.source")

    assert type(client.meter) is ProbeStationMeterClient
    assert type(visa) is RemoteVisaInstrument
    assert visa.timeout == 2500


def test_route_domain_owns_its_public_types() -> None:
    public_types = (
        ("ProbeStationRouteClient", ProbeStationRouteClient),
        ("ProbeStationRouteSession", ProbeStationRouteSession),
        ("RouteReadyContact", RouteReadyContact),
    )
    for name, canonical_type in public_types:
        assert getattr(public_api, name) is canonical_type
        assert canonical_type.__module__ == "probe_station_client.route"
        assert not hasattr(core_client, name)
    assert ProbeStationApiRouteControlClient.__module__ == "probe_station_client.route"
    assert not hasattr(core_client, "ProbeStationApiRouteControlClient")
    assert not hasattr(public_api, "ProbeStationApiRouteControlClient")


def test_composition_root_builds_canonical_route_clients_and_session_identity() -> None:
    client = core_client.ProbeStationClient(
        api_key="secret",
        transport=lambda *_args: (
            200,
            {},
            b'{"accepted": true, "session_id": "route-7"}',
        ),
    )

    session = client.route.start_external()

    assert type(client.api_route_control) is ProbeStationApiRouteControlClient
    assert type(client.route) is ProbeStationRouteClient
    assert type(session) is ProbeStationRouteSession
    assert session.route is client.route
    assert session.client is client
    assert session.session_id == "route-7"


def test_ready_contact_deduplication_key_tracks_all_three_identity_parts() -> None:
    source = textwrap.dedent(inspect.getsource(ProbeStationRouteSession.iter_ready))
    function = ast.parse(source).body[0]
    annotations = {
        node.target.id: ast.unparse(node.annotation)
        for node in ast.walk(function)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
    }

    assert annotations["yielded"] == "set[tuple[str, int, int]]"
