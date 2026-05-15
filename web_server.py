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
import time
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

# SAU uses different directory names than the CLI
SAU_DIR_MAP = {
    "douyin": "douyin_uploader",
    "xiaohongshu": "xiaohongshu_uploader",
    "kuaishou": "ks_uploader",
    "bilibili": "bilibili_uploader",
    "tencent": "tencent_uploader",
    "baijiahao": "baijiahao_uploader",
    "tiktok": "tk_uploader",
}
# Also map actual SAU dir names back to platform
SAU_DIR_MAP.update({
    "douyin_uploader": "douyin_uploader",
    "xiaohongshu_uploader": "xiaohongshu_uploader",
    "ks_uploader": "ks_uploader",
    "bilibili_uploader": "bilibili_uploader",
})

def _sau_dir(platform: str) -> str:
    """Map web UI platform name to SAU cookie directory name."""
    return SAU_DIR_MAP.get(platform, platform)

def _cookie_path(platform: str, account: str) -> Path:
    """Get the cookie file path for a platform + account.
    SAU saves cookies at cookies/{platform}_{account}.json (root level),
    but also sometimes at cookies/{platform}_uploader/{account}.json.
    Check both locations."""
    paths = [
        COOKIE_DIR / f"{platform}_{account}.json",
        COOKIE_DIR / f"{_sau_dir(platform)}_{account}.json",
        COOKIE_DIR / _sau_dir(platform) / f"{account}.json",
    ]
    # Return the first one that exists, or the first as default
    for p in paths:
        if p.exists():
            return p
    return paths[0]  # Default: root-level


def _run_sau(args, timeout=120):
    env = {**os.environ, "PYTHONPATH": str(SAU_DIR)}
    # Try `sau` command first, fallback to python3 sau_cli.py
    try:
        result = subprocess.run(
            args, cwd=str(SAU_DIR), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return result
    except FileNotFoundError:
        # Fallback: run via python3 directly
        try:
            fallback = ["python3", str(SAU_DIR / "sau_cli.py")] + args[1:]
            result = subprocess.run(
                fallback, cwd=str(SAU_DIR), capture_output=True, text=True,
                timeout=timeout, env=env,
            )
            return result
        except Exception:
            return None
    except subprocess.TimeoutExpired:
        return None


# ── Handlers ────────────────────────────────────────────

# Track background login processes
_login_processes: dict[str, subprocess.Popen] = {}

def handle_login(platform: str, account: str, headless: bool = True) -> dict:
    cookie_file = _cookie_path(platform, account)
    if cookie_file.exists():
        return {"ok": True, "status": "logged_in"}

    # Kill old login process and delete old QRs for this platform
    key = f"{platform}_{account}"
    if key in _login_processes:
        old_proc = _login_processes[key]
        if old_proc.poll() is None:
            old_proc.kill()
        del _login_processes[key]

    # Delete ALL old QR PNGs for this platform (prevent expired QR reuse)
    for old_qr in COOKIE_DIR.glob(f"{platform}_*qrcode*.png"):
        old_qr.unlink(missing_ok=True)
    for old_qr in (COOKIE_DIR / _sau_dir(platform)).glob("*.png") if (COOKIE_DIR / _sau_dir(platform)).exists() else []:
        old_qr.unlink(missing_ok=True)
    # Also clean uploads
    for old_up in UPLOAD_DIR.glob(f"qr_{platform}_*.png"):
        old_up.unlink(missing_ok=True)

    # Start fresh login in background
    args = ["sau", platform, "login", "--account", account]
    if headless:
        args.append("--headless")

    env = {**os.environ, "PYTHONPATH": str(SAU_DIR)}
    saucmd = "sau"
    if not shutil.which("sau"):
        # Fallback to python3
        args = ["python3", str(SAU_DIR / "sau_cli.py")] + args[1:]
        saucmd = "python3"

    try:
        # Start the process without waiting
        proc = subprocess.Popen(
            args, cwd=str(SAU_DIR), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
        )
        _login_processes[key] = proc
    except FileNotFoundError:
        return {"ok": False, "error": "SAU CLI 未安装。pip install -r requirements.txt"}

    # Wait briefly for QR code to appear
    import time
    for _ in range(30):  # wait up to 15 seconds
        time.sleep(0.5)
        if proc.poll() is not None:
            # Process exited
            del _login_processes[key]
            if cookie_file.exists():
                return {"ok": True, "status": "logged_in"}
            stdout = proc.stdout.read() if proc.stdout else ""
            stderr = proc.stderr.read() if proc.stderr else ""
            return {"ok": False, "error": stderr[:300] or stdout[:300] or "登录失败"}
        qr = _find_qr(platform, account)
        if qr:
            return {"ok": True, "status": "qr_ready", "qr_url": qr}

    return {"ok": True, "status": "waiting", "message": "正在启动浏览器..."}


def _find_qr(platform: str, account: str) -> str | None:
    """Find QR code image and copy to uploads for serving.
    Returns None if no QR found or if QR is older than 3 minutes (expired)."""
    now = time.time()
    qr_max_age = 180  # 3 minutes

    # Search in both cookies/ root and cookies/{platform}/
    search_dirs = [COOKIE_DIR, COOKIE_DIR / _sau_dir(platform)]
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for f in sorted(sdir.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True):
            mtime = f.stat().st_mtime
            if now - mtime > qr_max_age:
                continue  # Skip expired QRs
            if "qrcode" in f.name.lower() or "qr" in f.name.lower():
                if platform in f.name or "qrcode" in f.name.lower():
                    dest = UPLOAD_DIR / f"qr_{platform}_{account}.png"
                    shutil.copy(f, dest)
                    return f"/uploads/qr_{platform}_{account}.png"
    return None


def handle_publish(platform: str, account: str, video_file: str,
                   title: str, desc: str, tags: str, schedule: str = "") -> dict:
    cookie_file = _cookie_path(platform, account)
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
        # Check root-level cookies: {platform}_{account}.json
        for f in COOKIE_DIR.glob("*.json"):
            name = f.stem  # e.g. "douyin_default" or "xiaohongshu_default"
            parts = name.split("_", 1)
            if len(parts) == 2:
                platform, account = parts
                if platform not in platforms:
                    platforms[platform] = []
                platforms[platform].append(account)
        # Check subdirectory cookies
        for d in COOKIE_DIR.iterdir():
            if d.is_dir():
                accounts = [f.stem for f in d.glob("*.json")]
                if accounts:
                    for web_name, sau_name in SAU_DIR_MAP.items():
                        if sau_name == d.name:
                            if web_name not in platforms:
                                platforms[web_name] = []
                            platforms[web_name].extend(accounts)
                            break
                    else:
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
        if path == "/api/check-login":
            params = {}
            if "?" in self.path:
                params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            platform = params.get("platform", "douyin")
            account = params.get("account", "default")
            cookie_file = _cookie_path(platform, account)
            qr = _find_qr(platform, account)
            self._json({"logged_in": cookie_file.exists(), "qr_url": qr})
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
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        boundary = content_type.split("boundary=")[1].encode()
        parts = body.split(b"--" + boundary)

        fields = {}
        video_data = None
        video_filename = "upload.mp4"

        for part in parts:
            if b"\r\n\r\n" not in part:
                continue
            headers_raw, data = part.split(b"\r\n\r\n", 1)
            data = data.rstrip(b"\r\n").rstrip(b"--").rstrip(b"\r\n")

            headers_text = headers_raw.decode(errors="ignore")
            if "name=" not in headers_text:
                continue

            # Extract field name
            name_start = headers_text.index('name="') + 6
            name_end = headers_text.index('"', name_start)
            name = headers_text[name_start:name_end]

            if "filename=" in headers_text:
                video_data = data
                fn_start = headers_text.index('filename="') + 10
                fn_end = headers_text.index('"', fn_start)
                video_filename = headers_text[fn_start:fn_end]
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
