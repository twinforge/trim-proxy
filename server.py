Fly.io Trim Proxy — Option A (pure trim-and-return)
Accepts a video URL, trims to max_duration via lossless stream-copy,
and returns the raw trimmed MP4 bytes. No Supabase upload.
"""

import json
import os
import subprocess
import tempfile
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "8080"))


class TrimHandler(BaseHTTPRequestHandler):

    # ---------- health ----------
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    # ---------- trim ----------
    def do_POST(self):
        if self.path != "/trim":
            self._error(404, "Not found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception as e:
            self._error(400, f"Invalid JSON: {e}")
            return

        video_url = body.get("video_url", "")
        max_duration = body.get("max_duration", 14.8)
        job_id = body.get("job_id", "unknown")

        if not video_url:
            self._error(400, "video_url is required")
            return

        if not video_url.startswith("http://") and not video_url.startswith("https://"):
            self._error(400, f"video_url must be an absolute URL, got: {video_url[:200]}")
            return

        print(f"[{job_id}] Downloading: {video_url[:200]}")
        print(f"[{job_id}] max_duration: {max_duration}")

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "input.mp4")
            output_path = os.path.join(tmpdir, "trimmed.mp4")

            # ---- download ----
            try:
                urllib.request.urlretrieve(video_url, input_path)
                size_in = os.path.getsize(input_path)
                print(f"[{job_id}] Downloaded {size_in} bytes")
            except Exception as e:
                self._error(502, f"Download failed: {e}")
                return

            # ---- ffmpeg lossless trim ----
            cmd = [
                "ffmpeg", "-y",
                "-i", input_path,
                "-t", str(max_duration),
                "-c:v", "copy",
                "-an",
                "-movflags", "+faststart",
                output_path,
            ]
            print(f"[{job_id}] Running: {' '.join(cmd)}")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                print(f"[{job_id}] FFmpeg stderr: {result.stderr[-500:]}")
                self._error(500, f"FFmpeg failed (exit {result.returncode})")
                return

            size_out = os.path.getsize(output_path)
            print(f"[{job_id}] Trimmed output: {size_out} bytes")

            # ---- return raw bytes ----
            with open(output_path, "rb") as f:
                data = f.read()

            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            print(f"[{job_id}] Done — returned {len(data)} bytes")

    # ---------- helpers ----------
    def _error(self, code, msg):
        print(f"ERROR {code}: {msg}")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())


if __name__ == "__main__":
    print(f"Trim proxy listening on :{PORT}")
    HTTPServer(("0.0.0.0", PORT), TrimHandler).serve_forever()
