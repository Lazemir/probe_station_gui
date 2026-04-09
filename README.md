# Probe Station GUI

PySide6 UI for a FluidNC-powered probe station with camera-driven click-to-move, jogging, homing, and autofocus.

## What it does
- Live microscope view with click-to-move.
- Jog controls (buttons + keyboard) with configurable feedrates.
- Per-axis homing with progress indicators.
- Serial terminal with history and Ctrl+X.

## Requirements
- Python 3.10+ recommended.
- Dependencies in `pyproject.toml` (PySide6, numpy, opencv-python, rotpy, scipy).
- Hardware: Spinnaker/rotpy camera and FluidNC controller over serial.

## Install
```bash
python -m venv .venv
. .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e .
```

## Run
```bash
python main.py
```

## Windows `.venv`
For a reproducible Windows setup from this repo:

```cmd
scripts\bootstrap_venv.cmd
scripts\run_gui.cmd
```

Notes:
- Build `.venv` only after the repository is moved to its final folder. Windows virtual environments are path-bound.
- The repo can live in a shared writable directory for all users of the machine.
- Settings and logs are already per-user and go to `%APPDATA%\ProbeStationGUI`.

## License
TBD
