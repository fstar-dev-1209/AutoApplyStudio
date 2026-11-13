"""Optional app password.

The password is never stored. ``lock.json`` keeps a random salt and a small encrypted check value;
the key is derived from the password with scrypt and used with AES-256-GCM. While the app runs the
derived key is held in memory (per lock file) so the settings can be read and written; the eye
buttons in the UI only decide what is shown on screen.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MIN_PASSWORD_LENGTH = 8
_CHECK_PLAINTEXT = b"application-studio-lock-v1"
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}

# derived keys of unlocked lock files, shared by every AppLock for the same path
_KEYS: dict[str, bytes] = {}


class LockedError(Exception):
    """Protected data was requested while the app is locked, or the password was wrong."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * 1024 * 1024)


def _seal(key: bytes, plaintext: bytes) -> str:
    nonce = os.urandom(12)
    return _b64(nonce + AESGCM(key).encrypt(nonce, plaintext, None))


def _open(key: bytes, token: str) -> bytes:
    raw = base64.b64decode(token)
    return AESGCM(key).decrypt(raw[:12], raw[12:], None)


class AppLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ state

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and "salt" in data and "check" in data else None

    def enabled(self) -> bool:
        return self._read() is not None

    def unlocked(self) -> bool:
        return str(self.path) in _KEYS

    def forget_key(self) -> None:
        _KEYS.pop(str(self.path), None)

    # ------------------------------------------------------------------ password

    def _key_for(self, password: str) -> bytes | None:
        meta = self._read()
        if meta is None:
            return None
        try:
            key = _derive(password, base64.b64decode(meta["salt"]),
                          int(meta.get("n", _SCRYPT["n"])), int(meta.get("r", 8)), int(meta.get("p", 1)))
            if _open(key, meta["check"]) != _CHECK_PLAINTEXT:
                return None
            return key
        except (InvalidTag, ValueError, KeyError):
            return None

    def verify(self, password: str) -> bool:
        return self._key_for(password) is not None

    def unlock(self, password: str) -> bool:
        key = self._key_for(password)
        if key is None:
            return False
        _KEYS[str(self.path)] = key
        return True

    def create(self, password: str) -> None:
        """Turn the lock on (or replace its password); the app stays unlocked."""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"The password needs at least {MIN_PASSWORD_LENGTH} characters.")
        salt = os.urandom(16)
        key = _derive(password, salt, **_SCRYPT)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "salt": _b64(salt), **_SCRYPT, "check": _seal(key, _CHECK_PLAINTEXT)}),
                       encoding="utf-8")
        tmp.replace(self.path)
        _KEYS[str(self.path)] = key

    def remove(self) -> None:
        self.forget_key()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ data

    def seal(self, text: str) -> str:
        key = _KEYS.get(str(self.path))
        if key is None:
            raise LockedError("The app is locked.")
        return _seal(key, text.encode("utf-8"))

    def open(self, token: str) -> str:
        key = _KEYS.get(str(self.path))
        if key is None:
            raise LockedError("The app is locked.")
        try:
            return _open(key, token).decode("utf-8")
        except (InvalidTag, ValueError) as exc:
            raise LockedError("The protected data could not be read with this password.") from exc
