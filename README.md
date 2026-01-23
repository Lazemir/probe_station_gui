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

## License
TBD
