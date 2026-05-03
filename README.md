# G-Helper Clone for Fedora Linux

A lightweight system tray control panel for the **ASUS ROG Zephyrus G14 2024 (GA403)** running Fedora Linux. It replicates the core features of the Windows G-Helper app, giving you easy access to performance profiles, GPU switching, fan curves, display settings, and more — all from a single tray icon.

---

## Features

- **Performance Profiles** — Switch between Low Power, Balanced, and Performance modes via `asusctl`
- **GPU Mode Switching** — Toggle between Integrated, Hybrid, and Dedicated GPU modes via `supergfxctl`, with optional automatic session restart after switching
- **Fan Curve Presets** — Silent, Balanced, and Turbo presets with per-temperature-point speed control
- **Display Refresh Rate** — Toggle between 60 Hz and the panel's native max rate via GNOME Mutter D-Bus
- **Keyboard Backlight** — Four brightness levels: Off, Low, Med, High
- **Slash LED** — Toggle the ROG slash LED on/off
- **Battery Management** — View capacity, health, power draw, and estimated time remaining; set charge limit to 60%, 80%, or 100%
- **Temperature Monitoring** — Real-time CPU and GPU temperature readout
- **Auto-Switch** — Automatically apply profile, GPU mode, fan curve, and refresh rate when switching between AC and battery power
- **Persistent Settings** — All preferences are saved to `~/.config/ghelper.json`
- **Single Instance** — Subsequent launches bring the existing window to focus via Unix socket IPC

---

## Requirements

### System utilities

These must be installed and accessible before running G-Helper:

- [`asusctl`](https://gitlab.com/asus-linux/asusctl) — ASUS hardware control (profiles, fan curves, keyboard backlight)
- [`supergfxctl`](https://gitlab.com/asus-linux/supergfxctl) — GPU mode switching

### Python dependencies

- Python 3.8+
- PyQt6

### Optional (GNOME Wayland tray support)

- GNOME Shell extension: **AppIndicator and KStatusNotifierItem Support**

---

## Installation

### 1. Clone the repository

```bash
git clone <repo-url> ~/ghelper
cd ~/ghelper
```

### 2. Run the install script

```bash
bash install.sh
```

The script will:
1. Install **PyQt6** via `dnf` (or `pip3` as a fallback)
2. Install the **GNOME AppIndicator** extension for tray icon support on Wayland
3. Add a **desktop launcher** entry to `~/.local/share/applications/`
4. Set up an **autostart entry** so G-Helper launches automatically on login
5. Install the **sleep/suspend hook** to `/usr/lib/systemd/system-sleep/` (reduces s2idle power draw)
6. Install a **udev rule** to disable PCI D3cold for the MT7922 WiFi adapter (prevents firmware reinit failures after suspend)

### 3. Launch

```bash
python3 ~/ghelper/ghelper.py &
```

Or search for **G-Helper** in your application menu. It will also start automatically on your next login.

---

## Manual PyQt6 Installation (if needed)

**Via dnf (recommended):**
```bash
sudo dnf install python3-pyqt6
```

**Via pip:**
```bash
pip3 install --user PyQt6
```

---

## GNOME Wayland Tray Icon

If the tray icon does not appear on GNOME Wayland, install and enable the AppIndicator extension:

```bash
sudo dnf install gnome-shell-extension-appindicator
```

Or install it from [extensions.gnome.org](https://extensions.gnome.org/extension/615/appindicator-support/), then enable it in the **GNOME Extensions** app and log out/in.

---

## Configuration

Settings are stored at `~/.config/ghelper.json` and updated automatically as you make changes in the UI. You do not need to edit this file manually.

---

## Sleep / Suspend

The install script places a systemd sleep hook (`ghelper-sleep`) that runs before and after suspend to minimize s2idle power draw and ensure reliable resume. Key behaviors:

- **WiFi (MT7922)** — The `mt7921e` kernel module is fully unloaded before suspend and reloaded on resume, forcing a clean firmware reinit. A udev rule (`99-mt7922-no-d3cold.rules`) disables PCI D3cold for the adapter to prevent the firmware from entering a state it cannot recover from.
- **NVIDIA dGPU** — Driver is unbound before suspend and rebound on resume to guarantee the GPU is fully powered off during sleep.
- **Bluetooth** — Radio is blocked via `rfkill` before suspend and restored on resume if it was previously enabled.
- **USB / NVMe / CPU / iGPU** — Aggressively power-gated during sleep (USB autosuspend, NVMe deep power state, CPU boost off, AMDGPU low power).
- **Profile restore** — The active ghelper power profile and `asusctl` platform profile are saved before suspend and fully reapplied on resume, including signaling the GUI to restore fan curves, refresh rate, and keyboard backlight.

---

## Notes

- Designed and tested on **Fedora Linux** with GNOME on Wayland
- Targets the **ASUS ROG Zephyrus G14 2024 (GA403)** — other models may work but are untested
- GPU mode switching may require a session restart to take full effect
