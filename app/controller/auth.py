"""Single-account login for the web UI.

There is no account to set up beforehand: on first use the UI asks for a
username and password and stores them, hashed, next to the camera config. The
session is a cookie carrying an HMAC-signed token, so a controller restart does
not log everyone out and no session table has to be kept.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

COOKIE_NAME = "onvif_bridge_session"
SESSION_DAYS = 14

# scrypt at N=2^14, r=8 needs 16 MiB per hash. The next step up lands exactly on
# OpenSSL's default 32 MiB ceiling and raises "memory limit exceeded", so this is
# the practical maximum without passing a custom maxmem.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AuthStore:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "auth.json")
        self._lock = threading.RLock()
        os.makedirs(data_dir, exist_ok=True)

    # ------------------------------------------------------------------ storage

    def _read(self) -> dict:
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @property
    def configured(self) -> bool:
        data = self._read()
        return bool(data.get("username") and data.get("hash"))

    def username(self) -> str:
        return self._read().get("username", "")

    # ------------------------------------------------------------------ hashing

    @staticmethod
    def _hash(password: str, salt: bytes) -> str:
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
            dklen=32,
        )
        return _b64e(derived)

    def set_credentials(self, username: str, password: str):
        """Create or replace the account; invalidates every existing session."""
        with self._lock:
            data = self._read()
            salt = secrets.token_bytes(16)
            data.update(
                {
                    "username": username,
                    "salt": _b64e(salt),
                    "hash": self._hash(password, salt),
                    "secret": data.get("secret") or _b64e(secrets.token_bytes(32)),
                    # Bumping this makes tokens signed before the change invalid.
                    "generation": int(data.get("generation", 0)) + 1,
                    "updated_at": time.time(),
                }
            )
            self._write(data)

    def verify(self, username: str, password: str) -> bool:
        data = self._read()
        if not data.get("hash"):
            return False
        expected = data.get("username", "")
        candidate = self._hash(password, _b64d(data.get("salt", "")))
        # Compare both parts without short-circuiting on the username.
        user_ok = hmac.compare_digest(username, expected)
        pass_ok = hmac.compare_digest(candidate, data.get("hash", ""))
        return user_ok and pass_ok

    # ------------------------------------------------------------------ sessions

    def _secret(self) -> bytes:
        with self._lock:
            data = self._read()
            secret = data.get("secret")
            if not secret:
                secret = _b64e(secrets.token_bytes(32))
                data["secret"] = secret
                self._write(data)
        return _b64d(secret)

    def issue_token(self) -> str:
        data = self._read()
        payload = {
            "u": data.get("username", ""),
            "g": int(data.get("generation", 1)),
            "exp": int(time.time() + SESSION_DAYS * 86400),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._secret(), raw, hashlib.sha256).digest()
        return f"{_b64e(raw)}.{_b64e(signature)}"

    def valid_token(self, token: str) -> bool:
        if not token or "." not in token:
            return False
        body, _, signature = token.partition(".")
        try:
            raw = _b64d(body)
            given = _b64d(signature)
        except Exception:
            return False

        expected = hmac.new(self._secret(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(given, expected):
            return False

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False

        data = self._read()
        if payload.get("u") != data.get("username"):
            return False
        if int(payload.get("g", 0)) != int(data.get("generation", 1)):
            return False
        return float(payload.get("exp", 0)) > time.time()
