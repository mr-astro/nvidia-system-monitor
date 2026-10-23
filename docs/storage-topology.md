# Almacenamiento: fase 2A

La adquisición sigue en `StorageMonitor`, fuera de GTK. Se conserva
`DiskMetrics.partitions`, su uso de filesystems y todas las fuentes térmicas.
Esta fase no incorpora gráficos ni atributos SMART.

## Orden

`src/data/storage_order.py` contiene funciones sin I/O ni dependencias GTK:

1. Respaldos físicos verificados del montaje `/`.
2. NVMe con transporte declarado.
3. SATA/ATA con transporte declarado.
4. USB, según `TRAN` o la cadena `SUBSYSTEMS`.
5. Transporte desconocido u otros transportes.

La raíz requiere una correspondencia vigente entre `/proc/self/mountinfo` y el
montaje asociado durante la adquisición de topología. Se siguen las relaciones
padre-hijo; los nodos lógicos compartidos se identifican por `MAJ:MIN`. LUKS, LVM
y respaldos múltiples no se sustituyen por su primer padre. Cada respaldo físico
debe corresponder a una sesión verificada. Una raíz multidispositivo promueve todos
sus respaldos y no declara un disco principal único.

Para Btrfs se contrasta `UUID` con los miembros de
`/sys/fs/btrfs/<UUID>/devices/*/dev` y el inventario `devinfo`. Se exige una lectura
coherente y completa: un UUID compartido podría proceder de una copia del disco.
Estas lecturas se realizan únicamente al refrescar la topología. Si faltan miembros,
hay contradicciones, cambia el montaje o caduca/falla la topología, no se afirma
una raíz física actual. El motivo permanece en `DiskOrder.root.reason`.

Los desempates usan identidad física, sesión cuando falta identidad persistente,
y nombre técnico como último recurso. No dependen de modelo, alias ni orden de
enumeración. `order_disks()` devuelve una lista ordenada con los mismos objetos;
`StoragePanel` continúa reconciliando las mismas identidades y conservando expansión.

## Distribución física

`src/data/storage_layout.py` define tipos inmutables:

- `PartitionIdentity`: nombre, PARTUUID, PARTTYPE y número opcionales.
- `PhysicalRegion`: clase, inicio y fin exclusivo en bytes, tamaño derivado e
  identidad opcional de partición.
- `PhysicalLayout`: nombre del disco, `Presence`, capacidad física, sector lógico,
  regiones, contenedores extendidos, estado, diagnóstico y `SourceState` de topología.

`DiskMetrics.layout` es complementario. Una distribución no disponible no oculta
las métricas anteriores ni crea un error global de adquisición.

La consulta `lsblk -J -b` existente incorpora START, LOG-SEC, KNAME, PKNAME,
PARTUUID, PARTTYPE, PTTYPE, PARTN, UUID y SUBSYSTEMS. DISK-SEQ se normaliza al campo
interno `diskseq`. Si una columna opcional no existe, se retira solo esa columna;
el conjunto aceptado se reutiliza en consultas posteriores. El intervalo continúa
siendo 15 segundos por defecto, sin procesos por partición.

**START está en sectores de 512 bytes**, incluso con LOG-SEC de 4096; SIZE está
en bytes por `-b`. LOG-SEC sirve para comprobar alineación, no para convertir START.
Si falta LOG-SEC, los offsets aún pueden comprobarse en unidades de 512 bytes;
no se afirma conocer la alineación de un sector lógico mayor.

La capacidad procede del disco, nunca de sumar descendientes. Se verifican
límites, alineación, identidad, relaciones y ausencia de solapamientos. LUKS/LVM
anidados y subvolúmenes Btrfs no añaden regiones. Cada miembro físico de Btrfs tiene
su propia distribución, sin atribuirle la capacidad agregada del filesystem.

Los estados son `partitioned`, `empty`, `whole_device` y `unavailable`. Los
filesystems y contenedores sobre disco completo ocupan una región `whole_device`;
no se inventa una partición. Los errores devuelven regiones vacías y un motivo.
No se calculan porcentajes ni se normalizan tamaños contradictorios.

Los huecos `unpartitioned` son bytes **fuera de las extensiones de particiones**.
Pueden incluir cabeceras y metadatos de la tabla: no representan espacio libre de
filesystem ni garantizan espacio utilizable por una nueva partición. El disco sin
particiones queda explícitamente identificado, incluso cuando contiene una tabla
vacía. Los huecos dentro de una extendida son `extended_reserved`, no espacio sin
particionar. El contenedor extendido queda aparte y nunca se suma a sus lógicas.

Para DOS se requieren PARTTYPE y, cuando hay una extendida, PARTN. Solo se aceptan
lógicas contenidas en su extensión verificada. Algunos kernels exponen una
extendida de solo 1 KiB en lsblk: esa geometría no permite recuperar su extensión
real y se devuelve `unavailable`, sin adivinarla. Tablas distintas de GPT/DOS,
tablas anidadas sobre mappers, metadatos incompletos y relaciones contradictorias
también son explícitamente no disponibles.

La geometría derivada se reutiliza mientras no cambien la topología, la sesión ni
su estado de adquisición. Su reloj no se rejuvenece por actualizar uso o temperatura.
`layout.available` describe validez geométrica; `layout.current(now)` exige además
una adquisición vigente y sin error. Un futuro gráfico deberá comprobar ambas.

## Validación con el equipo real

Desde la raíz DEV, sin privilegios ni modificar particiones:

```bash
git branch --show-current
findmnt -n -o SOURCE,FSTYPE,MAJ:MIN /
lsblk -b -o NAME,KNAME,TYPE,SIZE,START,LOG-SEC,PKNAME,PARTTYPE,PTTYPE,PARTN,FSTYPE,MOUNTPOINTS,TRAN,SUBSYSTEMS
python3 -B bin/system_monitor.py --channel dev
```

Cerrar solo una instancia DEV previa para cargar el nuevo código. Contrastar los
respaldos de `/` con sus ancestros y, para Btrfs, con el inventario sysfs. En el equipo
de desarrollo se espera Dahua/Fedora, Samsung, Kingston y finalmente Toshiba USB,
si la evidencia del equipo corresponde a esa configuración; esos nombres no
participan en el algoritmo.

Este diagnóstico usa la adquisición normal de almacenamiento (incluidas sus
temperaturas opcionales) y permite inspeccionar el modelo sin dibujarlo:

```bash
python3 -B - <<'PY'
import time
from src.data.storage import StorageMonitor

monitor = StorageMonitor()
for disk in monitor.get_metrics():
    layout = disk.layout
    print(disk.name, disk.model, 'prioridad:', disk.order.priority)
    print('raíz:', disk.order.root.disks, 'diagnóstico:', disk.order.root.reason)
    print('distribución:', layout.state, 'capacidad:', layout.capacity_bytes,
          'vigente:', layout.current(time.monotonic()), 'motivo:', layout.reason)
    for region in layout.regions:
        print(' ', region.kind, region.start_bytes, region.end_bytes,
              region.size_bytes, region.partition)
    if layout.available:
        assert sum(r.size_bytes for r in layout.regions) == layout.capacity_bytes
PY
```

Conectar y desconectar únicamente una unidad USB prescindible que no esté en uso.
Esperar el intervalo de topología y comprobar que conserva el orden relativo de
las demás tarjetas y su expansión. No desconectar un respaldo raíz. Para comprobar
el modelo durante varios ciclos, reutilizar la misma instancia del monitor.

Las pruebas automáticas emplean dispositivos sintéticos: cubren raíces simples,
LUKS/LVM/Btrfs, múltiples respaldos, USB, ambigüedad, geometría, caducidad, cachés y
conservación de widgets. No certifican por sí solas la configuración física real.

Referencias primarias: [columnas y START de util-linux](https://kernel.googlesource.com/pub/scm/utils/util-linux/util-linux/+/refs/tags/v2.41.1/misc-utils/lsblk.c),
[relaciones y salida de lsblk](https://man7.org/linux/man-pages/man8/lsblk.8.html),
[inventario sysfs de Btrfs](https://btrfs.readthedocs.io/en/latest/ch-sysfs.html).
