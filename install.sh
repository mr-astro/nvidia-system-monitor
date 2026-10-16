#!/usr/bin/env bash
# Instala (o desinstala) el lanzador de escritorio para el usuario actual.
#   ./install.sh [--channel dev|stable]              instala el canal (stable por defecto)
#   ./install.sh [--channel dev|stable] --uninstall  elimina launcher e icono, conserva preferencias
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

APP_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# Python 3.10's -m prepends the working directory to sys.path. Anchor it to
# this checkout so a caller's unrelated src package cannot replace the helper.
cd -- "$APP_DIR"
PYTHONPATH="$APP_DIR" exec python3 -B -m src.utils.installer \
    --app-dir "$APP_DIR" --channel "$CHANNEL" --action "$ACTION"
