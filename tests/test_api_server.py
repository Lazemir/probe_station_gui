import unittest

from probe_station_gui.api_keys import (
    API_PERMISSION_ROUTE_MEASURE,
    API_PERMISSION_STAGE_READ,
    API_PERMISSION_STAGE_WRITE,
)
from probe_station_gui.api_server import _axis_targets_from_payload
from probe_station_gui.api_server import _coordinate_mode_from_payload
from probe_station_gui.api_server import _feedrate_from_payload
from probe_station_gui.api_server import _voltage_sweep_from_payload
from probe_station_gui.api_server import ProbeStationApiServer


class ApiServerPayloadTest(unittest.TestCase):
    def test_axis_targets_accept_top_level_axes(self) -> None:
        targets = _axis_targets_from_payload({"x": 1.25, "Y": 2.5})

        self.assertEqual(targets, {"X": 1.25, "Y": 2.5})

    def test_axis_targets_accept_coordinates_map(self) -> None:
        targets = _axis_targets_from_payload(
            {"coordinates": {"x": "1.25", "Z": 3.0}}
        )

        self.assertEqual(targets, {"X": 1.25, "Z": 3.0})

    def test_top_level_axis_overrides_coordinates_map(self) -> None:
        targets = _axis_targets_from_payload(
            {"coordinates": {"X": 1.25, "Y": 2.5}, "x": 9.0}
        )

        self.assertEqual(targets, {"X": 9.0, "Y": 2.5})

    def test_move_payload_accepts_mode_and_feedrate(self) -> None:
        payload = {"mode": "relative", "feedrate_mm_min": "123.4"}

        self.assertEqual(_coordinate_mode_from_payload(payload), "G91")
        self.assertEqual(_feedrate_from_payload(payload), 123.4)

    def test_voltage_sweep_payload_accepts_finite_array(self) -> None:
        self.assertEqual(
            _voltage_sweep_from_payload({"voltages_v": ["-0.1", 0, 0.1]}),
            [-0.1, 0.0, 0.1],
        )

    def test_voltage_sweep_payload_rejects_missing_or_nonfinite_values(self) -> None:
        with self.assertRaises(ValueError):
            _voltage_sweep_from_payload({})
        with self.assertRaises(ValueError):
            _voltage_sweep_from_payload({"voltages_v": [float("nan")]})


class ApiServerHttpTest(unittest.TestCase):
    def _client(
        self,
        *,
        move_callback=None,
        status_callback=None,
        command_callback=None,
        auth_callback=None,
    ):
        from fastapi.testclient import TestClient

        server = ProbeStationApiServer(
            move_callback=move_callback or (lambda request: {"accepted": True, **request}),
            status_callback=status_callback or (lambda: {"accepted": True, "state": "Idle"}),
            command_callback=command_callback,
            auth_callback=auth_callback,
        )
        app, _uvicorn = server._create_app()
        return TestClient(app)

    def test_health_and_status_endpoints_return_callback_payloads(self) -> None:
        client = self._client(
            status_callback=lambda: {
                "accepted": True,
                "state": "Idle",
                "position": {"X": 1.0},
            }
        )

        self.assertEqual(client.get("/health").json(), {"status": "ok"})
        docs_index = client.get("/")
        self.assertEqual(docs_index.status_code, 200)
        self.assertIn('href="/docs"', docs_index.text)
        self.assertIn('href="/redoc"', docs_index.text)

        response = client.get("/api/v1/stage/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"accepted": True, "state": "Idle", "position": {"X": 1.0}},
        )

    def test_move_endpoint_passes_json_body_targets_to_callback(self) -> None:
        calls = []

        client = self._client(
            move_callback=lambda request: (
                calls.append(request) or {"accepted": True, **request}
            )
        )

        response = client.post(
            "/api/v1/stage/move",
            json={
                "coordinates": {"x": "1.25"},
                "z": 3.0,
                "mode": "relative",
                "feedrate": 45.0,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            calls,
            [{"targets": {"X": 1.25, "Z": 3.0}, "mode": "G91", "feedrate": 45.0}],
        )
        self.assertEqual(response.json()["targets"], {"X": 1.25, "Z": 3.0})
        self.assertEqual(response.json()["mode"], "G91")
        self.assertEqual(response.json()["feedrate"], 45.0)

    def test_move_endpoint_rejects_empty_or_rejected_moves(self) -> None:
        client = self._client(
            move_callback=lambda _request: {
                "accepted": False,
                "status_code": 409,
                "message": "busy",
            }
        )

        empty = client.post("/api/v1/stage/move", json={})
        rejected = client.post("/api/v1/stage/move", json={"x": 1.0})

        self.assertEqual(empty.status_code, 400)
        self.assertEqual(
            empty.json()["detail"],
            {"message": "Provide at least one target coordinate."},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json()["detail"]["message"], "busy")

    def test_contact_raw_sweep_and_route_session_endpoints_delegate(self) -> None:
        calls = []

        def command_callback(request):
            calls.append(request)
            if request["action"] == "route_session_artifact":
                return {
                    "accepted": True,
                    "artifact_id": request["payload"]["artifact_id"],
                    "filename": "contact.jpg",
                    "content_type": "image/jpeg",
                    "data": b"jpeg-bytes",
                }
            return {"accepted": True, "echo": request}

        client = self._client(command_callback=command_callback)

        contacts = client.get("/api/v1/route/contacts")
        move = client.post(
            "/api/v1/route/contacts/7/move",
            json={"lower_needles": True},
        )
        needles = client.post(
            "/api/v1/route/contacts/7/needles",
            json={"action": "lift"},
        )
        check = client.post(
            "/api/v1/route/contacts/7/check",
            json={"check_sample_count": 10},
        )
        seek = client.post(
            "/api/v1/route/contacts/7/seek",
            json={"contact_seek_range_mm": 0.003},
        )
        configure = client.post(
            "/api/v1/meter/configure",
            json={"meter_type": "keithley"},
        )
        sweep = client.post(
            "/api/v1/measurements/raw-sweep",
            json={"contact_number": 7, "voltages_v": [-0.1, 0.1]},
        )
        session = client.post(
            "/api/v1/route/sessions",
            json={"initial_measurement_count": 10},
        )
        status = client.get("/api/v1/route/sessions/current")
        action = client.post(
            "/api/v1/route/sessions/current/actions",
            json={"action": "pause"},
        )
        result = client.post(
            "/api/v1/route/sessions/current/result",
            json={"status": "ok", "summary": {"points": 31}},
        )
        session_seek = client.post("/api/v1/route/sessions/current/seek", json={})
        artifact = client.get("/api/v1/route/sessions/current/artifacts/a1")

        self.assertEqual(contacts.status_code, 200)
        self.assertEqual(move.status_code, 200)
        self.assertEqual(needles.status_code, 200)
        self.assertEqual(check.status_code, 200)
        self.assertEqual(seek.status_code, 200)
        self.assertEqual(configure.status_code, 200)
        self.assertEqual(sweep.status_code, 200)
        self.assertEqual(session.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(action.status_code, 200)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(session_seek.status_code, 200)
        self.assertEqual(artifact.status_code, 200)
        self.assertEqual(artifact.content, b"jpeg-bytes")
        self.assertEqual(artifact.headers["x-artifact-id"], "a1")
        self.assertEqual(
            [call["action"] for call in calls],
            [
                "list_contacts",
                "move_to_contact",
                "contact_needles",
                "check_contact",
                "contact_seek",
                "configure_meter",
                "raw_voltage_sweep",
                "start_route_session",
                "route_session_status",
                "route_session_action",
                "route_session_result",
                "route_session_seek",
                "route_session_artifact",
            ],
        )
        self.assertEqual(calls[1]["payload"]["contact_number"], 7)
        self.assertEqual(calls[3]["payload"]["check_sample_count"], 10)
        self.assertEqual(calls[4]["payload"]["contact_seek_range_mm"], 0.003)
        self.assertEqual(calls[6]["payload"]["voltages_v"], [-0.1, 0.1])
        self.assertEqual(calls[7]["payload"]["initial_measurement_count"], 10)
        self.assertEqual(calls[9]["payload"]["action"], "pause")
        self.assertEqual(calls[10]["payload"]["summary"], {"points": 31})

    def test_authenticated_endpoints_require_matching_permissions(self) -> None:
        auth_calls = []

        def auth_callback(api_key, permission):
            auth_calls.append((api_key, permission))
            if api_key != "secret":
                return {
                    "accepted": False,
                    "status_code": 401,
                    "message": "bad key",
                }
            if permission == API_PERMISSION_STAGE_WRITE:
                return {
                    "accepted": False,
                    "status_code": 403,
                    "message": "missing permission",
                }
            return {"accepted": True}

        client = self._client(
            auth_callback=auth_callback,
            command_callback=lambda request: {"accepted": True, "echo": request},
        )

        missing = client.get("/api/v1/stage/status")
        status = client.get(
            "/api/v1/stage/status",
            headers={"Authorization": "Bearer secret"},
        )
        move = client.post(
            "/api/v1/stage/move",
            headers={"Authorization": "Bearer secret"},
            json={"x": 1.0},
        )
        seek = client.post(
            "/api/v1/route/contacts/3/seek",
            headers={"X-API-Key": "secret"},
            json={"check_sample_count": 10},
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(move.status_code, 403)
        self.assertEqual(seek.status_code, 200)
        self.assertIn(("secret", API_PERMISSION_STAGE_READ), auth_calls)
        self.assertIn(("secret", API_PERMISSION_STAGE_WRITE), auth_calls)
        self.assertIn(("secret", API_PERMISSION_ROUTE_MEASURE), auth_calls)


if __name__ == "__main__":
    unittest.main()

