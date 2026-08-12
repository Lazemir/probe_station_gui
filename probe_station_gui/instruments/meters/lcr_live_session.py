"""Qt-free lifecycle owner for one long-lived live LCR session."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from probe_station_gui.instruments.meters.lcr_session_backend import (
    DEFAULT_METER_TIMEOUT_MS,
    LCRMeterError,
    LCRSessionConfiguration,
    configure_live_session,
    open_configured_session,
    validate_required_resources,
    validate_session_identity,
)
from probe_station_gui.route.meter_config import ROUTE_METER_GWINSTEK


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LiveSessionState:
    """Immutable view of live configuration and lifecycle state."""

    configuration: LCRSessionConfiguration
    session: object | None
    connected_resource_name: str
    live_polling_enabled: bool
    stop_requested: bool
    backend_name: str
    instrument_id: str


class LiveLCRSession:
    """Own connection, polling-output, and cleanup state for one live meter."""

    def __init__(
        self,
        configuration: LCRSessionConfiguration,
        *,
        timeout_ms: int = DEFAULT_METER_TIMEOUT_MS,
        session_opener: Callable[..., object] = open_configured_session,
        session_configurer: Callable[
            [object, LCRSessionConfiguration], None
        ] = configure_live_session,
        identity_validator: Callable[
            [object, LCRSessionConfiguration], tuple[str, str]
        ] = validate_session_identity,
    ) -> None:
        self._configuration = configuration
        self._timeout_ms = int(timeout_ms)
        self._session_opener = session_opener
        self._session_configurer = session_configurer
        self._identity_validator = identity_validator
        self._session: object | None = None
        self._connected_resource_name = ""
        self._live_polling_enabled = True
        self._stop_polling = threading.Event()
        self._live_output_context: object | None = None
        self._manual_output_contexts: list[object] = []
        self._backend_name = ""
        self._instrument_id = ""

    def apply_configuration(self, configuration: LCRSessionConfiguration) -> None:
        self._configuration = configuration

    def snapshot(self) -> LiveSessionState:
        return LiveSessionState(
            configuration=self._configuration,
            session=self._session,
            connected_resource_name=self._connected_resource_name,
            live_polling_enabled=self._live_polling_enabled,
            stop_requested=self._stop_polling.is_set(),
            backend_name=self._backend_name,
            instrument_id=self._instrument_id,
        )

    def set_live_polling_enabled(self, enabled: bool) -> None:
        self._live_polling_enabled = bool(enabled)
        if not self._live_polling_enabled:
            self.request_stop()

    def request_stop(self) -> None:
        self._stop_polling.set()

    def resume_polling(self) -> bool:
        if not self._live_polling_enabled or self._session is None:
            return False
        self._stop_polling.clear()
        return True

    def connect(self, *, resume_polling: bool = True) -> LiveSessionState:
        validate_required_resources(self._configuration)
        self.disconnect()
        session = self._session_opener(
            self._configuration,
            timeout_ms=self._timeout_ms,
        )
        try:
            backend_name, instrument_id = self._identity_validator(
                session,
                self._configuration,
            )
            self._session_configurer(session, self._configuration)
        except BaseException:
            self._close_session(session, rejected=True)
            raise
        self._session = session
        self._connected_resource_name = self._configuration.connection_key
        self._backend_name = backend_name
        self._instrument_id = instrument_id
        if resume_polling:
            self._stop_polling.clear()
        return self.snapshot()

    def replace_for_reconfigure(self) -> LiveSessionState:
        validate_required_resources(self._configuration)
        self.disconnect()
        session = self._session_opener(
            self._configuration,
            timeout_ms=self._timeout_ms,
        )
        try:
            identify = getattr(session, "identify", None)
            instrument_id = str(identify()) if callable(identify) else ""
            backend_name = str(
                getattr(session, "backend_name", session.__class__.__name__)
            )
        except BaseException:
            self._close_session(session, rejected=True)
            raise
        self._session = session
        self._connected_resource_name = self._configuration.connection_key
        self._backend_name = backend_name
        self._instrument_id = instrument_id
        return self.snapshot()

    def finish_reconfigure(self) -> None:
        self.pause_polling()
        if self._configuration.meter_type == ROUTE_METER_GWINSTEK:
            self._session_configurer(
                self.connected_session(),
                self._configuration,
            )
        self._stop_polling.clear()

    def connected_session(self) -> object:
        session = self._session
        if session is None:
            raise LCRMeterError("Measurement instrument is not connected.")
        return session

    def poll_once(self) -> float | None:
        session = self._session
        if self._stop_polling.is_set() or session is None:
            return None
        if self._stop_polling.is_set() or session is not self._session:
            return None
        self._ensure_live_output_context(session)
        reader = getattr(session, "read_primary_value", None)
        if not callable(reader):
            raise LCRMeterError("Measurement instrument cannot read values.")
        primary_value = float(reader(trigger=True))
        if self._stop_polling.is_set() or session is not self._session:
            return None
        return primary_value

    def begin_output(self, enabled: bool) -> object | None:
        session = self.connected_session()
        self.pause_polling()
        output = getattr(session, "output", None)
        if not callable(output):
            return None
        context = output(bool(enabled))
        enter = getattr(context, "__enter__", None)
        if not callable(enter):
            return None
        try:
            enter()
        except BaseException:
            self._exit_output_context(context, suppress_errors=True)
            raise
        self._manual_output_contexts.append(context)
        return context

    def finish_output(self, context: object) -> bool:
        for index in range(len(self._manual_output_contexts) - 1, -1, -1):
            if self._manual_output_contexts[index] is context:
                del self._manual_output_contexts[index]
                self._exit_output_context(context)
                return True
        return False

    @staticmethod
    def _exit_output_context(
        context: object,
        *,
        suppress_errors: bool = False,
    ) -> None:
        try:
            exit_method = getattr(context, "__exit__", None)
            if callable(exit_method):
                exit_method(None, None, None)
        except BaseException:
            if not suppress_errors:
                raise
            logger.exception("Failed to clean up measurement instrument output context")

    def _ensure_live_output_context(self, session: object) -> None:
        if self._live_output_context is not None:
            return
        output = getattr(session, "output", None)
        if not callable(output):
            return
        context = output(True)
        enter = getattr(context, "__enter__", None)
        if not callable(enter):
            return
        try:
            enter()
        except BaseException:
            self._exit_output_context(context, suppress_errors=True)
            raise
        self._live_output_context = context

    def pause_polling(self) -> None:
        self._stop_polling.set()
        context = self._live_output_context
        self._live_output_context = None
        if context is not None:
            self._exit_output_context(context)

    def _drain_manual_output_contexts(self) -> None:
        while self._manual_output_contexts:
            context = self._manual_output_contexts[-1]
            self._exit_output_context(context, suppress_errors=True)
            self._manual_output_contexts.pop()

    def disconnect(self) -> None:
        session = self._session
        self._session = None
        self._connected_resource_name = ""
        self._backend_name = ""
        self._instrument_id = ""
        self._drain_manual_output_contexts()
        try:
            self.pause_polling()
        except Exception:  # pragma: no cover - best effort cleanup
            logger.exception(
                "Failed to close measurement instrument output context cleanly"
            )
        if session is not None:
            self._close_session(session, rejected=False)

    @staticmethod
    def _close_session(session: object, *, rejected: bool) -> None:
        try:
            closer = getattr(session, "close", None)
            if callable(closer):
                closer()
        except Exception:  # pragma: no cover - best effort cleanup
            subject = "rejected" if rejected else "measurement instrument"
            logger.exception("Failed to close %s session cleanly", subject)

    def abort_current_measurement(self) -> None:
        session = self._session
        abort = getattr(session, "abort_measurement", None)
        if callable(abort):
            abort()


__all__ = ["LiveLCRSession", "LiveSessionState"]
