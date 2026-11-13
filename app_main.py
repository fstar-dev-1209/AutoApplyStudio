"""Application Studio entry point.

Run:
    python app_main.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _configure_webengine_env() -> None:
    """Avoid Chromium GPU/EGL crashes on Linux VMs and headless-ish setups."""
    if sys.platform != "linux":
        return
    if os.environ.get("APPLICATION_STUDIO_WEBENGINE_GPU", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    extra = (
        "--disable-gpu "
        "--disable-gpu-compositing "
        "--disable-gpu-sandbox "
        "--no-sandbox "
        "--use-angle=swiftshader"
    )
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{current} {extra}".strip() if current else extra


def _configure_proxy() -> None:
    """Route the embedded chat browser through the proxy chosen in Settings (read early: Chromium takes it at launch)."""
    try:
        import json

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from core.paths import config_dir
        from core.settings_store import normalize_proxy_url

        raw = json.loads((config_dir() / "settings.json").read_text(encoding="utf-8"))
        proxy = normalize_proxy_url(str(raw.get("web_proxy") or ""))
    except Exception:  # noqa: BLE001 - no settings yet, or unreadable: start without a proxy
        return
    if not proxy:
        return
    flag = f"--proxy-server={proxy}"
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if "--proxy-server" not in current:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{current} {flag}".strip()


def _enable_windows_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        return


def _set_app_user_model_id() -> None:
    """Give Windows a stable identity so the taskbar shows our icon (not python.exe's) and groups our windows."""
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll

        windll.shell32.SetCurrentProcessExplicitAppUserModelID("AutoApplyStudio.Desktop")
    except Exception:  # noqa: BLE001
        return


def main() -> int:
    # Ensure project root is on sys.path for frozen and source runs
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    _enable_windows_dpi()
    _set_app_user_model_id()
    _configure_webengine_env()
    _configure_proxy()

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        if sys.platform == "linux" and os.environ.get(
            "APPLICATION_STUDIO_WEBENGINE_GPU", ""
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL)
    except ImportError:
        print(
            "PySide6 is required for Auto Apply Studio.\n"
            "Install dependencies:\n\n"
            "    python -m pip install -r requirements.txt\n",
            file=sys.stderr,
        )
        return 1

    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except ImportError:
        print(
            "PySide6 WebEngine is required for Claude web chat.\n"
            "Install: python -m pip install PySide6-WebEngine\n",
            file=sys.stderr,
        )
        return 1

    from ui.main_window import MainWindow
    from ui.theme import apply_theme, resolve_theme
    from core.settings_store import SettingsStore

    app = QApplication(sys.argv)
    app.setApplicationName("Auto Apply Studio")
    app.setOrganizationName("AutoApplyStudio")
    app.setStyle("Fusion")
    from ui.icons import brand_icon

    app.setWindowIcon(brand_icon())

    settings = SettingsStore()
    # Prefer explicit light/dark from settings (default light)
    apply_theme(app, resolve_theme(settings.settings.theme, app), settings.settings.density)

    from ui.lock_dialogs import unlock_at_launch

    if not unlock_at_launch(settings):  # the app password, when one is set
        return 0

    window = MainWindow()
    window.show()
    _watch_for_freezes(app)
    return app.exec()


def _watch_for_freezes(app) -> None:
    """Write where every thread is to hang_debug.log whenever the window stops answering for 15 seconds.

    Windows closes a window that stays frozen, which ends the whole bot; the record says what froze it."""
    import faulthandler
    import threading
    import time

    from PySide6.QtCore import QTimer

    from core.paths import config_dir

    beat = {"at": time.monotonic()}
    timer = QTimer(app)
    timer.setInterval(1000)
    timer.timeout.connect(lambda: beat.__setitem__("at", time.monotonic()))
    timer.start()
    path = config_dir() / "hang_debug.log"

    def watch() -> None:
        reported = 0.0
        while True:
            time.sleep(3)
            frozen_for = time.monotonic() - beat["at"]
            if frozen_for > 15 and time.monotonic() - reported > 30:
                reported = time.monotonic()
                try:
                    if path.exists() and path.stat().st_size > 400_000:
                        path.unlink()
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] window frozen for {frozen_for:.0f}s\n")
                        fh.flush()
                        faulthandler.dump_traceback(file=fh, all_threads=True)
                except Exception:  # noqa: BLE001 - diagnostics only
                    pass

    threading.Thread(target=watch, name="freeze-watch", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
