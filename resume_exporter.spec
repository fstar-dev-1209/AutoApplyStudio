# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

block_cipher = None

docx_datas, docx_binaries, docx_hidden = collect_all("docx")
reportlab_datas, reportlab_binaries, reportlab_hidden = collect_all("reportlab")
lxml_datas, lxml_binaries, lxml_hidden = collect_all("lxml")
bs4_datas, bs4_binaries, bs4_hidden = collect_all("bs4")
tz_datas, tz_binaries, tz_hidden = collect_all("tzdata")  # IANA zones for interview time zones
httpx_datas, httpx_binaries, httpx_hidden = collect_all("httpx")
pyside_datas, pyside_binaries, pyside_hidden = collect_all("PySide6")
shiboken_datas, shiboken_binaries, shiboken_hidden = collect_all("shiboken6")
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")  # the browser driver Playwright talks to

a = Analysis(
    ["app_main.py"],
    pathex=[],
    binaries=(
        docx_binaries
        + reportlab_binaries
        + lxml_binaries
        + bs4_binaries
        + tz_binaries
        + httpx_binaries
        + pyside_binaries
        + shiboken_binaries
        + pw_binaries
    ),
    datas=(
        docx_datas
        + reportlab_datas
        + lxml_datas
        + bs4_datas
        + tz_datas
        + httpx_datas
        + pyside_datas
        + shiboken_datas
        + pw_datas
    ),
    hiddenimports=[
        "engine",
        "pdf_engine",
        "cover_letter",
        "core",
        "core.models",
        "core.paths",
        "core.settings_store",
        "core.queue_engine",
        "core.scraper",
        "core.claude_client",
        "ui",
        "ui.main_window",
        "ui.theme",
        "ui.queue_view",
        "ui.inspector",
        "ui.files_view",
        "ui.manual_json_view",
        "ui.settings_view",
        "ui.widgets",
        "core.application_db",
        "core.application_stage",
        "core.applications",
        "core.analytics",
        "core.files",
        "core.lifecycle",
        "core.migrations",
        "core.secrets",
        "core.timeutil",
        "core.job_export",
        "ui.icons",
        "ui.charts",
        "ui.system",
        "ui.applications_view",
        "ui.application_detail",
        "ui.application_dialogs",
        "ui.analytics_view",
        "ui.files_gallery",
        "ui.doc_preview",
        "ui.json_editor",
        "ui.titlebar",
        "PySide6.QtSvg",
        "PySide6.QtPdf",  # PDF thumbnails / preview in Generated Files
        "zoneinfo",
        "tzdata",
        "docx",
        "lxml",
        "reportlab",
        "httpx",
        "bs4",
        "PySide6",
    ]
    + docx_hidden
    + reportlab_hidden
    + lxml_hidden
    + bs4_hidden
    + tz_hidden
    + httpx_hidden
    + pyside_hidden
    + shiboken_hidden
    + pw_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AutoApplyStudio",
    icon="assets/app.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
