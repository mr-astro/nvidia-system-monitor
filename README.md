# NVIDIA System Monitor for LINUX

![Captura de la aplicación](screenshot.png)

Monitor de escritorio GTK4 para Linux (desarrollado y probado en Fedora): GPU NVIDIA, CPU, RAM y almacenamiento, con sensores opcionales.

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

**Sensores** (opcional, desactivado por defecto): lecturas de temperatura de `lm_sensors`,
útiles para ver la placa base. Se activa con `"sensors": true` en `panels`.

Los datos ausentes se muestran como `N/D`; nunca se sustituyen por valores inventados.

## Instalación

### Fedora

```bash
sudo dnf install gtk4 python3-gobject
```

### Otras distribuciones

- **Ubuntu / Debian** (nombres de paquete verificados contra los repositorios de Ubuntu 24.04):

```bash
sudo apt install python3-gi gir1.2-gtk-4.0
```

- **Arch Linux / Manjaro** (sin probar):

```bash
sudo pacman -S gtk4 python-gobject
```

No se necesita `pip` ni libadwaita.

### Opcionales

```bash
sudo dnf install lm_sensors        # panel de sensores
sudo dnf install smartmontools     # temperatura de discos SATA/USB (requiere root)
```

**Temperatura de discos SATA sin root:** cargue el módulo `drivetemp` del kernel.

```bash
sudo modprobe drivetemp                                         # hasta el próximo reinicio
echo drivetemp | sudo tee /etc/modules-load.d/drivetemp.conf    # permanente
```

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

En la rama de desarrollo, la aplicación **DEV** utiliza
`~/.config/nvidia-system-monitor-dev/config.json` y un identificador GTK independiente.
La infraestructura de alias se conserva y lee `storage-aliases.json` en ese
mismo directorio XDG, reservada para una futura opción de personalización:

```json
{"aliases": {"wwn-IDENTIFICADOR_DEL_DISCO": "Mi respaldo"}}
```

La clave debe ser un enlace de disco completo existente en `/dev/disk/by-id`,
preferentemente WWN; también se admiten identificadores de serie inequívocos.
Los alias son preferencias locales: no se guardan identificadores reales en el
repositorio. Las tarjetas siempre usan el modelo detectado o el nombre técnico
cuando no hay modelo; los alias no sustituyen sus títulos. Las
particiones se despliegan por disco y conservan su estado durante las actualizaciones.
Los diagnósticos completos están en tooltips; las fuentes obsoletas mantienen su aviso.

En ventanas anchas, CPU y RAM comparten la columna izquierda; GPU y Almacenamiento,
la derecha. `Gtk.FlowBox` reorganiza estas columnas en una sola al reducir el ancho,
sin reconstruir tarjetas ni perder los expanders. La ventana inicia en 1000 × 720,
con un mínimo de 380 × 320; el contenido conserva su altura natural y se desplaza
verticalmente cuando es necesario.

La cabecera indica cuándo se recibió la última instantánea y nombra las fuentes
con avisos. Las temperaturas SMART y UDisks2 muestran su antigüedad en la tarjeta
del disco; una lectura almacenada no hace parecer antiguas las demás métricas.
El tooltip global conserva las edades por fuente y los diagnósticos completos,
con los mismos umbrales de caducidad.

Las etiquetas de volumen se leen mediante `LABEL` en la consulta existente de
`lsblk` y aparecen como título de cada partición. Si no hay etiqueta, el título
es el nombre técnico. El identificador técnico, sistema de archivos, capacidad y
montajes se conservan en la información secundaria; no se infieren nombres
por el formato o el punto de montaje. Esto no modifica las etiquetas reales ni
las reglas de validación de montajes y caducidad.

La barra de cabecera muestra **el uso del sistema de archivos identificado**, no
la ocupación física del disco: porcentaje, espacio usado y espacio disponible
según `statvfs` (porcentaje = usado / (usado + disponible)). Si existen varios
sistemas de archivos independientes, la cabecera indica «Uso por particiones»
y sus barras aparecen al desplegar. Los montajes compartidos de btrfs se muestran
una sola vez, sin sumar subvolúmenes. Sin montaje o medición aparece «Uso no
disponible»; un fallo temporal conserva el último uso con aviso y antigüedad,
mientras que un cambio de identidad del montaje lo invalida inmediatamente.

Para temperatura SATA, DEV prueba sysfs/drivetemp, después las propiedades
almacenadas de UDisks2 en el bus D-Bus del sistema y, si es necesario, `smartctl`.
Gio forma parte del PyGObject existente; UDisks2 es opcional. No se inicia el
servicio, no se fuerza `SmartUpdate` ni se solicita autenticación. La antigüedad
proviene de `SmartUpdated`, no de la consulta D-Bus, y la misma medición no se
rejuvenece al volver a leerla. La fuente UDisks2 utiliza un umbral propio basado
en su mantenimiento habitual de diez minutos (752 s con el timeout predeterminado);
su antigüedad aparece junto a la temperatura. Se evalúan alternativas si falla
o caduca y se conserva una medición reciente frente a otra más antigua. Los errores
de permisos de `smartctl` permanecen en el diagnóstico y no ocultan una temperatura
válida de otra fuente.

| Clave | Por defecto | Descripción |
|---|---|---|
| `refresh_interval_seconds` | `1.0` | Intervalo de actualización (0.5 – 60). |
| `temperature_unit` | `"C"` | `"C"` o `"F"`. |
| `theme` | `"dark"` | `"dark"` (paleta oscura propia) o `"system"` (usa el tema del sistema). |
| `logging_enabled` | `true` | Escribe el registro en disco. |
| `panels.cpu / gpu / ram / storage` | `true` | Muestra u oculta cada panel. Un panel oculto tampoco se consulta. |
| `panels.sensors` | `false` | Panel de sensores de `lm_sensors`. |
| `color_scheme.low/medium/high_usage_color` | verde / naranja / rojo | Colores de las barras de uso, en formato `#RRGGBB`. |
| `thresholds.medium_percent / high_percent` | `60` / `85` | Porcentajes a partir de los cuales la barra cambia de color. |

Registro: `~/.local/state/nvidia-system-monitor/monitor.log` (rotativo, máx. 3 × 512 KB).

## Pruebas

```bash
python3 -m unittest discover -s tests -t . -v
```

Las pruebas usan datos simulados (salidas de `nvidia-smi`, `lsblk`, `sensors`, `smartctl`, `/proc`, `/sys`) y no dependen del hardware de la máquina. Las pruebas de interfaz se
omiten automáticamente si no hay GTK 4 o pantalla. Para comprobar también el reflujo,
la pantalla virtual debe admitir la ventana de escritorio. Este comando fuerza X11
en sesiones Wayland y usa Cairo para evitar depender de aceleración en Xvfb:

```bash
xvfb-run -a -s '-screen 0 1280x1024x24' \
  env GDK_BACKEND=x11 GSK_RENDERER=cairo python3 -B -m unittest discover -s tests -t . -v
```

El flujo de GitHub Actions (`.github/workflows/tests.yml`) ejecuta toda la suite, incluida
la interfaz, en cada `push` y `pull request`.

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
- La temperatura de discos NVMe se lee de sysfs sin privilegios. La de discos SATA se
obtiene del módulo `drivetemp` si está cargado; si no, de `smartctl`, que normalmente
exige root. Sin ninguna de las dos se muestra `N/D`.
- Solo se consulta `nvidia-smi`; no hay soporte para GPU AMD o Intel.
