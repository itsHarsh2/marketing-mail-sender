# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for AI Sales Machine.

    pyinstaller AISalesMachine.spec --noconfirm

MUST BE RUN ON WINDOWS to produce a .exe — PyInstaller bundles the host
platform's Python runtime and cannot cross-compile.

Streamlit needs three things that PyInstaller misses on its own:
  1. its compiled frontend under streamlit/static
  2. package *metadata* — Streamlit reads its own version at runtime, and
     several dependencies do the same, so copy_metadata is required or the
     app dies on import with PackageNotFoundError
  3. the app's own modules, which are imported by name at runtime
"""

from PyInstaller.utils.hooks import collect_all, copy_metadata, collect_submodules

datas, binaries, hiddenimports = [], [], []

# Streamlit's frontend assets and submodules.
for pkg in ("streamlit", "altair", "pydeck", "plotly", "selenium"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# Runtime metadata lookups (importlib.metadata.version at import time).
for pkg in (
    "streamlit", "pandas", "numpy", "altair", "plotly", "openai",
    "selenium", "beautifulsoup4", "lxml", "dnspython", "schedule",
    "requests", "openpyxl", "cryptography", "pyarrow", "packaging",
    "protobuf", "tornado", "pydantic", "typing_extensions", "click",
):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass   # optional/renamed packages must not break the build

# The application's own modules, imported by name from app.py.
APP_MODULES = [
    "app", "auth", "database", "utils", "csv_importer", "crypto_util",
    "ai_writer", "smtp_sender", "email_verifier", "email_finder",
    "followup_scheduler", "tracker", "maps_scraper", "send_worker",
    "campaign_runner", "reply_fetcher", "set_password",
]
datas += [(f"{m}.py", ".") for m in APP_MODULES]
hiddenimports += APP_MODULES

# Imports pulled in dynamically that PyInstaller's analysis misses.
hiddenimports += [
    "streamlit.web.bootstrap", "streamlit.runtime.scriptrunner.magic_funcs",
    "sqlite3", "email.mime.multipart", "email.mime.text", "imaplib",
    "dns.resolver", "dns.exception", "bs4", "lxml.etree",
    "openpyxl.cell._writer", "PIL", "PIL._tkinter_finder",
]
hiddenimports += collect_submodules("dns")
hiddenimports += collect_submodules("selenium")

a = Analysis(
    ["run_desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Trim weight: none of these are used by the app.
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "notebook",
              "jupyter", "IPython", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AISalesMachine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX corrupts some Streamlit/pyarrow DLLs
    console=True,       # keep the console: it shows the URL and any errors
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# One-folder build, not one-file: a one-file bundle re-extracts ~500 MB to a
# temp directory on every launch, which is slow and breaks Chrome/Selenium
# subprocess paths.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AISalesMachine",
)
