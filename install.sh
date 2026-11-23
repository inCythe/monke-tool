#!/data/data/com.termux/files/usr/bin/bash
# Monke Tool installer - Termux one-liner:
#   curl -fsSL "https://raw.githubusercontent.com/inCythe/monke-tool/main/install.sh" | bash

set -e

# Only bootstrap what's actually missing - repeated runs just update the tool.
if command -v python >/dev/null 2>&1; then
    echo "[*] Python already installed - skipping package setup."
else
    echo "[*] Installing Python..."
    yes | pkg update -y
    yes | pkg upgrade -y
    yes | pkg install -y python python-pip
fi

if python -c 'import requests, discord_webhook, rich' >/dev/null 2>&1; then
    echo "[*] Dependencies already installed - skipping pip install."
else
    echo "[*] Installing Python dependencies..."
    yes | pip install requests discord-webhook rich
fi

echo "[*] Downloading Monke Tool..."
cd "$HOME" || exit 1
curl -fsSL "https://raw.githubusercontent.com/inCythe/monke-tool/main/monke_tool.py" -o monke_tool.py
chmod +x monke_tool.py

echo "[*] Starting Monke Tool..."
# Use /dev/tty so interactive prompts work even when piped via curl | bash
python monke_tool.py < /dev/tty
