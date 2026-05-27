"""Window for live Plotly dial-indicator surface mapping."""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QUrl, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:  # pragma: no cover - depends on installed Qt modules.
    from PySide6.QtWebEngineWidgets import QWebEngineView
except Exception:  # pragma: no cover
    QWebEngineView = None  # type: ignore[assignment]

from ..surface_mapping import (
    SurfaceMapConfig,
    SurfaceMapRecord,
    build_surface_route_plan,
    records_summary,
    route_length_mm,
    route_metadata,
    safe_radius_limits,
    save_surface_map,
)


def _plotly_script_tag_and_base_url() -> tuple[str, QUrl]:
    """Return a Plotly.js script tag, preferring the local plotly package."""

    try:
        import plotly  # type: ignore

        package_dir = Path(plotly.__file__).resolve().parent
        script_path = package_dir / "package_data" / "plotly.min.js"
        if script_path.exists():
            base_url = QUrl.fromLocalFile(str(script_path.parent.resolve()) + "/")
            return '<script src="plotly.min.js"></script>', base_url
    except Exception:
        pass
    return (
        '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>',
        QUrl("https://cdn.plot.ly/"),
    )


PLOTLY_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html, body, #surface-map { width: 100%; height: 100%; margin: 0; overflow: hidden; }
body { font-family: sans-serif; background: #ffffff; }
#missing {
  position: absolute;
  left: 12px;
  top: 12px;
  z-index: 2;
  padding: 8px 10px;
  color: #8a1c1c;
  background: #fff8f8;
  border: 1px solid #e0b4b4;
  display: none;
}
</style>
__PLOTLY_SCRIPT__
</head>
<body>
<div id="surface-map"></div>
<div id="missing">Plotly.js is not loaded.</div>
<script>
function circleTrace(radius, name, color, dash) {
  const x = [];
  const y = [];
  for (let i = 0; i <= 240; i++) {
    const t = 2 * Math.PI * i / 240;
    x.push(radius * Math.cos(t));
    y.push(radius * Math.sin(t));
  }
  return {
    type: "scattergl",
    mode: "lines",
    x: x,
    y: y,
    name: name,
    line: {color: color, width: 1.5, dash: dash || "solid"},
    hoverinfo: "skip"
  };
}

function drawSurfaceMap(payload) {
  if (typeof Plotly === "undefined") {
    document.getElementById("missing").style.display = "block";
    return;
  }
  const route = payload.route || [];
  const records = payload.records || [];
  const limits = payload.limits || {};
  const data = [];
  if (limits.outer_radius_mm > 0) {
    data.push(circleTrace(limits.outer_radius_mm, "outer limit", "#222222", "solid"));
  }
  if (limits.inner_guard_radius_mm > 0) {
    data.push(circleTrace(limits.inner_guard_radius_mm, "forbidden center", "#c62828", "dash"));
  }
  if (route.length > 0) {
    data.push({
      type: "scattergl",
      mode: "lines+markers",
      x: route.map(p => p.x),
      y: route.map(p => p.y),
      name: "route preview",
      line: {color: "#7d7d7d", width: 1},
      marker: {size: 4, color: "#7d7d7d"},
      customdata: route.map((p, i) => [i, p.r]),
      hovertemplate: "route %{customdata[0]}<br>X=%{x:.3f} mm<br>Y=%{y:.3f} mm<br>R=%{customdata[1]:.3f} mm<extra></extra>"
    });
  }
  if (records.length > 0) {
    data.push({
      type: "scattergl",
      mode: "markers",
      x: records.map(p => p.x),
      y: records.map(p => p.y),
      name: "measured",
      marker: {
        size: 8,
        color: records.map(p => p.delta),
        colorscale: "Viridis",
        showscale: true,
        colorbar: {title: "delta, mm"}
      },
      customdata: records.map(p => [p.index, p.r, p.indicator]),
      hovertemplate: "point %{customdata[0]}<br>X=%{x:.3f} mm<br>Y=%{y:.3f} mm<br>R=%{customdata[1]:.3f} mm<br>indicator=%{customdata[2]:.6f} mm<br>delta=%{marker.color:.6f} mm<extra></extra>"
    });
  }
  const tableRadius = limits.table_radius_mm || 30;
  const plotRadius = Math.max(1, tableRadius * 1.06);
  const layout = {
    margin: {l: 52, r: 16, t: 22, b: 44},
    hovermode: "closest",
    showlegend: true,
    uirevision: "surface-map-view",
    legend: {orientation: "h", y: 1.08},
    xaxis: {
      title: "X, mm",
      range: [-plotRadius, plotRadius],
      zeroline: true,
      scaleanchor: "y",
      scaleratio: 1
    },
    yaxis: {
      title: "Y, mm",
      range: [-plotRadius, plotRadius],
      zeroline: true
    }
  };
  Plotly.react("surface-map", data, layout, {responsive: true, displaylogo: false});
}

window.drawSurfaceMap = drawSurfaceMap;
</script>
</body>
</html>
"""


class SurfaceMapWorker(QObject):
    """Run the surface map route off the GUI thread."""

    route_ready = Signal(object, object)
    point_captured = Signal(object, int, int)
    status_changed = Signal(str)
    stage_status_requested = Signal(object)
    stage_move_requested = Signal(object)
    finished = Signal(bool, str, object)

    def __init__(self, config: SurfaceMapConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._stop_requested = threading.Event()

    def stop(self) -> None:
        self._stop_requested.set()

    @Slot()
    def run(self) -> None:
        records: list[SurfaceMapRecord] = []
        metadata: dict[str, Any] = {}
        success = False
        message = "Surface map stopped."
        base_path: Path | None = None
        try:
            status = self._wait_stage_idle(8.0)
            start_xy = self._display_xy(status)
            plan = build_surface_route_plan(self._config, start_xy)
            measurements = plan.measurements
            movement_route = plan.movement_points()
            metadata = route_metadata(
                self._config,
                measurements,
                approach_route=plan.approach,
                start_xy=start_xy,
            )
            metadata["start_stage_status"] = status
            metadata["start_xy_mm"] = {"x": start_xy[0], "y": start_xy[1]}
            metadata["started_at"] = datetime.now(timezone.utc).isoformat()
            output_dir = Path(self._config.output_dir)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_path = output_dir / f"surface_map_{self._config.route_mode}_{stamp}"

            self.route_ready.emit(movement_route, metadata)
            self.status_changed.emit(
                f"Route: {len(measurements)} points, "
                f"{len(plan.approach)} approach waypoints, "
                f"{metadata['route_length_mm']:.1f} mm."
            )

            for waypoint in plan.approach:
                if self._stop_requested.is_set():
                    message = "Surface map stopped by user."
                    break
                self._move_to_xy(waypoint)
            start_indicator: float | None = None
            t0 = time.monotonic()

            for index, target in enumerate(measurements):
                if self._stop_requested.is_set():
                    message = "Surface map stopped by user."
                    break
                self._move_to_xy(target)
                if self._config.settle_s > 0.0 and self._stop_requested.wait(
                    max(0.0, float(self._config.settle_s))
                ):
                    message = "Surface map stopped by user."
                    break
                sample = self._sample_indicator()
                indicator_mm = float(sample["median_mm"])
                if start_indicator is None:
                    start_indicator = indicator_mm
                    metadata["start_indicator_sample"] = sample
                    metadata["start_indicator_mm"] = start_indicator
                delta_mm = indicator_mm - start_indicator
                record = SurfaceMapRecord(
                    index=index,
                    x_mm=float(target[0]),
                    y_mm=float(target[1]),
                    radius_mm=math.hypot(float(target[0]), float(target[1])),
                    theta_rad=math.atan2(float(target[1]), float(target[0])),
                    indicator_mm=indicator_mm,
                    indicator_delta_mm=delta_mm,
                    timestamp_s=time.monotonic() - t0,
                    sample_count=int(sample["sample_count"]),
                    attempt_count=int(sample["attempt_count"]),
                    tracking_bad_count=int(sample["tracking_bad_count"]),
                )
                records.append(record)
                self.point_captured.emit(record, index + 1, len(measurements))
                if (
                    self._config.max_indicator_delta_mm > 0.0
                    and abs(delta_mm) > self._config.max_indicator_delta_mm
                ):
                    message = (
                        f"Indicator guard reached: {delta_mm:+.4f} mm "
                        f"at point {index + 1}."
                    )
                    break
                if base_path is not None and (index % 10 == 0 or index == len(measurements) - 1):
                    metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
                    metadata["complete"] = False
                    metadata["summary"] = records_summary(records)
                    save_surface_map(base_path, metadata=metadata, records=records)
            else:
                success = True
                message = "Surface map complete."

            if base_path is not None:
                metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
                metadata["complete"] = bool(success)
                metadata["stop_reason"] = message
                metadata["final_stage_status"] = self._request_stage_status()
                metadata["summary"] = records_summary(records)
                paths = save_surface_map(base_path, metadata=metadata, records=records)
                metadata["output_paths"] = [str(path) for path in paths]
        except Exception as exc:
            message = str(exc)
            self.status_changed.emit(message)
        self.finished.emit(success, message, metadata)

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        data = None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {url} failed: {exc}") from exc
        return json.loads(raw)

    def _request_stage_status(self, *, timeout: float = 5.0) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "event": threading.Event(),
            "result": None,
            "error": None,
        }
        self.stage_status_requested.emit(envelope)
        event = envelope["event"]
        if not isinstance(event, threading.Event) or not event.wait(timeout):
            raise TimeoutError("GUI did not return stage status in time.")
        error = envelope.get("error")
        if error:
            raise RuntimeError(str(error))
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("GUI returned invalid stage status.")
        return result

    def _request_stage_move(
        self,
        target: tuple[float, float],
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "event": threading.Event(),
            "target": (float(target[0]), float(target[1])),
            "result": None,
            "error": None,
        }
        self.stage_move_requested.emit(envelope)
        event = envelope["event"]
        if not isinstance(event, threading.Event) or not event.wait(timeout):
            raise TimeoutError("GUI did not accept the stage move in time.")
        error = envelope.get("error")
        if error:
            raise RuntimeError(str(error))
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("GUI returned invalid stage move response.")
        if not bool(result.get("accepted")):
            raise RuntimeError(str(result.get("message") or "Stage move was rejected."))
        return result

    @staticmethod
    def _display_xy(status: dict[str, Any]) -> tuple[float, float]:
        display = status.get("display_position")
        if not isinstance(display, dict):
            raise RuntimeError(f"Stage status has no display_position: {status}")
        return (float(display["X"]), float(display["Y"]))

    @staticmethod
    def _stage_is_idle(status: dict[str, Any]) -> bool:
        return (
            bool(status.get("connected"))
            and not bool(status.get("busy"))
            and str(status.get("state", "")).lower() == "idle"
            and status.get("active_coordinate_axis") in (None, "")
            and not bool(status.get("pending_targets"))
        )

    def _wait_stage_idle(self, timeout_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last_status: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if self._stop_requested.is_set():
                raise RuntimeError("Surface map stopped by user.")
            last_status = self._request_stage_status()
            if self._stage_is_idle(last_status):
                return last_status
            if self._stop_requested.wait(0.05):
                raise RuntimeError("Surface map stopped by user.")
        raise TimeoutError(f"Stage did not become idle; last={last_status}")

    def _move_to_xy(self, target: tuple[float, float]) -> None:
        if self._stop_requested.is_set():
            raise RuntimeError("Surface map stopped by user.")
        status = self._request_stage_status()
        current = self._display_xy(status)
        distance = math.hypot(float(target[0] - current[0]), float(target[1] - current[1]))
        try:
            feedrate = max(0.1, float(status.get("current_feedrate_mm_min", 0.0)))
        except (TypeError, ValueError):
            feedrate = 10.0
        timeout_s = max(
            20.0,
            distance / feedrate * 60.0 + 20.0,
        )
        if distance > 1e-9:
            self._request_stage_move(target, timeout=5.0)
        self._wait_stage_idle(timeout_s)

    def _indicator_reading(self) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"{self._config.indicator_api.rstrip('/')}/reading",
            timeout=3.0,
        )

    @staticmethod
    def _indicator_ok(payload: dict[str, Any]) -> bool:
        try:
            value = float(payload["reading_mm"])
        except (KeyError, TypeError, ValueError):
            return False
        return math.isfinite(value) and bool(payload.get("camera_ok")) and bool(
            payload.get("tracking_ok")
        )

    def _sample_indicator(self) -> dict[str, Any]:
        payload = self._indicator_reading()
        if not self._indicator_ok(payload):
            raise RuntimeError("Indicator returned an invalid sample.")
        value = float(payload["reading_mm"])
        return {
            "median_mm": value,
            "mean_mm": value,
            "sample_count": 1,
            "attempt_count": 1,
            "tracking_bad_count": 0 if payload.get("tracking_ok") else 1,
            "unique_timestamp_count": 1 if payload.get("timestamp") else 0,
        }


class SurfaceMapPanel(QWidget):
    """Control and live Plotly view for surface mapping."""

    running_changed = Signal(bool)

    def __init__(
        self,
        *,
        stage_status_provider: Callable[[], dict[str, Any]],
        stage_move_requester: Callable[[float, float], dict[str, Any]],
        settings_path: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._stage_status_provider = stage_status_provider
        self._stage_move_requester = stage_move_requester
        self._settings_path = Path(settings_path) if settings_path is not None else None
        self._thread: QThread | None = None
        self._worker: SurfaceMapWorker | None = None
        self._records: list[SurfaceMapRecord] = []
        self._route: list[tuple[float, float]] = []
        self._plot_ready = False
        self._pending_plot_payload: dict[str, Any] | None = None
        self._capture_running = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(6)

        self._status_label = QLabel("Surface map: idle", self)
        self._status_label.setWordWrap(True)
        root_layout.addWidget(self._status_label)

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        root_layout.addWidget(self._progress)

        if QWebEngineView is None:
            self._web_view = None
            root_layout.addWidget(QLabel("QtWebEngine is not available.", self), 1)
        else:
            self._web_view = QWebEngineView(self)
            self._web_view.setMinimumHeight(300)
            self._web_view.loadFinished.connect(self._on_plot_loaded)
            plotly_script, plotly_base_url = _plotly_script_tag_and_base_url()
            self._web_view.setHtml(
                PLOTLY_HTML.replace("__PLOTLY_SCRIPT__", plotly_script),
                plotly_base_url,
            )
            root_layout.addWidget(self._web_view, 1)

        settings_group = QGroupBox("Capture Settings", self)
        settings_layout = QFormLayout(settings_group)

        self._mode_combo = QComboBox(settings_group)
        self._mode_combo.addItem("Circle", "circle")
        self._mode_combo.addItem("Spiral", "spiral")
        self._mode_combo.addItem("Grid", "grid")
        settings_layout.addRow("Route", self._mode_combo)

        self._indicator_api_edit = QLineEdit("http://127.0.0.1:8000", settings_group)
        settings_layout.addRow("Indicator API", self._indicator_api_edit)

        self._inner_diameter_spin = self._double_spin(0.001, 500.0, 2.0, 3, " mm")
        settings_layout.addRow("Inner diameter", self._inner_diameter_spin)

        self._outer_diameter_spin = self._double_spin(0.001, 500.0, 56.0, 3, " mm")
        settings_layout.addRow("Outer diameter", self._outer_diameter_spin)

        self._circle_count_spin = QSpinBox(settings_group)
        self._circle_count_spin.setRange(1, 100)
        self._circle_count_spin.setValue(1)
        settings_layout.addRow("Circle count", self._circle_count_spin)

        self._radial_pitch_spin = self._double_spin(0.1, 50.0, 2.0, 3, " mm")
        settings_layout.addRow("Spiral pitch", self._radial_pitch_spin)

        self._point_spacing_spin = self._double_spin(0.1, 20.0, 2.0, 3, " mm")
        settings_layout.addRow("Point spacing", self._point_spacing_spin)

        self._grid_step_spin = self._double_spin(0.1, 50.0, 2.0, 3, " mm")
        settings_layout.addRow("Grid step", self._grid_step_spin)

        self._settle_spin = self._double_spin(0.0, 30.0, 0.08, 3, " s")
        settings_layout.addRow("Settle", self._settle_spin)

        self._guard_spin = self._double_spin(0.0, 10.0, 0.0, 4, " mm")
        settings_layout.addRow("Abort delta (0 off)", self._guard_spin)

        self._output_dir_edit = QLineEdit("calibrations", settings_group)
        settings_layout.addRow("Output dir", self._output_dir_edit)

        root_layout.addWidget(settings_group)

        button_row = QHBoxLayout()
        self._preview_button = QPushButton("Preview Route", self)
        self._start_button = QPushButton("Start", self)
        self._stop_button = QPushButton("Stop", self)
        button_row.addWidget(self._preview_button)
        button_row.addWidget(self._start_button)
        button_row.addWidget(self._stop_button)
        root_layout.addLayout(button_row)

        self._summary_label = QLabel("Points: 0", self)
        self._summary_label.setWordWrap(True)
        root_layout.addWidget(self._summary_label)

        self._preview_button.clicked.connect(self._preview_route)
        self._start_button.clicked.connect(self._start_capture)
        self._stop_button.clicked.connect(self._stop_capture)
        self._mode_combo.currentIndexChanged.connect(lambda _index: self._on_mode_changed())
        self._load_settings()
        for widget in (
            self._inner_diameter_spin,
            self._outer_diameter_spin,
            self._radial_pitch_spin,
            self._point_spacing_spin,
            self._grid_step_spin,
        ):
            widget.valueChanged.connect(lambda _value: self._render_plot())
        self._circle_count_spin.valueChanged.connect(lambda _value: self._render_plot())
        self._set_running(False)
        self._update_mode_fields()
        self._render_plot()

    @staticmethod
    def _double_spin(
        minimum: float,
        maximum: float,
        value: float,
        decimals: int,
        suffix: str,
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(10 ** -max(0, decimals - 1))
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin

    def _config_from_ui(self) -> SurfaceMapConfig:
        return SurfaceMapConfig(
            route_mode=str(self._mode_combo.currentData() or "circle"),
            indicator_api=self._indicator_api_edit.text().strip() or "http://127.0.0.1:8000",
            output_dir=self._output_dir_edit.text().strip() or "calibrations",
            inner_diameter_mm=self._inner_diameter_spin.value(),
            outer_diameter_mm=self._outer_diameter_spin.value(),
            circle_count=self._circle_count_spin.value(),
            radial_pitch_mm=self._radial_pitch_spin.value(),
            point_spacing_mm=self._point_spacing_spin.value(),
            grid_step_mm=self._grid_step_spin.value(),
            settle_s=self._settle_spin.value(),
            max_indicator_delta_mm=self._guard_spin.value(),
        )

    def _settings_payload(self) -> dict[str, Any]:
        return {
            "route_mode": str(self._mode_combo.currentData() or "circle"),
            "indicator_api": self._indicator_api_edit.text().strip(),
            "output_dir": self._output_dir_edit.text().strip(),
            "inner_diameter_mm": self._inner_diameter_spin.value(),
            "outer_diameter_mm": self._outer_diameter_spin.value(),
            "circle_count": self._circle_count_spin.value(),
            "radial_pitch_mm": self._radial_pitch_spin.value(),
            "point_spacing_mm": self._point_spacing_spin.value(),
            "grid_step_mm": self._grid_step_spin.value(),
            "settle_s": self._settle_spin.value(),
            "max_indicator_delta_mm": self._guard_spin.value(),
        }

    def _load_settings(self) -> None:
        if self._settings_path is None or not self._settings_path.exists():
            return
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        mode = str(data.get("route_mode", "")).strip().lower()
        mode_index = self._mode_combo.findData(mode)
        if mode_index >= 0:
            self._mode_combo.setCurrentIndex(mode_index)
        for key, widget in (
            ("indicator_api", self._indicator_api_edit),
            ("output_dir", self._output_dir_edit),
        ):
            value = data.get(key)
            if isinstance(value, str):
                widget.setText(value)
        for key, widget in (
            ("inner_diameter_mm", self._inner_diameter_spin),
            ("outer_diameter_mm", self._outer_diameter_spin),
            ("radial_pitch_mm", self._radial_pitch_spin),
            ("point_spacing_mm", self._point_spacing_spin),
            ("grid_step_mm", self._grid_step_spin),
            ("settle_s", self._settle_spin),
            ("max_indicator_delta_mm", self._guard_spin),
        ):
            value = data.get(key)
            try:
                if isinstance(value, (int, float, str)):
                    widget.setValue(float(value))
            except (TypeError, ValueError):
                pass
        value = data.get("circle_count")
        try:
            if isinstance(value, (int, float, str)):
                self._circle_count_spin.setValue(int(float(value)))
        except (TypeError, ValueError):
            pass

    def _save_settings(self) -> None:
        if self._settings_path is None:
            return
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(
                json.dumps(self._settings_payload(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return

    def save_settings(self) -> None:
        self._save_settings()

    def _start_capture(self) -> None:
        if self._worker is not None:
            return
        self._save_settings()
        config = self._config_from_ui()
        self._records.clear()
        self._render_plot()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._thread = QThread(self)
        self._worker = SurfaceMapWorker(config)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.route_ready.connect(self._on_route_ready)
        self._worker.point_captured.connect(self._on_point_captured)
        self._worker.status_changed.connect(self._status_label.setText)
        self._worker.stage_status_requested.connect(self._on_stage_status_requested)
        self._worker.stage_move_requested.connect(self._on_stage_move_requested)
        self._worker.finished.connect(self._on_capture_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._clear_worker_thread)
        self._set_running(True)
        self._status_label.setText("Surface map: starting")
        self._thread.start()

    def _stop_capture(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._status_label.setText("Surface map: stopping")

    def stop_capture(self) -> None:
        self._stop_capture()

    def is_capture_running(self) -> bool:
        return self._capture_running

    def _preview_route(self) -> None:
        try:
            self._save_settings()
            config = self._config_from_ui()
            start_xy = self._preview_start_xy(config)
            plan = build_surface_route_plan(config, start_xy)
            self._route = plan.movement_points()
            self._render_plot()
            self._status_label.setText(
                f"Preview: {len(plan.measurements)} points, "
                f"{len(plan.approach)} approach waypoints, "
                f"{route_length_mm(self._route):.1f} mm."
            )
        except Exception as exc:
            self._status_label.setText(str(exc))

    def _preview_start_xy(self, config: SurfaceMapConfig) -> tuple[float, float]:
        try:
            status = self._stage_status_provider()
            start_xy = SurfaceMapWorker._display_xy(status)
            _table_radius, inner_guard, _inner_route, outer_guard = safe_radius_limits(config)
            radius = math.hypot(start_xy[0], start_xy[1])
            if inner_guard <= radius <= outer_guard:
                return start_xy
        except Exception:
            pass
        _table_radius, _inner_guard, _inner_route, outer_guard = safe_radius_limits(config)
        return (outer_guard, 0.0)

    @Slot(object)
    def _on_stage_status_requested(self, envelope: object) -> None:
        if not isinstance(envelope, dict):
            return
        event = envelope.get("event")
        try:
            envelope["result"] = self._stage_status_provider()
            envelope["error"] = None
        except Exception as exc:
            envelope["result"] = None
            envelope["error"] = str(exc)
        finally:
            if isinstance(event, threading.Event):
                event.set()

    @Slot(object)
    def _on_stage_move_requested(self, envelope: object) -> None:
        if not isinstance(envelope, dict):
            return
        event = envelope.get("event")
        try:
            target = envelope.get("target")
            if not isinstance(target, (tuple, list)) or len(target) < 2:
                raise ValueError("Stage move target is invalid.")
            envelope["result"] = self._stage_move_requester(
                float(target[0]),
                float(target[1]),
            )
            envelope["error"] = None
        except Exception as exc:
            envelope["result"] = None
            envelope["error"] = str(exc)
        finally:
            if isinstance(event, threading.Event):
                event.set()

    def _on_route_ready(self, route: object, _metadata: object) -> None:
        if isinstance(route, list):
            self._route = [
                (float(point[0]), float(point[1]))
                for point in route
                if isinstance(point, (tuple, list)) and len(point) >= 2
            ]
            self._render_plot()

    def _on_point_captured(self, record: object, completed: int, total: int) -> None:
        if isinstance(record, SurfaceMapRecord):
            self._records.append(record)
        self._progress.setRange(0, max(1, int(total)))
        self._progress.setValue(int(completed))
        self._render_plot()
        self._refresh_summary()

    def _on_capture_finished(
        self,
        success: bool,
        message: str,
        metadata: object,
    ) -> None:
        self._set_running(False)
        output_text = ""
        if isinstance(metadata, dict):
            paths = metadata.get("output_paths")
            if isinstance(paths, list) and paths:
                output_text = f" Saved: {paths[0]}"
        state = "complete" if success else "stopped"
        self._status_label.setText(f"Surface map {state}: {message}{output_text}")
        self._refresh_summary()

    def _clear_worker_thread(self) -> None:
        self._worker = None
        self._thread = None
        self._update_mode_fields()

    def _set_running(self, running: bool) -> None:
        running = bool(running)
        if self._capture_running != running:
            self._capture_running = running
            self.running_changed.emit(running)
        for widget in (
            self._mode_combo,
            self._indicator_api_edit,
            self._inner_diameter_spin,
            self._outer_diameter_spin,
            self._circle_count_spin,
            self._radial_pitch_spin,
            self._point_spacing_spin,
            self._grid_step_spin,
            self._settle_spin,
            self._guard_spin,
            self._output_dir_edit,
            self._preview_button,
        ):
            widget.setEnabled(not running)
        self._start_button.setEnabled(not running)
        self._stop_button.setEnabled(running)
        self._update_mode_fields()

    def _on_mode_changed(self) -> None:
        self._update_mode_fields()
        self._render_plot()

    def _update_mode_fields(self) -> None:
        running = self._worker is not None
        mode = str(self._mode_combo.currentData() or "")
        is_circle = mode == "circle"
        is_spiral = mode == "spiral"
        is_grid = mode == "grid"
        self._circle_count_spin.setEnabled(is_circle and not running)
        self._radial_pitch_spin.setEnabled(is_spiral and not running)
        self._point_spacing_spin.setEnabled((is_circle or is_spiral) and not running)
        self._grid_step_spin.setEnabled(is_grid and not running)

    def _plot_payload(self) -> dict[str, Any]:
        try:
            config = self._config_from_ui()
            table_radius, inner_guard, _inner_route, outer_guard = safe_radius_limits(config)
        except Exception:
            table_radius = 30.0
            inner_guard = 1.0
            outer_guard = 28.5
        return {
            "route": [
                {"x": x, "y": y, "r": math.hypot(x, y)}
                for x, y in self._route
            ],
            "records": [
                {
                    "index": record.index,
                    "x": record.x_mm,
                    "y": record.y_mm,
                    "r": record.radius_mm,
                    "indicator": record.indicator_mm,
                    "delta": record.indicator_delta_mm,
                }
                for record in self._records
            ],
            "limits": {
                "table_radius_mm": table_radius,
                "inner_guard_radius_mm": inner_guard,
                "outer_radius_mm": outer_guard,
            },
        }

    def _render_plot(self) -> None:
        payload = self._plot_payload()
        if self._web_view is None:
            return
        if not self._plot_ready:
            self._pending_plot_payload = payload
            return
        script = "window.drawSurfaceMap(" + json.dumps(payload, allow_nan=False) + ");"
        self._web_view.page().runJavaScript(script)

    def _on_plot_loaded(self, ok: bool) -> None:
        self._plot_ready = bool(ok)
        payload = self._pending_plot_payload or self._plot_payload()
        self._pending_plot_payload = None
        if self._plot_ready:
            script = "window.drawSurfaceMap(" + json.dumps(payload, allow_nan=False) + ");"
            self._web_view.page().runJavaScript(script)

    def _refresh_summary(self) -> None:
        summary = records_summary(self._records)
        if not self._records:
            self._summary_label.setText("Points: 0")
            return
        self._summary_label.setText(
            "Points: {points}  delta min/max/span: "
            "{indicator_delta_min_mm:+.4f} / {indicator_delta_max_mm:+.4f} / "
            "{indicator_delta_span_mm:.4f} mm".format(**summary)
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._save_settings()
        self._stop_capture()
        super().closeEvent(event)


class SurfaceMapWindow(QMainWindow):
    """Top-level window that owns the surface map panel."""

    capture_running_changed = Signal(bool)

    def __init__(
        self,
        *,
        stage_status_provider: Callable[[], dict[str, Any]],
        stage_move_requester: Callable[[float, float], dict[str, Any]],
        settings_path: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Surface Map")
        self._panel = SurfaceMapPanel(
            stage_status_provider=stage_status_provider,
            stage_move_requester=stage_move_requester,
            settings_path=settings_path,
            parent=self,
        )
        self._panel.running_changed.connect(self.capture_running_changed.emit)
        self.setCentralWidget(self._panel)
        self.resize(980, 760)

    def stop_capture(self) -> None:
        self._panel.stop_capture()

    def is_capture_running(self) -> bool:
        return self._panel.is_capture_running()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._panel.save_settings()
        self._panel.stop_capture()
        super().closeEvent(event)


__all__ = ["SurfaceMapPanel", "SurfaceMapWindow"]
