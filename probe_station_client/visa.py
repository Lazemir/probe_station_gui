"""VISA-like client transports backed by the probe-station API."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


class RemoteVisaInstrument:
    """Small pyvisa-like handle for a station-owned instrument role."""

    def __init__(
        self,
        client: Any,
        role: str,
        *,
        timeout_ms: int = 10_000,
    ) -> None:
        self._client = client
        self.resource_name = str(role).strip()
        self.timeout = int(timeout_ms)
        self.read_termination = "\n"
        self.write_termination = "\n"

    def write(self, command: str) -> None:
        self._request("write", command=str(command))

    def query(self, command: str) -> str:
        response = self._request("query", command=str(command))
        return str(response.get("response", ""))

    def ask(self, command: str) -> str:
        return self.query(command)

    def read(self) -> str:
        response = self._request("read")
        return str(response.get("response", ""))

    def read_raw(self) -> bytes:
        return self._client._request_bytes(
            "POST",
            self._path("read-raw"),
            self._payload(),
        )

    def clear(self) -> None:
        self._request("clear")

    def close(self) -> None:
        """Keep the server-owned resource open; this only closes the client view."""

    def _request(self, operation: str, *, command: str | None = None) -> dict[str, Any]:
        return self._client._request(
            "POST",
            self._path(operation.replace("_", "-")),
            self._payload(command=command),
        )

    def _payload(self, *, command: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timeout_ms": int(self.timeout),
            "read_termination": self.read_termination,
            "write_termination": self.write_termination,
        }
        if command is not None:
            payload["command"] = command
        return payload

    def _path(self, operation: str) -> str:
        role = quote(self.resource_name, safe="")
        return f"/api/v1/visa/resources/{role}/{operation}"


class RemoteVisaResourceManager:
    """Resource manager that opens remote station instrument roles."""

    def __init__(
        self,
        client: Any,
        *,
        timeout_ms: int = 10_000,
    ) -> None:
        self._client = client
        self.timeout_ms = int(timeout_ms)

    def open_resource(self, address: str, **_kwargs: Any) -> RemoteVisaInstrument:
        return RemoteVisaInstrument(
            self._client,
            str(address),
            timeout_ms=self.timeout_ms,
        )

    def close(self) -> None:
        """The API server owns the real VISA resources."""


def build_remote_ohmmeter(
    client: Any,
    *,
    timeout_ms: int = 10_000,
):
    """Build the configured station ohmmeter from remote VISA roles."""

    resources_response = client.meter.visa_resources()
    resources = resources_response.get("resources")
    if not isinstance(resources, list):
        resources = []
    roles = {
        str(item.get("role") or "")
        for item in resources
        if isinstance(item, dict)
    }
    if "meter.source" not in roles:
        raise RuntimeError("The station did not expose a meter.source VISA role.")
    voltmeter_role = "meter.voltmeter" if "meter.voltmeter" in roles else None
    meter_type = str(resources_response.get("meter_type") or "").strip().lower()
    if meter_type and "keithley" not in meter_type:
        raise RuntimeError(
            "No high-level remote ohmmeter driver is available for "
            f"meter_type={meter_type!r}."
        )

    from probe_station_measure import Keithley2400SourceMeter, Keithley2400With2182A

    resource_manager = RemoteVisaResourceManager(
        client,
        timeout_ms=timeout_ms,
    )
    if voltmeter_role is None:
        return Keithley2400SourceMeter(
            "meter.source",
            timeout_ms=timeout_ms,
            resource_manager=resource_manager,
        )

    return Keithley2400With2182A(
        "meter.source",
        voltmeter_role,
        timeout_ms=timeout_ms,
        resource_manager=resource_manager,
    )
