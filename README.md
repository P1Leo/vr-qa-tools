# VR QA Tools

Herramientas de QA para visores VR Android (Meta Quest 3, Pico 4 Ultra Enterprise).
Solo usan la librería estándar de Python 3.8+ y adb.

| Herramienta | Estado | Qué hace |
| --- | --- | --- |
| [`vr-log-inspector/`](vr-log-inspector/) | 1.8 | Visor de logcat en directo en el navegador: filtro por app, búsqueda, palabras ignoradas, detección de crashes y ANR, marcadores, contexto de una línea, exportación, temas y tamaño de texto. |
| [`vr-session-recorder/`](vr-session-recorder/) | 0.1, en pruebas | Graba una sesión de bug (log, vídeo, métricas y marcadores) y la empaqueta en un zip con un informe HTML. |

La documentación de uso de VR Log Inspector está en Notion. Los cambios de cada versión, en [CHANGELOG.md](CHANGELOG.md).

## Probar en local

```bash
cd vr-log-inspector
python vr_log_inspector.py      # o doble clic en el lanzador de tu sistema
```

Si no hay adb, la página ofrece descargarlo de Google.

## Generar los paquetes

```bash
cd vr-log-inspector
python build_release.py                 # dist/VR_Log_Inspector_<versión>.zip
python build_release.py --portable      # además el portable para Windows, con Python incluido
```

## Publicar una versión

1. Sube `TOOL_VERSION` en `vr-log-inspector/vr_log_inspector.py`.
2. Añade la entrada en `CHANGELOG.md`.
3. Haz commit y crea la etiqueta:

   ```bash
   git tag log-inspector-v1.6
   git push origin main --tags
   ```

GitHub Actions comprueba que la etiqueta coincide con `TOOL_VERSION` y que los temas siguen cumpliendo el contraste, genera los dos zips y `latest.json`, y los publica en una Release con las notas de esa versión.

## Actualizaciones automáticas

Cada paquete lleva un `update.json` que dice dónde buscar versiones nuevas. Al arrancar, la herramienta lee ahí `latest.json` y, si hay una versión mayor, ofrece instalarla.

| Origen | Valor de `UPDATE_SOURCE` | Qué hay que publicar |
| --- | --- | --- |
| Carpeta compartida (red o sincronizada con OneDrive, Google Drive o SharePoint) | `\\servidor\compartido\VR Log Inspector` o `~/OneDrive - Empresa/Tools/VR Log Inspector` | El zip normal y `latest.json` de cada versión en esa carpeta |
| Dirección web | `https://servidor/vr-log-inspector/latest.json` | El zip normal y `latest.json` en esa dirección |
| Releases de este repo | `github:USUARIO/vr-qa-tools` (valor por defecto) | Nada: lo hace GitHub Actions. En un repo privado cada usuario necesita acceso al repo y un token de solo lectura en su `update.json` (`"github_token"`) |

Para elegir el origen de los paquetes que genera GitHub Actions, crea la variable `UPDATE_SOURCE` en **Settings → Secrets and variables → Actions → Variables**. Para una carpeta compartida también se puede generar todo directamente en ella:

```bash
python build_release.py --update-source "\\servidor\compartido\VR Log Inspector" --out "\\servidor\compartido\VR Log Inspector"
```

La actualización solo sustituye `vr_log_inspector.py` y `README.txt` (deja una copia `.bak` de la versión anterior). Los lanzadores no se tocan porque pueden estar en uso; si una versión los cambia, hay que descargar el paquete completo.

## Comprobaciones

En cada push a `main` y en cada pull request se compilan los scripts y se ejecuta `tools/check_contrast.py`, que verifica que todos los temas mantienen un contraste mínimo de 4,5:1 en el texto del log.
