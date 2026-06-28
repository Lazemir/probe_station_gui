"""Low-level VISA operation helpers for measurement instruments."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, Iterator


logger = logging.getLogger(__name__)


class VisaOperationError(RuntimeError):
    """Raised when a requested VISA operation cannot be dispatched."""


def session_visa_operation(
    session: object | None,
    role: str,
    operation: str,
    *,
    command: str | None,
    timeout_ms: int | None,
    read_termination: str | None,
    write_termination: str | None,
) -> object:
    if session is None:
        raise VisaOperationError("Measurement instrument is not connected.")
    resolver = getattr(session, "visa_handle_for_role", None)
    if not callable(resolver):
        raise VisaOperationError("Measurement instrument does not expose VISA roles.")
    try:
        handle = resolver(role)
    except KeyError as exc:
        raise VisaOperationError(str(exc)) from exc

    with temporary_visa_attributes(
        handle,
        timeout=timeout_ms,
        read_termination=read_termination,
        write_termination=write_termination,
    ):
        return visa_handle_operation(handle, operation, command=command)


@contextmanager
def temporary_visa_attributes(
    handle: object,
    *,
    timeout: object,
    read_termination: object,
    write_termination: object,
) -> Iterator[None]:
    previous: dict[str, object] = {}
    try:
        _set_temporary_visa_attribute(handle, previous, "timeout", timeout)
        _set_temporary_visa_attribute(
            handle,
            previous,
            "read_termination",
            read_termination,
        )
        _set_temporary_visa_attribute(
            handle,
            previous,
            "write_termination",
            write_termination,
        )
        yield
    finally:
        for name, value in previous.items():
            try:
                setattr(handle, name, value)
            except Exception:
                logger.debug("Failed to restore VISA attribute %s", name, exc_info=True)


def _set_temporary_visa_attribute(
    handle: object,
    previous: dict[str, object],
    name: str,
    value: object,
) -> None:
    if value is None or not hasattr(handle, name):
        return
    try:
        previous[name] = getattr(handle, name)
        setattr(handle, name, value)
    except Exception:
        logger.debug("VISA handle does not accept %s=%r", name, value, exc_info=True)


def visa_handle_operation(
    handle: object,
    operation: str,
    *,
    command: str | None,
) -> object:
    runner = _operation_runner(operation)
    return runner(handle, command)


def _operation_runner(operation: str) -> Callable[[object, str | None], object]:
    normalized = str(operation or "").strip().lower().replace("-", "_")
    if normalized == "ask":
        normalized = "query"
    try:
        return _OPERATION_RUNNERS[normalized]
    except KeyError as exc:
        raise VisaOperationError(f"Unsupported VISA operation: {operation}") from exc


def _write_operation(handle: object, command: str | None) -> None:
    if command is None:
        raise VisaOperationError("VISA write requires a command.")
    handle.write(str(command))


def _query_operation(handle: object, command: str | None) -> str:
    if command is None:
        raise VisaOperationError("VISA query requires a command.")
    query = getattr(handle, "query", None)
    if callable(query):
        return str(query(str(command))).strip()
    ask = getattr(handle, "ask", None)
    if callable(ask):
        return str(ask(str(command))).strip()
    raise VisaOperationError("VISA handle cannot run queries.")


def _read_operation(handle: object, _command: str | None) -> str:
    reader = getattr(handle, "read", None)
    if not callable(reader):
        raise VisaOperationError("VISA handle cannot read text.")
    return str(reader())


def _read_raw_operation(handle: object, _command: str | None) -> bytes:
    reader = getattr(handle, "read_raw", None)
    if callable(reader):
        data = reader()
    else:
        text_reader = getattr(handle, "read", None)
        if not callable(text_reader):
            raise VisaOperationError("VISA handle cannot read raw bytes.")
        data = str(text_reader()).encode("utf-8")
    return bytes(data)


def _clear_operation(handle: object, _command: str | None) -> None:
    clearer = getattr(handle, "clear", None)
    if not callable(clearer):
        clearer = getattr(handle, "device_clear", None)
    if not callable(clearer):
        visa_handle = getattr(handle, "visa_handle", None)
        clearer = getattr(visa_handle, "clear", None)
    if callable(clearer):
        clearer()


_OPERATION_RUNNERS: dict[str, Callable[[object, str | None], object]] = {
    "write": _write_operation,
    "query": _query_operation,
    "read": _read_operation,
    "read_raw": _read_raw_operation,
    "clear": _clear_operation,
}
