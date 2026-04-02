#!/usr/bin/env python3
"""
G-Helper Clone — ASUS ROG Zephyrus G14 2024 (GA403) control panel for Fedora Linux
"""

import sys
import json
import subprocess
import glob as _glob
import collections
import socket
import os
import tempfile
from pathlib import Path
try:
    import dbus as _dbus
    _DBUS_OK = True
except ImportError:
    _DBUS_OK = False
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QButtonGroup, QGroupBox, QSystemTrayIcon,
    QMenu, QFrame, QProgressBar, QCheckBox, QScrollArea,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction


# ---------------------------------------------------------------------------
# Backend: thin wrappers around asusctl / sysfs / xrandr
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


# 12 samples × 5 s refresh = 60 s rolling window for power draw average
_power_samples: collections.deque = collections.deque(maxlen=12)

# ---------------------------------------------------------------------------
# Persistent settings
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path.home() / ".config" / "ghelper.json"


def _load_settings() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_setting(key: str, value) -> None:
    s = _load_settings()
    s[key] = value
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CONFIG_PATH, "w") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass

FAN_PRESETS = {
    "Silent":   "30c:0%,40c:0%,50c:10%,60c:25%,70c:40%,80c:55%,90c:70%,100c:85%",
    "Balanced": "30c:0%,40c:10%,55c:20%,65c:35%,75c:50%,85c:65%,95c:80%,105c:100%",
    "Turbo":    "30c:25%,40c:35%,50c:45%,60c:58%,70c:72%,80c:85%,90c:93%,100c:100%",
}


# asusctl uses "Quiet" but the app displays "LowPower"
_PROFILE_TO_ASUSCTL = {"LowPower": "Quiet", "Balanced": "Balanced", "Performance": "Performance"}
_ASUSCTL_TO_PROFILE = {v.lower(): k for k, v in _PROFILE_TO_ASUSCTL.items()}


class Backend:
    @staticmethod
    def get_profile():
        out, _, rc = _run("asusctl profile get")
        if rc == 0:
            # Only parse the "Active profile:" line — the full output also
            # lists AC/Battery defaults which caused false substring matches.
            for line in out.splitlines():
                low = line.lower()
                if "active" not in low:
                    continue
                for asusctl_name, app_name in _ASUSCTL_TO_PROFILE.items():
                    if asusctl_name in low:
                        return app_name
        return "Unknown"

    @staticmethod
    def set_profile(profile):
        asusctl_name = _PROFILE_TO_ASUSCTL.get(profile, profile)
        out, err, rc = _run(f"asusctl profile set {asusctl_name}")
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

        charge_now    = _sysfs(f"{base}/charge_now")
        charge_full   = _sysfs(f"{base}/charge_full")
        charge_design = _sysfs(f"{base}/charge_full_design")
        charge_limit  = _sysfs(f"{base}/charge_control_end_threshold")

        # Power consumption: prefer power_now (direct), fall back to current*voltage
        # (same approach as auto-cpufreq)
        power_now = _sysfs(f"{base}/power_now")
        if power_now and power_now.isdigit():
            power_w = round(int(power_now) / 1_000_000, 1)
        else:
            current_now = _sysfs(f"{base}/current_now")
            voltage_now = _sysfs(f"{base}/voltage_now")
            if current_now and current_now.isdigit() and voltage_now and voltage_now.isdigit():
                power_w = round((int(current_now) * int(voltage_now)) / 1_000_000_000_000, 1)
            else:
                power_w = 0.0

        if power_w > 0:
            info["power_w"] = power_w
            _power_samples.append(power_w)
            if len(_power_samples) >= 3:
                avg = round(sum(_power_samples) / len(_power_samples), 1)
                info["power_w_avg"] = avg
            avg_power = info.get("power_w_avg", power_w)
            voltage_now_raw = _sysfs(f"{base}/voltage_now")
            if charge_now and voltage_now_raw and avg_power > 0:
                volts = int(voltage_now_raw) / 1_000_000
                wh_remaining = int(charge_now) / 1_000_000 * volts
                info["time_h"] = round(wh_remaining / avg_power, 1)

        if charge_full and charge_design and int(charge_design) > 0:
            info["health"] = round(int(charge_full) * 100 / int(charge_design), 1)

        if charge_limit:
            info["charge_limit"] = int(charge_limit)

        return info

    @staticmethod
    def get_ac_online():
        """Return True if AC adapter is physically connected, False otherwise.
        Reads the sysfs 'online' file for any Mains-type power supply."""
        for ps_path in _glob.glob("/sys/class/power_supply/*"):
            ps_type = _sysfs(f"{ps_path}/type")
            if ps_type == "Mains":
                online = _sysfs(f"{ps_path}/online")
                if online is not None:
                    return online.strip() == "1"
        return None  # unknown

    @staticmethod
    def set_charge_limit(limit):
        out, err, rc = _run(f"asusctl battery limit {limit}")
        return rc == 0, err or out


    @staticmethod
    def set_epp(pref: str):
        import glob as _glob
        try:
            for f in _glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference"):
                with open(f, "w") as fh:
                    fh.write(pref)
            return True, ""
        except Exception as e:
            return False, str(e)[:80]

    @staticmethod
    def get_epp():
        val = _sysfs("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")
        return val or "unknown"

    @staticmethod
    def apply_power_mode(mode: str):
        """Apply full battery or AC power profile via polkit helper."""
        out, err, rc = _run(f"sudo /usr/local/bin/ghelper-power {mode}", timeout=20)
        return rc == 0, err or out

    @staticmethod
    def get_cpu_boost():
        val = _sysfs("/sys/devices/system/cpu/cpufreq/boost")
        if val is None:
            return None
        return val == "1"

    _GPU_TO_SUPERGFX = {"Dedicated": "AsusMuxDgpu"}
    _SUPERGFX_TO_GPU = {"asusmuxdgpu": "Dedicated"}

    @staticmethod
    def get_gpu_mode():
        out, _, rc = _run("supergfxctl -g")
        if rc == 0:
            lower = out.strip().lower()
            if lower in Backend._SUPERGFX_TO_GPU:
                return Backend._SUPERGFX_TO_GPU[lower]
            for m in ("Integrated", "Hybrid"):
                if m.lower() in lower:
                    return m
        return "Unknown"

    @staticmethod
    def set_gpu_mode(mode):
        sgfx_mode = Backend._GPU_TO_SUPERGFX.get(mode, mode)
        # MUX switches (AsusMuxDgpu) take significantly longer than hybrid/integrated
        timeout = 180 if sgfx_mode == "AsusMuxDgpu" else 60
        out, err, rc = _run(f"supergfxctl -m {sgfx_mode}", timeout=timeout)
        return rc == 0, err or out

    @staticmethod
    def get_temps():
        temps = {}
        for hwmon_path in _glob.glob("/sys/class/hwmon/hwmon*"):
            name = _sysfs(f"{hwmon_path}/name")
            if name == "k10temp":
                # Tdie (temp2) preferred; fall back to Tctl (temp1)
                t = _sysfs(f"{hwmon_path}/temp2_input") or _sysfs(f"{hwmon_path}/temp1_input")
                if t:
                    temps["cpu"] = int(t) // 1000
            elif name == "amdgpu":
                t = _sysfs(f"{hwmon_path}/temp1_input")  # edge temp
                if t:
                    # First amdgpu = iGPU, second = dGPU (when active)
                    key = "gpu2" if "gpu" in temps else "gpu"
                    temps[key] = int(t) // 1000
        return temps

    @staticmethod
    def set_fan_preset(preset):
        data = FAN_PRESETS.get(preset)
        if not data:
            return False, "Unknown preset"
        profile = Backend.get_profile()
        if profile == "Unknown":
            return False, "Could not detect current profile"
        _run(f"asusctl fan-curve --mod-profile {profile} --enable-fan-curves true")
        errors = []
        for fan in ("cpu", "gpu", "mid"):
            _, err, rc = _run(
                f"asusctl fan-curve --mod-profile {profile} --fan {fan} --data '{data}'"
            )
            if rc != 0 and "not supported" not in err.lower() and "no fan" not in err.lower():
                errors.append(f"{fan}: {err[:40]}")
        return len(errors) == 0, "; ".join(errors)

    @staticmethod
    def _mutter():
        if not _DBUS_OK:
            return None
        try:
            bus = _dbus.SessionBus()
            proxy = bus.get_object("org.gnome.Mutter.DisplayConfig",
                                   "/org/gnome/Mutter/DisplayConfig")
            return _dbus.Interface(proxy, "org.gnome.Mutter.DisplayConfig")
        except Exception:
            return None

    @staticmethod
    def get_display_info():
        iface = Backend._mutter()
        if not iface:
            return None
        try:
            _, monitors, _, _ = iface.GetCurrentState()
            for monitor_id, modes, mon_props in monitors:
                if not mon_props.get("is-builtin", False):
                    continue
                current_rate = None
                cur_w = cur_h = None
                for _, width, height, refresh, _, _, mode_props in modes:
                    if mode_props.get("is-current", False):
                        current_rate = float(refresh)
                        cur_w, cur_h = int(width), int(height)
                        break
                max_rate = max(
                    float(r) for _, w, h, r, _, _, _ in modes
                    if int(w) == cur_w and int(h) == cur_h
                ) if cur_w else None
                return {"output": str(monitor_id[0]),
                        "current_rate": current_rate,
                        "max_rate": max_rate}
        except Exception:
            pass
        return None

    @staticmethod
    def set_refresh_rate(hz):
        iface = Backend._mutter()
        if not iface:
            return False, "python3-dbus unavailable"
        try:
            serial, monitors, logical_monitors, _ = iface.GetCurrentState()

            # Build per-connector info
            conn_info = {}
            for monitor_id, modes, mon_props in monitors:
                connector = str(monitor_id[0])
                is_builtin = bool(mon_props.get("is-builtin", False))
                cur_mode_id = cur_w = cur_h = None
                all_modes = []
                for mode_id, w, h, r, _, _, mode_props in modes:
                    all_modes.append((str(mode_id), int(w), int(h), float(r)))
                    if mode_props.get("is-current", False):
                        cur_mode_id = str(mode_id)
                        cur_w, cur_h = int(w), int(h)
                conn_info[connector] = {
                    "cur_mode_id": cur_mode_id,
                    "modes": all_modes,
                    "is_builtin": is_builtin,
                    "cur_res": (cur_w, cur_h),
                }

            new_lms = []
            for lm in logical_monitors:
                x, y = int(lm[0]), int(lm[1])
                scale, transform, is_primary = float(lm[2]), int(lm[3]), bool(lm[4])
                new_mons = []
                for lm_mon_id in lm[5]:
                    connector = str(lm_mon_id[0])
                    info = conn_info.get(connector, {})
                    target = info.get("cur_mode_id", "")
                    cur_w, cur_h = info.get("cur_res", (None, None))
                    if info.get("is_builtin") and cur_w:
                        best, best_diff = None, float("inf")
                        for mode_id, w, h, r in info["modes"]:
                            if w == cur_w and h == cur_h:
                                d = abs(r - hz)
                                if d < best_diff:
                                    best_diff, best = d, mode_id
                        if best:
                            target = best
                    new_mons.append((connector, target, _dbus.Dictionary({}, signature="sv")))
                new_lms.append((_dbus.Int32(x), _dbus.Int32(y), _dbus.Double(scale),
                                _dbus.UInt32(transform), _dbus.Boolean(is_primary), new_mons))

            iface.ApplyMonitorsConfig(
                _dbus.UInt32(int(serial)), _dbus.UInt32(1),
                new_lms, _dbus.Dictionary({}, signature="sv")
            )
            return True, ""
        except Exception as e:
            return False, str(e)[:80]


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class StatusWorker(QThread):
    done = pyqtSignal(dict)

    def run(self):
        self.done.emit({
            "profile":   Backend.get_profile(),
            "kbd":       Backend.get_kbd_brightness(),
            "battery":   Backend.get_battery(),
            "gpu":       Backend.get_gpu_mode(),
            "temps":     Backend.get_temps(),
            "display":   Backend.get_display_info(),
            "cpu_boost": Backend.get_cpu_boost(),
            "epp":       Backend.get_epp(),
        })


class GpuSwitchWorker(QThread):
    done = pyqtSignal(bool, str, str)

    def __init__(self, mode):
        super().__init__()
        self._mode = mode

    def run(self):
        ok, msg = Backend.set_gpu_mode(self._mode)
        self.done.emit(ok, msg, self._mode)


# ---------------------------------------------------------------------------
# UI helpers
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
        self._last_ac_status = None
        self._native_rate = None
        self._intended_profile = None   # profile ghelper actively defends
        self._build_ui()
        self._connect()
        self._worker = None
        self._gpu_worker = None
        self._gpu_pending = None
        self._restore_settings()
        self._schedule_refresh()

    # ------------------------------------------------------------------ UI build

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # Fixed header (outside scroll area)
        hdr_widget = QWidget()
        hdr_layout = QVBoxLayout(hdr_widget)
        hdr_layout.setContentsMargins(12, 12, 12, 4)
        hdr_layout.setSpacing(4)
        hdr = QHBoxLayout()
        title = QLabel("G-Helper")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #38bdf8; letter-spacing: 1px;")
        self._hdr_status = QLabel("…")
        self._hdr_status.setStyleSheet("color: #475569; font-size: 11px;")
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self._hdr_status)
        hdr_layout.addLayout(hdr)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #1e293b; margin: 2px 0;")
        hdr_layout.addWidget(sep)
        root.addWidget(hdr_widget)

        # Scrollable content area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollBar:vertical { width: 6px; background: #0f172a; }"
                             "QScrollBar::handle:vertical { background: #1e293b; border-radius: 3px; }"
                             "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }")
        content = QWidget()
        root_inner = QVBoxLayout(content)
        root_inner.setSpacing(6)
        root_inner.setContentsMargins(12, 6, 12, 4)
        scroll.setWidget(content)
        root.addWidget(scroll)

        # Status bar (fixed at bottom, outside scroll)
        self._status = QLabel("Ready")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet("color: #334155; font-size: 11px; padding: 4px; background: #000;")
        root.addWidget(self._status)

        # From here all widgets go into root_inner (the scrollable area)
        root = root_inner

        # Temperatures
        g = QGroupBox("Temperatures")
        gl = QHBoxLayout(g)
        self._cpu_temp = QLabel("CPU  --°C")
        self._gpu_temp = QLabel("GPU  --°C")
        self._gpu2_temp = QLabel("")
        for lbl in (self._cpu_temp, self._gpu_temp, self._gpu2_temp):
            lbl.setStyleSheet("font-size: 12px; color: #94a3b8;")
        gl.addWidget(self._cpu_temp)
        gl.addStretch()
        gl.addWidget(self._gpu_temp)
        gl.addWidget(self._gpu2_temp)
        root.addWidget(g)

        # Profile
        g = QGroupBox("Performance Profile")
        gl = QVBoxLayout(g)
        self._profile = _ButtonRow(["LowPower", "Balanced", "Performance"])
        gl.addWidget(self._profile)
        self._epp_label = QLabel("EPP: –")
        self._epp_label.setStyleSheet("color: #64748b; font-size: 11px;")
        gl.addWidget(self._epp_label)
        self._auto_switch = QCheckBox("Auto-switch on AC / battery  (profile · GPU · fan · display · kbd · slash)")
        self._auto_switch.setChecked(_load_settings().get("auto_switch", True))
        self._auto_switch.setStyleSheet("color: #94a3b8; font-size: 11px;")
        self._auto_switch.toggled.connect(lambda v: _save_setting("auto_switch", v))
        gl.addWidget(self._auto_switch)
        root.addWidget(g)

        # Power Tweaks
        g = QGroupBox("Power Tweaks")
        gl = QVBoxLayout(g)
        pm_row = QHBoxLayout()
        pm_lbl = QLabel("Mode:")
        pm_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        self._power_mode = _ButtonRow(["Battery", "AC"])
        pm_row.addWidget(pm_lbl)
        pm_row.addWidget(self._power_mode)
        gl.addLayout(pm_row)
        self._boost_label = QLabel("CPU Boost: –")
        self._boost_label.setStyleSheet("color: #475569; font-size: 10px;")
        self._aspm_label = QLabel("ASPM · writeback · NMI · PCI-PM · WiFi-PS · USB · GPU-DPM · freq-cap")
        self._aspm_label.setStyleSheet("color: #334155; font-size: 10px;")
        gl.addWidget(self._boost_label)
        gl.addWidget(self._aspm_label)
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

        # Fan Curve
        g = QGroupBox("Fan Curve")
        gl = QVBoxLayout(g)
        self._fan = _ButtonRow(["Silent", "Balanced", "Turbo"])
        gl.addWidget(self._fan)
        root.addWidget(g)

        # Display
        g = QGroupBox("Display")
        gl = QHBoxLayout(g)
        lbl = QLabel("Refresh rate:")
        lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        self._refresh_rate = _ButtonRow(["60 Hz", "Native"])
        gl.addWidget(lbl)
        gl.addWidget(self._refresh_rate)
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
        self._bat_info.setStyleSheet("color: #64748b; font-size: 11px; min-width: 140px;")
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

        for name, btn in self._fan.buttons.items():
            btn.clicked.connect(lambda _, n=name: self._do_fan(n))

        self._refresh_rate.buttons["60 Hz"].clicked.connect(lambda: self._do_refresh(60))
        self._refresh_rate.buttons["Native"].clicked.connect(lambda: self._do_refresh(None))


        self._power_mode.buttons["Battery"].clicked.connect(lambda: self._do_power_mode("battery"))
        self._power_mode.buttons["AC"].clicked.connect(lambda: self._do_power_mode("ac"))

    # ---------------------------------------------------------------- actions

    def _set_status(self, msg, color="#334155"):
        self._status.setText(msg)
        self._status.setStyleSheet(f"color: {color}; font-size: 11px; padding: 2px;")

    def _is_battery_mode_active(self):
        """Battery optimizations only apply when LowPower profile + Integrated GPU."""
        profile_ok = self._profile.buttons.get("LowPower") and \
                     self._profile.buttons["LowPower"].isChecked()
        gpu_ok = self._gpu.buttons.get("Integrated") and \
                 self._gpu.buttons["Integrated"].isChecked()
        return bool(profile_ok and gpu_ok)

    def _sync_power_mode(self):
        """Apply battery or AC tweaks based on current profile + GPU selection."""
        if self._is_battery_mode_active():
            self._do_power_mode("battery")
            self._power_mode.set_active("Battery")
        else:
            self._do_power_mode("ac")
            self._power_mode.set_active("AC")

    def _do_profile(self, profile):
        ok, msg = Backend.set_profile(profile)
        if ok:
            self._intended_profile = profile
            _save_setting("profile", profile)
            self._sync_power_mode()
        self._set_status(f"Profile → {profile}" if ok else f"Error: {msg[:70]}",
                         "#0ea5e9" if ok else "#ef4444")

    def _do_kbd(self, level):
        ok, msg = Backend.set_kbd_brightness(level)
        if ok:
            _save_setting("kbd", level)
        self._set_status(f"Keyboard → {level}" if ok else f"Error: {msg[:70]}",
                         "#0ea5e9" if ok else "#ef4444")

    def _do_slash(self, enabled):
        ok, msg = Backend.set_slash(enabled)
        if ok:
            _save_setting("slash", enabled)
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
        _save_setting("gpu", mode)
        self._sync_power_mode()
        cur_gpu = Backend.get_gpu_mode()
        needs_reboot = mode == "Dedicated" or cur_gpu == "Dedicated"
        if self._gpu_auto_restart.isChecked():
            if needs_reboot:
                self._set_status(f"GPU → {mode}  Rebooting (MUX switch)…", "#0ea5e9")
                QTimer.singleShot(1500, lambda: _run("systemctl reboot", timeout=10))
            else:
                self._set_status(f"GPU → {mode}  Restarting session…", "#0ea5e9")
                QTimer.singleShot(1500, lambda: _run(
                    "dbus-send --session --type=method_call "
                    "--dest=org.gnome.SessionManager "
                    "/org/gnome/SessionManager "
                    "org.gnome.SessionManager.Logout uint32:1",
                    timeout=10
                ))
        else:
            action = "reboot" if needs_reboot else "logout"
            self._set_status(f"GPU → {mode} ({action} to apply)", "#0ea5e9")

    def _do_limit(self, limit):
        ok, msg = Backend.set_charge_limit(limit)
        self._set_status(f"Charge limit → {limit}%" if ok else f"Error: {msg[:70]}",
                         "#0ea5e9" if ok else "#ef4444")

    def _do_fan(self, preset):
        self._set_status(f"Applying fan curve: {preset}…", "#f59e0b")
        ok, msg = Backend.set_fan_preset(preset)
        if ok:
            _save_setting("fan_preset", preset)
        self._set_status(f"Fan curve → {preset}" if ok else f"Fan curve error: {msg[:60]}",
                         "#0ea5e9" if ok else "#ef4444")

    def _do_power_mode(self, mode: str):
        """Apply full low-level power tweaks for 'battery' or 'ac' mode."""
        self._set_status(f"Applying {mode} power tweaks…", "#f59e0b")
        ok, msg = Backend.apply_power_mode(mode)
        label = "Battery" if mode == "battery" else "AC"
        self._set_status(
            f"Power tweaks → {label}  (boost·freq·ASPM·PCI-PM·GPU-DPM·WiFi·USB·audio·NMI)"
            if ok else f"Power tweak error: {msg[:60]}",
            "#0ea5e9" if ok else "#ef4444"
        )

    def _do_refresh(self, hz):
        if hz is None:
            hz = self._native_rate
        if hz is None:
            self._set_status("Native rate unknown — wait for first refresh", "#f59e0b")
            return
        ok, msg = Backend.set_refresh_rate(hz)
        self._set_status(f"Refresh → {hz} Hz" if ok else f"Error: {msg[:70]}",
                         "#0ea5e9" if ok else "#ef4444")

    def _check_ac_auto_switch(self, bat_status):
        # Use the AC adapter online file as the authoritative source to avoid
        # false transitions caused by "Not charging" appearing on battery
        # (e.g. when at/above the charge limit on ASUS ROG laptops).
        ac_online = Backend.get_ac_online()
        if ac_online is not None:
            current_on_ac = ac_online
        else:
            on_ac_statuses = {"Charging", "Full", "Not charging"}
            current_on_ac = bat_status in on_ac_statuses

        prev = self._last_ac_status
        self._last_ac_status = current_on_ac
        if not self._auto_switch.isChecked():
            return

        # On first refresh (launch), treat as a transition so the correct
        # power mode is applied immediately for the current AC state.
        if prev is None:
            if current_on_ac:
                prev = False   # pretend was_battery → now_on_ac
            else:
                prev = True    # pretend was_on_ac → now_battery

        was_on_ac   = prev is True
        now_battery = current_on_ac is False
        was_battery = prev is False
        now_on_ac   = current_on_ac is True

        if was_on_ac and now_battery:
            # Battery: LowPower + Integrated GPU + all power tweaks + Silent fan + 60 Hz + kbd/slash off
            Backend.set_profile("LowPower")
            self._intended_profile = "LowPower"
            _save_setting("profile", "LowPower")
            self._profile.set_active("LowPower")
            Backend.set_fan_preset("Silent")
            self._fan.set_active("Silent")
            self._do_refresh(60)
            self._refresh_rate.set_active("60 Hz")
            # Apply full battery power tweaks (EPP + boost off + ASPM + PCI-PM + NMI + writeback)
            self._do_power_mode("battery")
            self._power_mode.set_active("Battery")
            # Keyboard backlight off, slash LED off
            Backend.set_kbd_brightness("Off")
            self._kbd.set_active("Off")
            Backend.set_slash(False)
            self._slash_off_btn.setChecked(True)
            cur_gpu = Backend.get_gpu_mode()
            if cur_gpu != "Integrated":
                self._set_status("Unplugged → full battery mode · switching GPU to Integrated…", "#f59e0b")
                self._do_gpu("Integrated")
            else:
                self._set_status("Unplugged → full battery mode active", "#f59e0b")

        elif was_battery and now_on_ac:
            # AC: Balanced + Hybrid GPU + AC power tweaks + Balanced fan + Native
            Backend.set_profile("Balanced")
            self._intended_profile = "Balanced"
            _save_setting("profile", "Balanced")
            self._profile.set_active("Balanced")
            Backend.set_fan_preset("Balanced")
            self._fan.set_active("Balanced")
            self._do_refresh(None)  # Native
            self._refresh_rate.set_active("Native")
            # Apply AC power tweaks (EPP + boost on + ASPM default + NMI + writeback)
            self._do_power_mode("ac")
            self._power_mode.set_active("AC")
            # Keyboard backlight restore to Low
            Backend.set_kbd_brightness("Low")
            self._kbd.set_active("Low")
            cur_gpu = Backend.get_gpu_mode()
            if cur_gpu != "Hybrid":
                self._set_status("Plugged in → AC mode · switching GPU to Hybrid…", "#0ea5e9")
                self._do_gpu("Hybrid")
            else:
                self._set_status("Plugged in → AC mode active", "#0ea5e9")

    # ---------------------------------------------------------------- restore saved settings

    def _restore_settings(self):
        s = _load_settings()

        profile = s.get("profile")
        if profile and profile in self._profile.buttons:
            self._intended_profile = profile
            self._profile.set_active(profile)
            # If auto-switch is enabled, skip applying the saved profile now —
            # the first refresh will apply the correct one for the current
            # AC/battery state.  This prevents a stale "Performance" saved
            # from a previous AC session from briefly overriding LowPower
            # on battery.
            if not self._auto_switch.isChecked():
                Backend.set_profile(profile)

        fan = s.get("fan_preset")
        if fan and fan in self._fan.buttons:
            self._fan.set_active(fan)
            # Re-apply in background so fan curve survives reboots
            import threading
            threading.Thread(target=Backend.set_fan_preset, args=(fan,), daemon=True).start()

        kbd = s.get("kbd")
        if kbd and kbd in self._kbd.buttons:
            # Pre-select while waiting for first status poll
            self._kbd.set_active(kbd)

        gpu = s.get("gpu", "Integrated")
        if gpu not in self._gpu.buttons:
            gpu = "Integrated"
        self._gpu.set_active(gpu)
        import threading
        threading.Thread(target=Backend.set_gpu_mode, args=(gpu,), daemon=True).start()

        slash = s.get("slash")
        if slash is not None:
            if slash:
                self._slash_on_btn.setChecked(True)
            else:
                self._slash_off_btn.setChecked(True)

        # Default to 60 Hz on launch
        self._refresh_rate.set_active("60 Hz")
        import threading
        threading.Thread(target=Backend.set_refresh_rate, args=(60,), daemon=True).start()

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
        temps   = s.get("temps", {})
        display = s.get("display")

        # If ghelper has an intended profile and the system drifted (e.g.
        # power-profiles-daemon or asusd reverted it), re-apply our choice
        # instead of silently accepting the external change.
        if (self._intended_profile
                and profile != self._intended_profile
                and profile != "Unknown"):
            Backend.set_profile(self._intended_profile)
            profile = self._intended_profile

        self._profile.set_active(profile)
        self._kbd.set_active(kbd)
        self._gpu.set_active(self._gpu_pending or s.get("gpu", "Unknown"))

        # Temperatures
        def _temp_color(t):
            if t >= 85: return "#ef4444"
            if t >= 70: return "#f59e0b"
            return "#22c55e"

        if "cpu" in temps:
            t = temps["cpu"]
            self._cpu_temp.setText(f"CPU  {t}°C")
            self._cpu_temp.setStyleSheet(f"font-size: 12px; color: {_temp_color(t)};")
        if "gpu" in temps:
            t = temps["gpu"]
            self._gpu_temp.setText(f"GPU  {t}°C")
            self._gpu_temp.setStyleSheet(f"font-size: 12px; color: {_temp_color(t)};")
        if "gpu2" in temps:
            t = temps["gpu2"]
            self._gpu2_temp.setText(f"dGPU  {t}°C")
            self._gpu2_temp.setStyleSheet(f"font-size: 12px; color: {_temp_color(t)};")

        # Battery
        cap = bat.get("capacity", 0)
        self._bat_bar.setValue(cap)
        parts = [bat.get("status", "")]
        if "power_w_avg" in bat:
            parts.append(f"{bat['power_w_avg']} W")
        elif "power_w" in bat:
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

        # Display refresh rate
        if display and display.get("current_rate"):
            cr = display["current_rate"]
            mr = display.get("max_rate", cr)
            if mr and (self._native_rate is None or mr > self._native_rate):
                self._native_rate = mr
            if abs(cr - 60) < 1:
                self._refresh_rate.set_active("60 Hz")
            else:
                self._refresh_rate.set_active("Native")


        # EPP (read-only, managed by auto-cpufreq)
        epp_raw = s.get("epp")
        if epp_raw:
            self._epp_label.setText(f"EPP: {epp_raw}")

        # CPU boost indicator
        boost = s.get("cpu_boost")
        if boost is not None:
            boost_txt = "ON" if boost else "OFF"
            boost_color = "#ef4444" if boost else "#22c55e"
            self._boost_label.setText(f"CPU Boost: {boost_txt}")
            self._boost_label.setStyleSheet(f"color: {boost_color}; font-size: 10px;")
            # Reflect in power mode button
            if not boost:
                self._power_mode.set_active("Battery")
            else:
                self._power_mode.set_active("AC")

        self._hdr_status.setText(f"{profile}  ·  {cap}%")

        # AC/DC auto profile switch
        self._check_ac_auto_switch(bat.get("status", "Unknown"))

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
        self.app.aboutToQuit.connect(self._on_quit)
        self.win.show()

    def _on_quit(self):
        # Only reset GPU on exit when auto-switch is managing the GPU mode.
        # When the user has manually chosen a mode (e.g. Dedicated), respect it
        # across restarts by leaving the saved setting intact.
        if self.win._auto_switch.isChecked():
            Backend.set_gpu_mode("Integrated")

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
            a.triggered.connect(lambda _, pr=p: self.win._do_profile(pr))
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


_SOCKET_PATH = os.path.join(tempfile.gettempdir(), f"ghelper-{os.getenv('USER', 'user')}.sock")


def _try_show_existing() -> bool:
    """Return True if another instance is running (and was told to show)."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(_SOCKET_PATH)
        s.sendall(b"show")
        s.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


if __name__ == "__main__":
    if _try_show_existing():
        sys.exit(0)

    app_obj = GHelperApp()

    # Listen for "show" commands from future instances
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(_SOCKET_PATH)
    except FileNotFoundError:
        pass
    srv.bind(_SOCKET_PATH)
    srv.listen(5)
    srv.setblocking(False)

    def _check_ipc():
        try:
            conn, _ = srv.accept()
            conn.recv(16)
            conn.close()
            app_obj.win.show()
            app_obj.win.raise_()
            app_obj.win.activateWindow()
        except BlockingIOError:
            pass

    ipc_timer = QTimer()
    ipc_timer.timeout.connect(_check_ipc)
    ipc_timer.start(500)

    ret = app_obj.run()
    srv.close()
    try:
        os.unlink(_SOCKET_PATH)
    except FileNotFoundError:
        pass
    sys.exit(ret)
