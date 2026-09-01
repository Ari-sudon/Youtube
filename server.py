#!/usr/bin/env python3
import http.server
import socketserver
import urllib.parse
import urllib.request
import json
import subprocess
import os
import pathlib
import mimetypes
import re
import tempfile
import shutil
import time

PORT = int(os.environ.get("PORT", "8000"))
ROOT = pathlib.Path(__file__).parent.resolve()
TMPDIR = pathlib.Path(tempfile.gettempdir()) / "yt_downloader"
TMPDIR.mkdir(parents=True, exist_ok=True)

def clean_old_files():
    cutoff = time.time() - 3600
    for p in TMPDIR.glob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/":
            self.path = "/index.html"
            return super().do_GET()
        if parsed.path == "/api/health":
            return self.send_json(200, {
                "ok": True,
                "yt_dlp": shutil.which("yt-dlp"),
                "ffmpeg": shutil.which("ffmpeg")
            })
        if parsed.path == "/api/info":
            return self.handle_info(qs)
        if parsed.path == "/api/download":
            return self.handle_download(qs)
        if parsed.path == "/api/direct":
            return self.handle_direct(qs)
        return super().do_GET()

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def valid_url(self, url):
        try:
            p = urllib.parse.urlparse(url)
            return p.scheme in ("http", "https") and bool(p.netloc)
        except Exception:
            return False

    def run_ytdlp(self, args, timeout=60):
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings"] + args
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout
        )

    def handle_info(self, qs):
        url = qs.get("url", [""])[0].strip()
        if not self.valid_url(url):
            return self.send_json(400, {"error": "Please provide a valid HTTP/HTTPS URL."})

        proc = None
        attempts = [
            ["--dump-single-json", "--skip-download", url],
            ["--dump-single-json", "--skip-download",
             "--extractor-args", "youtube:player_client=android", url],
        ]
        for args in attempts:
            try:
                proc = self.run_ytdlp(args, timeout=45)
                if proc.returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                return self.send_json(504, {"error": "Fetching video information timed out."})

        if not proc or proc.returncode != 0:
            detail = (proc.stderr if proc else "yt-dlp did not run")[-1200:]
            return self.send_json(502, {
                "error": "Could not fetch video information.",
                "detail": detail
            })

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return self.send_json(502, {"error": "yt-dlp returned invalid data."})

        thumbs = data.get("thumbnails") or []
        thumbnail = data.get("thumbnail") or (thumbs[-1].get("url") if thumbs else "")
        return self.send_json(200, {
            "id": data.get("id"),
            "title": data.get("title"),
            "uploader": data.get("uploader") or data.get("channel"),
            "thumbnail": thumbnail,
            "duration": data.get("duration"),
            "duration_string": data.get("duration_string"),
            "view_count": data.get("view_count"),
        })

    def handle_download(self, qs):
        url = qs.get("url", [""])[0].strip()
        mode = qs.get("mode", ["video"])[0].lower()
        quality = qs.get("quality", ["720p"])[0]

        if not self.valid_url(url):
            return self.send_json(400, {"error": "Please provide a valid HTTP/HTTPS URL."})
        if mode not in ("video", "music"):
            return self.send_json(400, {"error": "Invalid download mode."})

        clean_old_files()
        token = f"{int(time.time())}_{os.getpid()}_{os.urandom(4).hex()}"
        template = str(TMPDIR / f"{token}_%(title).100B.%(ext)s")

        if mode == "video":
            heights = {"360p": 360, "480p": 480, "720p": 720,
                       "1080p": 1080, "1440p": 1440, "2160p": 2160, "4k": 2160}
            height = heights.get(quality.lower(), 720)
            fmt = f"bv*[height<={height}]+ba/b[height<={height}]/b"
            args = ["-f", fmt, "--merge-output-format", "mp4", "-o", template, url]
            mime = "video/mp4"
        else:
            q = quality.lower()
            audio_format = "mp3"
            if q in ("flac", "wav", "m4a", "opus"):
                audio_format = q
            audio_quality = {"128kbps": "5", "256kbps": "2", "320kbps": "0"}.get(q, "0")
            args = ["-x", "--audio-format", audio_format,
                    "--audio-quality", audio_quality, "-o", template, url]
            mime = mimetypes.types_map.get("." + audio_format, "application/octet-stream")

        try:
            proc = self.run_ytdlp(args, timeout=300)
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "Download timed out."})

        if proc.returncode != 0:
            return self.send_json(502, {
                "error": "Download failed.",
                "detail": proc.stderr[-1500:]
            })

        files = sorted(
            [x for x in TMPDIR.glob(f"{token}_*") if x.is_file() and x.suffix not in (".part", ".ytdl")],
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        if not files:
            return self.send_json(500, {"error": "Download completed but no output file was found."})

        path = files[0]
        filename = re.sub(r'[\r\n"]', "", path.name.split("_", 2)[-1])
        ctype = mimetypes.guess_type(str(path))[0] or mime

        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    def handle_direct(self, qs):
        url = qs.get("url", [""])[0].strip()
        if not self.valid_url(url):
            return self.send_json(400, {"error": "Please provide a valid HTTP/HTTPS URL."})
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                name = os.path.basename(urllib.parse.urlparse(url).path) or "download"
                name = re.sub(r'[^A-Za-z0-9._-]', "_", name)
                self.send_response(200)
                self.send_header("Content-Type", r.headers.get_content_type() or "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                if r.headers.get("Content-Length"):
                    self.send_header("Content-Length", r.headers["Content-Length"])
                self.end_headers()
                shutil.copyfileobj(r, self.wfile)
        except Exception as e:
            return self.send_json(502, {"error": "Direct download failed.", "detail": str(e)})

if __name__ == "__main__":
    print(f"Starting server on 0.0.0.0:{PORT}")
    print(f"yt-dlp: {shutil.which('yt-dlp')}")
    print(f"ffmpeg: {shutil.which('ffmpeg')}")
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as server:
        server.serve_forever()
