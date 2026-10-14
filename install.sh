#!/usr/bin/env bash
# Instala (o desinstala) el lanzador de escritorio para el usuario actual.
#   ./install.sh [--channel dev|stable]              instala el canal (stable por defecto)
#   ./install.sh [--channel dev|stable] --uninstall  elimina su .desktop, conserva configuración
#   ./install.sh --channel dev --check-launcher     comprueba sin modificar la instalación

set -euo pipefail
shopt -u patsub_replacement 2>/dev/null || true

CHANNEL=stable
ACTION=install
CHANNEL_SET=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --channel|--channel=*)
            if [[ "$CHANNEL_SET" == true ]]; then
                echo "Error: especifique --channel solo una vez" >&2
                exit 2
            fi
            CHANNEL_SET=true
            if [[ "$1" == --channel ]]; then
                if [[ $# -lt 2 ]]; then
                    echo "Error: --channel requiere dev o stable" >&2
                    exit 2
                fi
                CHANNEL="$2"
                shift 2
            else
                CHANNEL="${1#--channel=}"
                shift
            fi
            ;;
        --uninstall|--check-launcher)
            if [[ "$ACTION" != install ]]; then
                echo "Error: seleccione solo una acción" >&2
                exit 2
            fi
            if [[ "$1" == --uninstall ]]; then ACTION=uninstall; else ACTION=check; fi
            shift
            ;;
        *) echo "Uso: $0 [--channel dev|stable] [--uninstall|--check-launcher]" >&2; exit 2 ;;
    esac
done

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# All launcher/config writes use pinned directory descriptors in the helper.
run_helper() {
    PYTHONPATH="$APP_DIR" python3 -B -m src.utils.installer \
        --app-dir "$APP_DIR" --channel "$CHANNEL" --action "$1"
}

if [[ "$ACTION" == check ]]; then
    run_helper check
    exit $?
fi

# Reject unsafe destinations before the historical stable icon operations.
# The actual operation reopens and revalidates every destination afterwards.
run_helper validate

# The shared installed icon belongs to the historical stable installation.
# DEV references it, but must never replace it or update the global icon cache.
if [[ "$CHANNEL" == stable ]]; then
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
fi

run_helper "$ACTION"
