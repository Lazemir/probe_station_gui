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
- Motion safety: all movement is gated by `StageController._move_safety_check()` (needles must be known and raised). A-axis homing maps to “needles up.”
- Needles UI: `JoystickWindow` has Raise/Lower buttons with spinner; status bar is green when raised, yellow when down/unknown.
- Manual serial commands (terminal send/Ctrl+X) invalidate needles state via `StageController.invalidate_needles_state`.
- Autofocus performs a local refinement within ±1 mm of current Z (SciPy required) and auto-homes A if needles are not up.
- Homing buttons only show spinners after the homing task is accepted; they stop on ALARM/error via `homing_action_finished`.
- Keyboard jog stop: on key release, a `0x85` stop is sent and resent once after 120 ms if no keys remain; no status polling to avoid lag.

## Settings and logging
- Default settings file: `probe_station_gui/default_settings.json`.
- User settings path is platform-dependent (see `SettingsManager._determine_config_dir`).
- On Windows, user settings live in `%APPDATA%\ProbeStationGUI`; on this machine that is `C:\Users\Lazemir\AppData\Roaming\ProbeStationGUI`. Route measurement settings are in `route-measurement-settings.json` there.
- Runtime logs default to `%LOCALAPPDATA%\ProbeStationGUI\Logs`; on this machine, inspect `C:\Users\Lazemir\AppData\Local\ProbeStationGUI\Logs\status-history.log` and `C:\Users\Lazemir\AppData\Local\ProbeStationGUI\Logs\probe-station-gui.log` first.
- A `probe-station-gui.log` under `%APPDATA%\ProbeStationGUI` is a legacy/stale location unless the logging settings explicitly point there.
- Logging writes to a file configured in settings via `probe_station_gui/logging_config.py`.

## Development guidelines
- Avoid running hardware-dependent code in automation unless explicitly requested.
- Use the shared project virtual environment for Python commands, even from Codex worktrees: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`, `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe main.py`, etc. Do not use a missing worktree-local `.venv`, and do not rely on bare `python`, which may resolve to the Windows Store alias.
- Keep startup lightweight: `main.py` should create the main window and show the GUI first. Do not instantiate heavy optional panels, web engines, hardware clients, long scans, network clients, or calibration workers synchronously during startup; initialize them lazily when opened or after startup via timers/background threads.
- Keep the GUI thread for Qt widget updates only. Hardware I/O, camera/GenICam node scans, serial polling, calibration, file parsing, image processing, and any large periodic refresh must run in worker threads or be sliced into tiny queued UI updates. Never implement periodic full-widget rebuilds or blocking hardware reads in the GUI thread.
- Camera settings must not block frame acquisition or stop/restart camera acquisition for periodic refresh. Keep GenICam reads/writes in a dedicated worker/executor and lazily build only the visible Qt controls.
- Do not route in-process GUI features through the app's own localhost API. Use direct controller methods, Qt signals, or narrow callbacks inside the process; reserve the FastAPI server for external clients.
- Do not use ellipses in menu item labels.
- Keep GUI menus and primary controls laconic. Do not put live measurements, matrices, diagnostics, or implementation detail in top-level menu item text; put details inside dialogs, status panels, tooltips, or logs.
- Avoid per-objective magic constants in code, especially motion speeds. Prefer live measurements, calibration-script output, or user-editable settings; keep unavoidable numeric guards named and minimal.
- After the user runs the app or reports runtime behavior, inspect both `status-history.log` and `probe-station-gui.log` before diagnosing or changing behavior.
- Do not emit Qt signals while holding a non-reentrant lock. If controller state reset paths can emit signals that call back into the same object, use a reentrant lock or move signal emission outside the locked section.
- When changing serial behavior, check both `StageController` and `SerialTerminalWindow` for coordination.
- When updating controls or feedrates, keep `SettingsManager`, `settings_dialog.py`, and `joystick_window.py` in sync.

## How to run (local)
- Install deps from `pyproject.toml` (PySide6, numpy, opencv-python, rotpy).
- Run `.venv\Scripts\python.exe main.py`.

