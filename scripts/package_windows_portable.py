#!/usr/bin/env python3
"""Assemble a portable Windows Application Studio folder from Linux.

Creates:
  release/ApplicationStudio-Windows/
  release/ApplicationStudio-Windows.zip

Includes a private Windows Python runtime + PySide6 + app dependencies.
No Windows machine or system Python install required on the target PC.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "release" / "ApplicationStudio-Windows"
PYTHON_VERSION = "3.12.8"
NUGET_URL = f"https://www.nuget.org/api/v2/package/python/{PYTHON_VERSION}"

# Windows wheels for the Application Studio stack
PACKAGES = [
    "python-docx",
    "lxml",
    "reportlab",
    "typing_extensions",
    "pillow",
    "PySide6",
    "PySide6-Addons",
    "PySide6-Essentials",
    "PySide6-WebEngine",
    "shiboken6",
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "idna",
    "certifi",
    "beautifulsoup4",
    "soupsieve",
    "sniffio",
    "tzdata",
]

APP_FILES = (
    "app_main.py",
    "app.py",
    "engine.py",
    "pdf_engine.py",
    "cover_letter.py",
    "requirements.txt",
)

APP_DIRS = ("core", "ui")


def main() -> int:
    if RELEASE_DIR.exists():
        shutil.rmtree(RELEASE_DIR)
    RELEASE_DIR.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="studio-win-") as tmp:
        tmp_path = Path(tmp)

        runtime = RELEASE_DIR / "runtime"
        runtime.mkdir()
        print(f"Downloading Windows Python {PYTHON_VERSION} (NuGet)...")
        nupkg = tmp_path / "python.nupkg"
        _download(NUGET_URL, nupkg)
        with zipfile.ZipFile(nupkg) as archive:
            archive.extractall(tmp_path / "nuget")
        tools = tmp_path / "nuget" / "tools"
        if not (tools / "python.exe").exists():
            raise SystemExit(f"python.exe missing from NuGet package: {tools}")
        for item in tools.iterdir():
            dest = runtime / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

        site = runtime / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        wheels_dir = tmp_path / "wheels"
        wheels_dir.mkdir()
        print("Downloading Windows wheels (PySide6 + exporters + HTTP)...")
        _download_wheels(wheels_dir)
        for wheel in sorted(wheels_dir.glob("*.whl")):
            print(f"  installing {wheel.name}")
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(site)

        app_dir = RELEASE_DIR / "app"
        app_dir.mkdir()
        for name in APP_FILES:
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, app_dir / name)
        for dirname in APP_DIRS:
            src = ROOT / dirname
            dest = app_dir / dirname
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(
                src,
                dest,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".application-studio"),
            )

        _write_launchers(RELEASE_DIR)
        _write_readme(RELEASE_DIR)

    required = [
        RELEASE_DIR / "runtime" / "pythonw.exe",
        RELEASE_DIR / "runtime" / "python.exe",
        RELEASE_DIR / "app" / "app_main.py",
        RELEASE_DIR / "app" / "engine.py",
        RELEASE_DIR / "app" / "core" / "queue_engine.py",
        RELEASE_DIR / "app" / "ui" / "main_window.py",
        RELEASE_DIR / "ApplicationStudio.bat",
    ]
    # PySide6 marker
    pyside_markers = list((RELEASE_DIR / "runtime" / "Lib" / "site-packages").glob("PySide6*"))
    if not pyside_markers:
        raise SystemExit("PySide6 was not installed into the Windows runtime site-packages.")

    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("Release incomplete. Missing:\n" + "\n".join(missing))

    zip_path = ROOT / "release" / "ApplicationStudio-Windows.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", RELEASE_DIR.parent, RELEASE_DIR.name)
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"\nCreated {zip_path} ({size_mb:.1f} MB)")
    print(f"Folder:  {RELEASE_DIR}")
    print("On Windows: unzip, then double-click ApplicationStudio.bat")
    print("No Python install required.")
    return 0


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "application-studio-packager/1.0"})
    with urllib.request.urlopen(request, timeout=600) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _download_wheels(dest: Path) -> None:
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--dest",
        str(dest),
        "--only-binary=:all:",
        "--python-version",
        "312",
        "--platform",
        "win_amd64",
        "--implementation",
        "cp",
        "--abi",
        "cp312",
        *PACKAGES,
    ]
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(dest),
            "--only-binary=:all:",
            "--python-version",
            "312",
            "--platform",
            "win_amd64",
            *PACKAGES,
        ]
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(
            "Could not download Windows wheels.\n"
            f"{completed.stdout}\n{completed.stderr}"
        )


def _write_launchers(release_dir: Path) -> None:
    bat = release_dir / "ApplicationStudio.bat"
    bat.write_text(
        "\r\n".join(
            [
                "@echo off",
                "setlocal",
                'cd /d "%~dp0"',
                'set "PYTHONHOME=%~dp0runtime"',
                'set "PYTHONPATH=%~dp0app;%~dp0runtime\\Lib\\site-packages"',
                'set "PATH=%~dp0runtime;%~dp0runtime\\DLLs;%~dp0runtime\\Scripts;%PATH%"',
                'start "" "%~dp0runtime\\pythonw.exe" "%~dp0app\\app_main.py"',
                "endlocal",
                "",
            ]
        ),
        encoding="utf-8",
        newline="",
    )

    console = release_dir / "ApplicationStudio-Console.bat"
    console.write_text(
        "\r\n".join(
            [
                "@echo off",
                "setlocal",
                'cd /d "%~dp0"',
                'set "PYTHONHOME=%~dp0runtime"',
                'set "PYTHONPATH=%~dp0app;%~dp0runtime\\Lib\\site-packages"',
                'set "PATH=%~dp0runtime;%~dp0runtime\\DLLs;%~dp0runtime\\Scripts;%PATH%"',
                '"%~dp0runtime\\python.exe" "%~dp0app\\app_main.py"',
                "if errorlevel 1 pause",
                "endlocal",
                "",
            ]
        ),
        encoding="utf-8",
        newline="",
    )


def _write_readme(release_dir: Path) -> None:
    (release_dir / "README.txt").write_text(
        "\r\n".join(
            [
                "Application Studio for Windows",
                "==============================",
                "",
                "Portable build — no Python install required.",
                "",
                "1. Keep this whole folder together after unzipping.",
                "2. Double-click ApplicationStudio.bat",
                "3. Open the Claude tab and sign in to claude.ai once.",
                "4. Open Settings, confirm output folder and prompt template.",
                "5. Use Queue to add job links and Start processing.",
                "",
                "If the window does not open, use ApplicationStudio-Console.bat",
                "and send the error text.",
                "",
                "Optional single-file EXE (build on a Windows PC with Python):",
                "  run build_release.bat in the source folder.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="",
    )


if __name__ == "__main__":
    raise SystemExit(main())
