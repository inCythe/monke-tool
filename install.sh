#!/data/data/com.termux/files/usr/bin/bash
# Monke Tool installer - Termux one-liner:
#   curl -fsSL "https://raw.githubusercontent.com/inCythe/monke-tool/main/install.sh" | bash

set -e

echo "[*] Updating packages..."
yes | pkg update -y
yes | pkg upgrade -y

echo "[*] Installing Python..."
yes | pkg install -y python python-pip

echo "[*] Installing Python dependencies..."
pip install requests discord-webhook rich

echo "[*] Downloading Monke Tool..."
cd "$HOME" || exit 1
curl -fsSL "https://raw.githubusercontent.com/inCythe/monke-tool/main/monke_tool.py" -o monke_tool.py
chmod +x monke_tool.py

echo "[*] Starting Monke Tool..."
python monke_tool.py
