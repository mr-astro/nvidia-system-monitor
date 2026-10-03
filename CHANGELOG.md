# Cambios

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
