from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from probe_station_gui.shared import logging_config


def test_debug_logging_suppresses_hot_trace_records(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PROBE_STATION_GUI_TRACE_LOGS", raising=False)
    log_path = tmp_path / "probe-station-gui.log"

    logging_config.configure_logging(log_path, "DEBUG")
    try:
        logging.getLogger("probe_station_gui.stage.fluidnc_session").debug(
            "SERIAL TRACE stage_query_status write=?"
        )
        logging.getLogger("main").debug(
            "TIMING stage_position_changed position=%s",
            (1.0, 2.0, 3.0),
        )
        logging.getLogger("probe_station_gui.test").debug("ordinary debug record")
    finally:
        logging_config._stop_listener()

    text = log_path.read_text(encoding="utf-8")
    assert "ordinary debug record" in text
    assert "SERIAL TRACE" not in text
    assert "TIMING stage_position_changed" not in text


def test_logging_uses_rotating_file_handler(tmp_path) -> None:
    logging_config.configure_logging(tmp_path / "probe-station-gui.log", "INFO")
    try:
        handler = logging_config._LISTENER_HANDLER
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes > 0
        assert handler.backupCount > 0
    finally:
        logging_config._stop_listener()
