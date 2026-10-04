"""Descargador local de YouTube -> MP4 en ./downloads (los ficheros NO se borran)."""
import re
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

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
TIME_RE = re.compile(r"^\d+(:\d{1,2}){0,2}(\.\d+)?$")

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
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in YT_HOSTS:
        raise HTTPException(400, "Solo se admiten URLs de YouTube")
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
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
    }
    if start is not None or end is not None:
        suffix = f" ({int(start or 0)}s-{int(end) if end is not None else 'fin'}{'s' if end is not None else ''})"
        opts["download_ranges"] = download_range_func(None, [(start or 0, end if end is not None else float("inf"))])
    opts["outtmpl"] = str(DOWNLOAD_DIR / f"%(title).120B [%(id)s]{suffix}.%(ext)s")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        path = Path(info["requested_downloads"][0]["filepath"])
        job.update(status="done", progress=100.0, file=path.name)
    except Exception as e:  # noqa: BLE001 - mostramos cualquier fallo de yt-dlp al usuario
        job.update(status="error", error=str(e).strip()[:500])


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
