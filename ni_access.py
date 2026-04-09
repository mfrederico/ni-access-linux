#!/usr/bin/env python3
"""
NI Access for Linux — Native Instruments product manager
A single-file web app to browse, download, and install your NI products on Linux.

Usage:
    python3 ni_access.py

Then open http://localhost:6510 in your browser.

Requirements: python3, requests (pip install requests), aria2c (optional, for downloads)
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' module not found. Install it:")
    print("  pip install requests")
    sys.exit(1)

# ============================================================================
# Config
# ============================================================================
PORT = 6510
DOWNLOAD_DIR = os.path.expanduser("~/NI-Downloads")
INSTALL_DIR = os.path.expanduser("~/NI-Instruments")

AUTH0_DOMAIN = "auth.native-instruments.com"
AUTH0_CLIENT_ID = "GgcQZ2OCSvzqgVL7RSAoErQRNB9S59kh"
AUTH0_AUDIENCE = "https://api.native-instruments.com"
API_BASE = "https://api.native-instruments.com"
APP_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9."
    "eyJpYXQiOjE2MzQ3MzA1MjYsInN1YiI6ImFwcGxpY2F0aW9uIiwiZGF0YSI6eyJuYW1lIjoiTmF0aXZlQWNjZXNzIiwidmVyc2lvbiI6IjIuMCJ9LCJleHAiOjI1MzQwMjMwMDc5OX0."
    "U6EQdp8WNcOyYFIHWw9tGUDUCEtxSuLmqEOfLB2UCZMYUkmsV5TItuKPbPCg5-_s7Ls3_4vbMDpisfGqXretddhVnBg-UoSJB4vj4RZtZq29_KaSly9cFA2A5lVbCDEM1bKNkKfNSyfDM6Whkdu2ub3aqt3LgAg7dfMVI3-_MY24txhZNW8xQ44M1nVsiUkpMk7nqrhIwcnb7EX-DPLbIQQ2NCLtoEGiA9eeCu19RvekxTxbttghDptkFBYqs_6CTiKmg98BkU8kQn2225LuzLIeD43vA6yHGyPwyvZloO1Pid5TcRH5qjqjLcfnCk65lSEGR39fZY_AnuDQAtF4tg"
)
USER_AGENT = "NativeAccess/3.24.0"
TOKEN_FILE = os.path.expanduser("~/.ni-access-token.json")

# ============================================================================
# State
# ============================================================================
session_state = {
    "access_token": None,
    "refresh_token": None,
    "user": None,
    "products": [],
    "artifacts": [],
    "downloads": {},  # upid -> {status, progress, file}
}


def api_headers(use_user_token=True):
    h = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-NI-App-Token": APP_TOKEN,
    }
    token = session_state["access_token"] if use_user_token else APP_TOKEN
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ============================================================================
# Auth
# ============================================================================
def do_login(email, password):
    resp = requests.post(
        f"https://{AUTH0_DOMAIN}/oauth/token",
        json={
            "grant_type": "password",
            "client_id": AUTH0_CLIENT_ID,
            "audience": AUTH0_AUDIENCE,
            "username": email,
            "password": password,
            "scope": "openid profile email offline_access",
        },
        timeout=15,
    )
    data = resp.json()
    if "access_token" not in data:
        return {"error": data.get("error_description", data.get("error", "Login failed"))}

    session_state["access_token"] = data["access_token"]
    session_state["refresh_token"] = data.get("refresh_token")

    # Save tokens
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": data["access_token"], "refresh_token": data.get("refresh_token")}, f)

    # Fetch user info
    fetch_user()
    return {"ok": True, "user": session_state["user"]}


def do_logout():
    session_state["access_token"] = None
    session_state["refresh_token"] = None
    session_state["user"] = None
    session_state["products"] = []
    session_state["artifacts"] = []
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    return {"ok": True}


def try_restore_session():
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)
            session_state["access_token"] = data.get("access_token")
            session_state["refresh_token"] = data.get("refresh_token")
            fetch_user()
            if session_state["user"]:
                return True
        except Exception:
            pass
    return False


def fetch_user():
    try:
        resp = requests.get(f"{API_BASE}/v1/users/me", headers=api_headers(), timeout=10)
        data = resp.json()
        if data.get("response_head", {}).get("status") == "OK":
            session_state["user"] = data["response_body"]
        else:
            session_state["user"] = None
            session_state["access_token"] = None
    except Exception:
        session_state["user"] = None


# ============================================================================
# Products
# ============================================================================
def fetch_products():
    if not session_state["access_token"]:
        return {"error": "Not logged in"}

    # Get owned product UPIDs
    resp = requests.get(f"{API_BASE}/v1/users/me/products", headers=api_headers(), timeout=15)
    data = resp.json()
    if data.get("response_head", {}).get("status") != "OK":
        return {"error": "Failed to fetch products"}

    owned = data["response_body"]["products"]
    owned_upids = {p["upid"] for p in owned}

    # Get all available artifacts (downloads)
    resp2 = requests.get(f"{API_BASE}/v2/download/me/full-products", headers=api_headers(), timeout=30)
    all_artifacts = resp2.json().get("artifacts", [])

    # Match owned products with their artifacts and resolve names
    products = {}
    for a in all_artifacts:
        upid = a["upid"]
        if upid not in owned_upids:
            continue

        product_title = a.get("product_title", "Unknown")
        if upid not in products:
            products[upid] = {
                "upid": upid,
                "title": product_title,
                "artifacts": [],
            }

        products[upid]["artifacts"].append({
            "title": a.get("title", ""),
            "version": a.get("version", ""),
            "platform": a.get("platform", ""),
            "file": a.get("target_file", ""),
            "size": a.get("filesize", 0),
            "url": a.get("url", ""),
            "update_id": a.get("update_id", ""),
            "type": a.get("type", ""),
            "file_type": a.get("file_type", ""),
        })

    # Sort artifacts: prefer linux, then latest version
    for p in products.values():
        p["artifacts"].sort(
            key=lambda a: (
                0 if "linux" in a["platform"] else (1 if a["platform"] == "nativeos" else (2 if a["platform"] == "pc" else 3)),
                a["version"],
            ),
            reverse=True,
        )
        # Determine best linux artifact
        for a in p["artifacts"]:
            if "linux" in a["platform"] or (a["platform"] == "nativeos" and a["file"].endswith(".deb")):
                p["linux_native"] = True
                p["best_artifact"] = a
                break
        if "best_artifact" not in p:
            # Fall back to latest PC version
            pc = [a for a in p["artifacts"] if a["platform"] == "pc"]
            if pc:
                p["best_artifact"] = pc[0]
            elif p["artifacts"]:
                p["best_artifact"] = p["artifacts"][0]

    session_state["products"] = sorted(products.values(), key=lambda p: p["title"])
    return {"ok": True, "count": len(products)}


# ============================================================================
# Downloads
# ============================================================================
def get_download_url(upid, update_id):
    url = f"{API_BASE}/v2/download/links/{upid}/{update_id}"
    resp = requests.get(url, headers=api_headers(), timeout=15)
    text = resp.text

    # Parse metalink XML to extract CDN URL
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
        ns = {"ml": "urn:ietf:params:xml:ns:metalink"}
        url_elem = root.find(".//ml:url", ns)
        size_elem = root.find(".//ml:size", ns)
        md5_elem = root.find(".//ml:hash[@type='md5']", ns)
        sha256_elem = root.find(".//ml:hash[@type='sha-256']", ns)
        return {
            "url": url_elem.text if url_elem is not None else None,
            "size": int(size_elem.text) if size_elem is not None else 0,
            "md5": md5_elem.text if md5_elem is not None else None,
            "sha256": sha256_elem.text if sha256_elem is not None else None,
            "metalink": text,
        }
    except Exception as e:
        return {"error": f"Failed to parse download link: {e}"}


def start_download(upid, update_id, filename):
    if upid in session_state["downloads"] and session_state["downloads"][upid].get("status") == "downloading":
        return {"error": "Already downloading"}

    dl_info = get_download_url(upid, update_id)
    if "error" in dl_info:
        return dl_info

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, filename)

    session_state["downloads"][upid] = {
        "status": "downloading",
        "progress": 0,
        "file": dest,
        "filename": filename,
        "size": dl_info["size"],
        "sha256": dl_info.get("sha256"),
    }

    def _download():
        try:
            cdn_url = dl_info["url"]
            # Try aria2c first (supports resume, multi-connection)
            if subprocess.run(["which", "aria2c"], capture_output=True).returncode == 0:
                # Save metalink for aria2c
                ml_path = os.path.join(DOWNLOAD_DIR, f"{filename}.metalink")
                with open(ml_path, "w") as f:
                    f.write(dl_info["metalink"])
                proc = subprocess.Popen(
                    ["aria2c", "--metalink-file=" + ml_path, "-d", DOWNLOAD_DIR,
                     "--file-allocation=none", "--summary-interval=2", "--console-log-level=warn"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                for line in proc.stdout:
                    # Parse aria2c progress
                    if "%" in line:
                        try:
                            pct = int(line.split("(")[1].split("%")[0])
                            session_state["downloads"][upid]["progress"] = pct
                        except (IndexError, ValueError):
                            pass
                proc.wait()
                if proc.returncode == 0:
                    session_state["downloads"][upid]["status"] = "complete"
                    session_state["downloads"][upid]["progress"] = 100
                else:
                    session_state["downloads"][upid]["status"] = "error"
                os.remove(ml_path)
            else:
                # Fallback to requests streaming download
                r = requests.get(cdn_url, stream=True, timeout=30)
                r.raise_for_status()
                total = int(r.headers.get("content-length", dl_info["size"]))
                downloaded = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            session_state["downloads"][upid]["progress"] = int(downloaded * 100 / total)
                session_state["downloads"][upid]["status"] = "complete"
                session_state["downloads"][upid]["progress"] = 100
        except Exception as e:
            session_state["downloads"][upid]["status"] = f"error: {e}"

    threading.Thread(target=_download, daemon=True).start()
    return {"ok": True, "file": dest}


def install_deb(filepath):
    """Install a .deb file."""
    try:
        result = subprocess.run(
            ["sudo", "dpkg", "-i", filepath],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return {"ok": True, "output": result.stdout}
        else:
            # Try fixing dependencies
            subprocess.run(["sudo", "apt-get", "-f", "install", "-y"], capture_output=True, timeout=120)
            return {"ok": False, "output": result.stdout + "\n" + result.stderr}
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# HTML UI
# ============================================================================
def render_page():
    user = session_state["user"]
    products = session_state["products"]
    downloads = session_state["downloads"]

    if not user:
        login_html = """
        <div class="login-box">
            <h1>NI Access for Linux</h1>
            <p>Sign in with your Native Instruments account</p>
            <form method="POST" action="/api/login">
                <input type="email" name="email" placeholder="Email" required autofocus>
                <input type="password" name="password" placeholder="Password" required>
                <button type="submit">Sign In</button>
            </form>
            <p class="footer">Your credentials are sent directly to Native Instruments servers.<br>
            Nothing is stored except the session token.</p>
        </div>
        """
        return HTML_TEMPLATE.replace("{{CONTENT}}", login_html)

    # Products view
    product_rows = ""
    for p in products:
        best = p.get("best_artifact", {})
        linux = p.get("linux_native", False)
        badge = '<span class="badge linux">Linux Native</span>' if linux else '<span class="badge wine">Windows/Wine</span>'
        size_mb = best.get("size", 0) / 1024 / 1024
        dl_status = downloads.get(p["upid"], {})
        dl_html = ""
        if dl_status.get("status") == "downloading":
            pct = dl_status.get("progress", 0)
            dl_html = f'<div class="progress"><div class="progress-bar" style="width:{pct}%">{pct}%</div></div>'
        elif dl_status.get("status") == "complete":
            dl_html = '<span class="badge linux">Downloaded</span>'
            if dl_status.get("filename", "").endswith(".deb"):
                dl_html += f' <a href="/api/install?upid={p["upid"]}" class="btn btn-sm">Install .deb</a>'
        elif dl_status.get("status", "").startswith("error"):
            dl_html = f'<span class="badge wine">{dl_status["status"]}</span>'

        # Build artifact selector
        artifact_options = ""
        for i, a in enumerate(p["artifacts"]):
            plat_icon = "🐧" if ("linux" in a["platform"] or (a["platform"] == "nativeos" and a["file"].endswith(".deb"))) else ("🪟" if a["platform"] == "pc" else ("🍎" if a["platform"] == "mac" else "📦"))
            a_size = a["size"] / 1024 / 1024
            selected = "selected" if a == best else ""
            artifact_options += f'<option value="{i}" {selected}>{plat_icon} {a["title"]} v{a["version"]} ({a["platform"]}, {a_size:.0f}MB, {a["file"]})</option>\n'

        product_rows += f"""
        <div class="product">
            <div class="product-header">
                <strong>{p['title']}</strong> {badge}
            </div>
            <div class="product-body">
                <select name="artifact" class="artifact-select" id="sel-{p['upid']}">{artifact_options}</select>
                <a href="/api/download?upid={p['upid']}" class="btn" onclick="this.href='/api/download?upid={p['upid']}&idx='+document.getElementById('sel-{p['upid']}').value">Download ({size_mb:.0f} MB)</a>
                {dl_html}
            </div>
        </div>
        """

    content = f"""
    <div class="header">
        <h1>NI Access for Linux</h1>
        <div class="user-info">
            {user.get('first_name', '')} {user.get('last_name', '')} ({user.get('username', '')})
            <a href="/api/logout" class="btn btn-sm">Logout</a>
        </div>
    </div>
    <div class="toolbar">
        <a href="/api/refresh" class="btn">Refresh Products</a>
        <span>{len(products)} products</span>
        <span>Downloads: {DOWNLOAD_DIR}</span>
    </div>
    <div class="products">
        {product_rows if products else '<p>Click "Refresh Products" to load your library.</p>'}
    </div>
    <div class="footer-bar">
        <p>NI Access for Linux &mdash; Not affiliated with Native Instruments GmbH.
        Uses your existing NI account to access products you own.</p>
    </div>
    """
    return HTML_TEMPLATE.replace("{{CONTENT}}", content)


HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NI Access for Linux</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #1a1a1a; color: #e0e0e0; }
h1 { font-size: 1.5rem; font-weight: 600; }
.login-box { max-width: 400px; margin: 100px auto; padding: 40px; background: #2a2a2a; border-radius: 12px; text-align: center; }
.login-box h1 { margin-bottom: 8px; }
.login-box p { color: #888; margin-bottom: 24px; font-size: 0.9rem; }
.login-box form { display: flex; flex-direction: column; gap: 12px; }
.login-box input { padding: 12px; border: 1px solid #444; border-radius: 6px; background: #333; color: #fff; font-size: 1rem; }
.login-box input:focus { outline: none; border-color: #0af; }
.footer { margin-top: 20px; font-size: 0.75rem; color: #666; }
.header { display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; background: #222; border-bottom: 1px solid #333; }
.user-info { display: flex; align-items: center; gap: 12px; font-size: 0.9rem; color: #aaa; }
.toolbar { display: flex; align-items: center; gap: 16px; padding: 12px 24px; background: #1e1e1e; border-bottom: 1px solid #333; font-size: 0.85rem; color: #888; }
.btn { display: inline-block; padding: 8px 16px; background: #0af; color: #000; border: none; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 600; cursor: pointer; }
.btn:hover { background: #0cf; }
.btn-sm { padding: 4px 10px; font-size: 0.8rem; }
button { padding: 12px; background: #0af; color: #000; border: none; border-radius: 6px; font-size: 1rem; font-weight: 600; cursor: pointer; }
button:hover { background: #0cf; }
.products { padding: 16px 24px; display: flex; flex-direction: column; gap: 8px; }
.product { background: #252525; border-radius: 8px; padding: 12px 16px; }
.product-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.product-body { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.artifact-select { flex: 1; min-width: 200px; padding: 6px; background: #333; color: #ddd; border: 1px solid #444; border-radius: 4px; font-size: 0.8rem; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
.badge.linux { background: #2a5; color: #fff; }
.badge.wine { background: #a52; color: #fff; }
.progress { flex: 1; min-width: 120px; height: 20px; background: #333; border-radius: 4px; overflow: hidden; }
.progress-bar { height: 100%; background: #0af; text-align: center; font-size: 0.75rem; line-height: 20px; color: #000; font-weight: 600; transition: width 0.3s; }
.footer-bar { padding: 20px 24px; text-align: center; font-size: 0.75rem; color: #555; border-top: 1px solid #333; margin-top: 20px; }
.error { background: #a52; color: #fff; padding: 12px 24px; text-align: center; }
</style>
</head>
<body>
{{CONTENT}}
<script>
// Auto-refresh download progress every 2 seconds
setInterval(() => {
    if (document.querySelector('.progress-bar')) {
        fetch('/api/status').then(r => r.json()).then(d => {
            if (d.downloads) {
                for (const [upid, dl] of Object.entries(d.downloads)) {
                    const bar = document.querySelector(`#dl-${upid} .progress-bar`);
                    if (bar) { bar.style.width = dl.progress + '%'; bar.textContent = dl.progress + '%'; }
                    if (dl.status === 'complete') location.reload();
                }
            }
        }).catch(() => {});
    }
}, 2000);
</script>
</body>
</html>"""


# ============================================================================
# HTTP Handler
# ============================================================================
class NIHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

    def respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        if isinstance(body, str):
            body = body.encode()
        self.wfile.write(body)

    def respond_json(self, data, code=200):
        self.respond(code, "application/json", json.dumps(data))

    def redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_GET(self):
        try:
            self._handle_get()
        except Exception as e:
            print(f"  ERROR: {e}")
            self.respond(500, "text/html",
                HTML_TEMPLATE.replace("{{CONTENT}}", f'<div class="error">Server error: {e}</div><div class="login-box"><a href="/" class="btn">Back</a></div>'))

    def _handle_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self.respond(200, "text/html", render_page())

        elif path == "/api/status":
            self.respond_json({"downloads": session_state["downloads"]})

        elif path == "/api/refresh":
            result = fetch_products()
            self.redirect("/")

        elif path == "/api/logout":
            do_logout()
            self.redirect("/")

        elif path == "/api/download":
            upid = params.get("upid", [None])[0]
            idx = int(params.get("idx", [0])[0])
            if not upid:
                self.respond_json({"error": "Missing upid"}, 400)
                return
            # Find the product and artifact
            product = next((p for p in session_state["products"] if p["upid"] == upid), None)
            if not product:
                self.respond_json({"error": "Product not found"}, 404)
                return
            artifact = product["artifacts"][idx] if idx < len(product["artifacts"]) else product.get("best_artifact")
            if not artifact:
                self.respond_json({"error": "No artifact found"}, 404)
                return
            result = start_download(upid, artifact["update_id"], artifact["file"])
            self.redirect("/")

        elif path == "/api/install":
            upid = params.get("upid", [None])[0]
            dl = session_state["downloads"].get(upid, {})
            if dl.get("status") == "complete" and dl.get("file", "").endswith(".deb"):
                result = install_deb(dl["file"])
                self.respond_json(result)
            else:
                self.respond_json({"error": "No completed .deb download for this product"}, 400)

        else:
            self.respond(404, "text/plain", "Not found")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode()
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/login":
            params = urllib.parse.parse_qs(body)
            email = params.get("email", [""])[0]
            password = params.get("password", [""])[0]
            result = do_login(email, password)
            if "error" in result:
                error_html = HTML_TEMPLATE.replace("{{CONTENT}}",
                    f'<div class="error">{result["error"]}</div>' +
                    '<div class="login-box"><a href="/" class="btn">Try Again</a></div>')
                self.respond(401, "text/html", error_html)
            else:
                self.redirect("/")
        else:
            self.respond(404, "text/plain", "Not found")


# ============================================================================
# Main
# ============================================================================
def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Try to restore previous session
    if try_restore_session():
        print(f"  Restored session for {session_state['user'].get('first_name', '')} {session_state['user'].get('last_name', '')}")

    server = http.server.HTTPServer(("127.0.0.1", PORT), NIHandler)
    print(f"\n  NI Access for Linux")
    print(f"  Open http://localhost:{PORT} in your browser\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
