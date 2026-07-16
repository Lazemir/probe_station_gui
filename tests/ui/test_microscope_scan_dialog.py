from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.microscope_scan_dialog import MicroscopeScanDialog


@pytest.fixture(scope="module")
def app():
    existing = QApplication.instance()
    if existing is not None:
        yield existing
        return
    created = QApplication([])
    yield created
    created.quit()


def test_scan_dialog_enables_auto_exposure_by_default(app) -> None:
    dialog = MicroscopeScanDialog(default_output_dir="C:/scan")
    try:
        configuration = dialog.current_configuration()
        assert configuration.auto_exposure is True

        dialog._auto_exposure_check.setChecked(False)
        assert dialog.current_configuration().auto_exposure is False
    finally:
        dialog.close()
