# NVIDIA System Monitor for Fedora

Monitor de escritorio GTK4 para Fedora Linux: GPU NVIDIA, CPU, RAM, almacenamiento y sensores.

## Qué monitoriza

**GPU NVIDIA** (vía `nvidia-smi`; soporta varias GPU): modelo, uso, VRAM, temperatura,
consumo y límite de potencia, frecuencias de núcleo y memoria, ventilador, versión del
driver y de CUDA. Si NVIDIA o el driver no están disponibles, el panel lo indica.

**CPU**: modelo, uso, frecuencia actual y máxima, núcleos/hilos, carga 1/5/15 min y
temperatura cuando existe `k10temp`, `zenpower` (AMD) o `coretemp` (Intel).

**RAM**: total, usada, disponible, libre, caché y buffers, swap.

**Almacenamiento**: cada disco físico con modelo, tipo (NVMe, SSD SATA, HDD, USB, eMMC/SD),
capacidad y temperatura; debajo, sus particiones y volúmenes anidados (LUKS, LVM) con
sistema de archivos, punto(s) de montaje y uso. Si un dispositivo tiene varios montajes
(p. ej. subvolúmenes btrfs en `/` y `/home`), el uso mostrado es el del sistema de
archivos completo.

**Sensores**: lecturas de temperatura de `lm_sensors`.

Los datos ausentes se muestran como `N/D`; nunca se sustituyen por valores inventados.

## Instalación en Fedora

```bash
sudo dnf install gtk4 python3-gobject
```

Opcionales:

```bash
sudo dnf install lm_sensors        # panel de sensores
sudo dnf install smartmontools     # temperatura de discos SATA (requiere root, ver Limitaciones)
```

No se necesita `pip`.

## Ejecución

```bash
python3 bin/system_monitor.py
```

Para añadirlo al menú de aplicaciones (crea el lanzador con la ruta correcta y copia la
configuración de ejemplo si aún no tienes una):

```bash
./install.sh              # instalar
./install.sh --uninstall  # quitar el lanzador (no borra tu configuración)
```

## Configuración

Archivo: `~/.config/nvidia-system-monitor/config.json` (respeta `XDG_CONFIG_HOME`).
Hay un ejemplo completo en `config.json.example`. Todos los campos son opcionales; los
valores inválidos se ignoran y se usa el valor por defecto.

| Clave | Por defecto | Descripción |
|---|---|---|
| `refresh_interval_seconds` | `1.0` | Intervalo de actualización (0.5 – 60). |
| `temperature_unit` | `"C"` | `"C"` o `"F"`. |
| `logging_enabled` | `true` | Escribe el registro en disco. |
| `panels.cpu / gpu / ram / storage / sensors` | `true` | Muestra u oculta cada panel. Un panel oculto tampoco se consulta. |
| `color_scheme.low/medium/high_usage_color` | verde / ámbar / rojo | Colores de las barras de uso, en formato `#RRGGBB`. |
| `thresholds.medium_percent / high_percent` | `60` / `85` | Porcentajes a partir de los cuales la barra cambia de color. |

Registro: `~/.local/state/nvidia-system-monitor/monitor.log` (rotativo, máx. 3 × 512 KB).

## Pruebas

```bash
python3 -m unittest discover -s tests -t . -v
```

Las pruebas usan datos simulados (salidas de `nvidia-smi`, `lsblk`, `sensors`, `smartctl`,
`/proc`, `/sys`) y no dependen del hardware de la máquina. Las pruebas de interfaz se
omiten automáticamente si no hay GTK 4 o pantalla; para ejecutarlas en una sesión sin
entorno gráfico: `xvfb-run -a python3 -m unittest discover -s tests -t .`

## Diseño técnico

```
bin/system_monitor.py   punto de entrada
src/data/               recolectores (cpu, ram, nvidia, storage, sensors) y collector.py
src/gui/app.py          interfaz GTK4
src/utils/              configuración, registro, rutas XDG y formato
tests/                  pruebas unitarias y de humo de la GUI
```

- Toda la recolección (subprocesos y lecturas de `/proc` y `/sys`) corre en **un único hilo
  en segundo plano**. El bucle de GTK solo lee la última instantánea y actualiza los
  widgets existentes, por lo que un `nvidia-smi` lento o colgado no congela la ventana.
- Cada fuente falla de forma aislada: si una falla, las demás siguen funcionando y el
  encabezado muestra un aviso (el detalle aparece al pasar el cursor).
- La topología de discos (`lsblk`) se consulta cada 15 s; los sensores, cada 5 s;
  `smartctl`, como máximo cada 10 s por disco (cada 5 min si falla).
- No se ejecutan benchmarks de almacenamiento.

## Limitaciones

- La disponibilidad de temperaturas y consumo depende del hardware, el driver y los
  sensores que Linux exponga.
- **No se muestra el consumo eléctrico de la CPU**: no existe una fuente fiable y
  universal sin privilegios, y no se presenta una estimación como si fuera una medición.
- La temperatura de discos NVMe se lee de sysfs sin privilegios. La de discos SATA/USB
  usa `smartctl`, que normalmente exige root; sin él se muestra `N/D`.
- Solo se consulta `nvidia-smi`; no hay soporte para GPU AMD o Intel.
