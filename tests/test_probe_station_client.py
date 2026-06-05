import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import probe_station_client.qcodes_driver as qcodes_driver
from probe_station_client.client import (
    AuthenticationError,
    PermissionDeniedError,
    ProbeStationClient,
)
from probe_station_client.credentials import (
    ENV_API_KEY,
    CredentialNotFoundError,
    CredentialStore,
)


class _FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout_s):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_s": timeout_s,
            }
        )
        if not self.responses:
            return 200, {}, b'{"accepted": true}'
        status_code, payload = self.responses.pop(0)
        return status_code, {}, json.dumps(payload).encode("utf-8")


class ProbeStationClientTest(unittest.TestCase):
    def test_stage_status_sends_bearer_token(self) -> None:
        transport = _FakeTransport((200, {"accepted": True, "state": "Idle"}))
        client = ProbeStationClient(
            base_url="http://probe.local",
            api_key="secret",
            transport=transport,
        )

        response = client.stage_status()

        self.assertEqual(response["state"], "Idle")
        self.assertEqual(
            transport.calls[0]["url"],
            "http://probe.local/api/v1/stage/status",
        )
        self.assertEqual(
            transport.calls[0]["headers"]["Authorization"],
            "Bearer secret",
        )

    def test_move_stage_sends_coordinates_and_mode(self) -> None:
        transport = _FakeTransport((200, {"accepted": True}))
        client = ProbeStationClient(
            base_url="http://probe.local",
            api_key="secret",
            transport=transport,
        )

        client.move_stage({"x": 1.0}, z=2.5, mode="relative", feedrate=30.0)

        payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(payload["coordinates"], {"X": 1.0, "Z": 2.5})
        self.assertEqual(payload["mode"], "relative")
        self.assertEqual(payload["feedrate"], 30.0)

    def test_api_errors_map_auth_and_permission_statuses(self) -> None:
        auth_transport = _FakeTransport(
            (401, {"detail": {"message": "bad key"}}),
        )
        permission_transport = _FakeTransport(
            (403, {"detail": {"message": "missing permission"}}),
        )

        auth_client = ProbeStationClient(
            api_key="bad",
            transport=auth_transport,
        )
        permission_client = ProbeStationClient(
            api_key="limited",
            transport=permission_transport,
        )

        with self.assertRaises(AuthenticationError):
            auth_client.stage_status()
        with self.assertRaises(PermissionDeniedError):
            permission_client.stage_status()

    def test_check_contact_endpoint(self) -> None:
        transport = _FakeTransport((200, {"accepted": True}))
        client = ProbeStationClient(api_key="secret", transport=transport)

        client.check_contact(7, check_sample_count=10, contact_settle_s=0.2)

        self.assertTrue(
            transport.calls[0]["url"].endswith(
                "/api/v1/route/contacts/7/check"
            )
        )
        payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(payload["check_sample_count"], 10)
        self.assertEqual(payload["contact_settle_s"], 0.2)

    def test_contact_seek_endpoint(self) -> None:
        transport = _FakeTransport((200, {"accepted": True}))
        client = ProbeStationClient(api_key="secret", transport=transport)

        client.contact_seek(7, contact_seek_range_mm=0.003)

        self.assertTrue(
            transport.calls[0]["url"].endswith(
                "/api/v1/route/contacts/7/seek"
            )
        )
        payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(payload["contact_seek_range_mm"], 0.003)

    def test_meter_subclient_configure_sends_code_auto_ranges(self) -> None:
        transport = _FakeTransport((200, {"accepted": True}))
        client = ProbeStationClient(api_key="secret", transport=transport)

        client.meter.configure(
            meter_type="keithley",
            measurement_voltage_v=0.03,
            ranges={
                "mode": "code_auto",
                "expected_resistance_ohm": 100_000.0,
            },
        )

        self.assertTrue(
            transport.calls[0]["url"].endswith("/api/v1/meter/configure")
        )
        payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(payload["meter_type"], "keithley")
        self.assertEqual(payload["ranges"]["mode"], "code_auto")
        self.assertEqual(payload["ranges"]["expected_resistance_ohm"], 100_000.0)

    def test_meter_methods_are_not_top_level_client_methods(self) -> None:
        self.assertNotIn("configure_meter", ProbeStationClient.__dict__)
        self.assertNotIn("raw_sweep", ProbeStationClient.__dict__)

    def test_meter_subclient_raw_sweep_endpoint(self) -> None:
        transport = _FakeTransport((200, {"accepted": True}))
        client = ProbeStationClient(api_key="secret", transport=transport)

        client.meter.raw_sweep(
            [-0.03, 0.03],
            contact_number=7,
            meter={"meter_type": "keithley"},
        )

        self.assertTrue(
            transport.calls[0]["url"].endswith("/api/v1/measurements/raw-sweep")
        )
        payload = json.loads(transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(payload["voltages_v"], [-0.03, 0.03])
        self.assertEqual(payload["contact_number"], 7)
        self.assertEqual(payload["meter"], {"meter_type": "keithley"})

    def test_prepare_contact_is_client_side_recipe_without_backend_prepare(self) -> None:
        transport = _FakeTransport(
            (200, {"accepted": True, "moved": True}),
            (200, {"accepted": True, "needle_action": "lower"}),
            (
                200,
                {
                    "accepted": True,
                    "contact_ok": True,
                    "measurement": {"status": "ok"},
                },
            ),
        )
        client = ProbeStationClient(api_key="secret", transport=transport)

        result = client.prepare_contact(
            7,
            check_sample_count=10,
            contact_settle_s=0.2,
        )

        urls = [call["url"] for call in transport.calls]
        self.assertTrue(urls[0].endswith("/api/v1/route/contacts/7/move"))
        self.assertTrue(urls[1].endswith("/api/v1/route/contacts/7/needles"))
        self.assertTrue(urls[2].endswith("/api/v1/route/contacts/7/check"))
        self.assertFalse(any(url.endswith("/contact") for url in urls))
        self.assertTrue(result["prepared"])
        self.assertTrue(result["accepted"])

    def test_prepare_contact_runs_seek_after_failed_check(self) -> None:
        transport = _FakeTransport(
            (200, {"accepted": True, "moved": True}),
            (200, {"accepted": True, "needle_action": "lower"}),
            (
                200,
                {
                    "accepted": True,
                    "contact_ok": False,
                    "measurement": {"status": "bad_contact"},
                },
            ),
            (
                200,
                {
                    "accepted": True,
                    "contact_ok": True,
                    "contact_found": True,
                    "measurement": {"status": "ok"},
                    "contact_seek": {"found": True},
                },
            ),
        )
        client = ProbeStationClient(api_key="secret", transport=transport)

        result = client.prepare_contact(7, contact_seek_range_mm=0.003)

        urls = [call["url"] for call in transport.calls]
        self.assertTrue(urls[3].endswith("/api/v1/route/contacts/7/seek"))
        self.assertTrue(result["prepared"])
        self.assertEqual(result["contact_seek"], {"found": True})

    def test_prepare_contact_can_seek_before_measurement(self) -> None:
        transport = _FakeTransport(
            (200, {"accepted": True, "needle_action": "raise"}),
            (200, {"accepted": True, "moved": True}),
            (200, {"accepted": True, "focus": {"z": 1.2}}),
            (200, {"accepted": True, "needle_action": "lower"}),
            (
                200,
                {
                    "accepted": True,
                    "contact_ok": True,
                    "contact_found": True,
                    "measurement": {
                        "status": "ok",
                        "contact_quality": {"median_ohm": 10_200.0},
                    },
                    "contact_seek": {"found": True},
                },
            ),
        )
        client = ProbeStationClient(api_key="secret", transport=transport)

        result = client.prepare_contact(
            7,
            raise_before_move=True,
            lift_before_move=False,
            focus_before_lower=True,
            focus_range_mm=0.02,
            seek_before_measurement=True,
            reference_resistance_ohm=10_000.0,
            resistance_relative_tolerance=0.05,
        )

        urls = [call["url"] for call in transport.calls]
        self.assertTrue(urls[0].endswith("/api/v1/route/contacts/7/needles"))
        self.assertTrue(urls[1].endswith("/api/v1/route/contacts/7/move"))
        self.assertTrue(urls[2].endswith("/api/v1/route/contacts/7/focus"))
        self.assertTrue(urls[3].endswith("/api/v1/route/contacts/7/needles"))
        self.assertTrue(urls[4].endswith("/api/v1/route/contacts/7/seek"))
        self.assertFalse(any(url.endswith("/check") for url in urls))
        self.assertTrue(result["prepared"])
        self.assertTrue(result["resistance_match"])
        self.assertAlmostEqual(result["measured_resistance_ohm"], 10_200.0)

    def test_prepare_contact_stops_after_failed_raise_before_move(self) -> None:
        transport = _FakeTransport(
            (200, {"accepted": False, "message": "Needles are busy."}),
        )
        client = ProbeStationClient(api_key="secret", transport=transport)

        result = client.prepare_contact(7, raise_before_move=True)

        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(
            transport.calls[0]["url"].endswith("/api/v1/route/contacts/7/needles")
        )
        self.assertFalse(result["prepared"])
        self.assertIn("Needles are busy", result["message"])

    def test_prepare_contact_retries_seek_on_reference_resistance_mismatch(self) -> None:
        transport = _FakeTransport(
            (200, {"accepted": True, "moved": True}),
            (200, {"accepted": True, "needle_action": "lower"}),
            (
                200,
                {
                    "accepted": True,
                    "contact_ok": True,
                    "measurement": {"status": "ok", "resistance_ohm": 30_000.0},
                },
            ),
            (
                200,
                {
                    "accepted": True,
                    "contact_ok": True,
                    "contact_found": True,
                    "measurement": {"status": "ok", "resistance_ohm": 25_000.0},
                },
            ),
            (
                200,
                {
                    "accepted": True,
                    "contact_ok": True,
                    "contact_found": True,
                    "measurement": {"status": "ok", "resistance_ohm": 10_500.0},
                    "contact_seek": {"found": True},
                },
            ),
        )
        client = ProbeStationClient(api_key="secret", transport=transport)

        result = client.prepare_contact(
            7,
            reference_resistance_ohm=10_000.0,
            resistance_relative_tolerance=0.10,
            seek_attempts=2,
        )

        urls = [call["url"] for call in transport.calls]
        seek_urls = [url for url in urls if url.endswith("/api/v1/route/contacts/7/seek")]
        self.assertEqual(len(seek_urls), 2)
        self.assertTrue(result["prepared"])
        self.assertTrue(result["resistance_match"])
        self.assertEqual(result["seek_attempts"], 2)
        self.assertAlmostEqual(result["resistance_relative_error"], 0.05)


class CredentialStoreTest(unittest.TestCase):
    def test_environment_variable_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(config_dir=tmpdir)
            store.save_api_key("file-key", backend="file")
            with patch.dict(os.environ, {ENV_API_KEY: "env-key"}):
                self.assertEqual(store.load_api_key(), "env-key")

    def test_file_backend_round_trip_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CredentialStore(config_dir=tmpdir)

            backend = store.save_api_key("psk_secret", backend="file")

            self.assertEqual(backend, "file")
            self.assertEqual(store.load_api_key(), "psk_secret")
            if os.name != "nt":
                mode = stat.S_IMODE(store.path.stat().st_mode)
                self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)
            store.delete_api_key()
            with self.assertRaises(CredentialNotFoundError):
                store.load_api_key()
            self.assertFalse(store.path.exists())

    def test_profiles_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first = CredentialStore(profile="first", config_dir=Path(tmpdir))
            second = CredentialStore(profile="second", config_dir=Path(tmpdir))

            first.save_api_key("first-key", backend="file")
            second.save_api_key("second-key", backend="file")

            self.assertEqual(first.load_api_key(), "first-key")
            self.assertEqual(second.load_api_key(), "second-key")


class ProbeStationInstrumentTest(unittest.TestCase):
    def test_stage_is_qcodes_submodule_with_axis_parameters(self) -> None:
        if qcodes_driver.Instrument is None:
            self.skipTest("qcodes is not installed")

        transport = _FakeTransport(
            (200, {"state": "Idle", "position": {"X": 1.25, "Y": 2.0}}),
            (200, {"state": "Idle", "position": {"X": 1.25, "Y": 2.0}}),
            (200, {"accepted": True}),
        )
        client = ProbeStationClient(api_key="secret", transport=transport)
        instrument = qcodes_driver.ProbeStationInstrument(
            f"probe_station_test_{uuid.uuid4().hex}",
            client=client,
            default_stage_feedrate=15.0,
        )
        try:
            self.assertIn("stage", instrument.submodules)
            self.assertIn("meter", instrument.submodules)
            self.assertIs(instrument.stage.client, client)
            self.assertIs(instrument.meter.client, client)
            self.assertEqual(instrument.stage.state(), "Idle")
            self.assertEqual(instrument.stage.x(), 1.25)

            instrument.stage.y(3.0)

            payload = json.loads(transport.calls[-1]["body"].decode("utf-8"))
            self.assertEqual(payload["coordinates"], {"Y": 3.0})
            self.assertEqual(payload["mode"], "absolute")
            self.assertEqual(payload["feedrate"], 15.0)
        finally:
            instrument.close()

    def test_stage_move_methods_live_on_stage_submodule(self) -> None:
        if qcodes_driver.Instrument is None:
            self.skipTest("qcodes is not installed")

        transport = _FakeTransport((200, {"accepted": True}))
        client = ProbeStationClient(api_key="secret", transport=transport)
        instrument = qcodes_driver.ProbeStationInstrument(
            f"probe_station_test_{uuid.uuid4().hex}",
            client=client,
        )
        try:
            self.assertNotIn("move_to", qcodes_driver.ProbeStationInstrument.__dict__)
            self.assertNotIn("move_by", qcodes_driver.ProbeStationInstrument.__dict__)

            instrument.stage.move_by(z=-0.1)

            payload = json.loads(transport.calls[-1]["body"].decode("utf-8"))
            self.assertEqual(payload["coordinates"], {"Z": -0.1})
            self.assertEqual(payload["mode"], "relative")
        finally:
            instrument.close()

    def test_meter_methods_live_on_meter_submodule(self) -> None:
        if qcodes_driver.Instrument is None:
            self.skipTest("qcodes is not installed")

        transport = _FakeTransport(
            (200, {"accepted": True}),
            (200, {"accepted": True}),
        )
        client = ProbeStationClient(api_key="secret", transport=transport)
        instrument = qcodes_driver.ProbeStationInstrument(
            f"probe_station_test_{uuid.uuid4().hex}",
            client=client,
        )
        try:
            self.assertNotIn(
                "configure_meter",
                qcodes_driver.ProbeStationInstrument.__dict__,
            )
            self.assertNotIn("raw_sweep", qcodes_driver.ProbeStationInstrument.__dict__)

            instrument.meter.configure(meter_type="keithley")
            instrument.meter.raw_sweep([-0.03, 0.03], contact_number=7)

            self.assertTrue(
                transport.calls[0]["url"].endswith("/api/v1/meter/configure")
            )
            self.assertTrue(
                transport.calls[1]["url"].endswith(
                    "/api/v1/measurements/raw-sweep"
                )
            )
            payload = json.loads(transport.calls[1]["body"].decode("utf-8"))
            self.assertEqual(payload["voltages_v"], [-0.03, 0.03])
            self.assertEqual(payload["contact_number"], 7)
        finally:
            instrument.close()


if __name__ == "__main__":
    unittest.main()
