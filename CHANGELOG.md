# Changelog

## VR Log Inspector

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
- Aún no incluye las mejoras de VR Log Inspector 1.1–1.5 (temas, tamaño de texto, descarga automática de adb, lanzadores).
