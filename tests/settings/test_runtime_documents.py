from __future__ import annotations

import json
import logging

import pytest

from probe_station_gui.settings.runtime_documents import (
    CONTROLLER_STATE_FILENAME,
    METER_CONNECTION_STATE_FILENAME,
    SERIAL_CONNECTION_STATE_FILENAME,
    RuntimeStateDocuments,
)


def _documents(tmp_path) -> RuntimeStateDocuments:
    return RuntimeStateDocuments(tmp_path, logger=logging.getLogger(__name__))


def test_controller_document_round_trip_clear_and_path(tmp_path) -> None:
    documents = _documents(tmp_path)
    payload = {"machine_position": {"X": 1.25}, "label": "образец"}

    assert documents.load_controller_state() is None
    documents.save_controller_state(payload)

    path = tmp_path / CONTROLLER_STATE_FILENAME
    assert path.exists()
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert documents.load_controller_state() == payload

    documents.save_controller_state(None)
    assert not path.exists()
    assert documents.load_controller_state() is None


@pytest.mark.parametrize(
    ("filename", "loader", "fallback"),
    [
        (CONTROLLER_STATE_FILENAME, "load_controller_state", None),
        (SERIAL_CONNECTION_STATE_FILENAME, "load_serial_connection_state", {}),
        (METER_CONNECTION_STATE_FILENAME, "load_meter_connection_state", {}),
    ],
)
def test_malformed_and_non_object_documents_fall_back(
    tmp_path,
    caplog,
    filename: str,
    loader: str,
    fallback: object,
) -> None:
    documents = _documents(tmp_path)
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert getattr(documents, loader)() == fallback
    assert "Failed to load" in caplog.text

    caplog.clear()
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    assert getattr(documents, loader)() == fallback
    assert caplog.text == ""


def test_runtime_loaders_accept_utf8_bom(tmp_path) -> None:
    documents = _documents(tmp_path)
    for filename, loader in (
        (CONTROLLER_STATE_FILENAME, documents.load_controller_state),
        (SERIAL_CONNECTION_STATE_FILENAME, documents.load_serial_connection_state),
        (METER_CONNECTION_STATE_FILENAME, documents.load_meter_connection_state),
    ):
        path = tmp_path / filename
        path.write_bytes(b"\xef\xbb\xbf" + b'{"status": "connected"}')
        assert loader() == {"status": "connected"}


def test_serial_connection_payload_and_auto_connect_policy(tmp_path) -> None:
    documents = _documents(tmp_path)

    assert not documents.serial_auto_connect_enabled()
    documents.save_serial_connection_state(
        True,
        port="COM3",
        baud_rate=115200,
    )

    assert documents.load_serial_connection_state() == {
        "status": "connected",
        "port": "COM3",
        "baud_rate": 115200,
    }
    assert documents.serial_auto_connect_enabled()

    documents.save_serial_connection_state(False)
    assert documents.load_serial_connection_state() == {"status": "disconnected"}
    assert not documents.serial_auto_connect_enabled()


def test_meter_connection_payload_and_auto_connect_policy(tmp_path) -> None:
    documents = _documents(tmp_path)

    assert not documents.meter_auto_connect_enabled()
    documents.save_meter_connection_state(
        True,
        meter_type="keithley_2400_2182a",
        description="Keithley 2400 — образец",
    )

    assert documents.load_meter_connection_state() == {
        "status": "connected",
        "meter_type": "keithley_2400_2182a",
        "description": "Keithley 2400 — образец",
    }
    assert documents.meter_auto_connect_enabled()

    documents.save_meter_connection_state(False)
    assert documents.load_meter_connection_state() == {"status": "disconnected"}
    assert not documents.meter_auto_connect_enabled()


def test_config_directory_errors_propagate_for_every_runtime_document(
    tmp_path,
) -> None:
    blocked_config_dir = tmp_path / "config-is-a-file"
    blocked_config_dir.write_text("blocked", encoding="utf-8")
    documents = RuntimeStateDocuments(
        blocked_config_dir,
        logger=logging.getLogger(__name__),
    )

    with pytest.raises(OSError):
        documents.save_controller_state({"machine_position": {"X": 1.0}})
    with pytest.raises(OSError):
        documents.save_serial_connection_state(True, port="COM3")
    with pytest.raises(OSError):
        documents.save_meter_connection_state(True, meter_type="lcr")


def test_controller_target_write_errors_propagate_but_connections_warn(
    tmp_path,
    caplog,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for filename in (
        CONTROLLER_STATE_FILENAME,
        SERIAL_CONNECTION_STATE_FILENAME,
        METER_CONNECTION_STATE_FILENAME,
    ):
        (config_dir / filename).mkdir()
    documents = RuntimeStateDocuments(
        config_dir,
        logger=logging.getLogger(__name__),
    )

    with pytest.raises(OSError):
        documents.save_controller_state({"machine_position": {"X": 1.0}})

    with caplog.at_level(logging.WARNING):
        documents.save_serial_connection_state(True, port="COM3")
        documents.save_meter_connection_state(True, meter_type="lcr")

    assert "Failed to save serial connection state" in caplog.text
    assert "Failed to save measurement-instrument connection state" in caplog.text
