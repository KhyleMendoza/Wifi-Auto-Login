from __future__ import annotations

import argparse
import hmac
import html
import io
import os
import socket
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "WiFiAutoLogin.exe"
APK = ROOT / "dist" / "WiFiAutoLogin.apk"
CONFIG = ROOT / "config.example.ini"
USERS = ROOT / "users.json"
SCREENSHOT = ROOT / "docs" / "screenshot.png"
ZIP_NAME = "WiFiAutoLogin.zip"
# Hidden "admin" download: the same app plus users.json (the user list). Unlocked by
# clicking the page title 5 times. Note: this is just an obscure path, not real access
# control - anyone on the network who knows this URL can download the user list.
ADMIN_ZIP_NAME = "WiFiAutoLogin-with-users.zip"
# Set by --admin-password (or the WIFI_ADMIN_PASSWORD env var). Empty = the admin
# download is off: the 5-click section stays hidden and the URL returns 404.
ADMIN_KEY = ""

_zip_lock = threading.Lock()
_zip_cache: dict = {"key": None, "data": b""}


def build_zip() -> bytes:
    key = (EXE.stat().st_mtime, EXE.stat().st_size, CONFIG.stat().st_mtime, CONFIG.stat().st_size)
    with _zip_lock:
        if _zip_cache["key"] != key:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(EXE, "WiFiAutoLogin/WiFiAutoLogin.exe")
                z.write(CONFIG, "WiFiAutoLogin/config.ini")
            _zip_cache.update(key=key, data=buf.getvalue())
        return _zip_cache["data"]


def build_admin_zip() -> bytes:
    """The normal app plus users.json, so the picker works on the machine it's unzipped to."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(EXE, "WiFiAutoLogin/WiFiAutoLogin.exe")
        z.write(CONFIG, "WiFiAutoLogin/config.ini")
        if USERS.exists():
            z.write(USERS, "WiFiAutoLogin/users.json")
    return buf.getvalue()


def size_text(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def page() -> str:
    built = time.strftime('%b %d, %Y', time.localtime(EXE.stat().st_mtime))
    shot = '<img src="/screenshot.png" alt="WiFi Auto Login window">' if SCREENSHOT.exists() else ""
    admin_enabled = "true" if ADMIN_KEY else "false"
    apk = f"""
  <div class="card">
    <h2 style="margin:0 0 6px;font-size:20px">On your phone (Android)</h2>
    <a class="btn" href="/download/WiFiAutoLogin.apk">Download app (.apk)</a>
    <p class="muted" style="margin-top:10px">Open it on your phone to install ({size_text(APK.stat().st_size)}). You may need to allow installing from your browser. Then enter your login and tap Start.</p>
  </div>""" if APK.exists() else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WiFi Auto Login</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#1d2329; --muted:#5e6873; --accent:#1e6fd9; --line:#dde2e8; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#14181d; --card:#1d232a; --text:#e6e9ed; --muted:#9aa4af; --accent:#5aa0ff; --line:#2c343d; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.55 "Segoe UI", system-ui, sans-serif; }}
main {{ max-width:860px; margin:0 auto; padding:40px 16px 60px; }}
h1 {{ margin:0 0 6px; font-size:30px; }}
p {{ margin:0 0 14px; }}
.muted {{ color:var(--muted); }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:22px; margin:24px 0; }}
.btn {{ display:inline-block; background:var(--accent); color:#fff; text-decoration:none; font-weight:600;
        padding:12px 22px; border-radius:8px; margin:4px 10px 4px 0; }}
.link {{ color:var(--accent); }}
ol {{ padding-left:22px; margin:0; }}
li {{ margin-bottom:6px; }}
img {{ width:100%; height:auto; border:1px solid var(--line); border-radius:8px; }}
code {{ background:var(--bg); padding:1px 6px; border-radius:4px; }}
#title {{ cursor:default; user-select:none; }}
#admin {{ display:none; margin-top:12px; padding-top:12px; border-top:1px dashed var(--line); }}
</style>
</head>
<body>
<main>
  <h1 id="title">WiFi Auto Login</h1>
  <p class="muted">For when the Wi-Fi login page takes 20 tries to work. This app keeps trying for you until you're actually online.</p>

  {apk}

  <div class="card">
    <h2 style="margin:0 0 6px;font-size:20px">On your PC (Windows)</h2>
    <a class="btn" href="/download/{ZIP_NAME}">Download (zip)</a>
    <p class="muted" style="margin-top:10px">Unzip it and run <code>WiFiAutoLogin.exe</code>. Comes with a ready <code>config.ini</code> next to it. Built {html.escape(built)}.</p>
    <div id="admin">
      <a class="btn" id="adminbtn" href="#">Download with user list</a>
      <p class="muted" style="margin-top:10px">Includes <code>users.json</code> (the hidden user picker). Asks for a password. Only for admin PCs.</p>
    </div>
  </div>

  <div class="card">
    <ol>
      <li>Unzip it somewhere, like your Documents. Keep the exe and <code>config.ini</code> together.</li>
      <li>Open <code>WiFiAutoLogin.exe</code> and say yes when Windows asks for admin.</li>
      <li>Type your Wi-Fi username and password, then press <b>Start</b>.</li>
      <li>Leave it running. Tick <b>Watch mode</b> if you want it to log you back in when you get kicked out.</li>
    </ol>
  </div>

  {shot}
</main>
<script>
(function () {{
  var ADMIN_ENABLED = {admin_enabled};
  var ADMIN_URL = "/download/{ADMIN_ZIP_NAME}";
  var title = document.getElementById("title");
  var admin = document.getElementById("admin");
  var btn = document.getElementById("adminbtn");
  var clicks = [];
  title.addEventListener("click", function () {{
    var now = Date.now();
    clicks = clicks.filter(function (t) {{ return now - t < 3000; }});
    clicks.push(now);
    if (clicks.length >= 5) {{
      clicks = [];
      if (!ADMIN_ENABLED) {{ alert("The user-list download is not turned on for this server."); return; }}
      admin.style.display = "block";
      admin.scrollIntoView({{ behavior: "smooth", block: "center" }});
    }}
  }});
  btn.addEventListener("click", function (e) {{
    e.preventDefault();
    var pw = prompt("Password for the user-list download:");
    if (!pw) return;
    btn.textContent = "Checking...";
    fetch(ADMIN_URL, {{ headers: {{ "X-Admin-Key": pw }} }}).then(function (r) {{
      if (!r.ok) {{ alert(r.status === 403 ? "Wrong password." : "Not available."); return null; }}
      return r.blob();
    }}).then(function (b) {{
      btn.textContent = "Download with user list";
      if (!b) return;
      var u = URL.createObjectURL(b);
      var a = document.createElement("a");
      a.href = u; a.download = "{ADMIN_ZIP_NAME}";
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(u);
    }}).catch(function () {{ btn.textContent = "Download with user list"; alert("Download failed."); }});
  }});
}})();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WiFiAutoLogin"

    def _send(self, body: bytes, ctype: str, filename: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self._send(page().encode("utf-8"), "text/html; charset=utf-8")
            elif path == f"/download/{ZIP_NAME}":
                self._send(build_zip(), "application/zip", ZIP_NAME)
            elif path == f"/download/{ADMIN_ZIP_NAME}":
                if not ADMIN_KEY:
                    self.send_error(404)
                elif hmac.compare_digest(self.headers.get("X-Admin-Key", ""), ADMIN_KEY):
                    self._send(build_admin_zip(), "application/zip", ADMIN_ZIP_NAME)
                else:
                    self.send_error(403)
            elif path == "/download/WiFiAutoLogin.apk" and APK.exists():
                self._send(APK.read_bytes(), "application/vnd.android.package-archive",
                           "WiFiAutoLogin.apk")
            elif path == "/download/users.json":
                # The Android app fetches the user list here, with the same password.
                if not ADMIN_KEY or not USERS.exists():
                    self.send_error(404)
                elif hmac.compare_digest(self.headers.get("X-Admin-Key", ""), ADMIN_KEY):
                    self._send(USERS.read_bytes(), "application/json")
                else:
                    self.send_error(403)
            elif path == "/screenshot.png" and SCREENSHOT.exists():
                self._send(SCREENSHOT.read_bytes(), "image/png")
            else:
                self.send_error(404)
        except (ConnectionError, BrokenPipeError):
            pass

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.client_address[0]} {fmt % args}", flush=True)


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> int:
    ap = argparse.ArgumentParser(description="Download page for WiFi Auto Login")
    ap.add_argument("--port", type=int, default=3030)
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    ap.add_argument("--admin-password", default=os.environ.get("WIFI_ADMIN_PASSWORD", "password"),
                    help="password for the hidden 'with user list' download (blank = off, default: password)")
    args = ap.parse_args()

    global ADMIN_KEY
    ADMIN_KEY = args.admin_password

    missing = [p for p in (EXE, CONFIG) if not p.exists()]
    if missing:
        print("Missing: " + ", ".join(str(p) for p in missing))
        return 1
    build_zip()

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"Can't use port {args.port}: {e}. Try another one, e.g. --port 8080")
        return 1
    ip = lan_ip() if args.host == "0.0.0.0" else args.host
    print(f"Serving WiFi Auto Login at http://{ip}:{args.port}  (Ctrl+C to stop)", flush=True)
    print("User-list download: " + ("ON (click the title 5x, password required)" if ADMIN_KEY
                                     else "OFF (pass --admin-password to enable)"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
