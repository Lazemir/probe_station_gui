import sys, time, threading
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from rotpy.system import SpinSystem
from rotpy.camera import CameraList
import serial
from serial.tools import list_ports


# ------------------ camera in a separate thread ------------------
class Grabber(QtCore.QObject):
    frame_ready = QtCore.Signal(QtGui.QImage)
    error = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self._running = False

    @QtCore.Slot()
    def start(self):
        self._running = True
        try:
            system = SpinSystem()
            cams = CameraList.create_from_system(system, True, True)
            if cams.get_size() < 1:
                self.error.emit("No cameras detected")
                return

            cam = cams.create_camera_by_index(0)
            cam.init_cam()
            cam.camera_nodes.PixelFormat.set_node_value_from_str("RGB8")
            cam.begin_acquisition()

            while self._running:
                try:
                    icam = cam.get_next_image(timeout=5)
                except Exception as e:
                    self.error.emit(str(e))
                    time.sleep(0.1)
                    continue

                img = icam.deep_copy_image(icam)
                icam.release()

                h, w, stride = img.get_height(), img.get_width(), img.get_stride()
                buf = img.get_image_data()
                arr = np.frombuffer(buf, dtype=np.uint8, count=stride*h).reshape(h, stride)
                arr = arr[:, :w*3].reshape(h, w, 3)

                qimg = QtGui.QImage(arr.data, w, h, w*3, QtGui.QImage.Format_RGB888)
                self.frame_ready.emit(qimg.copy())

            cam.end_acquisition()
            cam.deinit_cam()
            cam.release()
        except Exception as e:
            self.error.emit(f"init: {e!r}")

    @QtCore.Slot()
    def stop(self):
        self._running = False


# ------------------ window with overlay ------------------
class MicroscopeView(QtWidgets.QWidget):
    clicked = QtCore.Signal(float, float)  # dx, dy in pixels relative to the center

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Microscope Qt")
        self.setMinimumSize(960, 720)
        self._pix = None
        self._cross = None

    def set_frame(self, qimg: QtGui.QImage):
        self._pix = QtGui.QPixmap.fromImage(qimg)
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtCore.Qt.black)
        if self._pix:
            scaled = self._pix.scaled(self.size(), QtCore.Qt.KeepAspectRatio)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)

            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setPen(QtGui.QPen(QtGui.QColor("lime"), 1))
            cx = self.width() // 2
            cy = self.height() // 2
            if self._cross:
                px, py = self._cross
            else:
                px, py = cx, cy
            p.drawLine(0, py, self.width(), py)
            p.drawLine(px, 0, px, self.height())
            p.drawEllipse(QtCore.QPoint(px, py), 4, 4)
        p.end()

    def mousePressEvent(self, ev: QtGui.QMouseEvent):
        if ev.button() == QtCore.Qt.LeftButton:
            cx = self.width() / 2
            cy = self.height() / 2
            dx = ev.position().x() - cx
            dy = cy - ev.position().y()
            self._cross = (ev.position().x(), ev.position().y())
            self.clicked.emit(dx, dy)
            self.update()


# ------------------ serial scanner dialog ------------------
class SerialScannerDialog(QtWidgets.QDialog):
    connected = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Serial Scanner")
        self.setModal(True)
        self._serial = None

        self.port_combo = QtWidgets.QComboBox()
        self.baud_combo = QtWidgets.QComboBox()
        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)

        baud_rates = [
            "250000",
            "230400",
            "200000",
            "128000",
            "115200",
            "57600",
            "38400",
            "19200",
            "9600",
        ]
        self.baud_combo.addItems(baud_rates)
        self.baud_combo.setCurrentIndex(baud_rates.index("115200"))

        refresh_button = QtWidgets.QPushButton("Refresh")
        connect_button = QtWidgets.QPushButton("Connect")
        cancel_button = QtWidgets.QPushButton("Cancel")

        refresh_button.clicked.connect(self.populate_ports)
        connect_button.clicked.connect(self.on_connect)
        cancel_button.clicked.connect(self.reject)

        form = QtWidgets.QFormLayout()
        form.addRow("Port", self.port_combo)
        form.addRow("Baud rate", self.baud_combo)

        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(refresh_button)
        button_layout.addWidget(connect_button)
        button_layout.addWidget(cancel_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addLayout(button_layout)

        self.populate_ports()

    def populate_ports(self):
        self.port_combo.clear()
        ports = list_ports.comports()
        if not ports:
            self.port_combo.addItem("No ports found")
            self.port_combo.setEnabled(False)
        else:
            self.port_combo.setEnabled(True)
            for port in ports:
                description = f"{port.device} — {port.description}"
                self.port_combo.addItem(description, port.device)
        self.status_label.clear()

    def on_connect(self):
        if not self.port_combo.isEnabled():
            self.status_label.setText("No serial ports available. Use Refresh to scan again.")
            return

        port_name = self.port_combo.currentData()
        if port_name is None:
            port_name = self.port_combo.currentText().split(" ")[0]
        baud_rate = int(self.baud_combo.currentText())

        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None

        try:
            self._serial = serial.Serial(port=port_name, baudrate=baud_rate, timeout=1)
        except serial.SerialException as exc:
            self.status_label.setText(f"Connection failed: {exc}")
            return

        self.status_label.setText(f"Connected to {port_name} @ {baud_rate} baud")
        self.connected.emit(self._serial)
        self._serial = None
        self.accept()

    def closeEvent(self, event):
        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None
        super().closeEvent(event)


# ------------------ main application ------------------
class Main(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.view = MicroscopeView()
        self.setCentralWidget(self.view)
        self.serial_connection = None

        self.grabber = Grabber()
        self.thread = QtCore.QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.grabber.frame_ready.connect(self.view.set_frame)
        self.grabber.error.connect(self.on_error)
        self.thread.start()

        tools_menu = self.menuBar().addMenu("Tools")
        serial_action = QtGui.QAction("Serial Scanner", self)
        serial_action.triggered.connect(self.open_serial_scanner)
        tools_menu.addAction(serial_action)

    def on_click(self, dx, dy):
        print(f"Click Δx={dx:.1f}px  Δy={dy:.1f}px")
        # later you’ll add coordinate conversion and G-code sending here

    def on_error(self, msg):
        print("Camera error:", msg)

    def open_serial_scanner(self):
        dialog = SerialScannerDialog(self)
        dialog.connected.connect(self.on_serial_connected)
        dialog.exec()

    def on_serial_connected(self, serial_port):
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = serial_port
        print(
            f"Serial connected: {self.serial_connection.port} @ {self.serial_connection.baudrate} baud"
        )

    def closeEvent(self, e):
        self.grabber.stop()
        self.thread.quit()
        self.thread.wait()
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        e.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = Main()
    win.show()
    sys.exit(app.exec())
