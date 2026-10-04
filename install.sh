#!/usr/bin/env bash
# Instala (o desinstala) el lanzador de escritorio para el usuario actual.
#   ./install.sh              crea el .desktop y copia la configuración de ejemplo
#   ./install.sh --uninstall  elimina el .desktop (no toca la configuración)
#!/bin/bash

# Ruta del icono en el repositorio
ICON_SOURCE="assets/icons/cl.mastro.NvidiaSystemMonitor.png"

# Ruta de destino en el sistema
ICON_DEST="/usr/share/icons/hicolor/scalable/apps/cl.mastro.NvidiaSystemMonitor.png"

# Copiar el icono
if [ -f "$ICON_SOURCE" ]; then
    sudo cp "$ICON_SOURCE" "$ICON_DEST"
    sudo gtk-update-icon-cache /usr/share/icons/hicolor/
    echo "Icono instalado correctamente."
else
    echo "Error: No se encontró el icono en $ICON_SOURCE"
    exit 1
fi

set -euo pipefail
shopt -u patsub_replacement 2>/dev/null || true

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nvidia-system-monitor"
DESKTOP_ID="cl.mastro.NvidiaSystemMonitor.desktop"
TARGET="$APPS_DIR/$DESKTOP_ID"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$TARGET"
    command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true
    echo "Lanzador eliminado: $TARGET"
    exit 0
fi

# Caracteres que romperían el campo Exec= del .desktop.
case "$APP_DIR" in
    *[\"\`\$\\%]*)
        echo "Error: la ruta del proyecto contiene caracteres no soportados: $APP_DIR" >&2
        exit 1
        ;;
esac

mkdir -p "$APPS_DIR" "$CONF_DIR"
content="$(<"$APP_DIR/$DESKTOP_ID.in")"
printf '%s\n' "${content//@APP_DIR@/"$APP_DIR"}" > "$TARGET"
chmod +x "$APP_DIR/bin/system_monitor.py"

if [[ ! -e "$CONF_DIR/config.json" ]]; then
    cp "$APP_DIR/config.json.example" "$CONF_DIR/config.json"
    echo "Configuración creada: $CONF_DIR/config.json"
fi

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" 2>/dev/null || true
echo "Lanzador instalado: $TARGET"
