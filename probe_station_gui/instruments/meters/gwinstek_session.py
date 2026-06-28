"""GW Instek LCR session adapter used by meter controllers."""

from __future__ import annotations

import logging
import math
import time

from probe_station_gui.instruments.meters.lcr_helpers import (
    format_source_level_value,
    normalize_resource_name,
    normalize_visa_role,
    parse_numeric_response,
)


logger = logging.getLogger(__name__)


_ConfigurationStep = tuple[str, str, str]


class GWInstekLCRSessionError(RuntimeError):
    """Raised when the GW Instek session cannot complete an operation."""


class GWInstekLCRSession:
    """Thin wrapper around the GW Instek LCR driver."""

    backend_name = "qcodes"
    error_type: type[Exception] = GWInstekLCRSessionError
    CONFIG_COMMAND_DELAY_S = 0.2
    CONFIG_VERIFY_RETRIES = 3
    CONFIG_VERIFY_DELAY_S = 0.15
    POST_CONFIG_SETTLE_S = 0.2
    OVERLOAD_RESISTANCE_OHM = 9.9e19

    def __init__(self, address: str, timeout_ms: int) -> None:
        from probe_station_gui.instruments.meters.gwinstek_lcr_76200 import (
            GWInstekLCR76200,
        )

        normalized_address = normalize_resource_name(address)
        try:
            self._instrument = GWInstekLCR76200(
                name="gwinstek_lcr76200",
                address=normalized_address,
                timeout=max(0.1, timeout_ms / 1000.0),
            )
        except ImportError as exc:
            raise self._error(
                "QCoDeS LCR driver is unavailable. Install optional dependencies "
                "with `pip install .[lcr]`."
            ) from exc
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise self._error(
                f"Unable to open LCR resource {normalized_address}: {exc}"
            ) from exc

    def identify(self) -> str:
        try:
            idn = self._instrument.get_idn()
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise self._error(f"LCR identify query failed: {exc}") from exc
        parts = (
            idn.get("model"),
            idn.get("serial"),
            idn.get("firmware"),
            idn.get("vendor"),
        )
        return ", ".join(part for part in parts if part)

    def configure_measurement(
        self,
        *,
        measurement_function: str,
        range_mode: str,
        impedance_range: int,
        dcr_range: int,
        frequency_hz: float,
        level_mode: str,
        voltage_level_v: float,
        current_level_a: float,
        source_resistance_ohm: int,
        aperture_rate: str,
        aperture_averages: int,
        trigger_source: str,
        trigger_delay_s: float,
        bias_enabled: bool,
        bias_level_v: float,
        monitor1: str,
        monitor2: str,
        alc_enabled: bool,
        **_ignored: object,
    ) -> None:
        try:
            measurement_function = str(measurement_function).strip() or "DCR"
            range_mode = str(range_mode).strip().upper() or "HOLD"
            dcr_mode = measurement_function.upper() == "DCR"
            configuration_steps = self._measurement_configuration_steps(
                measurement_function=measurement_function,
                range_mode=range_mode,
                dcr_mode=dcr_mode,
                impedance_range=impedance_range,
                dcr_range=dcr_range,
                frequency_hz=frequency_hz,
                level_mode=level_mode,
                voltage_level_v=voltage_level_v,
                current_level_a=current_level_a,
                source_resistance_ohm=source_resistance_ohm,
                aperture_rate=aperture_rate,
                aperture_averages=aperture_averages,
                trigger_source=trigger_source,
                trigger_delay_s=trigger_delay_s,
                bias_enabled=bias_enabled,
                bias_level_v=bias_level_v,
                monitor1=monitor1,
                monitor2=monitor2,
                alc_enabled=alc_enabled,
            )
            for command, query, expected in configuration_steps:
                self._write_and_verify(command, query, expected)
            logger.info(
                "Configured LCR: function=%s range_mode=%s impedance_range=%s dcr_range=%s frequency=%s level_mode=%s voltage=%s current=%s aperture=%s avg=%s trigger=%s",
                measurement_function,
                range_mode,
                int(impedance_range),
                int(dcr_range),
                float(frequency_hz),
                level_mode,
                float(voltage_level_v),
                float(current_level_a),
                aperture_rate,
                int(aperture_averages),
                trigger_source,
            )
            time.sleep(self.POST_CONFIG_SETTLE_S)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise self._error(f"Unable to configure LCR measurement: {exc}") from exc

    @classmethod
    def _measurement_configuration_steps(
        cls,
        *,
        measurement_function: str,
        range_mode: str,
        dcr_mode: bool,
        impedance_range: int,
        dcr_range: int,
        frequency_hz: float,
        level_mode: str,
        voltage_level_v: float,
        current_level_a: float,
        source_resistance_ohm: int,
        aperture_rate: str,
        aperture_averages: int,
        trigger_source: str,
        trigger_delay_s: float,
        bias_enabled: bool,
        bias_level_v: float,
        monitor1: str,
        monitor2: str,
        alc_enabled: bool,
    ) -> list[_ConfigurationStep]:
        steps = cls._base_measurement_configuration_steps(
            measurement_function=measurement_function,
            range_mode=range_mode,
            aperture_rate=aperture_rate,
            aperture_averages=aperture_averages,
            trigger_source=trigger_source,
            trigger_delay_s=trigger_delay_s,
        )
        steps.extend(
            cls._range_configuration_steps(
                range_mode=range_mode,
                dcr_mode=dcr_mode,
                impedance_range=impedance_range,
                dcr_range=dcr_range,
            )
        )
        if dcr_mode:
            steps.append(("BIAS OFF", "BIAS?", "OFF"))
            return steps
        steps.extend(
            cls._impedance_configuration_steps(
                frequency_hz=frequency_hz,
                level_mode=level_mode,
                voltage_level_v=voltage_level_v,
                current_level_a=current_level_a,
                source_resistance_ohm=source_resistance_ohm,
                bias_enabled=bias_enabled,
                bias_level_v=bias_level_v,
                monitor1=monitor1,
                monitor2=monitor2,
                alc_enabled=alc_enabled,
            )
        )
        return steps

    @staticmethod
    def _base_measurement_configuration_steps(
        *,
        measurement_function: str,
        range_mode: str,
        aperture_rate: str,
        aperture_averages: int,
        trigger_source: str,
        trigger_delay_s: float,
    ) -> list[_ConfigurationStep]:
        return [
            (f"FUNC {measurement_function}", "FUNC?", measurement_function),
            (f"TRIG:SOUR {trigger_source}", "TRIG:SOUR?", trigger_source),
            (f"FUNC:RANG:AUTO {range_mode}", "FUNC:RANG:AUTO?", range_mode),
            (f"APER {aperture_rate}", "APER:RATE?", aperture_rate),
            (
                f"APER {int(aperture_averages)}",
                "APER:AVG?",
                str(int(aperture_averages)),
            ),
            (
                f"TRIG:DLY {float(trigger_delay_s)}",
                "TRIG:DLY?",
                str(float(trigger_delay_s)),
            ),
        ]

    @staticmethod
    def _range_configuration_steps(
        *,
        range_mode: str,
        dcr_mode: bool,
        impedance_range: int,
        dcr_range: int,
    ) -> list[_ConfigurationStep]:
        if range_mode != "HOLD":
            return []
        if dcr_mode:
            return [
                (
                    f"FUNC:DCR:RANG {int(dcr_range)}",
                    "FUNC:DCR:RANG?",
                    str(int(dcr_range)),
                )
            ]
        return [
            (
                f"FUNC:IMP:RANG {int(impedance_range)}",
                "FUNC:IMP:RANG?",
                str(int(impedance_range)),
            )
        ]

    @classmethod
    def _impedance_configuration_steps(
        cls,
        *,
        frequency_hz: float,
        level_mode: str,
        voltage_level_v: float,
        current_level_a: float,
        source_resistance_ohm: int,
        bias_enabled: bool,
        bias_level_v: float,
        monitor1: str,
        monitor2: str,
        alc_enabled: bool,
    ) -> list[_ConfigurationStep]:
        steps = [
            (f"FREQ {float(frequency_hz)}", "FREQ?", str(float(frequency_hz))),
            (
                f"LEV:SRES {int(source_resistance_ohm)}",
                "LEV:SRES?",
                str(int(source_resistance_ohm)),
            ),
            (f"FUNC:MON1 {monitor1}", "FUNC:MON1?", monitor1),
            (f"FUNC:MON2 {monitor2}", "FUNC:MON2?", monitor2),
            (
                f"LEV:ALC {'ON' if alc_enabled else 'OFF'}",
                "LEV:ALC?",
                "ON" if alc_enabled else "OFF",
            ),
        ]
        steps.append(
            cls._level_configuration_step(
                level_mode=level_mode,
                voltage_level_v=voltage_level_v,
                current_level_a=current_level_a,
            )
        )
        steps.append(
            cls._bias_configuration_step(
                bias_enabled=bias_enabled,
                bias_level_v=bias_level_v,
            )
        )
        return steps

    @staticmethod
    def _level_configuration_step(
        *,
        level_mode: str,
        voltage_level_v: float,
        current_level_a: float,
    ) -> _ConfigurationStep:
        if level_mode.upper() == "CURRENT":
            return (
                f"LEV:CURR {format_source_level_value(current_level_a)}",
                "LEV:CURR?",
                str(float(current_level_a)),
            )
        return (
            f"LEV:VOLT {format_source_level_value(voltage_level_v)}",
            "LEV:VOLT?",
            str(float(voltage_level_v)),
        )

    @staticmethod
    def _bias_configuration_step(
        *,
        bias_enabled: bool,
        bias_level_v: float,
    ) -> _ConfigurationStep:
        if bias_enabled:
            return (
                f"BIAS {float(bias_level_v)}",
                "BIAS?",
                str(float(bias_level_v)),
            )
        return ("BIAS OFF", "BIAS?", "OFF")

    def configure_for_resistance(
        self, dcr_range: int, auto_range_enabled: bool
    ) -> None:
        try:
            configuration_steps = [
                ("FUNC DCR", "FUNC?", "DCR"),
                ("TRIG:SOUR INT", "TRIG:SOUR?", "INT"),
                ("BIAS OFF", "BIAS?", "OFF"),
            ]
            if auto_range_enabled:
                configuration_steps.append(
                    ("FUNC:RANG:AUTO AUTO", "FUNC:RANG:AUTO?", "AUTO")
                )
            else:
                configuration_steps.extend(
                    (
                        ("FUNC:RANG:AUTO HOLD", "FUNC:RANG:AUTO?", "HOLD"),
                        (
                            f"FUNC:DCR:RANG {int(dcr_range)}",
                            "FUNC:DCR:RANG?",
                            str(int(dcr_range)),
                        ),
                    )
                )
            configuration_steps.append(("APER FAST", "APER?", "FAST"))
            for command, query, expected in configuration_steps:
                self._write_and_verify(command, query, expected)
            logger.info(
                "Configured LCR for DCR measurement: auto_range=%s range=%s aperture=FAST trigger=INT",
                auto_range_enabled,
                int(dcr_range),
            )
            time.sleep(self.POST_CONFIG_SETTLE_S)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise self._error(f"Unable to configure resistance mode: {exc}") from exc

    def _write_and_verify(self, command: str, query: str, expected_token: str) -> None:
        last_response = ""
        normalized_expected = expected_token.strip().upper()
        for attempt in range(self.CONFIG_VERIFY_RETRIES):
            self._instrument.write(command)
            time.sleep(self.CONFIG_COMMAND_DELAY_S)
            response = str(self._instrument.ask(query)).strip()
            normalized_response = response.split(",", 1)[0].strip().upper()
            if self._configuration_response_matches(
                normalized_response, normalized_expected
            ):
                return
            last_response = response
            logger.warning(
                "LCR config verification mismatch after %s: expected %s from %s, got %s (attempt %s/%s)",
                command,
                expected_token,
                query,
                response,
                attempt + 1,
                self.CONFIG_VERIFY_RETRIES,
            )
            time.sleep(self.CONFIG_VERIFY_DELAY_S)
        raise self._error(
            f"LCR rejected configuration command {command!r}: "
            f"{query} returned {last_response!r}, expected {expected_token!r}"
        )

    def set_trigger_source(self, trigger_source: str) -> None:
        source = str(trigger_source).strip().upper() or "INT"
        self._write_and_verify(f"TRIG:SOUR {source}", "TRIG:SOUR?", source)

    def visa_resource_roles(self) -> dict[str, dict[str, object]]:
        return {
            "meter.source": {
                "role": "meter.source",
                "kind": "source_meter",
                "model": "GW Instek LCR-76200",
                "required": True,
            }
        }

    def visa_handle_for_role(self, role: str):
        normalized = normalize_visa_role(role)
        if normalized not in {"meter.source", "source", "source_meter", "meter"}:
            raise KeyError(f"Unsupported GW Instek VISA role: {role}")
        return getattr(self._instrument, "visa_handle", self._instrument)

    @staticmethod
    def _configuration_response_matches(response: str, expected: str) -> bool:
        if response == expected:
            return True
        if response in {"ON", "1"} and expected in {"ON", "1"}:
            return True
        if response in {"OFF", "0"} and expected in {"OFF", "0"}:
            return True
        try:
            return math.isclose(
                GWInstekLCRSession._parse_numeric_response(response),
                float(expected),
                rel_tol=1e-6,
                abs_tol=1e-9,
            )
        except ValueError:
            return False

    @staticmethod
    def _parse_numeric_response(response: str) -> float:
        return parse_numeric_response(response)

    def read_primary_value(self, *, trigger: bool = False) -> float:
        try:
            if trigger:
                reading = self._instrument.trigger_fetch()
            else:
                reading = self._instrument.fetch_main()
            primary_value = reading.primary
            if primary_value is None:
                raise ValueError("LCR read did not return a primary value")
            primary_value = float(primary_value)
        except Exception as exc:  # pragma: no cover - backend specific failures
            raise self._error(f"LCR fetch failed: {exc}") from exc
        if (
            not math.isfinite(primary_value)
            or abs(primary_value) >= self.OVERLOAD_RESISTANCE_OHM
        ):
            return math.inf
        return primary_value

    def read_resistance_ohm(self) -> float:
        return self.read_primary_value()

    def abort_measurement(self) -> None:
        writer = getattr(self._instrument, "write", None)
        if writer is None:
            writer = getattr(self._instrument, "write_raw", None)
        if callable(writer):
            try:
                writer("ABOR")
            except Exception:
                logger.debug("GW Instek abort command failed", exc_info=True)

    def close(self) -> None:
        self._instrument.close()

    def _error(self, message: str) -> Exception:
        return type(self).error_type(message)
