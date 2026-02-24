#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  HearThis.at Rhythmbox Plugin — Installer
# ─────────────────────────────────────────────────────────────

PLUGIN_DIR="$HOME/.local/share/rhythmbox/plugins/hearthis"

echo "Installing HearThis.at plugin to $PLUGIN_DIR …"

mkdir -p "$PLUGIN_DIR"
cp hearthis/hearthis.plugin "$PLUGIN_DIR/"
cp hearthis/hearthis.py     "$PLUGIN_DIR/"

echo ""
echo "✅  Done!  Now:"
echo "   1. Open Rhythmbox"
echo "   2. Go to  Edit → Plugins"
echo "   3. Enable  'HearThis.at'"
echo "   4. A new 'HearThis.at' entry will appear in the left sidebar."
