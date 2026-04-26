```python
"""
TwinForge ffmpeg proxy.

Two endpoints:
  POST /trim             — lossless stream-copy trim (preserved behavior; -c:v copy -c:a copy)
  POST /transcode-shorts — true 9:16 (1080x1920) cover-crop transcode for YouTube Shorts

Both endpoints accept JSON and stream raw mp4 bytes back. The Supabase edge
function owns the upload to storage. The proxy holds NO secrets.

Health:
  GET /health  -> {"ok": true, "ffmpeg": ""}
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, HttpUrl

# ---- Config ----------------------------------------------------------------

# Mirror video-transcode's allowlist exactly so the proxy can never fetch from
# arbitrary hosts. Keep this in sync with supabase/functions/video-transcode/index.ts
# and supabase/functions/video-download-proxy/index.ts.
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

MAX_INPUT_BYTES = int(os.getenv("MAX_INPUT_BYTES", str(500 * 1024 * 1024)))  # 500MB
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
    """Stream the source video to disk. Returns bytes written."""
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
    """Run ffmpeg, return (returncode, last_stderr_chunk)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", *args],
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_S,
    )
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    # Keep only the tail so error responses stay readable.
    tail = stderr[-2000:] if len(stderr) > 2000 else stderr
    return proc.returncode, tail

# ---- Models ----------------------------------------------------------------

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

# ---- Routes ----------------------------------------------------------------

@app.get("/health")
def health():
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    version_line = ""
    if ffmpeg_ok:
        try:
            out = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
            version_line = (out.stdout or b"").decode().splitlines()[0] if out.stdout else ""
        except Exception:
            ffmpeg_ok = False
    return JSONResponse({"ok": ffmpeg_ok, "ffmpeg": version_line})

@app.post("/trim")
def trim(req: TrimRequest):
    """Lossless stream-copy trim. Preserved exactly to keep the 15s+ pipeline working."""
    url = str(req.video_url)
    if not _is_allowed_url(url):
        raise HTTPException(status_code=403, detail="source URL not allowed")

    work = tempfile.mkdtemp(prefix="trim_")
    src = os.path.join(work, "in.mp4")
    dst = os.path.join(work, "out.mp4")
    try:
        t0 = time.time()
        bytes_in = _download(url, src)
        t_dl = time.time() - t0

        rc, err = _run_ffmpeg([
            "-i", src,
            "-t", f"{req.max_duration}",
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-f", "mp4",
            dst,
        ])
        if rc != 0 or not os.path.exists(dst):
            raise HTTPException(status_code=500, detail=f"ffmpeg trim failed: {err}")

        size = os.path.getsize(dst)
        with open(dst, "rb") as f:
            data = f.read()

        return Response(
            content=data,
            media_type="video/mp4",
            headers={
                "X-Job-Id": req.job_id or "",
                "X-Bytes-In": str(bytes_in),
                "X-Bytes-Out": str(size),
                "X-Download-Seconds": f"{t_dl:.2f}",
            },
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.post("/transcode-shorts")
def transcode_shorts(req: TranscodeRequest):
    """Re-encode any source mp4 to true 1080x1920 cover-crop H.264/AAC Shorts-Ready output.

    Filter chain matches the current video-transcode contract exactly:
      scale=1080:1920:force_original_aspect_ratio=increase,
      crop=1080:1920,
      setsar=1,
      format=yuv420p
    """
    url = str(req.video_url)
    if not _is_allowed_url(url):
        raise HTTPException(status_code=403, detail="source URL not allowed")

    work = tempfile.mkdtemp(prefix="ts_")
    src = os.path.join(work, "in.mp4")
    dst = os.path.join(work, "out.mp4")
    try:
        t0 = time.time()
        bytes_in = _download(url, src)
        t_dl = time.time() - t0

        t1 = time.time()
        rc, err = _run_ffmpeg([
            "-i", src,
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,format=yuv420p",
            "-r", "30",
            "-vsync", "cfr",
            "-c:v", "libx264",
            "-profile:v", "high",
            "-level:v", "4.2",
            "-crf", "23",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-color_range", "tv",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-movflags", "+faststart",
            "-aspect", "9:16",
            "-f", "mp4",
            dst,
        ])
        t_enc = time.time() - t1

        if rc != 0 or not os.path.exists(dst):
            raise HTTPException(status_code=500, detail=f"ffmpeg transcode failed: {err}")

        size = os.path.getsize(dst)
        with open(dst, "rb") as f:
            data = f.read()

        return Response(
            content=data,
            media_type="video/mp4",
            headers={
                "X-Job-Id": req.job_id or "",
                "X-Bytes-In": str(bytes_in),
                "X-Bytes-Out": str(size),
                "X-Download-Seconds": f"{t_dl:.2f}",
                "X-Encode-Seconds": f"{t_enc:.2f}",
            },
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.post("/burn-captions")
def burn_captions(req: BurnCaptionsRequest):
    """POC: burn an SRT caption track into a video using ffmpeg's subtitles filter.

    Re-encodes video (subtitles filter requires re-encode); audio stream-copied.
    Returns raw mp4 bytes; the edge function owns storage upload.
    """
    url = str(req.video_url)
    if not _is_allowed_url(url):
        raise HTTPException(status_code=403, detail="source URL not allowed")

    work = tempfile.mkdtemp(prefix="burn_")
    src = os.path.join(work, "in.mp4")
    srt = os.path.join(work, "captions.srt")
    dst = os.path.join(work, "out.mp4")
    try:
        t0 = time.time()
        bytes_in = _download(url, src)
        t_dl = time.time() - t0

        with open(srt, "w", encoding="utf-8") as f:
            f.write(req.srt_text)

        # Run ffmpeg from the work dir so the subtitles filter sees a plain
        # relative filename (avoids the libass path/colon escaping pitfalls).
        cwd_before = os.getcwd()
        os.chdir(work)
        try:
            t1 = time.time()
            rc, err = _run_ffmpeg([
                "-i", "in.mp4",
                "-vf", "subtitles=captions.srt",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-f", "mp4",
                "out.mp4",
            ])
            t_enc = time.time() - t1
        finally:
            os.chdir(cwd_before)

        if rc != 0 or not os.path.exists(dst):
            raise HTTPException(status_code=500, detail=f"ffmpeg burn failed: {err}")

        size = os.path.getsize(dst)
        with open(dst, "rb") as f:
            data = f.read()

        return Response(
            content=data,
            media_type="video/mp4",
            headers={
                "X-Job-Id": req.job_id or "",
                "X-Bytes-In": str(bytes_in),
                "X-Bytes-Out": str(size),
                "X-Download-Seconds": f"{t_dl:.2f}",
                "X-Encode-Seconds": f"{t_enc:.2f}",
                "X-Ffmpeg-Tail": err[-400:].replace("\n", " | "),
            },
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
```
