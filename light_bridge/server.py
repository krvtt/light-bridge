"""Small web server: the shared user interface plus a JSON API.

Every route maps 1:1 to an Engine method, so the later ESP32 box can offer the
same API and reuse the same interface unchanged.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("light_bridge.http")
WEB = Path(__file__).parent / "web"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/icon.png": ("icon.png", "image/png"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json")}


MAX_BODY = 512 * 1024
KEY_HEADER = "X-Light-Bridge-Key"
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                    "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


def load_or_create_key(data_dir) -> str:
    """A random access key per installation. Phones get it once from the QR code on the PC.
    Without it, other devices in the same network (e.g. at the office) cannot use the bridge."""
    f = Path(data_dir) / "access-key.txt"
    try:
        key = f.read_text(encoding="utf-8").strip()
        if len(key) >= 16:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(16)
    f.write_text(key, encoding="utf-8")
    return key


def client_allowed(client_ip: str, presented_key: str | None, key: str | None) -> bool:
    """The PC itself is always allowed; every other device needs the access key."""
    try:
        if ipaddress.ip_address(client_ip.split("%")[0]).is_loopback:
            return True
    except ValueError:
        return False
    return bool(key) and bool(presented_key) and hmac.compare_digest(presented_key, key)


def host_allowed(host_header: str) -> bool:
    """Only answer to addresses a home user would type: IPs, localhost, this PC's name.
    Blocks "DNS rebinding" tricks where a web page reaches the bridge through a foreign name."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]")[0]
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    return host in ("localhost", me, f"{me}.local") or host.endswith(".local") or "." not in host


def make_handler(engine, stop_callback, key=None):
    loop = engine.loop

    def call(coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LightBridge"

        def log_message(self, fmt, *args):  # keep the console readable
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:
                raise ValueError("Request too large")
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def _guard(self, write=False, api=True):
            """Reject requests from other web sites (CSRF) and foreign host names (DNS rebinding)."""
            if not host_allowed(self.headers.get("Host", "")):
                self._json(403, {"error": "Forbidden host"})
                return False
            if api and not client_allowed(self.client_address[0], self.headers.get(KEY_HEADER), key):
                self._json(401, {"error": "not paired", "pair": True})
                return False
            if write:
                origin = self.headers.get("Origin")
                if origin and origin.split("://", 1)[-1].rstrip("/").lower() != (self.headers.get("Host") or "").lower():
                    self._json(403, {"error": "Forbidden origin"})
                    return False
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    self._json(415, {"error": "JSON expected"})
                    return False
            return True

        def do_GET(self):
            path = self.path.split("?")[0]
            if not self._guard(api=path.startswith("/api/")):
                return None
            if path in STATIC:
                name, ctype = STATIC[path]
                f = WEB / name
                if f.exists():
                    return self._send(200, f.read_bytes(), ctype)
            try:
                if path == "/api/state":
                    return self._json(200, call(engine.api_state()))
                if path == "/api/templates":
                    return self._json(200, engine.templates())
                if path == "/api/backup":
                    return self._json(200, call(engine.api_backup()))
                m = re.fullmatch(r"/api/lights/([\w-]+)/profiles", path)
                if m:
                    return self._json(200, call(engine.api_profiles(m.group(1))))
                m = re.fullmatch(r"/api/lights/([\w-]+)/template/([\w-]+)", path)
                if m:
                    return self._json(200, call(engine.api_template(m.group(1), m.group(2))))
            except KeyError as e:
                return self._json(404, {"error": str(e).strip("'")})
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)})
            self._json(404, {"error": "Not found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            if not self._guard(write=True):
                return None
            try:
                b = self._body()
                if path == "/api/scan":
                    return self._json(200, call(engine.api_scan()))
                if path == "/api/lights/add":
                    return self._json(200, call(engine.api_add_ip(b.get("ip"))))
                if path == "/api/unignore":
                    return self._json(200, call(engine.api_unignore()))
                if path == "/api/find-helialux":
                    return self._json(200, call(engine.api_find_helialux()))
                if path == "/api/restore":
                    return self._json(200, call(engine.api_restore(b)))
                if path == "/api/stop":
                    self._json(200, {"ok": True})
                    threading.Thread(target=stop_callback, daemon=True).start()
                    return None
                m = re.fullmatch(r"/api/lights/([\w-]+)/([\w-]+)(?:/(\d+))?", path)
                if not m:
                    return self._json(404, {"error": "Not found"})
                lid, action, arg = m.group(1), m.group(2), m.group(3)
                if action == "settings":
                    r = engine.api_settings(lid, b)
                elif action == "forget":
                    r = engine.api_forget(lid)
                elif action == "manual":
                    r = engine.api_manual(lid, b.get("levels", {}), int(b.get("minutes", 60)))
                elif action == "maintenance":
                    r = engine.api_maintenance(lid, int(b.get("minutes", 60)))
                elif action == "program":
                    r = engine.api_program(lid)
                elif action == "preview":
                    r = engine.api_preview(lid, b.get("levels"), bool(b.get("stop")))
                elif action == "profiles" and arg is not None:
                    r = engine.api_save_profile(lid, int(arg), b)
                elif action == "weekdays":
                    r = engine.api_weekdays(lid, b.get("weekdays", []))
                elif action == "reload":
                    r = engine.api_reload(lid)
                elif action == "sync-clock":
                    r = engine.api_sync_clock(lid)
                elif action == "adopt":
                    r = engine.api_adopt(lid, b.get("name"), b.get("room"))
                elif action == "identify":
                    r = engine.api_identify(lid)
                elif action == "ignore":
                    r = engine.api_ignore(lid)
                elif action == "curve-test":
                    r = engine.api_curve_test(lid, b.get("variant", 1))
                elif action == "curve-result":
                    r = engine.api_curve_result(lid, b.get("variant", 1), b.get("worked"), b.get("apply_to_all"))
                elif action == "lamp-mode":
                    r = engine.api_lamp_mode(lid, b.get("mode", "simple"))
                else:
                    return self._json(404, {"error": "Unknown action"})
                return self._json(200, call(r))
            except KeyError as e:
                return self._json(404, {"error": str(e).strip("'")})
            except (ValueError, ConnectionError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("API error")
                return self._json(500, {"error": str(e) or e.__class__.__name__})

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows, "reuse address" would let a second bridge open the same port unnoticed.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def start_server(engine, port, stop_callback, key=None):
    """Start on the first free port from `port` upwards. Returns (server, port)."""
    for p in range(port, port + 11):
        try:
            srv = _Server(("0.0.0.0", p), make_handler(engine, stop_callback, key))
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return srv, p
        except OSError:
            continue
    raise OSError(f"No free port between {port} and {port + 10}")
