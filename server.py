from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, HttpUrl

ALLOWED_DOMAINS = {
    "fal.media",
    "v3.fal.media",
    "v3b.fal.media",
    "cdn.fal.media",
    "storage.fal.ai",
    "heygen.ai",
    "files.heygen.ai",
    "files2.heygen.ai",
    "resource.heygen.ai",
    "resource2.heygen.ai",
    "res.cloudinary.com",
    "rcqvgcdemyafannzidyy.supabase.co",
}

MAX_INPUT_BYTES = int(os.getenv("MAX_INPUT_BYTES", str(500 * 1024 * 1024)))
DOWNLOAD_TIMEOUT_S = float(os.getenv("DOWNLOAD_TIMEOUT_S", "120"))
FFMPEG_TIMEOUT_S = float(os.getenv("FFMPEG_TIMEOUT_S", "300"))

app = FastAPI(title="TwinForge ffmpeg proxy", version="2.0.0")

def _is_allowed_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith(f".{d}") for d in ALLOWED_DOMAINS)

def _download(url: str, dest: str) -> int:
    written = 0
    with httpx.stream("GET", url, timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as r:
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"source fetch failed {r.status_code}")
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                written += len(chunk)
                if written > MAX_INPUT_BYTES:
                    raise HTTPException(status_code=413, detail="source video too large")
                f.write(chunk)
    return written

def _run_ffmpeg(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", *args],
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_S,
    )
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    tail = stderr[-2000:] if len(stderr) > 2000 else stderr
    return proc.returncode, tail

class TrimRequest(BaseModel):
    video_url: HttpUrl
    max_duration: float = Field(gt=0, le=600)
    job_id: Optional[str] = None

class TranscodeRequest(BaseModel):
    video_url: HttpUrl
    job_id: Optional[str] = None

class BurnCaptionsRequest(BaseModel):
    video_url: HttpUrl
    srt_text: str = Field(min_length=1, max_length=200_000)
    job_id: Optional[str] = None

@app.get("/health")
def health():
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    return JSONResponse({"ok": ffmpeg_ok})

@app.post("/trim")
def trim(req: TrimRequest):
    url = str(req.video_url)
    if not _is_allowed_url(url):
        raise HTTPException(status_code=403, detail="source URL not allowed")

    work = tempfile.mkdtemp(prefix="trim_")
    src = os.path.join(work, "in.mp4")
    dst = os.path.join(work, "out.mp4")

    try:
        _download(url, src)

        rc, err = _run_ffmpeg([
            "-i", src,
            "-t", f"{req.max_duration}",
            "-c:v", "copy",
            "-c:a", "copy",
            dst,
        ])

        if rc != 0:
            raise HTTPException(status_code=500, detail=err)

        with open(dst, "rb") as f:
            return Response(f.read(), media_type="video/mp4")

    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.post("/transcode-shorts")
def transcode(req: TranscodeRequest):
    url = str(req.video_url)
    if not _is_allowed_url(url):
        raise HTTPException(status_code=403)

    work = tempfile.mkdtemp(prefix="ts_")
    src = os.path.join(work, "in.mp4")
    dst = os.path.join(work, "out.mp4")

    try:
        _download(url, src)

        rc, err = _run_ffmpeg([
            "-i", src,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-c:a", "aac",
            dst,
        ])

        if rc != 0:
            raise HTTPException(status_code=500, detail=err)

        with open(dst, "rb") as f:
            return Response(f.read(), media_type="video/mp4")

    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.post("/burn-captions")
def burn(req: BurnCaptionsRequest):
    url = str(req.video_url)
    if not _is_allowed_url(url):
        raise HTTPException(status_code=403)

    work = tempfile.mkdtemp(prefix="burn_")
    src = os.path.join(work, "in.mp4")
    srt = os.path.join(work, "sub.srt")
    dst = os.path.join(work, "out.mp4")

    try:
        _download(url, src)

        with open(srt, "w") as f:
            f.write(req.srt_text)

        os.chdir(work)

        rc, err = _run_ffmpeg([
            "-i", "in.mp4",
            "-vf", "subtitles=sub.srt",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-c:a", "copy",
            "out.mp4",
        ])

        if rc != 0:
            raise HTTPException(status_code=500, detail=err)

        with open(dst, "rb") as f:
            return Response(f.read(), media_type="video/mp4")

    finally:
        shutil.rmtree(work, ignore_errors=True)
