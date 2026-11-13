"""OS-backed secret storage for API credentials.

Windows: DPAPI (CryptProtectData) bound to the signed-in user, so the ciphertext in
``credentials.dat`` is unreadable to other accounts or when the file is copied elsewhere.
Other platforms: the ``keyring`` package when installed; otherwise a 0600 file (development
fallback only - reported by ``backend_name``).
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from core.paths import config_dir

SERVICE = "AutoApplyStudio"  # its own keyring entries; never Application Studio's


def _file() -> Path:
    return config_dir() / "credentials.dat"


def _read_all(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ------------------------------------------------------------------ Windows DPAPI

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _UI_FORBIDDEN = 0x1

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _dpapi(data: bytes, protect: bool) -> bytes:
        src, _keep = _blob(data)
        out = _Blob()
        fn = _crypt32.CryptProtectData if protect else _crypt32.CryptUnprotectData
        args = (ctypes.byref(src), None, None, None, None, _UI_FORBIDDEN, ctypes.byref(out))
        if protect:
            ok = fn(ctypes.byref(src), "AutoApplyStudio", None, None, None, _UI_FORBIDDEN, ctypes.byref(out))
        else:
            ok = fn(*args)
        if not ok:
            raise OSError("Windows could not protect or unprotect the credential")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def backend_name() -> str:
        return "Windows DPAPI (current user)"

    def store_secret(name: str, value: str, path: Path | None = None) -> None:
        path = path or _file()
        data = _read_all(path)
        if value:
            data[name] = base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")
        else:
            data.pop(name, None)
        path.write_text(json.dumps(data), encoding="utf-8")

    def load_secret(name: str, path: Path | None = None) -> str:
        token = _read_all(path or _file()).get(name, "")
        if not token:
            return ""
        try:
            return _dpapi(base64.b64decode(token), False).decode("utf-8")
        except (OSError, ValueError):
            return ""  # different Windows user / corrupted: treat as not set

else:

    def backend_name() -> str:
        try:
            import keyring  # noqa: F401

            return "System keyring"
        except ImportError:
            return "Local file (install 'keyring' for OS-backed storage)"

    def store_secret(name: str, value: str, path: Path | None = None) -> None:
        try:
            import keyring

            if value:
                keyring.set_password(SERVICE, name, value)
            else:
                try:
                    keyring.delete_password(SERVICE, name)
                except Exception:  # noqa: BLE001
                    pass
            return
        except ImportError:
            pass
        path = path or _file()
        data = _read_all(path)
        if value:
            data[name] = base64.b64encode(value.encode("utf-8")).decode("ascii")
        else:
            data.pop(name, None)
        path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(path, 0o600)

    def load_secret(name: str, path: Path | None = None) -> str:
        try:
            import keyring

            return keyring.get_password(SERVICE, name) or ""
        except ImportError:
            token = _read_all(path or _file()).get(name, "")
            return base64.b64decode(token).decode("utf-8") if token else ""
