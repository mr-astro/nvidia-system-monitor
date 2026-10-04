# Cambios

## 3.1.0

Sobre el repositorio publicado (commit `435a799`). Se conservan sus decisiones: tema
oscuro, paleta de colores, panel de sensores fuera de la vista y la reorganización de la
documentación.

### Errores corregidos
- **CUDA mostraba `None`.** Al extraer `_row_to_metrics()` en `nvidia.py`, el método se
  insertó en mitad de `get_cuda_version()`: su cuerpo quedó como código inalcanzable y la
  función devolvía `None`. Reubicado, conservando el refactor.
- **CUDA mostraba `N/D` en drivers recientes.** El banner de `nvidia-smi` 615.x dice
  `CUDA UMD Version: 13.4`, no `CUDA Version:`. La expresión regular acepta ambos.
- **El intervalo de refresco configurado se ignoraba:** se había quitado `interval=`
  al crear el `Collector`.
- **Los colores y umbrales de la configuración no se aplicaban:** coexistían dos CSS y
  se usaba uno con colores fijos (`build_css` era código muerto, además con llaves `{{ }}`
  sin escapar en una cadena que no era f-string).
- **Tema aplicado una vez** en lugar de en cada fila: `gtk-application-prefer-dark-theme`
  se asignaba dentro de `MetricCard.__init__` y de `_create_row`.
- Eliminados `src/test_validators.py` (reportaba «0 passed, 0 failed» con éxito) y el
  `nvidia-system-monitor.desktop` antiguo (`Exec=... %k/...` no funciona). Los reemplazan
  `tests/` e `install.sh`, ya presentes.
- Import duplicado de `Config`.
- README: los paquetes de Ubuntu/Debian (`gtk4`, `python3-gobject`) no existen en sus
  repositorios; el correcto es `python3-gi gir1.2-gtk-4.0`. `libadwaita` no se usa.

### Novedades
- **Temperatura de discos SATA sin root** mediante el módulo `drivetemp` (se asocia cada
  `hwmon` a su disco a través de sysfs). Orden: sysfs NVMe → `drivetemp` → `smartctl`.
- **Opción `theme`** (`"dark"` por defecto, o `"system"`). El modo oscuro se basa en CSS
  propio y no depende de la propiedad `gtk-application-prefer-dark-theme`, obsoleta desde
  GTK 4.20.
- **Diseño en dos columnas independientes** (CPU y RAM a la izquierda, GPU a la derecha) y
  almacenamiento a todo el ancho, sin los huecos que dejaba la cuadrícula.
- **Panel de sensores opcional** (`panels.sensors`, desactivado por defecto).
- Tamaños de fuente en porcentaje (respetan la escala de texto del sistema).
- **CI en GitHub Actions** (`.github/workflows/tests.yml`): ejecuta la suite completa,
  incluida la GUI bajo Xvfb, en cada `push`.
- 15 pruebas nuevas, entre ellas una por cada regresión anterior (CUDA, intervalo, colores,
  CSS sin errores de análisis, diseño).

### Cambios
- Paleta por defecto: naranja `#FFA726` y rojo `#EF5350` (la suya) en lugar de los de v3.0.

## 3.0.0 — versión corregida

### Errores corregidos
- **Pruebas**: las del v2 importaban clases inexistentes (`NVIDIAData`, `CPUData`…) y el
  ejecutor las marcaba como "SKIPPED (expected)", terminando en éxito con 0 pruebas.
  Reescritas contra las clases reales, con datos simulados; ahora un fallo es un fallo.
- **Interfaz congelada**: `nvidia-smi`, `lsblk` y `smartctl` se ejecutaban en el hilo de
  GTK. Con `nvidia-smi` colgado, la ventana se congelaba 2,2 s cada ciclo. La
  recolección pasa a un hilo en segundo plano (`src/data/collector.py`).
- **Sensores**: `sensors -u` ya entrega °C, pero el código dividía entre 1000
  (45 °C → 0,045 °C), y el nombre del chip se sobrescribía con las líneas `Adapter:` y
  las etiquetas. El panel no se usaba, por lo que el error estaba latente.
- **Temperatura SATA**: `smartctl` devolvía el último número de la línea, que es el
  máximo histórico (`Min/Max 20/55` → 55 °C) en lugar del valor actual (38 °C). Además,
  se descartaba la salida si el código de retorno no era 0, aunque `smartctl` usa ese
  código como máscara de bits.
- **Uso de CPU**: `guest` y `guest_nice` se sumaban dos veces al total.
- **Almacenamiento**:
  - `rota` como cadena (`"1"`) en util-linux antiguos no se interpretaba.
  - Los volúmenes anidados (LUKS sobre partición, LVM) no aparecían.
  - Con varios montajes (btrfs en `/` y `/home`, el esquema por defecto de Fedora) no se
    mostraba el uso.
  - El uso se calculaba con `f_bavail`, contando el espacio reservado como usado;
    ahora sigue la semántica de `df`.
  - Se añade respaldo para util-linux sin la columna `MOUNTPOINTS`.
- **Temperatura NVMe**: se buscan ambas disposiciones de sysfs (`nvmeX/hwmonN` y
  `nvmeX/device/hwmon/hwmonN`).
- **Configuración**: `config.json.example` usaba claves que el código ignoraba. Ahora
  coinciden y todas tienen efecto. Se valida cada valor (tipos, rangos, colores `#RRGGBB`);
  la clave antigua `update_interval_seconds` se sigue aceptando.
- **Lanzador `.desktop`**: `%k` expande a la ruta del propio archivo, no a un directorio,
  así que `Exec` apuntaba a una ruta inexistente. Ahora lo genera `install.sh` con la
  ruta absoluta y el nombre coincide con el `application_id`.
- **Registro**: era ilimitado y repetía un traceback por segundo ante un fallo
  persistente. Ahora es rotativo y los errores repetidos se registran una vez por minuto.
- **Unidades**: los valores en base 1024 se rotulaban "GB/MB"; ahora "GiB/MiB".
- `Gdk` se importaba sin fijar versión (aviso `PyGIWarning` al arrancar).

### Cambios
- La GUI actualiza las etiquetas existentes en lugar de destruir y recrear todos los
  widgets en cada ciclo, y se reconstruye solo si cambia la estructura (discos, GPU…).
- Barras de uso con colores por umbral (`color_scheme`, `thresholds`).
- Paneles activables (`panels`) y unidad de temperatura (`temperature_unit`); un panel
  desactivado no se consulta.
- Panel de sensores funcional. Soporte para varias GPU y para `coretemp` (Intel).
- La caché incluye `Buffers`, igual que `free(1)`.
- Los datos estáticos de la CPU se leen una vez, no dos veces por ciclo.
- Rutas XDG (`XDG_CONFIG_HOME`, `XDG_STATE_HOME`).
- README alineado con el comportamiento real del código.

### Eliminado (código muerto o sin fuente fiable)
- Historial (`deque`) que se acumulaba pero nunca se mostraba, y `history_seconds`.
- Fila "Consumo" de CPU, que siempre mostraba `N/D`.
- `get_cuda_version()` con su consulta descartada (ahora la versión se lee del banner y se
  guarda en caché), campos `encoder/decoder_percent` nunca poblados, `Config.save()`
  nunca invocado, imports sin usar.
- `src/test_validators.py` y `nvidia-system-monitor.desktop` (sustituidos por `tests/` e
  `install.sh`).
