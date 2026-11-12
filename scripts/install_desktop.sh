#!/usr/bin/env bash
set -euo pipefail
STATION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APPLICATION_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPLICATION_DIR"
DESKTOP_ENTRY="$APPLICATION_DIR/ecat-groundstation.desktop"
cat > "$DESKTOP_ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=Ecat GroundStation
Comment=AIM EtherCAT configuration and ROS 2 commissioning
Exec="$STATION_ROOT/run.sh"
Path=$STATION_ROOT
Icon=$STATION_ROOT/assets/icon.svg
Terminal=false
Categories=Development;Engineering;
StartupNotify=true
EOF
chmod +x "$STATION_ROOT/run.sh" "$DESKTOP_ENTRY"
if [[ -d "$HOME/Desktop" ]]; then
    cp "$DESKTOP_ENTRY" "$HOME/Desktop/Ecat GroundStation.desktop"
    chmod +x "$HOME/Desktop/Ecat GroundStation.desktop"
fi
printf 'Installed launcher: %s\n' "$DESKTOP_ENTRY"
