# Cambios

## 3.2.0 — 2026-10-15

Versión aprobada, integrada en `main` y verificada en la instalación local estable.
La etiqueta `v3.2.0` y la GitHub Release siguen pendientes.
Los antecedentes 3.0.0 y 3.1.0 se conservan; este cierre no los renumera.

### Estado de la auditoría

- A1, A2, A3, A4, A5, A6 y A7 están resueltos en código según las auditorías.
- A5 queda cerrado sin cambios funcionales: A1 y A4 ya separan IDs GTK,
  configuración, alias, registros, launchers e iconos de DEV y estable. Se
  verificaron 94 pruebas relacionadas sin fallos ni omisiones, además de rotación
  efectiva de logs y trabajadores simultáneos con datos sintéticos.
- Se mantienen las correcciones de A2 para identidad física, sesiones y antigüedad
  térmica; A3 para uso de sistemas de archivos en discos completos; y A6 para
  resolver alias fuera del hilo GTK y del colector de hardware.
- El cierre documental actualiza únicamente README y CHANGELOG con los resultados
  de integración e instalación ya verificados; no modifica código ni ejecuta
  operaciones de publicación.

### Instalador por usuario (A4)

- Instalación y desinstalación por usuario sin `sudo` ni escrituras globales.
  Launchers e iconos independientes para DEV y estable bajo `XDG_DATA_HOME`;
  se conserva el diseño del PNG y el icono global histórico no se toca.
- Helper anclado al checkout, compatible con Python 3.10 y con rutas con espacios,
  independientemente del directorio actual o de un paquete `src` ajeno.
- Fuentes y destinos comprobados antes de publicar, también para iconos, con
  recorrido `O_NOFOLLOW`, descriptores de directorio y rechazo de enlaces duros.
  Publicación atómica por archivo y limpieza de temporales ante fallos parciales.
- Desinstalación sin depender de plantillas, PNG ni configuración de ejemplo;
  conserva preferencias, alias, registros y recursos del otro canal.
- Caché de launchers opcional con límite de tres segundos; su fallo no invalida
  los archivos publicados. Pruebas aisladas de instalación, actualización,
  desinstalación, enlaces, rutas, cachés e identidades e iconos GTK por canal.
- Documentada la migración de recursos existentes, sin asignar una nueva versión
  de la aplicación ni crear etiquetas Git.

### Documentación de publicación

- Corregidas las referencias a hallazgos pendientes y al hilo único: el trabajador
  de alias es independiente. Documentado el uso de sistemas de archivos directos.
- Instrucciones explícitas de instalación, comprobación y desinstalación de ambos
  canales como usuario normal; los comandos administrativos solo gestionan
  dependencias del sistema o el módulo opcional `drivetemp`.
- Versión 3.2.0 aprobada y etiqueta futura propuesta `v3.2.0`. Documentadas la
  actualización estable y la reversión por canal; restaurar un launcher no revierte
  el código del checkout al que apunta. La validación sigue siendo manual, sin CI.

### Validación de preparación (2026-10-15)

- Suite completa: **335 pruebas aprobadas, cero fallos y cero omisiones**, bajo
  Xvfb y D-Bus con HOME/XDG temporales. Fedora Linux 44, Python 3.14.8, GTK 4.22.5;
  base de código `c77de5e` y cambios únicamente documentales.
- `git diff --check`, `bash -n install.sh` y validación de las dos plantillas
  `.desktop` con rutas con espacios: correctos en esa validación de preparación.
- Se conserva este resultado histórico anterior a la integración; no se registra
  una nueva suite completa posterior ni se acreditan pruebas de carga GPU.

### Cierre documental e instalación local (2026-10-16)

- `main` recibió mediante fast-forward el commit
  `2ffad3d1ad0b826e1a8dda146eae00b13aa8f8bc`.
- Instalación local estable actualizada de `4df03aa` a `2ffad3d`, con las acciones
  `validate`, `install` y `check` del instalador estable verificadas correctamente.
- Launcher estable con `--channel stable` explícito; el icono global histórico
  conserva su SHA-256.
- DEV y estable se ejecutaron simultáneamente con identidades D-Bus diferentes;
  ambas aplicaciones mostraron telemetría GPU coherente con `nvidia-smi`.
- Registros comprobados en directorios XDG independientes para ambos canales.
- Captura oficial `screenshot.png` actualizada en `f4f5f99`, sin otros cambios en
  ese commit. Este cierre documental no crea la etiqueta ni la GitHub Release.

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
- **Aclaración sobre CI:** la documentación de 3.1.0 mencionaba
  `.github/workflows/tests.yml`, pero el historial Git disponible no contiene ese
  workflow. No existe CI activa en esta rama; la suite se valida manualmente bajo
  Xvfb y D-Bus.
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
