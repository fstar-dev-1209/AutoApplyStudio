"""Backward-compatible launcher — starts Application Studio.

Prefer:
    python app_main.py
"""

from __future__ import annotations

from app_main import main


if __name__ == "__main__":
    raise SystemExit(main())
