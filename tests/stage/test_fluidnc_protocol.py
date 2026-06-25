from probe_station_gui.stage.fluidnc_protocol import (
    line_indicates_controller_reboot,
    line_indicates_controller_startup,
    parse_fluidnc_axis_max_feedrates,
    parse_fluidnc_status_line,
    parse_startup_axis_limits,
)


def test_line_indicates_controller_reboot_for_reset_markers() -> None:
    reboot_lines = [
        "[MSG:RST]",
        "rst:0xc (SW_CPU_RESET),boot:0x12 (SPI_FAST_FLASH_BOOT)",
        "ESP-ROM:esp32s3-20210327",
        "load:0x3fce3808,len:0x44c",
        "entry 0x403c98a8",
    ]

    for line in reboot_lines:
        assert line_indicates_controller_reboot(line) is True


def test_line_indicates_controller_reboot_ignores_status_and_version_lines() -> None:
    assert line_indicates_controller_reboot("ok") is False
    assert line_indicates_controller_reboot("<Idle|WPos:0,0,0|FS:0,0>") is False
    assert line_indicates_controller_reboot("[VER:3.9 FluidNC v3.9.x]") is False
    assert line_indicates_controller_reboot("[MSG:INFO: FluidNC ready]") is False


def test_line_indicates_controller_startup_includes_initial_banner_lines() -> None:
    startup_lines = [
        "[VER:3.9 FluidNC v3.9.x]",
        "[MSG:INFO: FluidNC ready]",
        "Grbl 1.1h ['$' for help]",
    ]

    for line in startup_lines:
        assert line_indicates_controller_startup(line) is True


def test_line_indicates_controller_startup_includes_reset_markers() -> None:
    assert line_indicates_controller_startup("[MSG:RST]") is True
    assert line_indicates_controller_startup("rst:0xc (SW_CPU_RESET)") is True


def test_parse_fluidnc_status_line_uses_native_work_position() -> None:
    status = parse_fluidnc_status_line(
        "<Idle|WPos:-6.894,-6.599,9.207,0.000,2.170|Bf:15,127|FS:0,0>",
        position_reporting_mode="work",
        active_work_coordinate_system="G54",
        controller_coordinate_offsets={"G54": (32.0, 32.0, 0.0, 0.0, 0.0)},
    )

    assert status is not None
    assert status.position is None
    assert status.work_offset[:2] == (32.0, 32.0)
    assert status.work_position[:2] == (-6.894, -6.599)


def test_parse_fluidnc_status_line_captures_limit_pins() -> None:
    status = parse_fluidnc_status_line(
        "<Alarm|WPos:0.000,10.000,1.000,0.000|Pn:XY|FS:0,0>",
        position_reporting_mode="work",
    )

    assert status is not None
    assert status.pins == {"X", "Y"}


def test_parse_fluidnc_status_line_ignores_wrong_position_mode() -> None:
    status = parse_fluidnc_status_line(
        "<Idle|MPos:25.106,25.401,9.207,0.000,2.170|Bf:15,127|FS:0,0>",
        position_reporting_mode="work",
    )

    assert status is None


def test_parse_fluidnc_status_line_rejects_truncated_machine_position() -> None:
    status = parse_fluidnc_status_line(
        "<Idle|MPos:29.459,31.4|FS:0,0>",
        position_reporting_mode="machine",
    )

    assert status is None


def test_parse_startup_axis_limits() -> None:
    limits = parse_startup_axis_limits(
        [
            "[MSG:INFO: Axis X (0.000,64.000)]",
            "[MSG:INFO: Axis Y (0.000,64.000)]",
            "[MSG:INFO: Axis A (-0.100,0.000)]",
            "ok",
        ]
    )

    assert limits == {
        "X": (0.0, 64.0),
        "Y": (0.0, 64.0),
        "A": (-0.1, 0.0),
    }


def test_parse_fluidnc_axis_max_feedrates() -> None:
    rates = parse_fluidnc_axis_max_feedrates(
        [
            "x:",
            "  max_rate_mm_per_min: 1000",
            "y:",
            "  max_rate_mm_per_min: bad",
            "z:",
            "  max_rate_mm_per_min: 750.5",
            "a:",
            "  max_rate_mm_per_min: -1",
            "b:",
            "  acceleration: 10",
        ]
    )

    assert rates == {"X": 1000.0, "Z": 750.5}
