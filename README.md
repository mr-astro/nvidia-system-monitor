# NVIDIA System Monitor for LINUX

![Captura de la aplicación](screenshot.png)

Monitor de escritorio GTK4 para Linux (desarrollado y probado en Fedora): GPU NVIDIA, CPU, RAM y almacenamiento, con sensores opcionales.

Este README describe el código de esta rama. El canal selecciona identidad y rutas;
no cambia de versión ni integra esta rama en `main`. **A1–A7 están resueltos en
código según las auditorías realizadas.** A5 quedó cerrado al comprobar que A1 y
A4 ya separan los recursos mutables de DEV y estable; no necesita cambios
funcionales adicionales. La comprobación de A5 ejecutó 94 pruebas relacionadas,
sin fallos ni omisiones, y reproducciones temporales de rotación de logs y de
trabajadores de alias simultáneos. La instalación real y la publicación estable
requieren una aprobación posterior; este cierre documental no las ejecuta.

## Qué monitoriza

**GPU NVIDIA** (vía `nvidia-smi`; soporta varias GPU): modelo, uso, VRAM, temperatura,
consumo y límite de potencia, frecuencias de núcleo y memoria, ventilador, versión del
driver y de CUDA. Si NVIDIA o el driver no están disponibles, el panel lo indica.

**CPU**: modelo, uso, frecuencia actual y máxima, núcleos/hilos, carga 1/5/15 min y
temperatura cuando existe `k10temp`, `zenpower` (AMD) o `coretemp` (Intel).

**RAM**: total, usada, disponible, libre, caché y buffers, swap.

**Almacenamiento**: cada disco físico con modelo, tipo (NVMe, SSD SATA, HDD, USB, eMMC/SD),
capacidad y temperatura; debajo, sus particiones y volúmenes anidados (LUKS, LVM) con
sistema de archivos, punto(s) de montaje y uso. Un sistema de archivos montado
directamente sobre el disco completo muestra su utilización en la cabecera, sin
crear una partición ficticia; su capacidad lógica permanece separada de la física.
Si un dispositivo tiene varios montajes (p. ej. subvolúmenes btrfs en `/` y `/home`),
el uso mostrado es el del sistema de
archivos completo.

**Sensores** (opcional, desactivado por defecto): lecturas de temperatura de `lm_sensors`,
útiles para ver la placa base. Se activa con `"sensors": true` en `panels`.

Los datos ausentes se muestran como `N/D`; nunca se sustituyen por valores inventados.

## Instalación

Requisitos: Linux, Python **3.10 o posterior**, PyGObject y GTK **4.6 o posterior**.
Se utiliza [`Gtk.FlowBox.append`, disponible desde GTK 4.6](https://docs.gtk.org/gtk4/method.FlowBox.append.html).
El namespace de introspección sigue siendo `Gtk` versión `4.0`; esto no significa
que cualquier versión menor de GTK 4 sea suficiente. La carga de CSS conserva una
alternativa para GTK anterior a 4.12. `lsblk` (util-linux) es necesario para detectar
la topología del almacenamiento. La aplicación requiere una sesión gráfica para
mostrar la interfaz.

### Fedora

```bash
sudo dnf install python3 gtk4 python3-gobject util-linux
```

### Otras distribuciones

- **Ubuntu / Debian** (compruebe que la versión de la distribución proporciona GTK ≥ 4.6):

```bash
sudo apt install python3 python3-gi gir1.2-gtk-4.0 util-linux
```

- **Arch Linux / Manjaro** (sin probar):

```bash
sudo pacman -S python gtk4 python-gobject util-linux
```

No se necesitan paquetes adicionales mediante `pip` ni libadwaita; PyGObject y
las bibliotecas de introspección se instalan con los paquetes del sistema.

### Opcionales

```bash
sudo dnf install lm_sensors        # panel de sensores
sudo dnf install udisks2           # temperaturas SMART almacenadas, si están disponibles
sudo dnf install smartmontools     # alternativa smartctl; necesita permisos sobre el dispositivo
```

`nvidia-smi`, proporcionado por el driver NVIDIA, es opcional para el resto de
paneles. Gio viene con PyGObject y permite consultar UDisks2 por D-Bus; no se
requiere un binding D-Bus adicional. Instalar UDisks2 no garantiza temperaturas:
el servicio debe estar disponible y exponer SMART habilitado y una medición válida
para ese disco. La aplicación no inicia el servicio ni pide autenticación.

**Otra fuente SATA sin ejecutar la aplicación como root:** el módulo `drivetemp`
del kernel, cuando el hardware lo admite. Su carga requiere administración:

```bash
sudo modprobe drivetemp                                         # hasta el próximo reinicio
echo drivetemp | sudo tee /etc/modules-load.d/drivetemp.conf    # permanente
```

## Ejecución

Desde la raíz del repositorio:

```bash
python3 bin/system_monitor.py --channel dev
python3 bin/system_monitor.py --channel stable
python3 bin/system_monitor.py                 # stable, por compatibilidad histórica
```

También se admite `--channel=dev`. El canal se valida antes de importar GTK;
un valor inválido o repetido produce un error. Los demás argumentos se entregan
a GTK sin cambios; `--` termina el procesamiento de opciones propias. No se
deduce el canal de la rama Git, la ubicación del repositorio ni el usuario.

Los canales usan IDs GTK diferentes y pueden ejecutarse simultáneamente. Una
segunda instancia del mismo canal activa la existente. El canal `stable` conserva
la identidad de las instalaciones anteriores, por lo que puede activar una
instancia estable ya abierta: use `--channel dev` para trabajar en esta rama.

El instalador fuente añade al menú un launcher y un icono independientes por canal,
y copia la configuración de ejemplo solo si no existe. No ejecuta `sudo` ni escribe
recursos globales. Los comandos administrativos anteriores solo corresponden a
paquetes del sistema y al módulo opcional `drivetemp`; no forman parte del
instalador de la aplicación. Los comandos siguientes se muestran desde la raíz
del repositorio y se ejecutan como usuario normal:

```bash
./install.sh --channel dev
./install.sh --channel dev --check-launcher     # comprobar sin escribir
./install.sh --channel dev --uninstall
./install.sh --channel stable
./install.sh --channel stable --check-launcher
./install.sh --channel stable --uninstall
./install.sh                                   # instala stable por defecto
./install.sh --uninstall                       # stable por defecto
```

Estos comandos modifican la instalación si se ejecutan. El launcher DEV es
`cl.mastro.NvidiaSystemMonitor.Dev.desktop`; el estable conserva
`cl.mastro.NvidiaSystemMonitor.desktop`. Las plantillas nuevas indican su canal explícitamente.
DEV no reemplaza el launcher, configuración ni icono de estable. Ambos canales
conservan el mismo diseño gráfico, con archivos instalados distintos:

| Recurso | `stable` | `dev` |
|---|---|---|
| Launcher en `applications/` | `cl.mastro.NvidiaSystemMonitor.desktop` | `cl.mastro.NvidiaSystemMonitor.Dev.desktop` |
| PNG en `icons/hicolor/scalable/apps/` | `cl.mastro.NvidiaSystemMonitor.png` | `cl.mastro.NvidiaSystemMonitor.Dev.png` |

Estas rutas se encuentran bajo `XDG_DATA_HOME`, o `~/.local/share` si la variable
está vacía o no es absoluta. El PNG existente del repositorio se copia sin alterar
su diseño. GTK encuentra ambos iconos mediante el tema `hicolor`, sin necesitar
una actualización global de cachés. El icono histórico de `/usr/share/icons` no se
sobrescribe ni se elimina; el recurso estable del usuario tiene prioridad.

También puede invocar `"/ruta/con espacios/al/repositorio/install.sh" --channel dev`
desde otro directorio. El script ancla la importación del helper a su propio
repositorio, incluso si el directorio actual contiene otro paquete `src`. Se admiten
espacios en la ruta; se rechazan comillas dobles, acentos graves, `$`, `\`, `%` y
saltos de línea por las reglas de `Exec` del launcher.

El launcher ejecuta el código de ese checkout mediante una ruta absoluta; no se
copia el programa a otra ubicación. Mantenga checkouts separados para DEV y estable.
Tras mover un checkout, repita la instalación del canal correspondiente para
actualizar su ruta. Una actualización conserva configuración, alias y registros.

**Migración de launchers DEV anteriores:** los instalados antes de esta separación
pueden carecer de `--channel dev`. Con el nuevo valor predeterminado iniciarían
la identidad estable, aunque el menú diga DEV. Deben regenerarse; modificar el
código o la plantilla no actualiza el launcher ya instalado.

Primero ejecute `./install.sh --channel dev --check-launcher`. Es una comprobación
de solo lectura: devuelve 0 si está compatible o ausente, 3 si detecta un launcher
DEV antiguo y 2 si rechaza una ruta, enlace o contenido inseguro. La regeneración
se solicita después mediante `./install.sh --channel dev`; reemplaza únicamente
el launcher e icono DEV y conserva la configuración existente. El cambio de `Exec` es:

```diff
-Exec=python3 "RUTA_DEL_REPOSITORIO/bin/system_monitor.py"
+Exec=python3 "RUTA_DEL_REPOSITORIO/bin/system_monitor.py" --channel dev
```

La ruta real del repositorio se conserva al regenerar desde ese mismo repositorio.
El launcher estable no necesita migración para mantener su funcionamiento histórico.
La comprobación no instala ni regenera archivos automáticamente.

Para actualizar instalaciones anteriores, vuelva a ejecutar el instalador con el
canal explícito desde el checkout que quiera utilizar. Esto publica el icono por
usuario del canal sin alterar el icono global ni los recursos del otro canal.
No es necesario borrar preferencias ni migrar sus directorios.

La desinstalación elimina únicamente el launcher y el icono por usuario del canal.
No necesita las plantillas, el PNG fuente ni el ejemplo de configuración, aunque
el script y sus módulos Python deben seguir disponibles. Conserva configuración,
alias, registros, el checkout y el icono global histórico. Pueden quedar directorios
vacíos y metadatos de caché del escritorio; no se borran directorios compartidos.

El helper `src/utils/installer.py` valida el canal de la plantilla y del launcher
existente, y obtiene el nombre de destino de la definición central de canales.
Antes de publicar valida todas las fuentes necesarias (plantilla, punto de entrada,
configuración de ejemplo e icono) y todos los destinos seleccionados.
Rechaza enlaces simbólicos (también colgantes), archivos con enlaces duros y
directorios padres enlazados, tanto al instalar como al desinstalar. La instalación
recorre los directorios con `O_NOFOLLOW`, conserva sus descriptores abiertos y
reemplaza el launcher y el icono por archivos temporales del mismo directorio mediante
renombrado atómico. La configuración inicial se publica sin sobrescribir un
archivo que aparezca concurrentemente. La desinstalación usa `unlink` relativo al
descriptor y nunca sigue el enlace final.

La actualización de `update-desktop-database`, si está disponible, espera como
máximo tres segundos; su ausencia, error o timeout no invalida la publicación.
Si una escritura falla, se eliminan sus temporales; pueden quedar archivos
completos ya publicados y directorios creados. Corrija la causa y repita el comando:
la configuración existente se conserva. No hay rollback conjunto de los recursos.

Esto evita escribir a través de enlaces y redirigir operaciones mediante una
sustitución de directorios padres. Si un directorio abierto se renombra durante
la operación, esta puede afectar al directorio original, ahora movido; puede ser
necesario repetir la instalación cuando la ruta vuelva a estar estable. No es una
barrera de seguridad frente a procesos maliciosos con los mismos permisos, ni una
transacción conjunta entre configuración, launcher, caché e icono. Los directorios
XDG que usan enlaces legítimos deben cambiarse por rutas físicas para instalar.

## Configuración

Las rutas son explícitas por canal:

| Recurso | `stable` (predeterminado) | `dev` |
|---|---|---|
| ID GTK | `cl.mastro.NvidiaSystemMonitor` | `cl.mastro.NvidiaSystemMonitor.Dev` |
| Título | NVIDIA System Monitor | NVIDIA System Monitor DEV |
| Configuración | `~/.config/nvidia-system-monitor/config.json` | `~/.config/nvidia-system-monitor-dev/config.json` |
| Estado | `~/.local/state/nvidia-system-monitor/` | `~/.local/state/nvidia-system-monitor-dev/` |
| Registro | `monitor.log` en el estado estable | `monitor.log` en el estado DEV |

Se respetan `XDG_CONFIG_HOME` y `XDG_STATE_HOME` si contienen rutas absolutas;
los valores vacíos o relativos usan las alternativas de la tabla. No se migran
ni copian preferencias o registros entre canales. Antes de esta separación, DEV
compartía el registro estable; ese archivo existente se conserva tal cual.

Hay un ejemplo completo en `config.json.example`. Todos los campos son opcionales; los
valores inválidos se ignoran y se usa el valor por defecto.

El código lee `storage-aliases.json` en el directorio de configuración del canal y
resuelve identidades mediante enlaces existentes de discos completos en
`/dev/disk/by-id`, dando preferencia a WWN. No existe una interfaz para editar
alias. Su configuración y resolución se conservan internamente, pero no se utilizan
como título: las tarjetas muestran el modelo detectado o el nombre técnico cuando
falta el modelo. No se deducen alias a partir de particiones, sistemas de archivos
ni puntos de montaje.
No se guardan identificadores reales de discos en el repositorio. Las
particiones se despliegan por disco y conservan su estado durante las actualizaciones.
Los diagnósticos completos están en tooltips; las fuentes obsoletas mantienen su aviso.

La interfaz de esta rama tiene dos pestañas, **RENDIMIENTO** y **ALMACENAMIENTO**,
con desplazamiento independiente. En Rendimiento, CPU y RAM comparten la columna
izquierda y GPU ocupa la derecha; los sensores opcionales aparecen junto a CPU y
RAM. `Gtk.FlowBox` reorganiza estas columnas al reducir el ancho, sin reconstruir
tarjetas ni perder los desplegables. Los porcentajes de utilización, temperaturas,
VRAM y consumo tienen mayor tamaño; las demás métricas siguen disponibles en
«Detalles técnicos». La ventana inicia en 880 × 700, con un mínimo de 380 × 320,
y se desplaza verticalmente cuando es necesario. El indicador DEV identifica el
canal sin repetir el título de la ventana.

En Almacenamiento, cada tarjeta conserva capacidad y temperatura a la izquierda,
y el resumen del dispositivo, montajes y uso a la derecha. El resumen permanece
visible al contraer «Particiones y detalles». No se incorporan gráficos de dona,
históricos ni nuevas consultas SMART en esta fase visual.

La fase 2A de esta rama ordena los discos por respaldo raíz verificado, NVMe,
SATA/ATA, USB y otros transportes. Añade un modelo físico complementario para futuras
donas, sin cambiar la composición visual ni sumar capas lógicas. La arquitectura,
los límites de verificación y el procedimiento de validación real se describen en
[Almacenamiento: fase 2A](docs/storage-topology.md).

La fase 2B presenta esa distribución mediante donas persistentes y leyendas
textuales adaptables, separadas del uso de filesystem. Los datos físicos obsoletos
o no verificables muestran un estado neutro. Véanse la arquitectura y la validación
visual en [Almacenamiento: fase 2B](docs/storage-chart.md).

El refinamiento 2B.1 añade un anillo interior para una lectura de filesystem
identificada, sin presentarla como utilización global del disco. Se prioriza el
filesystem montado en `/`, con su porcentaje destacado. En los demás discos se
elige el único filesystem montado y medible; varios medibles sin `/` dejan el
interior neutro y sus porcentajes siguen en los detalles. Una raíz obsoleta o con
error no se sustituye por otro montaje.
La fase 2B.2 añade niveles textuales y colores dinámicos solo al anillo interior:
Normal por debajo de 70%, Atención desde 70% y Crítico desde 85%, con estado
Desconocido si no hay una lectura vigente. La configuración específica
`storage_usage` permite ajustar esos niveles y colores conservando los ajustes
generales de barras y la paleta física.
La tarjeta compacta mantiene la leyenda completa y las cifras técnicas en el
desplegable, con los diagnósticos importantes visibles.

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

Las barras del desplegable muestran **el uso del sistema de archivos identificado**, no
la ocupación física del disco: porcentaje, espacio usado y espacio disponible
según `statvfs` (porcentaje = usado / (usado + disponible)). Si existen varios
sistemas de archivos independientes, el resumen técnico indica «Uso por particiones»
y sus barras aparecen al desplegar. Los montajes compartidos de btrfs se muestran
una sola vez, sin sumar subvolúmenes. Sin montaje o medición aparece «Uso no
disponible»; las barras pueden conservar el último uso con aviso y antigüedad
ante un fallo temporal, mientras que un cambio de identidad del montaje lo
invalida inmediatamente. El anillo interior aplica la prioridad de `/` descrita
arriba y queda neutro si la lectura seleccionada está obsoleta o tiene error.

Para temperatura SATA, el código de esta rama prueba sysfs/drivetemp, después las propiedades
almacenadas de UDisks2 en el bus D-Bus del sistema y, si es necesario, `smartctl`.
Gio forma parte del PyGObject existente; UDisks2 es opcional. No se inicia el
servicio, no se fuerza `SmartUpdate` ni se solicita autenticación. La antigüedad
proviene de `SmartUpdated`, no de la consulta D-Bus, y la misma medición no se
rejuvenece al volver a leerla. La fuente UDisks2 utiliza una cadencia de referencia
de 600 s para calcular su umbral de caducidad (752 s con el timeout predeterminado);
esto no configura ni garantiza la frecuencia de actualización del servicio.
Su antigüedad aparece junto a la temperatura. Se evalúan alternativas si falla
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
| `storage_usage.thresholds.medium_percent / high_percent` | `70` / `85` | Umbrales propios del anillo interior. |
| `storage_usage.color_scheme.low/medium/high_usage_color` | `#76B900` / `#E5A93D` / `#E65B57` | Colores Normal / Atención / Crítico del anillo interior. |

Para el interior se aplican sus valores predeterminados, luego los valores
generales válidos presentes explícitamente en el JSON y, finalmente, los valores
válidos de `storage_usage`. Así se respetan configuraciones anteriores sin
cambiar las barras generales ni el exterior. Un valor explícito igual al antiguo
predeterminado también se respeta. El ejemplo incluye `storage_usage` con el nuevo
perfil completo; las configuraciones existentes no se reescriben automáticamente.

Cada canal utiliza un logger y handlers propios, sin propagar mensajes al logger
raíz. Su `monitor.log` es rotativo: 512 KiB más dos archivos de respaldo. Si no
puede abrirse, el registro se desactiva sin impedir el inicio de la aplicación.
Las cachés de mediciones y alias, los diagnósticos y el estado de expansión son
propios de cada instancia y se mantienen en memoria. No hay una caché persistente
propia compartida de mediciones o alias. Las bibliotecas, servicios de consulta y
cachés regenerables de Python y del escritorio sí pueden compartirse; no contienen
las preferencias ni los registros de ambos canales en un único archivo de la aplicación.
El aislamiento por canal no es una barrera frente a procesos del mismo usuario:
redirigir manualmente rutas o logs mediante enlaces puede anularlo. El instalador
rechaza enlaces; la apertura de logs en ejecución utiliza las operaciones habituales
del sistema de archivos.

## Pruebas

```bash
python3 -B -m unittest discover -s tests -t . -v
```

Las pruebas usan datos simulados (salidas de `nvidia-smi`, `lsblk`, `sensors`, `smartctl`, `/proc`, `/sys`) y no dependen del hardware de la máquina. Requieren PyGObject/Gio
para las pruebas del transporte UDisks2. Las pruebas de interfaz se
omiten automáticamente si no hay GTK o pantalla; la prueba simultánea de canales
también requiere `dbus-run-session`. Para comprobar también el reflujo,
la pantalla virtual debe admitir la ventana de escritorio. Este comando fuerza X11
en sesiones Wayland y usa Cairo para evitar depender de aceleración en Xvfb:

```bash
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x1024x24' \
  env GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=none \
  python3 -B -m unittest discover -s tests -t . -v
```

En Fedora, las herramientas de pruebas se pueden instalar con:

```bash
sudo dnf install xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon
```

Se necesitan `xvfb-run`, Xvfb, `xauth` y `dbus-run-session` para ejecutar todas las
pruebas de interfaz y aislamiento. La prueba de canales crea su propio bus de
sesión y directorios XDG temporales. Verifique que el resultado no tenga omisiones
para considerar completa la validación. Esta rama no contiene un workflow de
GitHub Actions; la suite se ejecuta manualmente con los comandos anteriores.

Validación de preparación del 15 de octubre de 2026, sobre `c77de5e` con cambios
únicamente documentales: **335 pruebas aprobadas, cero fallos y cero omisiones**.
Se ejecutó la suite completa bajo Xvfb y D-Bus con HOME/XDG temporales, en Fedora
Linux 44, Python 3.14.8 y GTK 4.22.5. También pasaron `git diff --check`,
`bash -n install.sh` y la validación de ambos `.desktop` con rutas con espacios.
Esta ejecución no certifica por sí sola todas las versiones mínimas admitidas ni
sustituye la prueba de instalación real pendiente de autorización.

## Preparación de publicación estable

**Versión aprobada: 3.2.0; notas finalizadas el 15 de octubre de 2026 en DEV.**
La integración en `main`, la creación de la etiqueta y la release estable siguen
pendientes de autorización. El historial documenta
3.0.0 en el commit inicial `59ca107` y añade 3.1.0 en `2266242`. En la consulta de
preparación del 15 de octubre de 2026 no había etiquetas Git locales o remotas ni
[releases en GitHub](https://github.com/mr-astro/nvidia-system-monitor/releases).
Son antecedentes documentales, no publicaciones etiquetadas que deban reconstruirse
retroactivamente. El historial usa mensajes `fix:`, `feat:` y `docs:`, pero no
establece una convención de etiquetas o una automatización de publicación.

Se adopta 3.2.0 por las capacidades compatibles incorporadas desde 3.1.0:
telemetría UDisks2, uso de sistemas de archivos en discos completos, canales
independientes e instalador por usuario. Se conservan las rutas históricas estables
y la lectura de configuración anterior. La futura etiqueta anotada `v3.2.0` sería
una convención nueva; crearla y publicarla requiere autorización.
`Version=1.0` en las plantillas `.desktop`
identifica el formato Desktop Entry, no la versión de la aplicación.

El siguiente procedimiento es un plan; no autoriza su ejecución automática:

1. La versión 3.2.0 está aprobada y el changelog tiene su entrada fechada.
   Tras publicar la documentación autorizada en DEV, registrar el hash candidato
   y comprobar que el checkout está limpio antes de continuar con la migración.
2. Antes de instalar DEV realmente, cerrar ambos monitores y guardar una copia
   privada de los launchers e iconos existentes, anotando también cuáles no existen.
   Registrar las rutas XDG efectivas y los hashes de configuración y alias por canal.
   Comprobar destinos regulares y directorios físicos; detenerse ante enlaces o
   contenido ajeno. El respaldo y cualquier instalación requieren autorización.
3. Desde el checkout DEV aprobado, ejecutar
   `./install.sh --channel dev --check-launcher`. Un código 3 identifica el launcher
   antiguo y requiere regeneración; 2 exige revisar la causa. Con autorización,
   ejecutar `./install.sh --channel dev` y repetir la comprobación. Validar el
   `.desktop` generado con `desktop-file-validate` y su ruta absoluta, canal e icono.
   Verificar que la instalación no cambió archivos estables ni preferencias existentes.
4. Abrir DEV desde el menú y estable desde su instalación anterior. Comprobar títulos,
   iconos, procesos e identidades GTK independientes, rutas de logs y ausencia de
   mezcla entre registros. Verificar respuesta de la interfaz bajo carga y telemetría
   disponible, sin montar ni alterar discos. Los logs pueden crecer por la ejecución
   normal; la comparación de instalación del paso anterior se hace antes de abrirlos.
   Para revertir DEV, cerrarlo y restaurar sus recursos previos desde el respaldo,
   retirando únicamente los archivos nuevos que antes no existían. Si no había
   instalación previa, usar `./install.sh --channel dev --uninstall`.
   Conservar preferencias y logs. No utilizar
   para revertir el instalador histórico que copiaba el icono global con `sudo`.
5. Tras aprobar la prueba real, preparar la integración en un clon independiente;
   `main` está asociado al worktree estable protegido, por lo que no debe moverse
   en el repositorio compartido durante esta preparación. Verificar de nuevo las
   ramas remotas y la ascendencia; actualmente `main` es antecesor de DEV. En ese
   clon, con autorización, integrar el candidato mediante avance rápido
   (`git merge --ff-only HASH_CANDIDATO`). Detenerse ante divergencias.
6. Ejecutar la suite completa bajo Xvfb y D-Bus sobre el candidato integrado,
   `git diff --check`, `bash -n install.sh` y la validación de ambos `.desktop`.
   Exigir cero fallos y cero omisiones. Probar el canal estable del candidato con
   HOME/XDG temporales y un bus de sesión separado para no activar la instalación
   estable existente. Registrar Python, GTK, Fedora/GNOME, hardware y resultado.
7. Con autorización de publicación, volver a comprobar el remoto y publicar solo
   `main`, sin forzar. Verificar su hash. Después, si se autoriza el etiquetado,
   crear y publicar únicamente su etiqueta anotada sobre ese mismo commit y crear
   la release de GitHub. No mover etiquetas publicadas. Incluir el hash exacto,
   cambios A1–A7, requisitos, instrucciones por canal, migración, pruebas y límites
   conocidos. Si se adjunta un archivo de fuentes, publicar también su SHA-256.
   Distribuir fuentes con licencia MIT; no anunciar RPM, Flatpak ni CI inexistentes.
8. Instalar estable solo después de otra autorización, desde un checkout de la
   versión fijada al hash publicado, manteniendo disponible la instalación anterior.
   Ejecutar `./install.sh --channel stable`, comprobar el launcher y verificar la
   convivencia con DEV. Si falla, cerrar estable y restaurar sus launchers e iconos
   anteriores desde el respaldo sin borrar preferencias. Para revertir código ya
   publicado, preparar un nuevo commit de reversión y validarlo; no reescribir
   `main` ni desplazar la etiqueta. Cualquier actualización del worktree protegido
   requiere autorización expresa y queda fuera de este plan de preparación.

## Diseño técnico

```
bin/system_monitor.py   punto de entrada
src/data/               recolectores (cpu, ram, nvidia, storage, sensors) y collector.py
src/gui/app.py          interfaz GTK4
src/utils/              configuración, registro, rutas XDG y formato
tests/                  pruebas unitarias y de humo de la GUI
```

- La recolección de hardware (subprocesos y lecturas de `/proc` y `/sys`) corre en
un hilo en segundo plano. La resolución de alias tiene un trabajador independiente
por instancia, con una única solicitud pendiente reemplazable y resultados
inmutables. Su lectura de JSON y enlaces no bloquea GTK ni la publicación de
telemetría. El bucle de GTK lee instantáneas y actualiza los widgets existentes.
- Cada fuente falla de forma aislada: si una falla, las demás siguen funcionando y el
encabezado muestra un aviso (el detalle aparece al pasar el cursor).
- La topología de discos (`lsblk`) se consulta cada 15 s; los sensores, cada 5 s.
UDisks2 comparte una consulta entre discos cada 10 s y espera 300 s tras un fallo.
`smartctl -A` se usa como alternativa y almacena una lectura de temperatura durante
10 s por disco; sin lectura espera 300 s antes de reintentar. Una temperatura
extraída con diagnóstico de error conserva ese diagnóstico.
- La edad de una temperatura SMART directa se ancla a su adquisición; la de UDisks2,
a `SmartUpdated`. Volver a consultar una lectura almacenada no reinicia su edad.
El umbral SMART directo es 14,5 s con los parámetros predeterminados; el de UDisks2
es 752 s. Un error genera aviso inmediatamente; al superar el umbral, la tarjeta
deja de mostrar la temperatura y conserva el último valor y edad en el tooltip.
Los 300 s de reintento tras fallo no extienden la validez de la lectura anterior.
- No se ejecutan benchmarks de almacenamiento.

## Limitaciones

- La disponibilidad de temperaturas y consumo depende del hardware, el driver y los
sensores que Linux exponga.
- **No se muestra el consumo eléctrico de la CPU**: no existe una fuente fiable y
universal sin privilegios, y no se presenta una estimación como si fuera una medición.
- La temperatura NVMe se intenta leer de sysfs y, si no está disponible, de
`smartctl`. Para SATA se intenta drivetemp, luego UDisks2 y finalmente `smartctl`.
La disponibilidad depende del hardware, soporte del kernel, propiedades SMART
del servicio y permisos de lectura. La aplicación no eleva privilegios; si no
hay una medición disponible dentro de su umbral de edad, se muestra `N/D`.
- Solo se consulta `nvidia-smi`; no hay soporte para GPU AMD o Intel.
