"""QCoDeS driver for the GW Instek LCR-76200 / LCR-6000 remote interface.

Primary source:
- ``manuals/LCR-6000_User_Manual_Rev_1_07_20230822.pdf``

The manual in this repository documents the LCR-6000 remote-control command
set. The connected bench instrument identifies itself as ``LCR-76200`` and
answers to the same SCPI commands, so this driver targets the LCR-76200 while
remaining compatible with the documented LCR-6000 command family.

Example
-------
```python
from probe_station_gui.instruments.meters.gwinstek_lcr_76200 import GWInstekLCR76200

lcr = GWInstekLCR76200("lcr", "COM4", terminator="\r\n")
lcr.function("DCR")
lcr.range_mode("AUTO")
print(lcr.read_resistance())
lcr.close()
```
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from probe_station_gui.instruments.meters.lcr_helpers import (
    format_source_level_value,
    normalize_resource_name,
)

try:  # pragma: no cover - optional dependency at runtime
    import pyvisa
except ImportError:  # pragma: no cover - optional dependency at runtime
    pyvisa = None

try:  # pragma: no cover - optional dependency at runtime
    from qcodes.instrument.visa import VisaInstrument
    from qcodes.validators import Enum, Ints, Numbers
except ImportError:  # pragma: no cover - optional dependency at runtime
    VisaInstrument = None
    Enum = None
    Ints = None
    Numbers = None


SUPPORTED_FUNCTIONS = (
    "Cs-Rs",
    "Cs-D",
    "Cp-Rp",
    "Cp-D",
    "Lp-Rp",
    "Lp-Q",
    "Ls-Rs",
    "Ls-Q",
    "Rs-Q",
    "Rp-Q",
    "R-X",
    "DCR",
    "Z-thr",
    "Z-thd",
    "Z-D",
    "Z-Q",
)
SUPPORTED_MONITORS = (
    "OFF",
    "Z",
    "D",
    "Q",
    "THR",
    "THD",
    "R",
    "X",
    "G",
    "B",
    "Y",
    "ABS",
    "PER",
    "VAC",
    "IAC",
)
SUPPORTED_TRIGGER_SOURCES = ("INT", "MAN", "EXT", "BUS")
SUPPORTED_RANGE_MODES = ("HOLD", "AUTO", "NOM")
SUPPORTED_APERTURE_RATES = ("SLOW", "MED", "FAST")
SUPPORTED_SOURCE_RESISTANCES = (30, 50, 100)

DEFAULT_TIMEOUT_S = 5.0
DEFAULT_BAUD_RATE = 115200
DEFAULT_TERMINATOR = "\r\n"
FLOAT_PATTERN = re.compile(
    r"^\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?(?:[a-zA-Z]+)?\s*$"
)


def _parse_float(token: str) -> float:
    return float(token.strip())


def _is_number_token(token: str) -> bool:
    return bool(FLOAT_PATTERN.fullmatch(token or ""))


def _parse_on_off(token: str) -> bool:
    value = (token or "").strip().upper()
    if value == "ON":
        return True
    if value == "OFF":
        return False
    raise ValueError(f"Unsupported ON/OFF token: {token!r}")


def _format_bool(value: bool) -> str:
    return "ON" if bool(value) else "OFF"


def _format_upper_token(value: object) -> str:
    return str(value).strip().upper()


def _parse_bias_response(token: str) -> Optional[float]:
    value = (token or "").strip().upper()
    if value == "OFF":
        return None
    return float(value.rstrip("V"))


def _parse_time_seconds(token: str) -> float:
    value = (token or "").strip().lower()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000.0
    if value.endswith("s"):
        return float(value[:-1])
    return float(value)


@dataclass(frozen=True)
class FetchReading:
    """Parsed response of a FETCH-family query."""

    primary: Optional[float] = None
    secondary: Optional[float] = None
    monitor1: Optional[float] = None
    monitor2: Optional[float] = None
    comparator_tokens: tuple[str, ...] = ()

    @property
    def resistance_ohm(self) -> float:
        """Return the primary reading when the instrument is in DCR mode."""

        if self.primary is None:
            raise ValueError("FETCH response does not contain a primary value.")
        return self.primary


def parse_fetch_response(response: str) -> FetchReading:
    """Parse any FETCH-family response into numeric values plus comparator tail."""

    tokens = [token.strip() for token in (response or "").strip().split(",")]
    numeric_values: list[float] = []
    comparator_tokens: list[str] = []
    tail_started = False
    for token in tokens:
        if token == "":
            continue
        if not tail_started and _is_number_token(token):
            numeric_values.append(_parse_float(token))
            continue
        tail_started = True
        comparator_tokens.append(token)
    values = numeric_values + [None] * max(0, 4 - len(numeric_values))
    return FetchReading(
        primary=values[0],
        secondary=values[1],
        monitor1=values[2],
        monitor2=values[3],
        comparator_tokens=tuple(comparator_tokens),
    )


def parse_idn_response(response: str) -> dict[str, Optional[str]]:
    """Parse the standard ``*IDN?`` response into QCoDeS-style fields."""

    parts = [part.strip() for part in (response or "").strip().split(",")]
    if len(parts) >= 4:
        vendor = ", ".join(part for part in parts[3:] if part).strip() or None
        parts = [parts[0], parts[1], parts[2], vendor]
    while len(parts) < 4:
        parts.append(None)
    return {
        "vendor": parts[3],
        "model": parts[0],
        "serial": parts[2],
        "firmware": parts[1],
    }


if VisaInstrument is not None and Enum is not None and Ints is not None and Numbers is not None:

    class GWInstekLCR76200(VisaInstrument):
        """QCoDeS VISA driver for the GW Instek LCR-76200 / LCR-6000 family."""

        def __init__(
            self,
            name: str,
            address: str,
            *,
            terminator: str = DEFAULT_TERMINATOR,
            timeout: float = DEFAULT_TIMEOUT_S,
            baud_rate: int = DEFAULT_BAUD_RATE,
            **kwargs,
        ) -> None:
            normalized_address = normalize_resource_name(address)
            super().__init__(
                name=name,
                address=normalized_address,
                terminator=terminator,
                timeout=timeout,
                **kwargs,
            )
            self._configure_serial_transport(
                baud_rate=baud_rate,
                terminator=terminator,
            )
            self._install_parameters()
            self.connect_message()

        @classmethod
        def from_serial_port(
            cls,
            name: str,
            port: str,
            **kwargs,
        ) -> "GWInstekLCR76200":
            """Convenience constructor that accepts ``COM4``-style names."""

            return cls(name=name, address=normalize_resource_name(port), **kwargs)

        def _configure_serial_transport(self, *, baud_rate: int, terminator: str) -> None:
            handle = self.visa_handle
            handle.baud_rate = int(baud_rate)
            handle.data_bits = 8
            handle.stop_bits = pyvisa.constants.StopBits.one
            handle.parity = pyvisa.constants.Parity.none
            handle.read_termination = terminator
            handle.write_termination = terminator
            try:
                handle.flow_control = pyvisa.constants.VI_ASRL_FLOW_NONE
            except Exception:
                pass
            try:
                handle.set_visa_attribute(
                    pyvisa.constants.VI_ATTR_ASRL_FLOW_CNTRL,
                    pyvisa.constants.VI_ASRL_FLOW_NONE,
                )
            except Exception:
                pass

        def _add_uppercase_enum_parameter(
            self,
            name: str,
            *,
            set_cmd: str,
            get_cmd: str,
            values: tuple[str, ...],
        ) -> None:
            self.add_parameter(
                name,
                set_cmd=set_cmd,
                get_cmd=get_cmd,
                vals=Enum(*values),
                get_parser=_format_upper_token,
                set_parser=_format_upper_token,
            )

        def _install_parameters(self) -> None:
            self.add_parameter(
                "function",
                set_cmd="FUNC {}",
                get_cmd="FUNC?",
                vals=Enum(*SUPPORTED_FUNCTIONS),
            )
            self.add_parameter(
                "impedance_autorange_enabled",
                set_cmd="FUNC:IMP:AUTO {}",
                get_cmd="FUNC:IMP:AUTO?",
                set_parser=_format_bool,
                get_parser=_parse_on_off,
            )
            self.add_parameter(
                "impedance_range",
                set_cmd="FUNC:IMP:RANG {}",
                get_cmd="FUNC:IMP:RANG?",
                get_parser=int,
                vals=Ints(0, 8),
            )
            self.add_parameter(
                "dcr_range",
                set_cmd="FUNC:DCR:RANG {}",
                get_cmd="FUNC:DCR:RANG?",
                get_parser=int,
                vals=Ints(0, 8),
            )
            self._add_uppercase_enum_parameter(
                "range_mode",
                set_cmd="FUNC:RANG:AUTO {}",
                get_cmd="FUNC:RANG:AUTO?",
                values=SUPPORTED_RANGE_MODES,
            )
            self._add_uppercase_enum_parameter(
                "monitor1_mode",
                set_cmd="FUNC:MON1 {}",
                get_cmd="FUNC:MON1?",
                values=SUPPORTED_MONITORS,
            )
            self._add_uppercase_enum_parameter(
                "monitor2_mode",
                set_cmd="FUNC:MON2 {}",
                get_cmd="FUNC:MON2?",
                values=SUPPORTED_MONITORS,
            )
            self.add_parameter(
                "frequency_hz",
                unit="Hz",
                set_cmd="FREQ {}",
                get_cmd="FREQ?",
                get_parser=float,
                vals=Numbers(min_value=20.0, max_value=10_000_000.0),
            )
            self.add_parameter(
                "voltage_level_v",
                unit="V",
                set_cmd="LEV:VOLT {}",
                get_cmd="LEV:VOLT?",
                set_parser=format_source_level_value,
                get_parser=float,
                vals=Numbers(min_value=0.0),
            )
            self.add_parameter(
                "current_level_a",
                unit="A",
                set_cmd="LEV:CURR {}",
                get_cmd="LEV:CURR?",
                set_parser=format_source_level_value,
                get_parser=float,
                vals=Numbers(min_value=0.0),
            )
            self.add_parameter(
                "source_resistance_ohm",
                unit="ohm",
                set_cmd="LEV:SRES {}",
                get_cmd="LEV:SRES?",
                get_parser=int,
                vals=Enum(*SUPPORTED_SOURCE_RESISTANCES),
            )
            self.add_parameter(
                "alc_enabled",
                set_cmd="LEV:ALC {}",
                get_cmd="LEV:ALC?",
                set_parser=_format_bool,
                get_parser=_parse_on_off,
            )
            self.add_parameter(
                "level_mode",
                get_cmd="LEV:MODE?",
                get_parser=lambda value: value.strip().lower(),
            )
            self._add_uppercase_enum_parameter(
                "aperture_rate",
                set_cmd="APER {}",
                get_cmd="APER:RATE?",
                values=SUPPORTED_APERTURE_RATES,
            )
            self.add_parameter(
                "aperture_averages",
                set_cmd="APER {}",
                get_cmd="APER:AVG?",
                get_parser=int,
                vals=Ints(0, 256),
            )
            self._add_uppercase_enum_parameter(
                "trigger_source",
                set_cmd="TRIG:SOUR {}",
                get_cmd="TRIG:SOUR?",
                values=SUPPORTED_TRIGGER_SOURCES,
            )
            self.add_parameter(
                "trigger_delay_s",
                unit="s",
                set_cmd="TRIG:DLY {}",
                get_cmd="TRIG:DLY?",
                get_parser=_parse_time_seconds,
                vals=Numbers(min_value=0.0, max_value=60.0),
            )
            self.add_parameter(
                "bias_level_v",
                unit="V",
                set_cmd="BIAS {}",
                get_cmd="BIAS?",
                get_parser=_parse_bias_response,
                vals=Numbers(min_value=-2.5, max_value=2.5),
            )

        def get_idn(self) -> dict[str, Optional[str]]:
            return parse_idn_response(self.ask_raw("*IDN?"))

        def disable_bias(self) -> None:
            self.write("BIAS OFF")

        def force_trigger(self) -> None:
            """Execute an immediate trigger. The manual requires BUS mode."""

            self.write("TRIG")

        def trigger_fetch(self) -> FetchReading:
            """Execute a BUS trigger and return the completed measurement response."""

            return parse_fetch_response(self.ask("*TRG"))

        def fetch(self) -> FetchReading:
            return parse_fetch_response(self.ask("FETCh?"))

        def fetch_impedance(self) -> FetchReading:
            return parse_fetch_response(self.ask("FETCh:IMPedance?"))

        def fetch_main(self) -> FetchReading:
            return parse_fetch_response(self.ask("FETCh:MAIN?"))

        def fetch_monitor1(self) -> Optional[float]:
            response = self.ask("FETCh:MON1?")
            return parse_fetch_response(response).primary

        def fetch_monitor2(self) -> Optional[float]:
            response = self.ask("FETCh:MON2?")
            return parse_fetch_response(response).primary

        def fetch_monitors(self) -> tuple[Optional[float], Optional[float]]:
            reading = parse_fetch_response(self.ask("FETCh:MONitor?"))
            return reading.primary, reading.secondary

        def read_resistance(self, *, configure_dcr: bool = False) -> float:
            """Return the primary DCR reading in ohms."""

            if configure_dcr:
                self.function("DCR")
            return self.fetch_main().resistance_ohm

else:

    class GWInstekLCR76200:  # pragma: no cover - import guard
        """Placeholder that fails with a useful error when QCoDeS is absent."""

        def __init__(self, *_args, **_kwargs) -> None:
            raise ImportError(
                "GWInstekLCR76200 requires optional dependencies "
                "'qcodes' and 'pyvisa'. Install with `pip install .[lcr]`."
            )
