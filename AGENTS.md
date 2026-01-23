## Overview
- This repo is a PySide6 GUI for a FluidNC-powered probe station with camera-driven click-to-move.
- Entry point: `main.py` -> `Main` window with docked panels and the microscope view.
- Hardware dependencies: a Spinnaker/rotpy camera and a FluidNC controller over serial.

## Runtime layout
- Camera acquisition: `probe_station_gui/camera_worker.py` (`Grabber`) runs in a `QThread` and emits `QImage` frames.
- Stage control: `probe_station_gui/stage_controller.py` turns pixel deltas into mm moves, runs auto-calibration, and sends G-code over serial.
- UI panels:
  - `probe_station_gui/views/microscope_view.py` renders frames, crosshair, and click target overlay.
  - `probe_station_gui/views/joystick_window.py` provides jog/home/reset controls plus keyboard bindings.
  - `probe_station_gui/views/serial_terminal_window.py` is a terminal with history and Ctrl+X support.
  - `probe_station_gui/views/serial_connection_panel.py` manages serial scanning/connect/auto-reconnect.
  - `probe_station_gui/views/dock_widgets.py` defines collapsible dock containers.
- Settings system: `probe_station_gui/settings_manager.py` loads/saves JSON settings (controls, logging, feedrates).
- Settings UI: `probe_station_gui/dialogs/settings_dialog.py` exposes tabs for controls, feedrates, logging.

## Behavior notes
- Click-to-move: `MicroscopeView.clicked` feeds pixel deltas into `StageController.request_move`.
- Calibration uses phase correlation on frames to estimate pixel shift per mm.
- Stage commands are issued in relative mode (`G91`) with a fixed default feed rate in `StageController`.
- Joystick jog uses `$J=G91 G21 ...` and supports mixed linear/rotary axes.
- Motion safety: all movement is gated by `StageController._move_safety_check()` (A axis must be homed and at zero).
- Joystick/keyboard motion also calls `StageController.check_motion_safety()` to query `?` and enforce A-axis safety.
- Autofocus performs a local refinement within ±1 mm of current Z (SciPy required) and auto-homes A if needed.

## Settings and logging
- Default settings file: `probe_station_gui/default_settings.json`.
- User settings path is platform-dependent (see `SettingsManager._determine_config_dir`).
- Logging writes to a file configured in settings via `probe_station_gui/logging_config.py`.

## Development guidelines
- Avoid running hardware-dependent code in automation unless explicitly requested.
- When changing serial behavior, check both `StageController` and `SerialTerminalWindow` for coordination.
- When updating controls or feedrates, keep `SettingsManager`, `settings_dialog.py`, and `joystick_window.py` in sync.

## How to run (local)
- Install deps from `pyproject.toml` (PySide6, numpy, opencv-python, rotpy).
- Run `python main.py`.
