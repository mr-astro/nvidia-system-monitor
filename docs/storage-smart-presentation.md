# Fase 3.2 — Presentación SMART

## Cierre de fase 3.1

Se verificaron la rama `fix/monitor-reliability`, el HEAD inicial
`b33a984eae25e3d8eabe1c681a7506afd76bb4b6`, los cambios y archivos nuevos SMART.
La suite completa bajo Xvfb/D-Bus aprobó 572 pruebas en 20,624 s, sin fallos ni
omisiones; `git diff --check` y la comprobación del índice terminaron limpios.

El commit exclusivo de fase 3.1 es
`5c14a90afcb6e30d1ed9054fbcf86edf1def89cb`:
`feat(storage): complete phase 3.1 SMART acquisition and SATA reliability`.
Contiene 18 archivos. Se excluyó explícitamente `screenshot.png`, que conserva su
cambio previo fuera del índice. No se hizo push. `main` permaneció en
`cf0b599b72108de508b081b7856302bf4aad351a`, sin tocar su worktree.

## Sección SMART

Cada tarjeta conserva su gráfico, temperatura, particiones, alias y expansión.
La presentación SMART se compone de:

- Encabezado SMART con el estado de la lectura.
- Métricas separadas: salud, desgaste y horas.
- Fuente, edad y tiempo restante hasta caducidad.
- Error de adquisición, cuando existe, separado de las advertencias del disco.
- Advertencias visibles en su propio bloque. Las anteriores se identifican como
  tales cuando la lectura ha caducado o una actualización ha fallado.

`SmartSection` reutiliza sus widgets en cada refresco. Un `Gtk.FlowBox` adapta
las tres métricas al ancho disponible; los textos permiten saltos de línea y
partición de palabras, sin elipsis ni tamaños fijos. La implementación usa APIs
disponibles en GTK4 4.6. Referencia: [Gtk.FlowBox](https://docs.gtk.org/gtk4/class.FlowBox.html).

El modelo de presentación es puro: no importa GTK, no usa el reloj civil y no
consulta dispositivos, D-Bus, permisos ni comandos. Consume una única muestra
inmutable y el tiempo monotónico proporcionado por el renderizador existente.
No cambia adquisición, identidad, backoff, tiempos ni cachés de fase 3.1.

## Significado de los valores

| Lectura | Métricas visibles | Información adicional |
| --- | --- | --- |
| Vigente | Valores proporcionados; campos ausentes como N/D | Edad y segundos hasta caducar |
| Desconocida | N/D en los campos no conocidos | Distingue ausencia de muestra de campos desconocidos en una muestra reciente |
| Obsoleta | N/D | Edad y «caducada»; valores anteriores solo en los detalles |
| Error | N/D | Diagnóstico y edad original, si existe; nunca renueva la medición |

Salud `passed` se presenta como «Sin fallo declarado», no como predicción de
vida restante. Un fallo declarado se marca en la métrica de salud; ni el número
de advertencias ni un CRC acumulado convierten una salud `passed` en `failed`.

El desgaste solo acepta un entero 0–255 de los caminos de adquisición que
validan Percentage Used NVMe (`udisks2-nvme` o smartctl JSON). Se mantiene 0% y
también valores superiores a 100%. Fuentes ATA o no reconocidas muestran N/D,
aunque una muestra construida incorrectamente contenga un porcentaje.
La interpretación de atributos SATA ambiguos sigue excluida. El Kingston A400
presenta desgaste N/D y su CRC acumulado 131109 se explica como histórico,
sin declarar un fallo actual.

La fuente smartctl conserva el contrato validado de fase 3.1: su campo Percentage
Used solo procede del log NVMe, nunca de atributos ATA. No se calcula vida
restante ni se confunde resistencia consumida con espacio del filesystem.

## Archivos de fase 3.2

- `src/gui/storage_smart_model.py`: valores, estados, edad, detalles y advertencias.
- `src/gui/storage_smart.py`: sección GTK4 y estilos locales.
- `src/gui/storage.py`: inserción de la sección en cada tarjeta.
- `tests/test_storage_smart_model.py`: ocho pruebas de semántica y procedencia.
- `tests/test_gui_smart.py`: migración de las regresiones a métricas y advertencias
  independientes; conserva las pruebas de adquisición retenida y heartbeat GTK.
- `tests/test_gui_smart_rendering.py`: cuatro pruebas nuevas de renderizado,
  reflujo, textos largos y preservación de la tarjeta completa.
- `docs/storage-smart.md` y este documento: presentación y resultados.

## Validación

```bash
/usr/bin/python3 -B -m unittest tests.test_storage_smart_model -q
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' \
  env GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=none \
  /usr/bin/python3 -B -m unittest tests.test_gui_smart_rendering -v
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' \
  env GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=none \
  /usr/bin/python3 -B -m unittest discover -s tests -t . -q
git diff --check
```

El 23 de octubre de 2026, con Python 3.14.8 y GTK 4.22.5:

- Ocho pruebas del modelo aprobadas.
- 83 pruebas dirigidas de interfaz y almacenamiento aprobadas.
- Cuatro pruebas nuevas de renderizado aprobadas. Se generan nodos GSK y texturas
  reales para los estados válido, desconocido, obsoleto y error.
- Se verifica reflujo desde 720 hasta 240 px, texto largo sin elipsis ni altura
  recortada, y actualización sin reemplazar widgets, alias o expansión.
- El test integrado verifica que cambiar SMART no invalida ni modifica los
  segmentos del anillo exterior o los arcos del anillo interior.
- Suite completa: 584 pruebas aprobadas en 22,465 s, sin fallos ni omisiones.
- `git diff --check` limpio. Archivos de gráficos y `screenshot.png` conservan sus
  hashes; el índice queda vacío tras el commit de fase 3.1.

Se inspeccionó una vista previa GTK en tema oscuro con los tres discos y los
cuatro estados de lectura. La imagen temporal se guardó fuera del repositorio,
sin sobrescribir `screenshot.png`. La validación visual usa datos simulados;
no se reinició ni alteró la instancia del usuario.

## Cierre de fase 3.2

El usuario confirmó la validación visual en Fedora con tres discos reales.
Para el cierre se repitió la suite completa bajo Xvfb/D-Bus: 584 pruebas en
22,641 s, sin fallos ni omisiones. `git diff --check` terminó limpio.

Los ocho archivos de presentación, pruebas y documentación se incluyen en el
commit `feat(storage): add SMART health presentation`. `screenshot.png` y los
archivos ajenos quedan excluidos. `main` y el worktree estable conservan su estado;
no se realiza push ni se crea un release.
