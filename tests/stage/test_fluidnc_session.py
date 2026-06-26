import pytest

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.fluidnc_session import (
    FluidNCSession,
    FluidNCSessionCallbacks,
)
from probe_station_gui.stage.types import _Status


class _BufferedSerial:
    def __init__(self, data: bytes = b"", lines: list[bytes] | None = None) -> None:
        self.data = bytearray(data)
        self.lines = list(lines or [])
        self.writes: list[bytes] = []
        self.reset_calls: list[bool] = []

    @property
    def in_waiting(self) -> int:
        return len(self.data)

    def read(self, size: int) -> bytes:
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        return b""

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        self.reset_calls.append(True)
        self.data.clear()


def _session_callbacks(
    *,
    homing_lines: list[str] | None = None,
    limit_lines: list[str] | None = None,
    pending_reads: list[tuple[bytes, str]] | None = None,
    coordinate_lines: list[str] | None = None,
    updated_homing: list[set[str]] | None = None,
) -> FluidNCSessionCallbacks:
    def handle_homing_message_line(line: str) -> bool:
        if not line.startswith("[MSG:Homed:"):
            return False
        if homing_lines is not None:
            homing_lines.append(line)
        return True

    def parse_status_line(line: str) -> _Status | None:
        if not line.startswith("<Idle|"):
            return None
        return _Status(state="Idle", work_position=(1.0, 2.0, 3.0))

    def extract_status_homed_axes(line: str) -> set[str] | None:
        if "|H:XY" not in line:
            return None
        return {"X", "Y"}

    return FluidNCSessionCallbacks(
        check_cancelled=lambda: None,
        raise_if_controller_reboot_line=lambda _line, _source: None,
        handle_limit_line=lambda line: (
            limit_lines.append(line) if limit_lines is not None else None
        ),
        handle_homing_message_line=handle_homing_message_line,
        handle_coordinate_state_line=lambda line: (
            coordinate_lines.append(line) if coordinate_lines is not None else None
        ),
        parse_status_line=parse_status_line,
        extract_status_homed_axes=extract_status_homed_axes,
        update_homing_status=lambda axes: (
            updated_homing.append(set(axes)) if updated_homing is not None else None
        ),
        handle_pending_serial_data_side_effects=lambda data, reason: (
            pending_reads.append((data, reason)) if pending_reads is not None else None
        ),
    )


def test_write_command_discards_pending_input_before_writing() -> None:
    pending_reads: list[tuple[bytes, str]] = []
    serial_connection = _BufferedSerial(data=b"stale\r\n")
    session = FluidNCSession(
        serial_connection=serial_connection,
        callbacks=_session_callbacks(pending_reads=pending_reads),
    )

    session.write_command("G1 X1.000")

    assert serial_connection.writes == [b"G1 X1.000\n"]
    assert pending_reads == [(b"stale\r\n", "before command G1 X1.000")]


def test_wait_for_ok_consumes_homing_message_before_ack() -> None:
    homing_lines: list[str] = []
    serial_connection = _BufferedSerial(lines=[b"[MSG:Homed:XA]\n", b"ok\n"])
    session = FluidNCSession(
        serial_connection=serial_connection,
        callbacks=_session_callbacks(homing_lines=homing_lines),
    )

    session.wait_for_ok()

    assert homing_lines == ["[MSG:Homed:XA]"]


def test_read_status_frame_uses_parser_and_updates_homing() -> None:
    pending_reads: list[tuple[bytes, str]] = []
    coordinate_lines: list[str] = []
    updated_homing: list[set[str]] = []
    serial_connection = _BufferedSerial(
        data=b"old\r\n",
        lines=[b"<Idle|WPos:1.000,2.000,3.000|H:XY>\n"],
    )
    session = FluidNCSession(
        serial_connection=serial_connection,
        callbacks=_session_callbacks(
            pending_reads=pending_reads,
            coordinate_lines=coordinate_lines,
            updated_homing=updated_homing,
        ),
    )

    status = session.read_status_frame(timeout=0.1)

    assert status == _Status(
        state="Idle",
        work_position=(1.0, 2.0, 3.0),
        homed_axes={"X", "Y"},
    )
    assert serial_connection.writes == [b"?\n"]
    assert pending_reads == [(b"old\r\n", "before status query")]
    assert coordinate_lines == ["<Idle|WPos:1.000,2.000,3.000|H:XY>"]
    assert updated_homing == [{"X", "Y"}]


def test_read_pending_output_caps_bytes_and_reports_side_effects() -> None:
    pending_reads: list[tuple[bytes, str]] = []
    serial_connection = _BufferedSerial(data=b"abcde")
    session = FluidNCSession(
        serial_connection=serial_connection,
        callbacks=_session_callbacks(pending_reads=pending_reads),
    )

    data = session.read_pending_output(max_bytes=3)

    assert data == b"abc"
    assert pending_reads == [(b"abc", "terminal pending read")]
    assert serial_connection.in_waiting == 2


def test_wait_for_ok_raises_stage_error_on_alarm() -> None:
    serial_connection = _BufferedSerial(lines=[b"ALARM:1\n"])
    session = FluidNCSession(
        serial_connection=serial_connection,
        callbacks=_session_callbacks(),
    )

    with pytest.raises(StageControllerError, match="Controller alarm: ALARM:1"):
        session.wait_for_ok(timeout=0.1)


def test_iter_command_response_lines_routes_side_effects_and_yields_ok() -> None:
    homing_lines: list[str] = []
    limit_lines: list[str] = []
    coordinate_lines: list[str] = []
    serial_connection = _BufferedSerial(
        lines=[
            b"[MSG:Homed:XA]\n",
            b"[GC:G1 G54 G17 G21 G90]\n",
            b"ok\n",
        ]
    )
    session = FluidNCSession(
        serial_connection=serial_connection,
        callbacks=_session_callbacks(
            homing_lines=homing_lines,
            limit_lines=limit_lines,
            coordinate_lines=coordinate_lines,
        ),
    )

    lines = list(
        session.iter_command_response_lines(
            "$G",
            timeout=0.1,
            source="$G",
            handle_homing_messages=True,
            handle_coordinate_state=True,
        )
    )

    assert serial_connection.writes == [b"$G\n"]
    assert homing_lines == ["[MSG:Homed:XA]"]
    assert limit_lines == ["[MSG:Homed:XA]", "[GC:G1 G54 G17 G21 G90]", "ok"]
    assert coordinate_lines == ["[GC:G1 G54 G17 G21 G90]", "ok"]
    assert lines == ["[GC:G1 G54 G17 G21 G90]", "ok"]


def test_reset_input_buffer_uses_serial_method() -> None:
    serial_connection = _BufferedSerial(data=b"stale\r\n")
    session = FluidNCSession(
        serial_connection=serial_connection,
        callbacks=_session_callbacks(),
    )

    session.reset_input_buffer(reason="test reset")

    assert serial_connection.reset_calls == [True]
    assert serial_connection.in_waiting == 0
