#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  HearThis.at Rhythmbox Plugin — Installer
# ─────────────────────────────────────────────────────────────
set -e

PLUGIN_SRC="$(cd "$(dirname "$0")/hearthis" && pwd)"

# ── Wykryj typ instalacji Rhythmbox ──────────────────────────
detect_rhythmbox() {
    # 1. Flatpak
    if flatpak list 2>/dev/null | grep -qi "rhythmbox"; then
        INSTALL_TYPE="flatpak"
        FLATPAK_ID=$(flatpak list 2>/dev/null | grep -i rhythmbox | awk '{print $2}' | head -1)
        PLUGIN_DIR="$HOME/.var/app/${FLATPAK_ID}/data/rhythmbox/plugins/hearthis"
        return
    fi

    # 2. Snap
    if snap list 2>/dev/null | grep -qi "rhythmbox"; then
        INSTALL_TYPE="snap"
        PLUGIN_DIR="$HOME/snap/rhythmbox/current/.local/share/rhythmbox/plugins/hearthis"
        return
    fi

    # 3. Standardowa instalacja (apt/dnf/pacman/zypper)
    INSTALL_TYPE="native"
    PLUGIN_DIR="$HOME/.local/share/rhythmbox/plugins/hearthis"
}

detect_rhythmbox

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║          HearThis.at Plugin — Instalator                ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "  Typ instalacji Rhythmbox : $INSTALL_TYPE"
echo "  Katalog docelowy         : $PLUGIN_DIR"
echo ""

# ── Sprawdź zależności Python ──────────────────────────────
echo "  Sprawdzam zależności Python…"

if [ "$INSTALL_TYPE" = "flatpak" ]; then
    # Uruchom w kontekście flatpak
    PYTHON_CHECK=$(flatpak run --command=python3 "$FLATPAK_ID" -c \
        "import gi; gi.require_version('RB','3.0'); from gi.repository import RB; print('OK')" 2>&1)
else
    PYTHON_CHECK=$(python3 -c \
        "import gi; gi.require_version('RB','3.0'); from gi.repository import RB; print('OK')" 2>&1)
fi

if echo "$PYTHON_CHECK" | grep -q "OK"; then
    echo "  ✅ Zależności OK"
else
    echo "  ⚠  Brak gi.repository.RB — zainstaluj:"
    if command -v apt &>/dev/null; then
        echo "     sudo apt install gir1.2-rb-3.0 python3-gi"
    elif command -v dnf &>/dev/null; then
        echo "     sudo dnf install rhythmbox-devel python3-gobject"
    elif command -v pacman &>/dev/null; then
        echo "     sudo pacman -S python-gobject"
    fi
    echo ""
fi

# ── Zainstaluj ────────────────────────────────────────────
echo "  Instaluję plugin…"
mkdir -p "$PLUGIN_DIR"
cp "$PLUGIN_SRC/hearthis.plugin" "$PLUGIN_DIR/"
cp "$PLUGIN_SRC/hearthis.py"     "$PLUGIN_DIR/"

echo ""
echo "  ✅  Gotowe!"
echo ""
echo "  Następne kroki:"

if [ "$INSTALL_TYPE" = "flatpak" ]; then
    echo "    1. Uruchom:  flatpak run $FLATPAK_ID"
    echo "    2. Idź do:   Edit → Plugins"
else
    echo "    1. Uruchom Rhythmbox"
    echo "    2. Idź do:   Edit → Plugins"
fi

echo "    3. Zaznacz:  HearThis.at"
echo "    4. W lewym panelu pojawi się źródło 'HearThis.at'"
echo ""

# ── Opcja: aktualizacja działającego Rhythmbox ─────────────
if pgrep -x rhythmbox &>/dev/null; then
    echo "  💡 Rhythmbox jest uruchomiony."
    echo "     Wyłącz i włącz plugin w Edit → Plugins żeby załadować nową wersję."
    echo "     (lub zrestartuj Rhythmbox)"
    echo ""
fi
