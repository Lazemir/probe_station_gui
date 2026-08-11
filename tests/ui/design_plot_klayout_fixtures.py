"""Pytest registration for shared Design plot KLayout fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.model import DesignDocument
from probe_station_gui.route.model import MeasurementRoute
from tests.ui import design_plot_klayout_support as support


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return support.qt_app()


@pytest.fixture
def pane(monkeypatch, qt_app: QApplication):
    yield from support.pane(monkeypatch, qt_app)


@pytest.fixture
def document(tmp_path: Path) -> DesignDocument:
    return support.document(tmp_path)


@pytest.fixture
def window(monkeypatch, qt_app: QApplication):
    yield from support.window(monkeypatch, qt_app)


@pytest.fixture
def distant_hidden_markup(document: DesignDocument):
    return support.distant_hidden_markup(document)


@pytest.fixture
def distant_route(document: DesignDocument) -> MeasurementRoute:
    return support.distant_route(document)
