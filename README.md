# VR QA Tools

Herramientas de QA para visores VR Android (Meta Quest 3, Pico 4 Ultra Enterprise).
Solo usan la librería estándar de Python 3.8+ y adb.

| Herramienta | Estado | Qué hace |
| --- | --- | --- |
| [`vr-log-inspector/`](vr-log-inspector/) | 1.5 | Visor de logcat en directo en el navegador: filtro por app, búsqueda, palabras ignoradas, contexto de una línea, exportación, temas y tamaño de texto. |
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

GitHub Actions comprueba que la etiqueta coincide con `TOOL_VERSION` y que los temas siguen cumpliendo el contraste, genera los dos zips y los publica en una Release con las notas de esa versión.

## Comprobaciones

En cada push a `main` y en cada pull request se compilan los scripts y se ejecuta `tools/check_contrast.py`, que verifica que todos los temas mantienen un contraste mínimo de 4,5:1 en el texto del log.
