import unittest

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
    def _client(self, *, move_callback=None, status_callback=None, command_callback=None):
        from fastapi.testclient import TestClient

        server = ProbeStationApiServer(
            move_callback=move_callback or (lambda request: {"accepted": True, **request}),
            status_callback=status_callback or (lambda: {"accepted": True, "state": "Idle"}),
            command_callback=command_callback,
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

    def test_contact_and_raw_sweep_endpoints_delegate_to_command_callback(self) -> None:
        calls = []

        def command_callback(request):
            calls.append(request)
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
        configure = client.post(
            "/api/v1/meter/configure",
            json={"meter_type": "keithley"},
        )
        sweep = client.post(
            "/api/v1/measurements/raw-sweep",
            json={"contact_number": 7, "voltages_v": [-0.1, 0.1]},
        )

        self.assertEqual(contacts.status_code, 200)
        self.assertEqual(move.status_code, 200)
        self.assertEqual(needles.status_code, 200)
        self.assertEqual(configure.status_code, 200)
        self.assertEqual(sweep.status_code, 200)
        self.assertEqual(
            [call["action"] for call in calls],
            [
                "list_contacts",
                "move_to_contact",
                "contact_needles",
                "configure_meter",
                "raw_voltage_sweep",
            ],
        )
        self.assertEqual(calls[1]["payload"]["contact_number"], 7)
        self.assertEqual(calls[4]["payload"]["voltages_v"], [-0.1, 0.1])


if __name__ == "__main__":
    unittest.main()

