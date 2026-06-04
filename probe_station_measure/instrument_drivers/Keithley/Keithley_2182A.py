"""QCoDeS-style driver for the Keithley 2182A Nanovoltmeter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from qcodes import validators as vals
from qcodes.instrument import VisaInstrument

if TYPE_CHECKING:
    from typing import Unpack

    from qcodes.instrument import VisaInstrumentKWArgs


log = logging.getLogger(__name__)

MAX_TRACE_POINTS = 1024


class Keithley2182A(VisaInstrument):
    """QCoDeS driver for the Keithley 2182A Nanovoltmeter."""

    default_terminator = "\n"
    default_timeout = 120

    def __init__(
        self,
        name: str,
        address: str,
        **kwargs: "Unpack[VisaInstrumentKWArgs]",
    ) -> None:
        super().__init__(name, address, **kwargs)
        self.voltage_range = self.add_parameter(
            "voltage_range",
            label="Voltage range",
            unit="V",
            set_cmd="SENS:VOLT:RANG {:.12g}",
            get_cmd="SENS:VOLT:RANG?",
            get_parser=float,
            vals=vals.Numbers(0.0, 120.0),
        )
        """Voltage measurement range."""

        self.voltage_nplc = self.add_parameter(
            "voltage_nplc",
            label="Voltage integration time",
            unit="NPLC",
            set_cmd="SENS:VOLT:NPLC {:.12g}",
            get_cmd="SENS:VOLT:NPLC?",
            get_parser=float,
            vals=vals.Numbers(0.01, 50.0),
        )
        """Voltage measurement integration time."""

        self.trigger_count = self.add_parameter(
            "trigger_count",
            label="Trigger count",
            set_cmd="TRIG:COUN {:.0f}",
            get_cmd="TRIG:COUN?",
            get_parser=int,
            vals=vals.Ints(1, MAX_TRACE_POINTS),
        )
        """Number of external trigger events to accept."""

        self.sample_count = self.add_parameter(
            "sample_count",
            label="Sample count",
            set_cmd="SAMP:COUN {:.0f}",
            get_cmd="SAMP:COUN?",
            get_parser=int,
            vals=vals.Ints(1, MAX_TRACE_POINTS),
        )
        """Number of samples per trigger event."""

        self.trace_points = self.add_parameter(
            "trace_points",
            label="Trace buffer points",
            set_cmd="TRAC:POIN {:.0f}",
            get_cmd="TRAC:POIN?",
            get_parser=int,
            vals=vals.Ints(1, MAX_TRACE_POINTS),
        )
        """Trace buffer size."""

    def clear_status(self) -> None:
        """Clear status registers and the SCPI error queue."""

        self.write("*CLS")

    def abort(self) -> None:
        """Abort the current measurement operation."""

        self.write("ABOR")

    def configure_voltage_channel(self, channel: int = 1) -> None:
        """Configure the selected voltage channel for DC-voltage readings."""

        self.write("CONF:VOLT")
        self.write(f"SENS:CHAN {int(channel)}")

    def read_voltage(self) -> float:
        """Trigger and return one voltage reading."""

        return float(self.ask("READ?"))

    def trace_data(self) -> list[float]:
        """Return numeric values from the trace buffer."""

        return _parse_float_list(self.ask("TRAC:DATA?"))

    def system_errors(self, limit: int = 8) -> list[str]:
        """Read non-zero SCPI errors from the error queue."""

        errors: list[str] = []
        for _index in range(max(1, int(limit))):
            response = str(self.ask("SYST:ERR?")).strip()
            if response.startswith(("0,", "+0,")):
                break
            errors.append(response)
        return errors


def _parse_float_list(response: str) -> list[float]:
    values: list[float] = []
    for token in str(response).replace(",", " ").split():
        values.append(float(token))
    return values
