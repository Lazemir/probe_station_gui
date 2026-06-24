from probe_station_gui.fluidnc_protocol import (
    line_indicates_controller_reboot,
    line_indicates_controller_startup,
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
