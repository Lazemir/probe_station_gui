import sys, time, threading
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from rotpy.system import SpinSystem
from rotpy.camera import CameraList


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


# ------------------ main application ------------------
class Main(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.view = MicroscopeView()
        self.setCentralWidget(self.view)

        self.grabber = Grabber()
        self.thread = QtCore.QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.grabber.frame_ready.connect(self.view.set_frame)
        self.grabber.error.connect(self.on_error)
        self.thread.start()

    def on_click(self, dx, dy):
        print(f"Click Δx={dx:.1f}px  Δy={dy:.1f}px")
        # later you’ll add coordinate conversion and G-code sending here

    def on_error(self, msg):
        print("Camera error:", msg)

    def closeEvent(self, e):
        self.grabber.stop()
        self.thread.quit()
        self.thread.wait()
        e.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = Main()
    win.show()
    sys.exit(app.exec())
