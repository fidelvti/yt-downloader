# YT Clip

Descargador local de YouTube a MP4, con recorte opcional por tiempo. Se usa desde el navegador y los ficheros se guardan en `downloads/` (no se borran).

## Requisitos

- Python 3.10 o superior (probado con 3.12)
- [ffmpeg](https://ffmpeg.org/) en el PATH (`brew install ffmpeg` en macOS), necesario para unir audio y vídeo y para recortar

## Instalación

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Arrancar

```bash
source venv/bin/activate
uvicorn main:app --reload
```

Abre <http://127.0.0.1:8000>, pega la URL de YouTube y pulsa **Descargar MP4**. Los campos de inicio y fin son opcionales (formatos `SS`, `MM:SS` o `HH:MM:SS`); vacíos descargan el vídeo entero.

Para salir del entorno virtual: `deactivate`.

## Cómo está hecho

- **Backend** ([main.py](main.py)): FastAPI + [yt-dlp](https://github.com/yt-dlp/yt-dlp).
  - `POST /api/download` valida la URL (solo dominios de YouTube) y los tiempos, y lanza la descarga en un pool de 2 hilos.
  - `GET /api/download/{id}` devuelve el estado y el progreso del trabajo, que el frontend consulta periódicamente.
  - Los MP4 también se sirven en `/files/<nombre>`.
- **Frontend** ([static/index.html](static/index.html)): una sola página en HTML y JavaScript, sin dependencias.
- **Formato**: mejor calidad MP4 hasta 1080p, con alternativas si no existe.
- **Recortes**: se cortan en el keyframe más cercano para ser rápidos, así que pueden desplazarse uno o dos segundos. Si necesitas precisión exacta, añade `opts["force_keyframes_at_cuts"] = True` en `run_job`; recodifica el clip y es mucho más lento.
- El estado de los trabajos vive en memoria, así que se pierde al reiniciar el servidor.

## Problemas habituales

- **`The page needs to be reloaded` u otros errores de YouTube**: yt-dlp está desactualizado. YouTube cambia a menudo, así que actualízalo:

  ```bash
  pip install -U yt-dlp
  ```

  Las versiones recientes exigen Python 3.10+; con 3.9 pip se queda en una versión antigua.
- **`Address already in use`**: ya hay un servidor en el puerto 8000. Ciérralo o usa otro puerto con `--port 8001`.

## Aviso

Úsalo solo con contenido que tengas derecho a descargar y respeta los términos de YouTube.
