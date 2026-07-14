# KLayout Design Viewer Prototype

This is throwaway code. Its only question is whether KLayout's Qt-less renderer
and hierarchical geometry queries feel good enough inside a PySide6 canvas to
justify a production rewrite.

Run it from the repository root:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m probe_station_gui.prototypes.klayout_design_viewer
```

You can also pass a GDS path directly:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m probe_station_gui.prototypes.klayout_design_viewer C:\path\design.gds
```

Evaluation:

- [ ] Pan and zoom quality
- [ ] Frame-label latency
- [ ] Layer-2 latency
- [ ] Snap responsiveness

Automated baseline on the recent two-layer design:

- KLayout load: about 135 ms
- Full-design 800x600 render: about 53 ms
- Expanded interactive frame: about 190-230 ms
- Local vertex snap: about 0.15 ms for one returned shape

Decision after evaluation: delete, revise, or rewrite for production.
