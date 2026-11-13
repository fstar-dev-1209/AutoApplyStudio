"""Config and output path helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    override = os.environ.get("AUTO_APPLY_STUDIO_HOME", "").strip()
    if override:  # explicit data folder (portable use, tests, trying a build without touching real data)
        base = Path(override)
        base.mkdir(parents=True, exist_ok=True)
        return base
    # Deliberately not Application Studio's folder: the two apps must never share a database or secrets.
    if sys.platform == "win32":
        candidates = [
            Path.home() / "AppData" / "Local" / "AutoApplyStudio",
            app_dir() / ".auto-apply-studio",
        ]
    else:
        candidates = [
            Path.home() / ".config" / "auto-apply-studio",
            app_dir() / ".auto-apply-studio",
        ]
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return base
        except OSError:
            continue
    # Last resort: temp-like folder next to the app
    fallback = app_dir() / ".auto-apply-studio"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def default_output_root() -> Path:
    if sys.platform == "win32":
        return Path("E:/Remote")
    return Path.home() / "Remote"


def company_dir(output_root: Path, person_name: str, company: str) -> Path:
    from engine import safe_stem

    return output_root / safe_stem(person_name or "Candidate") / safe_stem(company or "Company")


def role_dir(output_root: Path, person_name: str, company: str, role: str) -> Path:
    from engine import safe_stem

    return company_dir(output_root, person_name, company) / safe_stem(role or "Role")


def company_folder_exists(output_root: Path, person_name: str, company: str) -> Path | None:
    """Return existing company folder if present (skip rule)."""
    folder = company_dir(output_root, person_name, company)
    if folder.is_dir():
        return folder
    # Case-insensitive match on Windows-style trees
    parent = folder.parent
    if not parent.is_dir():
        return None
    target = folder.name.casefold()
    for child in parent.iterdir():
        if child.is_dir() and child.name.casefold() == target:
            return child
    return None
