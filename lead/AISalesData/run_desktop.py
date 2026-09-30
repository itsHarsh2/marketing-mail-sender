"""
run_desktop.py — entry point for the packaged desktop build.

A Streamlit app cannot simply be frozen: `streamlit run` is a CLI that
re-executes your script, which does not survive PyInstaller. This launcher
starts the server in-process via Streamlit's bootstrap API instead, then
opens a browser at it.

Data lives next to the executable, not inside it — a frozen bundle unpacks
to a temporary directory that is wiped on exit, so a database written there
would vanish. AISALES_HOME pins everything (database, secrets, logs) beside
the .exe so it persists between runs.
"""

import os
import sys
import threading
import time
import webbrowser

PORT = int(os.environ.get("AISALES_PORT", "8501"))


def _app_home() -> str:
    """Directory beside the executable (or the source dir when not frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir() -> str:
    """Where the bundled code lives (PyInstaller's temp dir when frozen)."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _open_browser_when_ready(url: str) -> None:
    """Poll the port, then open a browser once the server answers."""
    import socket
    for _ in range(90):
        time.sleep(1)
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                return


def main() -> int:
    home = _app_home()
    os.environ["AISALES_HOME"] = home
    # The desktop build may offer first-run password setup in the UI.
    os.environ["AISALES_ALLOW_SETUP"] = "1"
    # Keep the database and logs beside the executable so they survive.
    os.environ.setdefault("SALES_DB_PATH", os.path.join(home, "sales_campaign.db"))
    os.makedirs(os.path.join(home, "logs"), exist_ok=True)
    os.chdir(home)

    app_path = os.path.join(_bundle_dir(), "app.py")
    if not os.path.exists(app_path):
        print(f"ERROR: app.py not found at {app_path}")
        return 1

    url = f"http://localhost:{PORT}"
    print("=" * 58)
    print("  AI Sales Machine")
    print(f"  Opening {url}")
    print("  Keep this window open. Close it to stop the app.")
    print("=" * 58)

    threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    from streamlit.web import bootstrap
    flag_options = {
        "server.port": PORT,
        "server.address": "127.0.0.1",   # never expose a desktop build
        "server.headless": True,
        "browser.gatherUsageStats": False,
        "global.developmentMode": False,
    }
    bootstrap.load_config_options(flag_options=flag_options)
    bootstrap.run(app_path, False, [], flag_options)
    return 0


if __name__ == "__main__":
    sys.exit(main())
