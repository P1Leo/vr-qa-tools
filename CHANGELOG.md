# Changelog

## VR Log Inspector

### 1.9 · 2026-10-27

- **Visores por Wi-Fi sin comandos:** nuevo botón **Wi-Fi** en la barra superior. **Switch to Wi-Fi** pasa a Wi-Fi un visor conectado por cable con un clic: obtiene su IP, activa la depuración por Wi-Fi y se conecta.
- **Visores conocidos:** los visores que se pasan a Wi-Fi quedan guardados y se reconectan con **Connect**, sin cable, mientras no se reinicien. Si el router les ha dado otra IP, la herramienta los encuentra sola en la red del PC (los identifica por su número de serie) y guarda la nueva dirección.
- **Desenchufar sin cortar la captura:** si se quita el cable de un visor que ya está en modo Wi-Fi, la captura sigue por Wi-Fi sin perder líneas; y si se cae la Wi-Fi con el cable conectado, sigue por cable.
- El desplegable de visores indica si cada uno está conectado por USB o por Wi-Fi.
- Corregido: la captura podía quedarse parada mostrando solo `- waiting for device -`, porque las versiones recientes de adb no terminan al perder el visor. Ahora la herramienta lo detecta, reconecta y sigue desde la última línea.
- Al pulsar **Start** con el visor aún no listo, espera a que lo esté y explica por qué: falta aceptar el aviso de depuración, el visor está *offline* o no está conectado.
- Avisa cuando otro programa (PICO Developer Center, Meta Quest Developer Hub, SideQuest, Android Studio…) está reiniciando adb con otra versión y cortando la conexión.
- Actualizaciones: **Check for updates** vuelve a mostrar el aviso aunque se hubiera pulsado **Later**, y **Later** dura solo hasta que se cierra la herramienta. La página vuelve a comprobar si hay versión nueva cuando la herramienta se reinicia, y una copia sin `update.json` explica por qué no puede actualizarse.

### 1.8 · 2026-10-26

- **Actualizaciones automáticas:** al arrancar comprueba si hay una versión nueva y muestra un aviso con sus novedades. "Update now" la descarga, verifica su checksum, la instala, reinicia la herramienta y continúa la captura donde estaba. Las actualizaciones pueden venir de una carpeta compartida, de una dirección web o de las Releases de GitHub. También se puede comprobar desde Appearance.
- **Autoguardado de sesiones:** cada captura se guarda en disco mientras se hace. Al volver a abrir la herramienta ofrece recuperar la última sesión, con sus marcadores y notas, y el panel izquierdo lista las sesiones recientes.
- **Reconexión automática:** si el visor se desconecta (cable, reposo o Wi-Fi), la herramienta reintenta sola y retoma el log justo donde se cortó, sin duplicar líneas. El log marca el corte y la reconexión.
- **Información del dispositivo y de las apps:** modelo, build, versión de Android, batería y versión de cada app vigilada, visibles bajo la barra superior e incluidas en las exportaciones (.log y .json).
- **Copy bug summary:** copia un resumen para el bug con el dispositivo, las versiones, el intervalo del log, los problemas detectados y las líneas marcadas con sus notas. Se pega con formato en Azure DevOps y como Markdown en cualquier otro sitio.
- **Buscador de ruido (pestaña Noise):** lista los mensajes que más se repiten, con su porcentaje, y permite ignorar un mensaje (sean cuales sean sus números) o su tag con un clic.
- La exportación en .json pasa a incluir el dispositivo y las apps, con las líneas dentro de `lines`.

### 1.7 · 2026-10-21

- **Los mensajes de varias líneas se tratan como uno solo.** La captura usa ahora el formato `logcat -v long`, que indica exactamente dónde empieza y acaba cada mensaje. Al ignorar un error de Unity se ocultan también su traza, la línea `(Filename: …)` y sus líneas en blanco.
- Las líneas en blanco del final de cada mensaje ya no aparecen.
- Nueva opción **Hide lines with an empty message**, activada por defecto, en la sección de palabras ignoradas. También se aplica a los logs abiertos desde archivo.
- Las excepciones de Unity se detectan solo en la primera línea de cada mensaje, no en su traza.
- Los archivos exportados en .log mantienen el formato de siempre (`threadtime`).

### 1.6 · 2026-10-21

- **Lista de problemas (Issues):** detecta en directo, y también en logs abiertos desde archivo, crashes de Java (con la excepción que los causó), crashes nativos, ANR, excepciones de Unity, errores de memoria (OutOfMemoryError), apps que mueren o que el sistema cierra por falta de memoria, y caídas de FPS en Quest. Los problemas repetidos se agrupan con un contador. Con el filtro por app activo, muestra solo los de esas apps.
- **Marcadores (Bookmarks):** marca líneas con **B** o con el botón del panel de detalle, añade una nota a cada una y exporta solo las marcadas (en .json, con sus notas).
- Panel lateral con las dos listas: clic en un elemento para saltar a esa línea. **F8 / Mayús+F8** recorren los problemas y **F2 / Mayús+F2** los marcadores.
- Nueva opción de exportación **Bookmarked lines**.

### 1.5 · 2026-10-21

- **adb se instala solo:** si no está instalado, la página ofrece descargarlo de Google con un clic, con barra de progreso. Se guarda en una carpeta `platform-tools` junto a la herramienta, sin permisos de administrador ni cambios en el PATH. Si ya hay un adb en el PATH, se usa ese.
- **Lanzadores de doble clic** para Windows (`.bat`), macOS (`.command`) y Linux (`.sh`). Buscan Python y, si no lo encuentran, explican qué hacer.
- **Paquete portable para Windows** con Python 3.14 incluido: descomprimir y doble clic, sin instalar nada.
- La herramienta se distribuye en `.zip`.

### 1.4 · 2026-10-21

- **Tamaño de texto ajustable** del 85% al 200%, con botones A− / A+ en la barra superior y en el panel Appearance.
- El diseño se adapta al tamaño: las filas, columnas, casillas y el panel lateral crecen con el texto, y las columnas de PID y hora se ocultan solas cuando no caben.
- Nuevo botón **Hide filters** para ocultar el panel izquierdo y dar todo el ancho al log.
- El número de versión aparece en la terminal al arrancar y al final del panel Appearance.
- Corregido: en pantallas estrechas, al ocultarse la columna de PID las demás celdas se desplazaban a una columna equivocada.

### 1.3 · 2026-10-21

- **Temas de color** en el nuevo panel Appearance: Graphite (por defecto), Graphite light, Match system, Midnight, Daylight, High contrast y Ludus.
- Contraste verificado: en todos los temas, el texto del log (cada nivel y cada color de tag) cumple el mínimo de 4,5:1 sobre filas normales, alternas, resaltadas, seleccionadas y enfocadas. Se comprueba con `tools/check_contrast.py`.
- Cada tag tiene su propio color, las filas de warning y error se resaltan, y el nivel se muestra como etiqueta de color.
- Opciones de sombreado alterno y altura de fila (Compact, Normal, Roomy).
- La línea enfocada se marca con un contorno en lugar de un fondo fuerte, para no perder legibilidad.

### 1.2 · 2026-10-21

- **Filtro por app**: muestra solo los logs de las apps elegidas, identificando cada proceso por su PID.
- Botón para añadir la app abierta en el visor y autocompletado con las apps instaladas.
- Seguimiento automático de las apps que lanza una app de la lista (por ejemplo, una plataforma que abre una experiencia).
- Opción para incluir las líneas del sistema que mencionan esas apps (arranque, cierre, crashes y ANR).
- El panel de detalle muestra el nombre del proceso junto al PID.

### 1.1 · 2026-10-15

- **Show in context**: salta a una línea limpiando la búsqueda para ver qué pasó antes y después. Se abre con doble clic, Enter o el botón del panel de detalle.
- La línea queda marcada mientras navegas, y **Back to search** recupera la búsqueda anterior.

### 1.0 · 2026-10-15

- Primera versión: captura en directo con adb logcat para Pico 4 Ultra Enterprise y Meta Quest 3, con conexión por USB o Wi-Fi.
- Lista de palabras ignoradas, búsqueda con sintaxis (`tag:`, `pid:`, `-palabra`, `/regex/`) y filtros por origen (Unity, XR runtime, System) y por nivel.
- Selección de líneas y exportación en .log, .csv o .json.
- Apertura de logs guardados y atajos de teclado.

## VR Session Recorder

### 0.1 · 2026-10-21

- Primera versión, en pruebas: graba a la vez logcat, vídeo (scrcpy o el grabador del visor), métricas de rendimiento y marcadores (notas, capturas, crashes, ANR, excepciones de Unity, caídas de FPS) y lo empaqueta en un zip con un informe HTML.
- Aún no incluye las mejoras de VR Log Inspector 1.1–1.9 (temas, tamaño de texto, descarga automática de adb, lanzadores).
