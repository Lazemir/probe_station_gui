"""QCoDeS instrument wrapper for the probe station API."""

from __future__ import annotations

from typing import Any, Mapping

from .client import ProbeStationClient
from .credentials import CredentialStore


try:
    from qcodes.instrument import Instrument, InstrumentChannel  # type: ignore
except Exception:
    try:
        from qcodes import Instrument  # type: ignore
        from qcodes.instrument.channel import InstrumentChannel  # type: ignore
    except Exception:
        Instrument = None  # type: ignore
        InstrumentChannel = None  # type: ignore

try:
    from qcodes.validators import Numbers  # type: ignore
except Exception:
    Numbers = None  # type: ignore


_AXES = ("X", "Y", "Z", "A", "B", "C")


if Instrument is not None and InstrumentChannel is not None:

    class ProbeStationStage(InstrumentChannel):  # type: ignore[misc]
        """Stage submodule for a probe station QCoDeS instrument."""

        def __init__(
            self,
            parent: Instrument,
            name: str,
            *,
            client: ProbeStationClient,
            default_feedrate: float | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(parent, name, **kwargs)
            self.client = client
            self.default_feedrate = default_feedrate
            self.add_parameter("state", get_cmd=self.get_state)
            self.add_parameter("position", get_cmd=self.get_position)
            for axis in _AXES:
                parameter_kwargs: dict[str, Any] = {
                    "get_cmd": lambda axis=axis: self.get_axis(axis),
                    "set_cmd": lambda value, axis=axis: self.set_axis(axis, value),
                }
                if axis in {"X", "Y", "Z"}:
                    parameter_kwargs["unit"] = "mm"
                if Numbers is not None:
                    parameter_kwargs["vals"] = Numbers()
                self.add_parameter(axis.lower(), **parameter_kwargs)

        def get_state(self) -> str:
            return str(self.client.stage_status().get("state", "unknown"))

        def get_position(self) -> dict[str, float]:
            return self.client.stage_position()

        def get_axis(self, axis: str) -> float | None:
            position = self.get_position()
            value = position.get(str(axis).upper())
            return float(value) if value is not None else None

        def set_axis(self, axis: str, value: float) -> dict[str, Any]:
            return self.move_to(
                {str(axis).upper(): float(value)},
                feedrate=self.default_feedrate,
            )

        def move_to(
            self,
            coordinates: Mapping[str, float] | None = None,
            *,
            feedrate: float | None = None,
            **axes: float,
        ) -> dict[str, Any]:
            return self.client.move_to(
                coordinates,
                feedrate=self.default_feedrate if feedrate is None else feedrate,
                **axes,
            )

        def move_by(
            self,
            coordinates: Mapping[str, float] | None = None,
            *,
            feedrate: float | None = None,
            **axes: float,
        ) -> dict[str, Any]:
            return self.client.move_by(
                coordinates,
                feedrate=self.default_feedrate if feedrate is None else feedrate,
                **axes,
            )

    class ProbeStationMeter(InstrumentChannel):  # type: ignore[misc]
        """Measurement-instrument submodule for a probe station QCoDeS driver."""

        def __init__(
            self,
            parent: Instrument,
            name: str,
            *,
            client: ProbeStationClient,
            **kwargs: Any,
        ) -> None:
            super().__init__(parent, name, **kwargs)
            self.client = client

        def configure(self, **configuration: Any) -> dict[str, Any]:
            return self.client.meter.configure(**configuration)

        def raw_sweep(
            self,
            voltages_v: list[float] | tuple[float, ...],
            **options: Any,
        ) -> dict[str, Any]:
            return self.client.meter.raw_sweep(voltages_v, **options)

        def visa_resources(self) -> dict[str, Any]:
            return self.client.meter.visa_resources()

        def visa(self, role: str = "meter.source", **options: Any):
            return self.client.meter.visa(role, **options)

        def source(self, **options: Any):
            return self.client.meter.source(**options)

        def voltmeter(self, **options: Any):
            return self.client.meter.voltmeter(**options)

        def ohmmeter(self, **options: Any):
            return self.client.meter.ohmmeter(**options)

    class ProbeStationRoute(InstrumentChannel):  # type: ignore[misc]
        """Route workflow submodule for notebook-owned measurements."""

        def __init__(
            self,
            parent: Instrument,
            name: str,
            *,
            client: ProbeStationClient,
            **kwargs: Any,
        ) -> None:
            super().__init__(parent, name, **kwargs)
            self.client = client

        def start_external(self, **options: Any):
            return self.client.route.start_external(**options)

        def status(self) -> dict[str, Any]:
            return self.client.route.status()

        def pause(self) -> dict[str, Any]:
            return self.client.route.pause()

        def resume(self) -> dict[str, Any]:
            return self.client.route.resume()

        def interrupt(self) -> dict[str, Any]:
            return self.client.route.interrupt()

        def stop(self) -> dict[str, Any]:
            return self.client.route.stop()

        def skip(self) -> dict[str, Any]:
            return self.client.route.skip()

        def remeasure(self) -> dict[str, Any]:
            return self.client.route.remeasure()

        def seek_current(self) -> dict[str, Any]:
            return self.client.route.seek_current()

        def submit_result(self, **result: Any) -> dict[str, Any]:
            return self.client.route.submit_result(**result)

        def download_artifact(self, artifact_id: str) -> bytes:
            return self.client.route.download_artifact(artifact_id)

    class ProbeStationInstrument(Instrument):  # type: ignore[misc]
        """QCoDeS-style driver for the probe station GUI API."""

        def __init__(
            self,
            name: str,
            *,
            base_url: str | None = None,
            api_key: str | None = None,
            credential_store: CredentialStore | None = None,
            client: ProbeStationClient | None = None,
            profile: str = "default",
            timeout_s: float = 10.0,
            default_stage_feedrate: float | None = None,
            default_feedrate: float | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(name, **kwargs)
            self.client = client or ProbeStationClient(
                base_url=base_url,
                api_key=api_key,
                credential_store=credential_store,
                profile=profile,
                timeout_s=timeout_s,
            )
            stage_feedrate = (
                default_feedrate
                if default_stage_feedrate is None
                else default_stage_feedrate
            )
            self.add_submodule(
                "stage",
                ProbeStationStage(
                    self,
                    "stage",
                    client=self.client,
                    default_feedrate=stage_feedrate,
                ),
            )
            self.add_submodule(
                "meter",
                ProbeStationMeter(
                    self,
                    "meter",
                    client=self.client,
                ),
            )
            self.add_submodule(
                "route",
                ProbeStationRoute(
                    self,
                    "route",
                    client=self.client,
                ),
            )

        def get_idn(self) -> dict[str, str | None]:
            return {
                "vendor": "Probe Station GUI",
                "model": "ProbeStation",
                "serial": None,
                "firmware": None,
            }

        def route_contacts(self) -> dict[str, Any]:
            return self.client.route_contacts()

        def move_to_contact(self, contact_number: int, **options: Any) -> dict[str, Any]:
            return self.client.move_to_contact(contact_number, **options)

        def contact_needles(
            self,
            contact_number: int,
            *,
            action: str = "lower",
            **options: Any,
        ) -> dict[str, Any]:
            return self.client.contact_needles(
                contact_number,
                action=action,
                **options,
            )

        def check_contact(self, contact_number: int, **options: Any) -> dict[str, Any]:
            return self.client.check_contact(contact_number, **options)

        def contact_seek(self, contact_number: int, **options: Any) -> dict[str, Any]:
            return self.client.contact_seek(contact_number, **options)

        def prepare_contact(self, contact_number: int, **options: Any) -> dict[str, Any]:
            return self.client.prepare_contact(contact_number, **options)

else:

    class ProbeStationStage:  # type: ignore[no-redef]
        """Placeholder when QCoDeS is not installed."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "ProbeStationStage requires qcodes. Install the qcodes-client "
                "extra or add qcodes to the Python environment."
            )

    class ProbeStationMeter:  # type: ignore[no-redef]
        """Placeholder when QCoDeS is not installed."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "ProbeStationMeter requires qcodes. Install the qcodes-client "
                "extra or add qcodes to the Python environment."
            )

    class ProbeStationRoute:  # type: ignore[no-redef]
        """Placeholder when QCoDeS is not installed."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "ProbeStationRoute requires qcodes. Install the qcodes-client "
                "extra or add qcodes to the Python environment."
            )

    class ProbeStationInstrument:  # type: ignore[no-redef]
        """Placeholder when QCoDeS is not installed."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "ProbeStationInstrument requires qcodes. Install the qcodes-client "
                "extra or add qcodes to the Python environment."
            )
