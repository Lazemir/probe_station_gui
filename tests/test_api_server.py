import unittest

from probe_station_gui.api_server import _axis_targets_from_payload
from probe_station_gui.api_server import _coordinate_mode_from_payload
from probe_station_gui.api_server import _feedrate_from_payload
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


class ApiServerHttpTest(unittest.TestCase):
    def _client(self, *, move_callback=None, status_callback=None):
        from fastapi.testclient import TestClient

        server = ProbeStationApiServer(
            move_callback=move_callback or (lambda request: {"accepted": True, **request}),
            status_callback=status_callback or (lambda: {"accepted": True, "state": "Idle"}),
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


if __name__ == "__main__":
    unittest.main()

