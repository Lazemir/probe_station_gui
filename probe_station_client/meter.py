"""Meter and VISA access for the probe-station API client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from .visa import RemoteVisaInstrument, build_remote_ohmmeter


if TYPE_CHECKING:
    from .client import ProbeStationClient


class ProbeStationMeterClient:
    """Measurement-instrument namespace for the probe station API client."""

    def __init__(self, client: ProbeStationClient) -> None:
        self._client = client

    def configure(self, **configuration: Any) -> dict[str, Any]:
        return self._client._request(
            "POST",
            "/api/v1/meter/configure",
            dict(configuration),
        )

    def raw_sweep(
        self,
        voltages_v: list[float] | tuple[float, ...],
        **options: Any,
    ) -> dict[str, Any]:
        payload = dict(options)
        payload["voltages_v"] = [float(value) for value in voltages_v]
        return self._client._request(
            "POST",
            "/api/v1/measurements/raw-sweep",
            payload,
        )

    def visa_resources(self) -> dict[str, Any]:
        return self._client._request("GET", "/api/v1/visa/resources")

    def visa(
        self,
        role: str = "meter.source",
        *,
        timeout_ms: int | None = None,
    ) -> RemoteVisaInstrument:
        return RemoteVisaInstrument(
            self._client,
            role,
            timeout_ms=int(timeout_ms or self._client.timeout_s * 1000),
        )

    def source(self, *, timeout_ms: int | None = None) -> RemoteVisaInstrument:
        return self.visa("meter.source", timeout_ms=timeout_ms)

    def voltmeter(
        self,
        *,
        timeout_ms: int | None = None,
        required: bool = True,
    ) -> RemoteVisaInstrument | None:
        resources = self.visa_resources().get("resources")
        if not isinstance(resources, list):
            resources = []
        roles = {
            str(item.get("role") or "")
            for item in resources
            if isinstance(item, Mapping)
        }
        if "meter.voltmeter" not in roles:
            if required:
                raise RuntimeError("The station did not expose meter.voltmeter.")
            return None
        return self.visa("meter.voltmeter", timeout_ms=timeout_ms)

    def ohmmeter(self, *, timeout_ms: int = 10_000):
        return build_remote_ohmmeter(self._client, timeout_ms=timeout_ms)
