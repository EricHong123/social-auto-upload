#!/usr/bin/env python3
"""SAU Web Server — one-click social media publishing.

Start:   python3 web_server.py
UI:      http://127.0.0.1:8001
"""

import json
import subprocess
import shutil
import os
import sys
import uuid
import tempfile
import mimetypes
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

SAU_DIR = Path(__file__).parent
COOKIE_DIR = SAU_DIR / "cookies"
UPLOAD_DIR = SAU_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("SAU_PORT", "8001"))


def _run_sau(args, timeout=120):
    env = {**os.environ, "PYTHONPATH": str(SAU_DIR)}
    try:
        result = subprocess.run(
            args, cwd=str(SAU_DIR), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return result
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None


# ── Handlers ────────────────────────────────────────────

def handle_login(platform: str, account: str, headless: bool = False) -> dict:
    cookie_file = COOKIE_DIR / platform / f"{account}.json"
    if cookie_file.exists():
        return {"ok": True, "status": "logged_in"}

    args = ["sau", platform, "login", "--account", account]
    if headless:
        args.append("--headless")

    result = _run_sau(args)
    if result is None:
        return {"ok": False, "error": "SAU CLI 未安装。pip install -r requirements.txt"}

    # SAU login generates QR code at cookies/{platform}/{account}_qrcode.png
    qr_patterns = [
        COOKIE_DIR / platform / f"{account}_qrcode.png",
        COOKIE_DIR / platform / "qrcode.png",
    ]
    qr_path = None
    for p in qr_patterns:
        if p.exists():
            qr_path = p
            break

    if qr_path:
        # Copy QR to uploads for serving
        dest = UPLOAD_DIR / f"qr_{platform}_{account}.png"
        shutil.copy(qr_path, dest)
        return {"ok": True, "status": "qr_ready", "qr_url": f"/uploads/qr_{platform}_{account}.png"}

    if cookie_file.exists():
        return {"ok": True, "status": "logged_in"}

    return {"ok": False, "error": result.stderr[:300] or result.stdout[:300] or "登录超时"}


def handle_publish(platform: str, account: str, video_file: str,
                   title: str, desc: str, tags: str, schedule: str = "") -> dict:
    cookie_file = COOKIE_DIR / platform / f"{account}.json"
    if not cookie_file.exists():
        return {"ok": False, "error": f"请先在 {platform} 平台扫码登录"}

    args = [
        "sau", platform, "upload-video",
        "--account", account,
        "--file", video_file,
        "--title", title,
        "--desc", desc,
        "--tags", tags,
        "--headless",
    ]
    if schedule:
        args.extend(["--schedule", schedule])

    result = _run_sau(args, timeout=300)
    if result is None:
        return {"ok": False, "error": "SAU CLI 未安装"}

    if result.returncode == 0:
        return {"ok": True, "status": "published"}
    else:
        return {"ok": False, "error": result.stderr[:300] or result.stdout[:300] or "发布失败"}


def handle_status() -> dict:
    platforms = {}
    if COOKIE_DIR.exists():
        for d in COOKIE_DIR.iterdir():
            if d.is_dir():
                accounts = [f.stem for f in d.glob("*.json")]
                platforms[d.name] = accounts
    return {"platforms": platforms}


# ── HTTP Server ─────────────────────────────────────────

class SAUHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SAU_DIR / "web_ui"), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/status":
            self._json(handle_status())
            return
        if path.startswith("/uploads/"):
            filepath = UPLOAD_DIR / Path(path).name
            if filepath.exists():
                self.send_response(200)
                ct, _ = mimetypes.guess_type(str(filepath))
                self.send_header("Content-Type", ct or "application/octet-stream")
                self.end_headers()
                self.wfile.write(filepath.read_bytes())
                return
            self.send_error(404)
            return

        # Serve from web_ui directory
        if path == "/":
            path = "/index.html"
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))

        if path == "/api/login":
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
            platform = body.get("platform", "douyin")
            account = body.get("account", "default")
            result = handle_login(platform, account)
            self._json(result)
            return

        if path == "/api/publish":
            # Multipart: video file + metadata
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                result = self._handle_multipart_publish()
            else:
                body = json.loads(self.rfile.read(length)) if length > 0 else {}
                result = handle_publish(
                    platform=body.get("platform", "douyin"),
                    account=body.get("account", "default"),
                    video_file=body.get("video_file", ""),
                    title=body.get("title", ""),
                    desc=body.get("desc", ""),
                    tags=body.get("tags", ""),
                    schedule=body.get("schedule", ""),
                )
            self._json(result)
            return

        self.send_error(404)

    def _handle_multipart_publish(self) -> dict:
        """Parse multipart form data and publish."""
        import cgi
        from io import BytesIO

        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Parse multipart manually
        boundary = content_type.split("boundary=")[1].encode()
        parts = body.split(b"--" + boundary)

        fields = {}
        video_data = None
        video_filename = "upload.mp4"

        for part in parts:
            if b"\r\n\r\n" not in part:
                continue
            headers_raw, data = part.split(b"\r\n\r\n", 1)
            data = data.rstrip(b"\r\n--")

            headers_text = headers_raw.decode(errors="ignore")
            if "name=" not in headers_text:
                continue

            name = headers_text.split('name="')[1].split('"')[0]

            if "filename=" in headers_text:
                video_data = data
                video_filename = headers_text.split('filename="')[1].split('"')[0]
            else:
                fields[name] = data.decode(errors="ignore")

        if not video_data:
            return {"ok": False, "error": "请上传视频文件"}

        # Save video
        video_path = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{video_filename}"
        video_path.write_bytes(video_data)

        platform = fields.get("platform", "douyin")
        account = fields.get("account", "default")
        title = fields.get("title", "")
        desc = fields.get("desc", "")
        tags = fields.get("tags", "")

        return handle_publish(platform, account, str(video_path), title, desc, tags)

    def _json(self, data: dict):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        print(f"[SAU] {args[0]}")


if __name__ == "__main__":
    os.makedirs(SAU_DIR / "web_ui", exist_ok=True)
    print(f"SAU Web UI → http://127.0.0.1:{PORT}")
    HTTPServer(("127.0.0.1", PORT), SAUHandler).serve_forever()
