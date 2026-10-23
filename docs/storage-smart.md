# Adquisición SMART — fase 3.1

UDisks2 es la fuente principal de salud SMART. `UDisksSMART` amplía el proveedor
anterior de temperaturas y comparte su inventario ObjectManager. Implementa
`Drive.Ata` y `NVMe.Controller`; `smartctl -j -a` queda como alternativa cuando
UDisks2 no ofrece una muestra vigente y hay acceso de lectura al dispositivo.
Toda adquisición corre en `hw-collector`. GTK solo presenta instantáneas y no
realiza llamadas D-Bus, procesos ni comprobaciones de permisos. Los gráficos de
doble anillo no cambian.

## Modelo

`DiskMetrics.smart` contiene un `SmartMetrics` inmutable:

| Campo | Significado |
| --- | --- |
| `source` | `udisks2-ata`, `udisks2-nvme` o `smartctl` |
| `health` | `passed`, `failed` o `unknown`; no predice vida restante |
| `percentage_used` | Desgaste NVMe, 0–255; puede superar 100 |
| `available_spare` | Reserva disponible NVMe, 0–100; no es espacio del filesystem |
| `unsafe_shutdowns` | Contador histórico de apagados inseguros NVMe |
| `power_on_hours` | Horas decodificadas por la fuente |
| `temperature_c` | Temperatura de esa misma muestra |
| `critical_warning` | Máscara NVMe de smartctl; UDisks2 aporta nombres en `warnings` |
| `media_errors`, `error_log_entries` | Contadores NVMe; ausente no equivale a cero |
| `interface_crc_errors` | Contador ATA corroborado por ID 199, nombre CRC y unidad |
| `ata_attributes` | Tupla de atributos ATA reportados, sin equivalencias de desgaste |
| `warnings` | Avisos de salud, atributos, registros o contadores |
| `exit_status` | Código del proceso, solo para muestras smartctl |
| `state` | Adquisición, finalización, edad máxima y error de consulta |

La interfaz muestra fuente, edad y contadores en el tooltip. Los valores que han
caducado o tienen error de consulta solo aparecen como lectura anterior. La
medición principal se identifica como no disponible. Se conservan los avisos
junto a lecturas vigentes: un contador CRC histórico no declara fallo de salud.

## Interfaces UDisks2

Se consultan `GetManagedObjects` y `SmartGetAttributes` con tiempo acotado,
`NO_AUTO_START` y sin autorización interactiva. No se solicita `SmartUpdate`,
activación de SMART, autopruebas, operaciones de dispositivo ni cambios de permisos.

- **ATA:** se validan `SmartSupported`, `SmartEnabled`, `SmartUpdated`,
  `SmartFailing`, temperatura Kelvin, `SmartPowerOnSeconds` y contadores de
  atributos/sectores. Las horas resultan de segundos / 3600; cero significa
  desconocido según la API. La tabla tiene firma `a(ysqiiixia{sv})`.
- **NVMe:** se exige controlador `live`, `SmartUpdated` válido, y se leen
  `SmartCriticalWarning`, `SmartPowerOnHours` y temperatura Kelvin. Las
  advertencias son cadenas, no una máscara numérica. Una lista vacía declara
  ausencia de fallo crítico; una propiedad ausente significa salud desconocida.
  `SmartGetAttributes` devuelve `a{sv}`: `percent_used`, `avail_spare`,
  `media_errors`, `unsafe_shutdowns` y `num_err_log_entries` se validan con sus
  tipos y rangos. NVMe requiere la interfaz disponible desde UDisks2 2.10.

Referencias primarias: [Drive.Ata](https://storaged.org/doc/udisks2-api/latest/gdbus-org.freedesktop.UDisks2.Drive.Ata.html)
y [NVMe.Controller](https://storaged.org/doc/udisks2-api/latest/gdbus-org.freedesktop.UDisks2.NVMe.Controller.html).

## Identidad y coherencia

La asociación parte del objeto Block del disco completo, excluye particiones y
exige una única coincidencia. Block.Drive debe conducir a un Drive corroborado
con WWN o serie de la identidad física actual; un modelo contradictorio invalida
la asociación. En discos completos `sd` se admite exclusivamente el truncamiento
ASCII de 16 caracteres que expone SCSI en sysfs, si coincide con el inicio del
modelo completo de UDisks2 y existe corroboración física. No se acepta cualquier
prefijo ni se aplica esa excepción a NVMe. Ni el nombre `/dev/...`, ni el modelo
solo, ni la ruta D-Bus bastan.

La identidad del kernel y DISKSEQ se comprueban antes y después de la adquisición.
El proveedor vuelve a obtener ObjectManager después de `SmartGetAttributes` y
exige la misma asociación física, interfaz, `SmartUpdated` y propiedades. Si
cambian, rechaza la muestra. Si las propiedades avanzan mientras está vigente la
caché de atributos, la muestra se marca pendiente de renovación; no se combinan
propiedades nuevas con atributos antiguos. No se mezclan campos de UDisks2 con
smartctl ni se les asigna una fecha común.

La caché participa en invalidación, traslado y enriquecimiento A2: una nueva
sesión, desconexión o identidad contradictoria invalida el estado anterior. Los
cambios de nombre requieren corroborar continuidad de la misma sesión. Las
instantáneas publicadas y sus muestras permanecen inmutables.

## Cadencia, antigüedad y alternativa

El inventario se comparte entre discos y se consulta cada 10 s. Los atributos
se consultan cada 60 s por sesión; los fallos de transporte/método esperan 300 s.
Cada adquisición SMART tiene un presupuesto conjunto de 2 s para sus llamadas
D-Bus, incluida la verificación posterior, con cancelación de Gio. La temperatura
del kernel conserva su prioridad; después se usa UDisks2, también para NVMe.

La fecha de UDisks2 se ancla una vez a `SmartUpdated`, convertido a tiempo
monotónico. Volver a leer el mismo timestamp, consultar la caché, cambiar el reloj
civil o refrescar GTK no reinicia la edad. Se rechazan timestamps cero, futuros
no corroborados y regresiones. El umbral predeterminado es 752 s, coherente con
la política anterior de 600 s más tolerancia. Esta política no presupone que el
servicio actualice NVMe con esa periodicidad.

Si UDisks2 falla, no ofrece una muestra válida o caduca, se evalúa smartctl. Los
campos opcionales ausentes siguen como desconocidos, sin completar desde otra
fuente. Primero se exige
binario y `os.access(..., R_OK)`; sin acceso no se ejecuta el proceso. Este control
no garantiza permisos de ioctl: la salida real aún se valida y un rechazo entra
en backoff. No se utiliza sudo ni se modifica el sistema.

La alternativa conserva JSON validado, tipos/rangos, concordancia de código de
salida, timeout de 2 s, intervalo de 60 s y backoff de al menos 300 s. Sus datos
caducan a los 77 s. Los bits 0–1 invalidan la respuesta; el bit 2 conserva datos
parciales con error explícito; los bits 3–7 son avisos. Véase el
[manual de smartctl](https://github.com/smartmontools/smartmontools/blob/main/src/smartctl.8.in).
Los errores de ambas fuentes se publican por separado en `sources` y `errors`
(`udisks:smart:NOMBRE` y `smart:NOMBRE`). Una muestra vigente de UDisks2 evita
consultar smartctl aunque haya una alternativa anterior en caché.
Los diagnósticos publicados de smartctl se retiran cuando UDisks2 está vigente y
la temperatura tampoco usa smartctl. La caché de backoff no se elimina; si vuelve
a necesitarse esa alternativa, se restaura su estado original, sin una consulta
anticipada ni una fecha renovada.

## Fixtures de los discos confirmados

| Disco | Datos confirmados incluidos |
| --- | --- |
| Dahua C970 | `percent_used=7`, `avail_spare=100`, `media_errors=0`, `unsafe_shutdowns=104` |
| Samsung PM9C1b | `percent_used=0`, `avail_spare=100`, `media_errors=0` |
| Kingston A400 | Atributo 199 CRC=131109; atributo 231 contradictorio entre fuentes |

Son envolturas D-Bus y JSON simuladas. Los modelos y contadores indicados proceden
del usuario; series, rutas, fechas, temperaturas, horas y otros campos auxiliares
son sintéticos y no se presentan como mediciones del hardware real. Un contador
no comunicado, como apagados inseguros del Samsung, permanece ausente.

El atributo 231 del A400 se conserva para inspección sin interpretarlo como
porcentaje usado, salud, temperatura ni vida restante. La temperatura procede de
la propiedad correspondiente. Para el CRC se exigen conjuntamente ID 199,
`udma-crc-error-count` y unidad dimensionless (1); con otro nombre, unidad o ID no
se deduce el contador. Las unidades desconocidas de atributos no se convierten.

## Limitaciones y decisiones

- Sin DISKSEQ verificable no se publica salud reutilizable. UDisks2 también
  requiere corroboración física, interfaz apropiada y timestamps válidos.
- No se fuerza actualización de SMART. NVMe puede recibir actualizaciones solo
  por eventos; si el servicio conserva una muestra demasiado antigua y tampoco
  hay acceso a smartctl, se muestra medición no disponible.
- Un fallo de `SmartGetAttributes` invalida la muestra completa para presentación
  vigente, aunque algunas propiedades estén disponibles. Se conserva la historia
  y se intenta la alternativa con acceso, sin fabricar atributos ausentes.
- No se interpreta desgaste SATA, no se diagnostica la causa de CRC/apagados
  inseguros y no se configuran puentes USB/RAID especiales con `-d`.
- Varias consultas pueden retrasar el hilo de recolección; GTK sigue procesando
  eventos y las muestras publicadas caducan con independencia de ese hilo.
- La fase 3.1C validó el Kingston A400 real en Fedora 44 mediante lecturas
  UDisks2 y un ciclo del recolector; no se cambiaron permisos ni servicios.
  La validación NVMe de esta entrega sigue basada en fixtures. No hay decisiones
  pendientes que requieran aprobación para esta implementación de lectura.

## Verificación

Las pruebas cubren ambos formatos D-Bus, los tres discos, JSON, datos ausentes o
malformados, permisos, timeout/deadline, caché y backoff, recuperación de fuentes,
cambios de reloj, identidad/sesión y GTK durante consultas retenidas. La suite
completa se ejecuta con:

```bash
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' \
  env GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=none \
  /usr/bin/python3 -B -m unittest discover -s tests -t . -q
git diff --check
```

Validación del 23 de octubre de 2026: 161 pruebas dirigidas aprobadas; suite
completa de 561 pruebas aprobadas en 21,239 s, sin fallos ni omisiones. Se
añadieron 23 pruebas UDisks2 y 2 de interfaz respecto de la entrega anterior.
Entorno: Python 3.14.8 y GTK 4.22.5. `git diff --check` limpio. La pantalla y el
bus temporales requieren sockets locales y se ejecutaron fuera del sandbox;
los avisos de deprecación y cierre de los servicios de prueba no produjeron fallos.

La continuación 3.1C terminó con 572 pruebas aprobadas en 21,192 s, sin fallos ni
omisiones, incluyendo 11 regresiones nuevas. Véase [diagnóstico SATA real en Fedora](storage-smart-fedora.md)
para la causa raíz, evidencia antes/después y verificación de frescura.
