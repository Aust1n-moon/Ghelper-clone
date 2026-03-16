#!/usr/bin/env python3
"""
G-Helper Clone — ASUS ROG Zephyrus G14 2024 (GA403) control panel for Fedora Linux
"""

import sys
import os
import subprocess
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QButtonGroup, QGroupBox, QSystemTrayIcon,
    QMenu, QFrame, QProgressBar, QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction


# ---------------------------------------------------------------------------
# Backend: thin wrappers around asusctl / sysfs
# ---------------------------------------------------------------------------

def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), -1


def _sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


class Backend:
    @staticmethod
    def get_profile():
        out, _, rc = _run("asusctl profile get")
        if rc == 0:
            for p in ("LowPower", "Balanced", "Performance"):
                if p.lower() in out.lower():
                    return p
        return "Unknown"

    @staticmethod
    def set_profile(profile):
        out, err, rc = _run(f"asusctl profile set {profile}")
        return rc == 0, err or out

    @staticmethod
    def get_kbd_brightness():
        out, _, rc = _run("asusctl leds get")
        if rc == 0:
            for level in ("Off", "Low", "Med", "High"):
                if level.lower() in out.lower():
                    return level
        return "Unknown"

    @staticmethod
    def set_kbd_brightness(level):
        out, err, rc = _run(f"asusctl leds set {level.lower()}")
        return rc == 0, err or out

    @staticmethod
    def set_slash(enabled):
        flag = "--enable" if enabled else "--disable"
        out, err, rc = _run(f"asusctl slash {flag}")
        return rc == 0, err or out

    @staticmethod
    def get_battery():
        base = "/sys/class/power_supply/BAT1"
        cap_raw = _sysfs(f"{base}/capacity")
        if cap_raw is None:
            return {"status": "Unknown", "capacity": 0}

        info = {
            "status":   _sysfs(f"{base}/status") or "Unknown",
            "capacity": int(cap_raw),
        }

        current_now  = _sysfs(f"{base}/current_now")
        voltage_now  = _sysfs(f"{base}/voltage_now")
        charge_now   = _sysfs(f"{base}/charge_now")
        charge_full  = _sysfs(f"{base}/charge_full")
        charge_design = _sysfs(f"{base}/charge_full_design")
        charge_limit = _sysfs(f"{base}/charge_control_end_threshold")

        if current_now and voltage_now:
            amps = int(current_now) / 1_000_000
            volts = int(voltage_now) / 1_000_000
            power_w = round(amps * volts, 1)
            if power_w > 0:
                info["power_w"] = power_w
                if charge_now and voltage_now:
                    wh_remaining = int(charge_now) / 1_000_000 * volts
                    info["time_h"] = round(wh_remaining / (amps * volts), 1)

        if charge_full and charge_design and int(charge_design) > 0:
            info["health"] = round(int(charge_full) * 100 / int(charge_design), 1)

        if charge_limit:
            info["charge_limit"] = int(charge_limit)

        return info

    @staticmethod
    def set_charge_limit(limit):
        out, err, rc = _run(f"asusctl battery limit {limit}")
        return rc == 0, err or out

    @staticmethod
    def get_gpu_mode():
        out, _, rc = _run("supergfxctl -g")
        if rc == 0:
            for m in ("Integrated", "Hybrid", "Dedicated", "Vfio"):
                if m.lower() in out.lower():
                    return m
        return "Unknown"

    @staticmethod
    def set_gpu_mode(mode):
        out, err, rc = _run(f"supergfxctl -m {mode}", timeout=60)
        return rc == 0, err or out


# ---------------------------------------------------------------------------
# Background status refresh thread
# ---------------------------------------------------------------------------

class StatusWorker(QThread):
    done = pyqtSignal(dict)

    def run(self):
        self.done.emit({
            "profile": Backend.get_profile(),
            "kbd":     Backend.get_kbd_brightness(),
            "battery": Backend.get_battery(),
            "gpu":     Backend.get_gpu_mode(),
        })


class GpuSwitchWorker(QThread):
    done = pyqtSignal(bool, str, str)  # ok, msg, mode

    def __init__(self, mode):
        super().__init__()
        self._mode = mode

    def run(self):
        ok, msg = Backend.set_gpu_mode(self._mode)
        self.done.emit(ok, msg, self._mode)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_icon(letter="G", bg="#2563eb", size=64):
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(bg))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, size - 4, size - 4)
    p.setPen(QColor("white"))
    f = QFont("Arial", size // 2, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, letter)
    p.end()
    return QIcon(px)


class _ButtonRow(QWidget):
    """Horizontally-laid-out exclusive toggle buttons."""

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        lo = QHBoxLayout(self)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(6)
        self.buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for lbl in labels:
            btn = QPushButton(lbl)
            btn.setCheckable(True)
            self.buttons[lbl] = btn
            self._group.addButton(btn)
            lo.addWidget(btn)
        lo.addStretch()

    def set_active(self, label):
        btn = self.buttons.get(label)
        if btn:
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

    def clear(self):
        for btn in self.buttons.values():
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

STYLE = """
QWidget {
    background-color: #000000;
    color: #e2e8f0;
    font-family: 'Inter', 'Segoe UI', sans-serif;
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #1e293b;
    border-radius: 8px;
    margin-top: 14px;
    padding: 8px 8px 6px 8px;
    font-size: 10px;
    font-weight: bold;
    color: #475569;
    letter-spacing: 1px;
    text-transform: uppercase;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QPushButton {
    background-color: #0f172a;
    border: 1px solid #1e293b;
    border-radius: 6px;
    padding: 6px 14px;
    color: #94a3b8;
    min-width: 70px;
}
QPushButton:hover   { background-color: #1e293b; border-color: #38bdf8; color: #e2e8f0; }
QPushButton:checked { background-color: #0ea5e9; border-color: #38bdf8; color: #fff; font-weight: bold; }
QPushButton:checked:hover { background-color: #38bdf8; }
QPushButton:disabled { color: #334155; border-color: #0f172a; }
QProgressBar {
    border: 1px solid #1e293b;
    border-radius: 4px;
    background: #0f172a;
    text-align: center;
    height: 18px;
    color: #e2e8f0;
    font-size: 11px;
}
QProgressBar::chunk { border-radius: 3px; background-color: #0ea5e9; }
QFrame[frameShape="4"] { color: #1e293b; }
QMenu {
    background-color: #0f172a;
    border: 1px solid #1e293b;
    color: #e2e8f0;
    padding: 4px;
}
QMenu::item { padding: 6px 20px; border-radius: 4px; }
QMenu::item:selected { background-color: #1e293b; }
QMenu::separator { height: 1px; background: #1e293b; margin: 4px 8px; }
"""


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("G-Helper")
        self.setFixedWidth(420)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self._build_ui()
        self._connect()
        self._worker = None
        self._gpu_worker = None
        self._gpu_pending = None
        self._schedule_refresh()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(12, 12, 12, 10)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("G-Helper")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #38bdf8; letter-spacing: 1px;")
        self._hdr_status = QLabel("…")
        self._hdr_status.setStyleSheet("color: #475569; font-size: 11px;")
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self._hdr_status)
        root.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #1e293b; margin: 2px 0;")
        root.addWidget(sep)

        # Profile
        g = QGroupBox("Performance Profile")
        gl = QVBoxLayout(g)
        self._profile = _ButtonRow(["LowPower", "Balanced", "Performance"])
        gl.addWidget(self._profile)
        root.addWidget(g)

        # GPU Mode
        g = QGroupBox("GPU Mode")
        gl = QVBoxLayout(g)
        self._gpu = _ButtonRow(["Integrated", "Hybrid", "Dedicated"])
        gl.addWidget(self._gpu)
        self._gpu_auto_restart = QCheckBox("Auto restart session after switch")
        self._gpu_auto_restart.setStyleSheet("color: #94a3b8; font-size: 11px;")
        gl.addWidget(self._gpu_auto_restart)
        root.addWidget(g)

        # Keyboard
        g = QGroupBox("Keyboard Backlight")
        gl = QVBoxLayout(g)
        self._kbd = _ButtonRow(["Off", "Low", "Med", "High"])
        gl.addWidget(self._kbd)
        root.addWidget(g)

        # Slash LED
        g = QGroupBox("Slash LED")
        gl = QHBoxLayout(g)
        self._slash_on_btn  = QPushButton("On")
        self._slash_off_btn = QPushButton("Off")
        self._slash_on_btn.setCheckable(True)
        self._slash_off_btn.setCheckable(True)
        sg = QButtonGroup(self)
        sg.addButton(self._slash_on_btn)
        sg.addButton(self._slash_off_btn)
        gl.addWidget(self._slash_on_btn)
        gl.addWidget(self._slash_off_btn)
        gl.addStretch()
        root.addWidget(g)

        # Battery
        g = QGroupBox("Battery")
        gl = QVBoxLayout(g)
        bar_row = QHBoxLayout()
        self._bat_bar  = QProgressBar()
        self._bat_info = QLabel("")
        self._bat_info.setStyleSheet("color: #64748b; font-size: 11px; min-width: 130px;")
        bar_row.addWidget(self._bat_bar)
        bar_row.addWidget(self._bat_info)
        gl.addLayout(bar_row)

        limit_row = QHBoxLayout()
        lbl = QLabel("Charge limit:")
        lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        self._bat_limit = _ButtonRow(["60%", "80%", "100%"])
        self._bat_health = QLabel("")
        self._bat_health.setStyleSheet("color: #475569; font-size: 10px;")
        limit_row.addWidget(lbl)
        limit_row.addWidget(self._bat_limit)
        gl.addLayout(limit_row)
        gl.addWidget(self._bat_health)
        root.addWidget(g)

        # Status bar
        self._status = QLabel("Ready")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet("color: #334155; font-size: 11px; padding: 2px;")
        root.addWidget(self._status)

    # ---------------------------------------------------------------- connect signals

    def _connect(self):
        for name, btn in self._profile.buttons.items():
            btn.clicked.connect(lambda _, n=name: self._do_profile(n))

        for name, btn in self._kbd.buttons.items():
            btn.clicked.connect(lambda _, n=name: self._do_kbd(n))

        self._slash_on_btn.clicked.connect(lambda: self._do_slash(True))
        self._slash_off_btn.clicked.connect(lambda: self._do_slash(False))

        for name, btn in self._gpu.buttons.items():
            btn.clicked.connect(lambda _, n=name: self._do_gpu(n))

        for name, btn in self._bat_limit.buttons.items():
            pct = int(name.replace("%", ""))
            btn.clicked.connect(lambda _, p=pct: self._do_limit(p))

    # ---------------------------------------------------------------- actions

    def _set_status(self, msg, color="#334155"):
        self._status.setText(msg)
        self._status.setStyleSheet(f"color: {color}; font-size: 11px; padding: 2px;")

    def _do_profile(self, profile):
        ok, msg = Backend.set_profile(profile)
        self._set_status(f"Profile → {profile}" if ok else f"Error: {msg[:70]}",
                         "#0ea5e9" if ok else "#ef4444")

    def _do_kbd(self, level):
        ok, msg = Backend.set_kbd_brightness(level)
        self._set_status(f"Keyboard → {level}" if ok else f"Error: {msg[:70]}",
                         "#0ea5e9" if ok else "#ef4444")

    def _do_slash(self, enabled):
        ok, msg = Backend.set_slash(enabled)
        self._set_status(f"Slash LED → {'On' if enabled else 'Off'}" if ok else f"Error: {msg[:70]}",
                         "#0ea5e9" if ok else "#ef4444")

    def _do_gpu(self, mode):
        if self._gpu_worker and self._gpu_worker.isRunning():
            self._set_status("GPU switch already in progress…", "#f59e0b")
            return
        self._set_status(f"Switching GPU to {mode}…", "#f59e0b")
        self._gpu.setEnabled(False)
        self._gpu_worker = GpuSwitchWorker(mode)
        self._gpu_worker.done.connect(self._on_gpu_done)
        self._gpu_worker.start()

    def _on_gpu_done(self, ok, msg, mode):
        self._gpu.setEnabled(True)
        if not ok:
            self._set_status(f"Error: {msg[:70]}", "#ef4444")
            return
        self._gpu_pending = mode
        if self._gpu_auto_restart.isChecked():
            self._set_status(f"GPU → {mode}  Restarting session…", "#0ea5e9")
            QTimer.singleShot(1500, lambda: _run(
                "dbus-send --session --type=method_call "
                "--dest=org.gnome.SessionManager "
                "/org/gnome/SessionManager "
                "org.gnome.SessionManager.Logout uint32:1",
                timeout=10
            ))
        else:
            self._set_status(f"GPU → {mode} (logout to apply)", "#0ea5e9")

    def _do_limit(self, limit):
        ok, msg = Backend.set_charge_limit(limit)
        self._set_status(f"Charge limit → {limit}%" if ok else f"Error: {msg[:70]}",
                         "#0ea5e9" if ok else "#ef4444")

    # ---------------------------------------------------------------- status refresh

    def _schedule_refresh(self):
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(5000)
        QTimer.singleShot(0, self._refresh)

    def _refresh(self):
        if self._worker and self._worker.isRunning():
            return
        self._worker = StatusWorker()
        self._worker.done.connect(self._apply_status)
        self._worker.start()

    def _apply_status(self, s):
        profile = s.get("profile", "Unknown")
        kbd     = s.get("kbd", "Unknown")
        bat     = s.get("battery", {})

        self._profile.set_active(profile)
        self._kbd.set_active(kbd)
        self._gpu.set_active(self._gpu_pending or s.get("gpu", "Unknown"))

        cap = bat.get("capacity", 0)
        self._bat_bar.setValue(cap)
        parts = [bat.get("status", "")]
        if "power_w" in bat:
            parts.append(f"{bat['power_w']} W")
        if "time_h" in bat:
            parts.append(f"~{bat['time_h']} h")
        self._bat_info.setText("  ".join(p for p in parts if p))

        if "health" in bat:
            self._bat_health.setText(f"Battery health: {bat['health']}%")

        if "charge_limit" in bat:
            self._bat_limit.set_active(f"{bat['charge_limit']}%")

        chunk = "#0ea5e9" if cap >= 50 else ("#f59e0b" if cap >= 20 else "#ef4444")
        self._bat_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {chunk}; border-radius: 3px; }}"
        )

        self._hdr_status.setText(f"{profile}  ·  {cap}%")

    # ---------------------------------------------------------------- close → hide

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def toggle(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()


# ---------------------------------------------------------------------------
# Application + tray
# ---------------------------------------------------------------------------

class GHelperApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setApplicationName("G-Helper")
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setStyleSheet(STYLE)

        self.win = MainWindow()
        self._build_tray()
        self.win.show()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(_make_icon(), self.app)
        self.tray.setToolTip("G-Helper — ASUS ROG G14")

        menu = QMenu()

        open_act = QAction("Open G-Helper", self.app)
        open_act.triggered.connect(self.win.show)
        menu.addAction(open_act)

        menu.addSeparator()
        for p in ("LowPower", "Balanced", "Performance"):
            a = QAction(f"Profile: {p}", self.app)
            a.triggered.connect(lambda _, pr=p: Backend.set_profile(pr))
            menu.addAction(a)

        menu.addSeparator()
        quit_act = QAction("Quit", self.app)
        quit_act.triggered.connect(self.app.quit)
        menu.addAction(quit_act)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.show()

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.win.toggle()

    def run(self):
        return self.app.exec()


if __name__ == "__main__":
    sys.exit(GHelperApp().run())
