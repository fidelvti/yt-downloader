"""Descargador local de YouTube -> MP4 en ./downloads (los ficheros NO se borran)."""
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp.utils import download_range_func

BASE = Path(__file__).parent
DOWNLOAD_DIR = BASE / "downloads"
STATIC_DIR = BASE / "static"
DOWNLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
    "x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com",
}
TIME_RE = re.compile(r"^\d+(:\d{1,2}){0,2}(\.\d+)?$")

# Recortes: bajar solo el tramo con ffmpeg es muy lento (~2x tiempo real), mientras que bajar el
# vídeo entero con yt-dlp es rápido y luego se corta en local al instante. Solo compensa pedir
# el tramo suelto cuando el vídeo dura más de CLIP_RATIO veces lo que dura el clip.
CLIP_RATIO = 30
RETRIES = 3  # intentos por método antes de pasar al siguiente o dar error
# yt-dlp solo activa deno por defecto; node se permite por si está instalado (p. ej. con Homebrew)
JS_RUNTIMES = {"deno": {}, "node": {}}

app = FastAPI()
pool = ThreadPoolExecutor(max_workers=2)  # máximo 2 descargas a la vez
jobs: dict[str, dict] = {}  # solo para el progreso en vivo; no es historial


class DownloadRequest(BaseModel):
    url: str
    start: str = ""  # "90", "1:30" o "1:02:03"; vacío = desde el principio
    end: str = ""    # igual; vacío = hasta el final


def parse_time(text: str, field: str) -> Optional[float]:
    text = text.strip()
    if not text:
        return None
    if not TIME_RE.match(text):
        raise HTTPException(400, f"Tiempo no válido en '{field}': usa SS, MM:SS o HH:MM:SS")
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


def check_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise HTTPException(400, "Solo se admiten URLs de YouTube o X (Twitter)")
    return url


def run_job(job_id: str, url: str, start: Optional[float], end: Optional[float]) -> None:
    job = jobs[job_id]
    state = {"done_streams": 0}

    def on_progress(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                streams = len(d.get("info_dict", {}).get("requested_formats") or [1])
                frac = (state["done_streams"] + d["downloaded_bytes"] / total) / streams
                job["progress"] = round(min(frac, 1.0) * 100, 1)
            job["status"] = "downloading"
        elif d["status"] == "finished":
            state["done_streams"] += 1

    def on_postprocess(d):
        if d["status"] == "started":
            job["status"] = "processing"
            job["progress"] = 100.0

    suffix = ""
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "windowsfilenames": True,
        "js_runtimes": JS_RUNTIMES,
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
    }
    clipping = start is not None or end is not None
    if clipping:
        suffix = f" ({int(start or 0)}s-{int(end) if end is not None else 'fin'}{'s' if end is not None else ''})"
    name_tmpl = f"%(title).120B [%(id)s]{suffix}.%(ext)s"

    tmp_dir = DOWNLOAD_DIR / f".tmp-{job_id}"

    def fetch(mode: str) -> Path:
        """mode: 'plain' (sin recorte), 'range' (solo el tramo, con ffmpeg) o 'full' (entero + recorte local)."""
        o = dict(opts)
        state["done_streams"] = 0
        job["progress"] = 0.0
        if mode == "range":
            o["download_ranges"] = download_range_func(None, [(start or 0, end if end is not None else float("inf"))])
        o["outtmpl"] = str((tmp_dir if mode == "full" else DOWNLOAD_DIR) / name_tmpl)
        with yt_dlp.YoutubeDL(o) as ydl:
            info = ydl.extract_info(url, download=True)
        path = Path(info["requested_downloads"][0]["filepath"])
        if mode != "full":
            return path

        job.update(status="processing", progress=100.0)
        out = DOWNLOAD_DIR / path.name
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start or 0), "-i", str(path)]
        if end is not None:
            cmd += ["-t", str(end - (start or 0))]
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg falló al recortar: {res.stderr.strip()[-300:]}")
        return out

    def discard_partials(video_id: str) -> None:
        """Borra restos de un intento fallido de este clip (nunca ficheros terminados ni de otras descargas)."""
        for f in DOWNLOAD_DIR.glob(f"*[[]{video_id}[]]{suffix}*"):
            if f.is_file() and (f.suffix in (".part", ".ytdl") or ".temp." in f.name):
                f.unlink(missing_ok=True)

    try:
        strategies, video_id = ["plain"] * RETRIES, ""
        if clipping:
            probe = {k: opts[k] for k in ("noplaylist", "quiet", "no_warnings", "no_color", "js_runtimes")}
            with yt_dlp.YoutubeDL(probe) as ydl:
                probe_info = ydl.extract_info(url, download=False)
            duration, video_id = probe_info.get("duration"), probe_info.get("id", "")
            clip_len = (end if end is not None else duration or 0) - (start or 0)
            if duration and duration <= CLIP_RATIO * clip_len:
                strategies = ["full"] * RETRIES
            else:
                # Tramo suelto con ffmpeg: ahorra datos. Si falla, se baja el vídeo entero y se recorta en local.
                strategies = ["range"] * RETRIES + ["full"] * RETRIES

        # YouTube devuelve a veces un 403 suelto al descargar: se reintenta antes de dar el error
        for i, mode in enumerate(strategies):
            try:
                path = fetch(mode)
                break
            except Exception:  # noqa: BLE001
                if i == len(strategies) - 1:
                    raise
                discard_partials(video_id)
                time.sleep(1)
        job.update(status="done", progress=100.0, file=path.name)
    except Exception as e:  # noqa: BLE001 - mostramos cualquier fallo de yt-dlp al usuario
        job.update(status="error", error=str(e).strip()[:500])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/download")
def start_download(req: DownloadRequest):
    url = check_url(req.url)
    start = parse_time(req.start, "inicio")
    end = parse_time(req.end, "fin")
    if end is not None and end <= (start or 0):
        raise HTTPException(400, "El fin debe ser posterior al inicio")
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "queued", "progress": 0.0, "file": None, "error": None}
    pool.submit(run_job, job_id, url, start, end)
    return {"id": job_id}


@app.get("/api/download/{job_id}")
def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Descarga no encontrada")
    return jobs[job_id]


# Los MP4 quedan accesibles en /files/<nombre> además de en la carpeta downloads/
app.mount("/files", StaticFiles(directory=DOWNLOAD_DIR), name="files")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
