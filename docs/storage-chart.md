# Almacenamiento: fases 2B, 2B.1 y 2B.2

Implementación visual pendiente de revisión. No cambia adquisición, intervalos,
identidades, ordenamiento, temperaturas ni SMART de la fase 2A.

## Componentes y datos

- `src/gui/storage_chart_model.py`: preparación inmutable de capacidad, ángulos,
  etiquetas, porcentajes y claves de color. No importa GTK ni Cairo y no hace I/O.
- `src/gui/storage_usage_model.py`: `UsageModel` y `prepare_usage`, selección y
  validación de una lectura de filesystem publicada. Sin I/O ni agregación de uso.
- `src/gui/storage_usage_style.py`: nivel y etiqueta del interior, reutilizando
  `src/utils/formatting.py:usage_level` sin cambiar la selección ni los datos.
- `src/gui/storage_chart.py`: `PhysicalDonut`, un `Gtk.FlowBox` que conserva un único
  `Gtk.DrawingArea`, un centro de texto GTK y filas de leyenda reconciliadas por
  identidad. Refluye de dos columnas a una según el ancho disponible.
- `src/gui/storage.py`: integra esa vista en `DiskCard`, conservando el mecanismo
  de reconciliación de `StoragePanel`, detalles, montajes, barras de filesystem,
  temperatura, antigüedad y expansión. El modelo sigue siendo el título, sin alias.
- `src/gui/app.py`: permite aplicar el acento configurable a la primera categoría
  de color sin cambiar la paleta del resto de la aplicación.

La única fuente de geometría física del anillo exterior es `DiskMetrics.layout`. Los segmentos usan
`start_bytes / capacity_bytes` y `end_bytes / capacity_bytes`; no se suman volúmenes
lógicos ni se normaliza un resultado incompleto. Cada región queda en su orden
físico. Las extendidas no se añaden a sus particiones lógicas. Las reservas de
extendidas tienen su propio nombre; el dispositivo completo y el disco vacío
mantienen sus estados diferentes.

La capacidad central procede del layout. La cabecera conserva además la capacidad
de `DiskMetrics`. Los valores de uso del filesystem siguen en sus barras existentes
y no determinan ningún sector exterior. «Sin particionar» significa fuera de extensiones
de particiones, puede incluir metadatos y no promete espacio utilizable.

Se comprueban `layout.available` y `layout.current(now)` en cada actualización,
incluso si no llega una instantánea nueva. Una distribución ausente, inválida,
caducada o con error muestra un anillo neutro, «Distribución no disponible» y su
motivo. Nunca conserva sectores físicos antiguos presentados como actuales.

## Anillo interior: una lectura de filesystem

El anillo interior nunca es un porcentaje global del disco, incluso cuando hay
un filesystem directo. La etiqueta visible «Interior · FS nombre · tipo · montaje»
declara su alcance y el centro sigue mostrando únicamente la capacidad física.
Usa `PartitionMetrics.used_bytes`, `free_bytes`, `usage_percent`, `mountpoints` y
el `SourceState` de `usage:nombre` ya publicados por el monitor.

La semántica coincide con `_filesystem_usage()` y las barras existentes: usado
dividido por usado más disponible (`f_bavail`), no por el tamaño de la partición,
del disco ni por el total bruto del filesystem. Se comprueban valores finitos,
no negativos, denominador positivo y coherencia del porcentaje publicado (se
tolera su redondeo a una décima). Los dos colores representan usado y disponible.
No se convierten datos ausentes en 0%; una medición válida de 0% sí es mostrable.

La política de selección prioriza el filesystem cuyo conjunto de montajes
publicado contiene exactamente `/`. Se muestra «Interior · FS /», seguido de su
nombre técnico, tipo y montajes compartidos cuando corresponda. El porcentaje
usado tiene una tipografía mayor. No se busca por modelo, alias, etiqueta ni
prefijos como `/home` o `/root`.

La raíz seleccionada pasa las mismas comprobaciones de coherencia, adquisición
y caducidad: si falla, el anillo queda neutro con diagnóstico de raíz. Nunca se
sustituye silenciosamente por un `/boot` medible. Si el respaldo raíz ya está
verificado en `DiskMetrics.order.root` pero no hay una lectura de `/` identificable,
también queda neutro; no se infiere qué volumen es el raíz. Dos nombres distintos
que declaran `/` se consideran contradictorios y no se elige ninguno.

Sin `/`, se selecciona una lectura reciente, coherente y montada si es la única
medible. Otros volúmenes sin montar o sin medición no se añaden a ella. Si hay varias
lecturas recientes de nombres técnicos distintos sin raíz, el anillo queda neutro con
«Varios filesystems · ver detalles» y «Sin porcentaje agregado». Los porcentajes
individuales continúan en las barras del desplegable. No hay suma ni selector manual.
Un volumen sin montar muestra «Interior · Uso no disponible · sin montar».

Un nodo técnico repetido se cuenta una vez solo si tipo y cifras coinciden;
se unen sus montajes sin modificar el snapshot. Esto incluye bind mounts y los
subvolúmenes Btrfs publicados bajo un mismo nodo. Cifras iguales en nombres
distintos no prueban identidad compartida; no se fusionan. Las contradicciones
para un mismo nombre producen un estado ambiguo sin sectores interiores.

Btrfs conserva la lectura compartida publicada, no una suma de subvolúmenes ni
un uso individual por miembro físico. Los datos existentes no exponen UUID de
filesystem, qgroups ni la ruta exacta cuyo statvfs resultó legible entre varios
montajes: la interfaz declara el nodo y el conjunto de montajes publicados,
sin inventar esa precisión. Una lectura Btrfs mayor que el disco puede mostrarse
como lectura de ese filesystem; nunca se convierte en ocupación física del miembro.
NTFS sin montar permanece no disponible, aun si llegaran valores almacenados.

Las lecturas obsoletas y los errores no dibujan sectores interiores. Conservan
etiquetas distintas, edad y diagnóstico; los valores anteriores solo figuran
como lectura anterior en el tooltip. Los errores del snapshot tienen prioridad
igual que los de `SourceState`. Los estados de ambos anillos son independientes:
geometría vigente no hace reciente un uso antiguo, y una lectura de filesystem
verificable no valida una geometría ausente.

Referencias primarias: [statvfs y disponible para usuarios sin privilegios](https://man7.org/linux/man-pages/man3/statvfs.3.html),
[Btrfs: espacio reportado por statfs/df](https://btrfs.readthedocs.io/en/latest/btrfs-filesystem.html)
y [contabilidad compartida y qgroups](https://btrfs.readthedocs.io/en/latest/Qgroups.html).

## Composición compacta de 2B.1

Se conserva la misma tarjeta, reconciliación y `DrawingArea`. Ambos anillos se
dibujan en ese único lienzo, ahora de 164 unidades lógicas en lugar de 200.
La leyenda principal resume hasta tres regiones en orden físico y avisa de las
restantes. La leyenda completa, explicación de huecos y cifras usadas/disponibles
quedan en «Particiones y detalles», junto a las barras anteriores. Todas las
regiones y ambos alcances también tienen texto equivalente accesible en el gráfico.
Temperatura, antigüedad, estado y avisos de adquisición siguen fuera del desplegable.

Las muestras de color permanecen en la vista principal: `Gtk.Expander` desparenta
su contenido al contraerlo y no debe hacer que la paleta cambie. Al estrechar la
ventana, gráfico y resumen pasan a una columna sin reconstrucción de widgets.

## Colores dinámicos de 2B.2

Valores predeterminados exclusivos del anillo interior:

| Uso válido | Nivel textual | Color usado |
|---|---|---|
| 0% a menos de 70% | Normal | `#76B900` |
| 70% a menos de 85% | Atención | `#E5A93D` |
| 85% a 100% | Crítico | `#E65B57` |
| Ausente, obsoleto, con error o no verificable | Desconocido | Gris neutro `#8A969E` |

El estado aparece en el resumen y el texto accesible. Un 0% medido es Normal;
una ausencia de medición no se convierte en cero. La parte disponible conserva
su gris, con la misma proporción; una lectura desconocida muestra un anillo
interior gris sin sectores de uso. La antigüedad no cambia el nivel mientras la
lectura sea vigente. El exterior mantiene sus colores categóricos por partición.

Los ajustes generales existentes usan 60/85 y otra paleta. Se mantienen para las
barras y el exterior. `storage_usage` introduce un ámbito para el interior con
la misma estructura `thresholds` / `color_scheme`, la misma validación y el mismo
clasificador. La precedencia por valores válidos es: valores predeterminados del
interior, preferencias generales explícitas en el JSON, preferencias específicas
de `storage_usage`. Los colores se heredan por clave; los umbrales requieren un
par completo con medio menor que alto. Un ajuste inválido mantiene el valor
heredado o predeterminado. Incluso valores iguales a los antiguos predeterminados
se respetan si están escritos explícitamente: no se intenta adivinar su intención.

`config.json.example` muestra el perfil específico completo. Para cambiar solo
el interior sin afectar los otros paneles, usar por ejemplo:

```json
"storage_usage": {
  "thresholds": {"medium_percent": 70, "high_percent": 85},
  "color_scheme": {
    "low_usage_color": "#76B900",
    "medium_usage_color": "#E5A93D",
    "high_usage_color": "#E65B57"
  }
}
```

La configuración existente no se escribe ni migra. Se carga al iniciar DEV,
igual que los demás ajustes; reiniciar DEV tras editar el JSON. Los cambios de
tema/CSS actualizan la paleta preparada mediante el mecanismo de estilo existente.
La clasificación y la resolución de colores se ejecutan fuera de Cairo. Dentro
de un nivel, un cambio de porcentaje actualiza ángulos; al cruzar un umbral cambia
también el color. Sin cambios visuales no se solicita dibujo; no hay timers nuevos.

Los colores de particiones se conservan por PARTUUID; si falta, por número de
partición y finalmente nombre técnico, dentro de la tarjeta identificada por la
arquitectura existente. Hay ocho colores categóricos, más colores específicos
para huecos y reservas. El registro está acotado a 256 identidades por tarjeta;
después usa una selección determinista. Más de ocho identidades pueden repetir
color: la identificación textual siempre acompaña a cada región.

## Dibujo y actualización

Las coordenadas angulares y textos se preparan fuera de Cairo. Los cambios CSS
preparan también los colores fuera del callback, mediante un idle agrupado. El
callback utiliza ángulos y colores preparados y las dimensiones lógicas asignadas;
GTK gestiona HiDPI y cambios de tamaño. Los textos centrales son etiquetas GTK,
sin texto diminuto en sectores.

Solo se solicita `queue_draw()` si cambia el modelo visible o el estilo relevante.
Solo los cambios de porcentaje o disponibilidad del uso invalidan el anillo
interior; temperatura y edad por sí solas no solicitan dibujo. Se evita preparar
de nuevo la geometría exterior cuando solo cambia el uso.
La misma geometría con una fecha de adquisición renovada tampoco la invalida.
Mientras la página está oculta se conserva el modelo más reciente y se difiere
la solicitud de dibujo hasta `map`. No hay un temporizador gráfico nuevo.
Los callbacks persistentes usan referencias débiles para liberar la vista al
retirar una tarjeta por hotplug; existe una prueba de regresión de esa liberación.

El dibujo se realiza en un grupo Cairo: un fallo descarta todos sus sectores.
Un idle muestra el diagnóstico sin alterar widgets dentro del callback. Se dibuja
un anillo neutro hasta el siguiente intento en una actualización normal; no hay
un bucle de reintentos de dibujo. La recuperación conserva el `DrawingArea`.

La leyenda muestra identidad, capacidad y porcentaje físico, con ajuste de línea
y en orden de offsets. Los porcentajes se redondean a dos decimales; los extremos
positivos inferiores a 0,01% o superiores a 99,99% se expresan como desigualdades.
No se aumenta el ángulo de regiones subpíxel para hacerlas visibles.

Referencias GTK: [DrawingArea y Cairo](https://docs.gtk.org/gtk4/class.DrawingArea.html),
[restricciones del callback](https://docs.gtk.org/gtk4/callback.DrawingAreaDrawFunc.html),
[reflujo FlowBox](https://docs.gtk.org/gtk4/class.FlowBox.html).
Se utiliza la API compatible con el mínimo GTK 4.6 del proyecto.

## Pruebas y vista simulada

Desde la raíz DEV, con los paquetes de pruebas ya disponibles:

```bash
dbus-run-session -- xvfb-run -a python3 -B -m unittest discover -s tests -t . -v
python3 -B -m tests.preview_storage_chart --case usage
python3 -B -m tests.preview_storage_chart --case single --width 380
python3 -B -m tests.preview_storage_chart --case usage --theme system
python3 -B -m tests.preview_storage_chart --case root --width 380
python3 -B -m tests.preview_storage_chart --case fedora
python3 -B -m tests.preview_storage_chart --case colors
python3 -B -m tests.preview_storage_chart --case colors --theme system
python3 -B -m tests.preview_storage_chart --case attention --width 380
```

La vista simulada no construye monitores ni consulta hardware. Sus escenarios
`partitions`, `empty`, `direct`, `tiny`, `stale` e `invalid` usan geometría creada
por el validador real con identidades sintéticas. Se indica siempre DATOS SIMULADOS.
El grupo `usage` añade `single`, `multiple`, `btrfs`, `ntfs`, `usage_stale`,
`usage_error`, `usage_missing` y `ambiguous`. `all` conserva todos los escenarios.
`root` combina raíz Btrfs compartida con otro filesystem medible. `fedora` usa
datos exclusivamente sintéticos para Dahua con `/`, Samsung y Kingston sin montar;
esos modelos explícitos en el simulador no participan en la selección real.
`colors` muestra cuatro escenarios sintéticos: Normal, Atención, Crítico y
Desconocido. Los valores de ejemplo del hardware no están codificados en el
programa. La simulación usa el perfil específico predeterminado de 2B.2.

Las pruebas incluyen proporciones, obsolescencia y error, espacio sin particionar,
disco vacío, filesystem directo, capas lógicas, extendidas, regiones pequeñas,
paleta estable, conservación de widgets, cambios de geometría, ausencia de
redibujos, página oculta, hotplug, expansión, temas, colores configurables,
navegación, ventanas estrechas y recuperación después de un error Cairo. La fase
2B.1 añade alcance y denominador, Btrfs compartido, múltiples filesystems,
NTFS sin montar, mediciones ausentes, obsolescencia, errores, cambios de montaje,
neutralidad independiente de los anillos, leyenda compacta y paleta al expandir.
La fase 2B.2 cubre los límites 0/70/85/100, ausencia de lectura, obsolescencia,
errores, porcentaje dentro de un nivel y cruces, etiquetas accesibles, herencia
y precedencia de configuración, temas y colores personalizados, independencia
del exterior, persistencia, hotplug y recuperación de Cairo.

## Validación visual con hardware real

1. Desde este worktree, verificar `git branch --show-current` y arrancar únicamente
   la instancia DEV: `python3 -B bin/system_monitor.py --channel dev`.
2. Abrir ALMACENAMIENTO. Contrastar modelo, transporte y capacidad con
   `lsblk -b -o NAME,TYPE,SIZE,START,LOG-SEC,PKNAME,PTTYPE,FSTYPE,MOUNTPOINTS,TRAN`.
   START se convierte con 512 bytes, independientemente de LOG-SEC.
3. Comprobar Dahua, Samsung, Kingston y Toshiba USB cuando estén presentes. La
   fase 2A mantiene el orden y la identidad; el alias configurado no debe sustituir
   al modelo. Contrastar particiones con la leyenda, sin sumar LUKS/LVM/subvolúmenes.
4. Desplegar detalles, cambiar de pestaña, esperar varios refrescos y reducir la
   ventana a unos 380 píxeles. Deben conservarse expansión, colores y barras;
   la leyenda se coloca bajo la dona sin desplazamiento horizontal.
5. Revisar los temas oscuro y del sistema y el escalado normal/HiDPI. Las etiquetas
   deben ser legibles y las regiones pequeñas seguir identificables en la leyenda.
6. Conectar/desconectar una unidad USB prescindible que no esté en uso y esperar
   el refresco normal de topología. Las otras tarjetas deben conservar su estado.
   Nunca desconectar un respaldo raíz.
7. Si la adquisición caduca o informa un error, confirmar el anillo neutro y el
   diagnóstico, manteniendo los avisos térmicos y de filesystem independientes.
   Usar los casos simulados `stale` e `invalid` para revisar esos estados sin
   provocar fallos en hardware real.
8. Para el interior, contrastar el nodo y los montajes declarados con
   `findmnt -o TARGET,SOURCE,FSTYPE` y `df -B1 --output=source,used,avail,pcent,target`.
   Comparar el mismo alcance, permitiendo la diferencia temporal entre lecturas;
   el porcentaje gráfico debe coincidir con la barra existente, no con usado/SIZE.
9. En Fedora con raíz Btrfs, `/` y `/home` del mismo nodo no suman dos usos. Aunque
   estén medibles `/boot` y EFI, Dahua debe mostrar «Interior · FS /» con el
   porcentaje de esa lectura compartida, coincidente con su barra en detalles.
   Samsung y Kingston sin montar deben indicar uso no disponible y no mostrar
   sectores interiores. Estos resultados dependen de los montajes publicados,
   nunca de los modelos de disco. Otros discos con varios filesystems medibles
   sin `/` conservan el interior neutro.
10. Una unidad NTFS sin montar debe quedar sin porcentaje interior; no montarla
    solo para esta comprobación si no estaba previsto. Revisar `usage_stale`,
    `usage_error` y `ambiguous` en la vista simulada para no provocar fallos reales.
11. Con el perfil predeterminado, lecturas reales de 84,8% y 78,7% deben ser
    Atención/ámbar; Samsung sin montar debe ser Desconocido/gris. Contrastar el
    valor actual del filesystem seleccionado con su barra, sin esperar esos
    porcentajes concretos en todas las ejecuciones. Si hay preferencias explícitas,
    se aplican sus umbrales y colores. No llenar discos para probar el límite:
    revisar Normal, Atención y Crítico con `--case colors`, también a 380 píxeles.

La validación automatizada y las capturas simuladas no sustituyen esa revisión
visual del equipo. No se han añadido gráficos históricos, atributos SMART ni
consultas de hardware. No hay cambios de dependencias, commit ni push en esta fase.
