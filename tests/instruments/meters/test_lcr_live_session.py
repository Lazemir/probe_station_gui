from __future__ import annotations

import threading
import unittest

from probe_station_gui.instruments.meters.lcr_live_session import (
    LiveLCRSession,
    LiveSessionState,
)
from probe_station_gui.instruments.meters.lcr_session_backend import (
    LCRSessionConfiguration,
)
from probe_station_gui.route.meter_config import ROUTE_METER_GWINSTEK


def _configuration() -> LCRSessionConfiguration:
    return LCRSessionConfiguration.normalized(
        meter_type=ROUTE_METER_GWINSTEK,
        resource_name="COM4",
        measurement_function="DCR",
        range_mode="AUTO",
        auto_range_enabled=True,
        impedance_range=3,
        dcr_range=4,
        frequency_hz=1000.0,
        level_mode="VOLTAGE",
        voltage_level_v=0.01,
        current_level_a=0.0001,
        source_resistance_ohm=30,
        aperture_rate="FAST",
        aperture_averages=1,
        trigger_source="INT",
        trigger_delay_s=0.0,
        bias_enabled=False,
        bias_level_v=0.0,
        monitor1="OFF",
        monitor2="OFF",
        alc_enabled=False,
        short_threshold_ohm=10.0,
        poll_interval_ms=250,
    )


class _LiveFakeSession:
    backend_name = "fake-live"

    def __init__(self, events: list[tuple[str, str]]) -> None:
        self.events = events
        self.close_count = 0
        self.abort_count = 0
        self.read_count = 0
        self.exit_count = 0

    def identify(self) -> str:
        self.events.append(("identify", threading.current_thread().name))
        return "fake-id"

    def read_primary_value(self, *, trigger: bool = False) -> float:
        self.read_count += 1
        self.events.append((f"read:{bool(trigger)}", threading.current_thread().name))
        return 42.0

    def output(self, enabled: bool = True):
        session = self

        class _OutputContext:
            def __enter__(self):
                session.events.append(
                    (f"enter:{bool(enabled)}", threading.current_thread().name)
                )
                return session

            def __exit__(self, _exc_type, _exc, _tb) -> None:
                session.exit_count += 1
                session.events.append(("exit", threading.current_thread().name))

        return _OutputContext()

    def abort_measurement(self) -> None:
        self.abort_count += 1

    def close(self) -> None:
        self.close_count += 1
        self.events.append(("close", threading.current_thread().name))


class LiveLCRSessionTests(unittest.TestCase):
    def test_connect_configures_and_publishes_only_committed_session(self) -> None:
        events: list[tuple[str, str]] = []
        session = _LiveFakeSession(events)
        configured: list[object] = []

        live = LiveLCRSession(
            _configuration(),
            session_opener=lambda _configuration, **_kwargs: session,
            session_configurer=lambda candidate, _configuration: configured.append(
                candidate
            ),
        )

        connection = live.connect()

        state = live.snapshot()
        self.assertIsInstance(state, LiveSessionState)
        self.assertIs(state.session, session)
        self.assertEqual(connection.backend_name, "fake-live")
        self.assertEqual(connection.instrument_id, "fake-id")
        self.assertEqual(configured, [session])
        self.assertFalse(state.stop_requested)

    def test_connect_failure_closes_uncommitted_session_once(self) -> None:
        events: list[tuple[str, str]] = []
        session = _LiveFakeSession(events)
        live = LiveLCRSession(
            _configuration(),
            session_opener=lambda _configuration, **_kwargs: session,
            session_configurer=lambda _candidate, _configuration: (_ for _ in ()).throw(
                RuntimeError("configure failed")
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "configure failed"):
            live.connect()

        self.assertIsNone(live.snapshot().session)
        self.assertEqual(session.close_count, 1)

    def test_polling_reuses_output_and_pause_exits_once(self) -> None:
        events: list[tuple[str, str]] = []
        session = _LiveFakeSession(events)
        live = LiveLCRSession(
            _configuration(),
            session_opener=lambda _configuration, **_kwargs: session,
            session_configurer=lambda _candidate, _configuration: None,
        )
        live.connect()

        first = live.poll_once()
        second = live.poll_once()
        live.pause_polling()
        live.pause_polling()

        self.assertEqual(first, 42.0)
        self.assertEqual(second, 42.0)
        self.assertEqual(session.read_count, 2)
        self.assertEqual(session.exit_count, 1)
        self.assertEqual(
            [event for event, _thread in events],
            ["identify", "enter:True", "read:True", "read:True", "exit"],
        )

    def test_disconnect_closes_session_even_when_output_exit_fails(self) -> None:
        events: list[tuple[str, str]] = []

        class _ExitFailure(_LiveFakeSession):
            def output(self, enabled: bool = True):
                session = self

                class _Context:
                    def __enter__(self):
                        return session

                    def __exit__(self, _exc_type, _exc, _tb) -> None:
                        session.exit_count += 1
                        raise RuntimeError("exit failed")

                return _Context()

        session = _ExitFailure(events)
        live = LiveLCRSession(
            _configuration(),
            session_opener=lambda _configuration, **_kwargs: session,
            session_configurer=lambda _candidate, _configuration: None,
        )
        live.connect()
        live.poll_once()

        live.disconnect()

        self.assertIsNone(live.snapshot().session)
        self.assertEqual(session.exit_count, 1)
        self.assertEqual(session.close_count, 1)

    def test_abort_uses_current_snapshot_without_polling_state_change(self) -> None:
        events: list[tuple[str, str]] = []
        session = _LiveFakeSession(events)
        live = LiveLCRSession(
            _configuration(),
            session_opener=lambda _configuration, **_kwargs: session,
            session_configurer=lambda _candidate, _configuration: None,
        )
        live.connect()

        live.abort_current_measurement()

        self.assertEqual(session.abort_count, 1)
        self.assertFalse(live.snapshot().stop_requested)

    def test_snapshot_replaces_scalar_forwarding_surface(self) -> None:
        live = LiveLCRSession(_configuration())

        state = live.snapshot()

        self.assertEqual(state.configuration, _configuration())
        self.assertIsNone(state.session)
        self.assertEqual(state.connected_resource_name, "")
        self.assertTrue(state.live_polling_enabled)
        self.assertTrue(
            {
                "meter_type",
                "connection_key",
                "connection_label",
                "poll_interval_ms",
                "short_threshold_ohm",
                "uses_bus_trigger",
                "is_short_reading",
                "is_connected",
                "connected_resource_name",
                "live_polling_enabled",
                "stop_requested",
                "session_snapshot",
            }.isdisjoint(type(live).__dict__)
        )


if __name__ == "__main__":
    unittest.main()
