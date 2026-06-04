"""QCoDeS-style driver for the Keithley 2400 SourceMeter."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from qcodes import validators as vals
from qcodes.instrument import VisaInstrument

if TYPE_CHECKING:
    from typing import Unpack

    from qcodes.instrument import VisaInstrumentKWArgs


log = logging.getLogger(__name__)

MAX_SOURCE_LIST_POINTS = 2500
MAX_SOURCE_LIST_POINTS_PER_COMMAND = 100


class Keithley2400(VisaInstrument):
    """QCoDeS driver for the Keithley 2400 SourceMeter.

    Args:
        name: QCoDeS instrument name.
        address: VISA resource address.
        **kwargs: Forwarded to :class:`qcodes.instrument.VisaInstrument`.
    """

    default_terminator = "\n"
    default_timeout = 120

    def __init__(
        self,
        name: str,
        address: str,
        **kwargs: "Unpack[VisaInstrumentKWArgs]",
    ) -> None:
        super().__init__(name, address, **kwargs)
        self.source_voltage = self.add_parameter(
            "source_voltage",
            label="Source voltage",
            unit="V",
            set_cmd=":SOUR:VOLT {:.12g}",
            get_cmd=":SOUR:VOLT?",
            get_parser=float,
            vals=vals.Numbers(-210.0, 210.0),
        )
        """Programmed source voltage."""

        self.source_voltage_range = self.add_parameter(
            "source_voltage_range",
            label="Source voltage range",
            unit="V",
            set_cmd=":SOUR:VOLT:RANG {:.12g}",
            get_cmd=":SOUR:VOLT:RANG?",
            get_parser=float,
            vals=vals.Numbers(0.0, 210.0),
        )
        """Voltage source range."""

        self.current_range = self.add_parameter(
            "current_range",
            label="Current range",
            unit="A",
            set_cmd=":SENS:CURR:RANG {:.12g}",
            get_cmd=":SENS:CURR:RANG?",
            get_parser=float,
            vals=vals.Numbers(0.0, 1.05),
        )
        """Current measurement range."""

        self.current_compliance = self.add_parameter(
            "current_compliance",
            label="Current compliance",
            unit="A",
            set_cmd=":SENS:CURR:PROT {:.12g}",
            get_cmd=":SENS:CURR:PROT?",
            get_parser=float,
            vals=vals.Numbers(0.0, 1.05),
        )
        """Current compliance limit."""

        self.current_nplc = self.add_parameter(
            "current_nplc",
            label="Current integration time",
            unit="NPLC",
            set_cmd=":SENS:CURR:NPLC {:.12g}",
            get_cmd=":SENS:CURR:NPLC?",
            get_parser=float,
            vals=vals.Numbers(0.01, 50.0),
        )
        """Current measurement integration time."""

        self.trigger_count = self.add_parameter(
            "trigger_count",
            label="Trigger count",
            set_cmd=":TRIG:COUN {:.0f}",
            get_cmd=":TRIG:COUN?",
            get_parser=int,
            vals=vals.Ints(1, MAX_SOURCE_LIST_POINTS),
        )
        """Number of trigger events for a programmed sequence."""

        self.trace_points = self.add_parameter(
            "trace_points",
            label="Trace buffer points",
            set_cmd="TRAC:POIN {:.0f}",
            get_cmd="TRAC:POIN?",
            get_parser=int,
            vals=vals.Ints(1, MAX_SOURCE_LIST_POINTS),
        )
        """Trace buffer size."""

    def clear_status(self) -> None:
        """Clear status registers and the SCPI error queue."""

        self.write("*CLS")

    def abort(self) -> None:
        """Abort the current measurement operation."""

        self.write(":ABOR")

    def output_enabled(self, enabled: bool) -> None:
        """Set the output relay state."""

        self.write(f":OUTP {'ON' if enabled else 'OFF'}")

    def fixed_voltage_mode(self) -> None:
        """Use fixed source voltage mode."""

        self.write(":SOUR:VOLT:MODE FIX")

    def list_voltage_mode(self) -> None:
        """Use source voltage list mode."""

        self.write(":SOUR:VOLT:MODE LIST")

    def set_voltage_list(self, values: Sequence[float]) -> None:
        """Load a source voltage list, using APPend for lists over 100 points.

        Args:
            values: Source voltages in volts.

        Raises:
            ValueError: If the list is empty or exceeds instrument limits.
        """

        voltages = [float(value) for value in values]
        if not voltages:
            raise ValueError("Source voltage list cannot be empty.")
        if len(voltages) > MAX_SOURCE_LIST_POINTS:
            raise ValueError(
                f"Keithley 2400 accepts at most {MAX_SOURCE_LIST_POINTS} list points."
            )
        for offset in range(0, len(voltages), MAX_SOURCE_LIST_POINTS_PER_COMMAND):
            chunk = voltages[offset : offset + MAX_SOURCE_LIST_POINTS_PER_COMMAND]
            command = (
                ":SOUR:LIST:VOLT"
                if offset == 0
                else ":SOUR:LIST:VOLT:APPend"
            )
            self.write(
                f"{command} "
                + ",".join(f"{value:.12g}" for value in chunk)
            )

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
