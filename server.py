iimport os
import json
import subprocess
import tempfile
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", 8080))

TRIM_SECRET = os.environ.get("TRIM_PROXY_SECRET", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "motion-videos")


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "trim-proxy/1.0"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def trim(input_path, output_path, duration):
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "copy",
        "-an",
        "-t", str(duration),
        "-movflags", "+faststart",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def upload(file_path, storage_path):
    url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{storage_path}"
    with open(file_path, "rb") as f:
        data = f.read()

    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apikey": SUPABASE_KEY,
            "Content-Type": "video/mp4",
            "x-upsert": "true",
        },
    )
    urllib.request.urlopen(req)
    return f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{storage_path}"


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/":
            res = {"status": "ok"}
            data = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self): 
        if self.path != "/trim":
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("Authorization", "")
        if TRIM_SECRET and auth != f"Bearer {TRIM_SECRET}":
            self.send_response(401)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        video_url = body.get("video_url")
        duration = body.get("max_duration", 14.8)
        job_id = body.get("job_id", "test")

        with tempfile.TemporaryDirectory() as tmp:
            inp = os.path.join(tmp, "in.mp4")
            out = os.path.join(tmp, "out.mp4")

            download(video_url, inp)

            if not trim(inp, out, duration):
                self.send_response(500)
                self.end_headers()
                return

            storage_path = f"trimmed/{job_id}.mp4"
            public_url = upload(out, storage_path)

            res = {
                "success": True,
                "trimmed_url": public_url,
                "duration": duration,
                "stream_copy": True
            }

            data = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print("Running on port", PORT)
    server.serve_forever()
