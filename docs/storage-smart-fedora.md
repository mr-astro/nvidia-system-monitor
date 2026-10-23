# Fase 3.1C — Kingston A400 en Fedora 44

## Causa raíz confirmada

La asociación `/dev/sda` → Block.Drive → Drive.Ata era única y correcta. WWN y
serie coincidían con la identidad del kernel; DISKSEQ era 1 antes y después de
la adquisición. El rechazo procedía de la comparación literal del modelo:

| Fuente | Modelo |
| --- | --- |
| `/sys/class/block/sda/device/model` | `KINGSTON SA400S3` |
| UDisks2 Drive.Model | `KINGSTON SA400S37480G` |

El campo SCSI exportado por sysfs está limitado a 16 caracteres, como establece
el [código del kernel Linux](https://github.com/torvalds/linux/blob/master/drivers/scsi/scsi_sysfs.c)
en `sdev_rd_attr(model, "%.16s\n")`. UDisks2 conserva el modelo ATA completo.
El comparador introducido en la fase anterior trataba esta diferencia como
contradicción de identidad, aun cuando WWN y serie corroboraban el dispositivo.

Esto bloqueaba tanto temperatura como salud antes de llamar a
`SmartGetAttributes`. A continuación se evaluaba la alternativa smartctl, cuyo
acceso directo al dispositivo no estaba permitido. El aviso de permisos describía
esa alternativa, mientras UDisks2 sí permitía leer los datos.

## Evidencia real antes y después

Lecturas realizadas el 23 de octubre de 2026 en Fedora Linux 44, mediante el mismo
usuario, sin sudo, SmartUpdate, cambios de permisos ni reinicio del servicio.
El diagnóstico D-Bus se ejecutó fuera del sandbox que bloquea sockets del sistema.
No se incluyen series, WWN ni rutas con identificadores físicos completos.

| Comprobación | Antes | Después |
| --- | --- | --- |
| Bloques que corresponden a `/dev/sda` | 1 | 1 |
| WWN y serie coinciden | Sí | Sí |
| Sesión de kernel estable | DISKSEQ 1 | DISKSEQ 1 |
| Comparador acepta la asociación | No | Sí |
| SmartSupported / SmartEnabled | True / True | True / True |
| SmartFailing | False | False |
| SmartGetAttributes | 26 filas válidas en lectura directa | 26 filas en el recolector |
| Atributo 199 CRC | 131109 | 131109 |
| SmartTemperature | 299 K | 299 K = 25,85 °C |
| Edad de SmartUpdated al leer | ~372 s | ~54 s |
| Fuente térmica del recolector | Rechazada por identidad | `udisks:sda` |
| Fuente de salud del recolector | Rechazada por identidad | `udisks2-ata`, `passed` |
| Errores del ciclo corregido | — | Ninguno |
| Llamadas al fallback en el ciclo corregido | — | 0 |

La primera lectura tenía SmartPowerOnSeconds=8298000 (2305 h); la segunda,
8301600 (2306 h). SmartUpdated avanzó 600 segundos por la actualización normal
del servicio. La variación respecto de los 300 K reportados inicialmente es una
lectura posterior del sensor; el código conserva la conversión Kelvin a Celsius.

SmartGetAttributes no fallaba: el parser aceptó sus 26 filas y el contador CRC.
El atributo 231 se identificó como `temperature-celsius`, con valor interpretado
368150 y unidad millikelvin; no se utilizó como temperatura, salud o desgaste.
La temperatura procede de SmartTemperature.

La caducidad tampoco causaba el rechazo inicial: ~372 s está dentro de los 752 s
permitidos. Se mantiene ese umbral, sin reiniciar la edad al consultar la caché ni
solicitar actualizaciones de SMART.

## Corrección

- El matcher admite la diferencia modelo corto/largo exclusivamente cuando el
  modelo de sysfs tiene exactamente 16 caracteres ASCII, es prefijo del modelo
  completo y el dispositivo es un disco `sd` completo. Se siguen exigiendo WWN o
  serie corroborados; las contradicciones de cualquiera de ellos se rechazan.
- La excepción no aplica a NVMe, particiones ni prefijos arbitrarios. Las
  comprobaciones de DISKSEQ antes y después no cambian.
- Cuando UDisks2 funciona y la temperatura tampoco depende de smartctl, se
  retiran los diagnósticos publicados de intentos anteriores de esa alternativa.
  Su caché/backoff se conserva y solo restaura los diagnósticos si vuelve a ser
  necesaria, sin ejecutar smartctl sin acceso ni rejuvenecer datos.
- La temperatura UDisks2 válida conserva estado y edad independientes incluso
  si una consulta de atributos falla. Las lecturas vencidas siguen como N/D en
  la interfaz y como lectura anterior en los detalles SMART.

## Pruebas

La regresión reproduce ambos modelos y las propiedades aportadas por el usuario,
con 26 filas de forma ATA. Serie, WWN, timestamps y las 24 filas no aportadas son
sintéticos. Los dos atributos relevantes reproducen CRC=131109 y el ambiguo 231.

Antes de corregir, la nueva suite reprodujo seis fallos y dos errores por la
asociación rechazada. Después pasan:

- Asociación SATA y lectura de 26 atributos sin acceso directo al dispositivo.
- Temperatura 300 K → 26,85 °C y 8298000 s → 2305 h.
- Rechazo de WWN/serie contradictorios, prefijos arbitrarios y excepción en NVMe.
- Invalidación ante cambio de DISKSEQ durante la adquisición.
- Conservación térmica ante fallo de atributos y caducidad sin reanclar la edad.
- Retirada y restauración de errores de fallback, conservando backoff.
- Tarjeta GTK con temperatura, salud y contadores, sin avisos falsos; N/D al caducar.

Comandos:

```bash
/usr/bin/python3 -B -m unittest tests.test_sata_smart_regression tests.test_udisks_smart tests.test_udisks tests.test_storage tests.test_storage_identity tests.test_smart -q
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' \
  env GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=none \
  /usr/bin/python3 -B -m unittest discover -s tests -t . -q
git diff --check
```

Resultado: 171 pruebas dirigidas aprobadas y suite completa de 572 pruebas
aprobadas en 21,192 s, sin fallos ni omisiones. Se añadieron diez regresiones SATA
y una GTK. `git diff --check` limpio. Los archivos de gráficos y el screenshot
previo conservan sus hashes. No se modificó main ni se realizó commit o push.
