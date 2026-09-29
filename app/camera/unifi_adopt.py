"""Receive the adoption payload Protect pushes to the camera.

After discovery lists the camera and the operator clicks Adopt, Protect makes an
HTTPS request to the camera itself: `POST /api/1.2/manage`, carrying the
management token and the console hosts to connect back to. That is the token
that otherwise has to be fetched by hand.

The payload is stored next to the camera's certificate, so a restart reconnects
without adopting again.

Endpoint shape and field names come from the reverse-engineering in the
unifi-cam-proxy-redalert fork (MIT); see CREDITS.md.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADOPT_PORT = 443
MAX_BODY = 256 * 1024


def payload_path(cam_id: str, state_dir: str) -> str:
    return os.path.join(state_dir, "certs", f"{cam_id}-mgmt.json")


def load_payload(cam_id: str, state_dir: str) -> dict:
    """What Protect told us last time, or {} if this camera is not adopted."""
    try:
        with open(payload_path(cam_id, state_dir)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_payload(cam_id: str, state_dir: str, payload: dict):
    path = payload_path(cam_id, state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def forget_payload(cam_id: str, state_dir: str):
    try:
        os.remove(payload_path(cam_id, state_dir))
    except OSError:
        pass


def split_host(host: str) -> tuple[str, int]:
    if ":" in host:
        name, _, port = host.rpartition(":")
        try:
            return name, int(port)
        except ValueError:
            return name, 7442
    return host, 7442


class AdoptionHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UniFiCamera"
    sys_version = ""

    service = None  # injected by serve()

    def log_message(self, fmt, *args):
        print(f"[unifi-adopt] {self.address_string()} {fmt % args}")

    def _send(self, data, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            return {}

    def do_GET(self):
        # Protect probes the camera before adopting; answer with who we are.
        if self.path.startswith("/api/1.2/"):
            return self._send(self.service.describe())
        self._send({"error": "Not Found"}, 404)

    def do_POST(self):
        if self.path == "/api/1.2/manage":
            return self._manage()
        if self.path == "/api/1.2/login":
            return self._login()
        self._send({"error": "Not Found"}, 404)

    def _login(self):
        body = self._body()
        user = body.get("username", "")
        password = body.get("password", "")
        if self.service.credentials_ok(user, password):
            return self._send({"status": "ok"})
        print(f"[unifi-adopt] rejected login for '{user}'")
        self._send({"error": "Invalid credentials"}, 401)

    def _manage(self):
        body = self._body()
        mgmt = body.get("mgmt") or {}
        token = mgmt.get("token")
        hosts = mgmt.get("hosts") or []
        if not token or not hosts:
            print("[unifi-adopt] manage request without a token or hosts")
            return self._send({"error": "Missing token or hosts"}, 400)

        host, port = split_host(str(hosts[0]))
        print(f"[unifi-adopt] adopted by {host}:{port}")
        self.service.accept(
            {
                "token": token,
                "host": host,
                "port": port,
                "hosts": [str(h) for h in hosts],
                "protocol": mgmt.get("protocol", "wss"),
                "consoleName": mgmt.get("consoleName"),
                "nvr": mgmt.get("nvr"),
                "adopted_at": time.time(),
            }
        )
        identity = self.service.describe()
        self._send(
            {
                "mac": identity["mac"],
                "model": identity["model"],
                "firmwareVersion": identity["firmwareVersion"],
                "sysid": identity["sysid"],
                "token": token,
                "hosts": [f"{host}:{port}"],
                "services": {"https": ADOPT_PORT, "wss": port},
            }
        )


class AdoptionService:
    """Holds what the camera claims to be, and what Protect told it."""

    def __init__(self, cfg, state, identity: dict, state_dir: str, on_adopted=None):
        self.cfg = cfg
        self.state = state
        self.identity = identity
        self.state_dir = state_dir
        self.on_adopted = on_adopted
        self.payload = load_payload(cfg.id, state_dir)
        self.adopted = threading.Event()
        if self.payload.get("token"):
            self.adopted.set()

    def describe(self) -> dict:
        return {
            "mac": self.state.mac or self.cfg.mac_hint,
            "model": self.identity["model"],
            "platform": self.identity["platform"],
            "sysid": self.identity["sysid"],
            "firmwareVersion": self.cfg.unifi_firmware,
            "name": self.cfg.name,
            "ip": self.state.ip,
        }

    def credentials_ok(self, user: str, password: str) -> bool:
        # Protect asks the operator for the camera's credentials; with none set
        # there is nothing to check against.
        if not self.cfg.password:
            return True
        return user == self.cfg.username and password == self.cfg.password

    def accept(self, payload: dict):
        self.payload = payload
        save_payload(self.cfg.id, self.state_dir, payload)
        self.adopted.set()
        if self.on_adopted:
            try:
                self.on_adopted(payload)
            except Exception as exc:  # noqa: BLE001
                print(f"[unifi-adopt] could not act on the adoption: {exc}")


def serve(service: AdoptionService, certfile: str, port: int = ADOPT_PORT):
    """Start the HTTPS endpoint Protect pushes the adoption payload to."""
    handler = type("BoundAdoptionHandler", (AdoptionHandler,), {"service": service})
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # The same self-signed pair the proxy identifies with. Protect does not
    # validate it: a real camera ships a self-signed certificate too.
    context.load_cert_chain(certfile)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)

    threading.Thread(target=httpd.serve_forever, name="unifi-adopt", daemon=True).start()
    print(f"[unifi-adopt] listening for adoption on https://0.0.0.0:{port}")
    return httpd
