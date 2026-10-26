VR Log Inspector
================

CÓMO ARRANCARLO
  Windows:  doble clic en "Start VR Log Inspector.bat"
  macOS:    doble clic en "Start VR Log Inspector.command"
  Linux:    ./start_vr_log_inspector.sh

Se abre el navegador con la herramienta. Deja abierta la ventana de la terminal
mientras la uses; al cerrarla, la herramienta se detiene. Exporta antes los logs
que necesites: solo se guardan en memoria.

LA PRIMERA VEZ
  1. Python 3.8 o superior. Si no lo tienes, el lanzador te lo indica y abre la
     página de descarga. El paquete portable para Windows (el que trae una
     carpeta "python") ya lo incluye y no necesita nada más.
  2. adb. Si no lo tienes instalado, la página muestra un aviso arriba con el
     botón "Download adb": lo descarga de Google y lo guarda en una carpeta
     "platform-tools" junto a estos archivos. Sin permisos de administrador ni
     cambios en el PATH. Si ya tienes adb instalado, se usa ese.
  3. El visor, en modo desarrollador y con la depuración USB activada.
     Los pasos para Quest 3 y Pico 4 Ultra Enterprise están en la página de Notion.

SI EL SISTEMA LO BLOQUEA
  Windows: si aparece "Windows protegió su PC", pulsa "Más información" y
           después "Ejecutar de todas formas".
  macOS:   si dice que no se puede abrir porque es de un desarrollador no
           identificado, ve a Ajustes del Sistema > Privacidad y seguridad y
           pulsa "Abrir igualmente".

SESIONES GUARDADAS
  Cada captura se guarda sola en la carpeta "sessions" mientras la haces. Si
  cierras la herramienta, se cuelga o se actualiza, al volver a abrirla te
  ofrece recuperar la última sesión, con sus marcadores y notas. Las sesiones
  anteriores están en "Recent sessions", en el panel izquierdo. Se guardan las
  15 más recientes de los últimos 14 días.

ACTUALIZACIONES
  Si este paquete tiene un archivo update.json, la herramienta comprueba al
  arrancar si hay una versión nueva y muestra un aviso con el botón
  "Update now": se descarga, se verifica, se instala y se reinicia sola,
  continuando la captura. También puedes comprobarlo en Appearance > Check for
  updates. Si las actualizaciones vienen de un repo privado de GitHub, añade a
  update.json tu token de solo lectura:
      {"source": "github:USUARIO/REPO", "github_token": "github_pat_..."}

NO BORRES
  - La carpeta "python" (paquete portable): es el Python que usa la herramienta.
  - La carpeta "platform-tools" una vez descargada: es adb. Si la borras, la
    herramienta te ofrecerá descargarla otra vez.
  - update.json: indica dónde buscar actualizaciones.

Para actualizar a una versión nueva, sustituye vr_log_inspector.py (o el
paquete entero). Tus preferencias se guardan en el navegador y se conservan.
