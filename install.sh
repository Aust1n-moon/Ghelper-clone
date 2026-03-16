#!/usr/bin/env bash
# G-Helper install script for Fedora Linux
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== G-Helper Installer ==="
echo ""

# 1. Install PyQt6
echo "[1/4] Installing Python dependencies..."
if ! python3 -c "import PyQt6" 2>/dev/null; then
    sudo dnf install -y python3-pyqt6 || pip3 install --user PyQt6
fi
echo "      PyQt6 OK"

# 2. GNOME AppIndicator extension (needed for tray icon on GNOME Wayland)
echo ""
echo "[2/4] Checking GNOME AppIndicator extension..."
if command -v gnome-extensions &>/dev/null; then
    if gnome-extensions list | grep -q "appindicatorsupport"; then
        echo "      AppIndicator extension already installed."
    else
        echo "      Installing AppIndicator extension..."
        sudo dnf install -y gnome-shell-extension-appindicator 2>/dev/null || \
            echo "      NOTE: Install 'AppIndicator and KStatusNotifierItem Support' from"
        echo "            https://extensions.gnome.org/extension/615/appindicator-support/"
        echo "      Then enable it in GNOME Extensions app and log out/in."
    fi
else
    echo "      (not on GNOME — skipping)"
fi

# 3. Install the desktop entry
echo ""
echo "[3/4] Installing application launcher..."
DESKTOP_SRC="$SCRIPT_DIR/ghelper.desktop"
DESKTOP_DST="$HOME/.local/share/applications/ghelper.desktop"
mkdir -p "$HOME/.local/share/applications"

sed "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" "$DESKTOP_SRC" > "$DESKTOP_DST"
chmod +x "$DESKTOP_DST"
echo "      Installed to $DESKTOP_DST"

# 4. Autostart on login
echo ""
echo "[4/4] Setting up autostart on login..."
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
sed "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" "$DESKTOP_SRC" > "$AUTOSTART_DIR/ghelper.desktop"
chmod +x "$AUTOSTART_DIR/ghelper.desktop"
echo "      Autostart entry created."

echo ""
echo "=== Done! ==="
echo ""
echo "Run G-Helper now with:"
echo "    python3 \"$SCRIPT_DIR/ghelper.py\" &"
echo ""
echo "Or launch it from your app menu (search for 'G-Helper')."
echo "It will also start automatically on your next login."
