"""Present this camera to UniFi Protect as a native device.

Protect only adopts its own cameras over a proprietary protocol; ONVIF cameras
are accepted in a reduced "generic" mode. unifi-cam-proxy speaks that protocol,
so a camera in this mode is adopted like a real one.

The client certificate is generated here rather than lifted off a real UniFi
camera, and is kept in the shared state directory: it is what Protect
recognises the camera by, so regenerating it would look like a different device
and the camera would have to be adopted again.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile

CERT_SUBJECT = (
    "/C=TW/L=Taipei/O=Ubiquiti Networks Inc./OU=devint"
    "/CN=camera.ubnt.dev/emailAddress=support@ubnt.com"
)


def cert_path(cam_id: str, state_dir: str) -> str:
    return os.path.join(state_dir, "certs", f"{cam_id}.pem")


def ensure_certificate(cam_id: str, state_dir: str) -> str:
    """Return the camera's client certificate, creating it the first time."""
    path = cert_path(cam_id, state_dir)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path

    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A scratch directory rather than a fixed /tmp path: openssl has to be able
    # to write here, and two cameras generating at once must not collide.
    work = tempfile.mkdtemp(prefix="unifi-cert-")
    key = os.path.join(work, "private.key")
    csr = os.path.join(work, "request.csr")
    public = os.path.join(work, "public.crt")
    steps = [
        ["openssl", "ecparam", "-out", key, "-name", "prime256v1", "-genkey"],
        ["openssl", "req", "-new", "-sha256", "-key", key, "-out", csr,
         "-subj", CERT_SUBJECT],
        ["openssl", "x509", "-req", "-sha256", "-days", "36500", "-in", csr,
         "-signkey", key, "-out", public],
    ]
    for step in steps:
        result = subprocess.run(step, capture_output=True, timeout=60)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[:200]
            raise RuntimeError(f"openssl failed: {detail}")

    with open(path, "wb") as out:
        for part in (key, public):
            with open(part, "rb") as fh:
                out.write(fh.read())
    shutil.rmtree(work, ignore_errors=True)
    os.chmod(path, 0o600)
    print(f"[unifi] generated a client certificate at {path}")
    return path


def build_args(cfg, state, cert: str, stream_url: str) -> list[str]:
    """The unifi-cam-proxy invocation for this camera."""
    args = [
        "unifi-cam-proxy",
        "--host", cfg.unifi_host,
        "--cert", cert,
        "--mac", state.mac or cfg.mac_hint,
    ]
    # Only needed while adopting; afterwards the certificate identifies the
    # camera and a stale token would just be refused.
    if cfg.unifi_token:
        args += ["--token", cfg.unifi_token]
    if cfg.unifi_extra_args:
        args += shlex.split(cfg.unifi_extra_args)
    args += ["rtsp", "-s", stream_url]
    return args


def start(args: list[str]) -> subprocess.Popen:
    printable = " ".join(
        # The adoption token is a credential; keep it out of the log.
        "***" if index and args[index - 1] == "--token" else part
        for index, part in enumerate(args)
    )
    print(f"[unifi] starting: {printable}")
    return subprocess.Popen(args)
