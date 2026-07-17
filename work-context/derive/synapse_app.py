#!/usr/bin/env python3
"""Synapse desktop app — a native window over the unified local server.

Spawns derive/synapse_server.py (which starts the dashboard + planner behind one
origin), waits for it, then opens a native macOS WebView window. Closing the window
tears the whole stack down. No Chrome, no Node, no Rust — pywebview + the system
WebKit view.

    bin/synapse-app                 # window at the default port (8765)
    SYNAPSE_PORT=8790 bin/synapse-app
"""
import os, sys, subprocess, time, urllib.request, urllib.error
import webview

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "derive", "synapse_server.py")
ICNS = os.path.join(ROOT, "assets", "synapse.icns")
PORT = int(os.environ.get("SYNAPSE_PORT", 8765))


def _set_dock_icon():
    """Replace the generic Python rocket in the dock with the Synapse mark.
    (The definitive icon ships via the py2app bundle later; this covers the
    unbundled `bin/synapse-app` launch.)"""
    try:
        from AppKit import NSApplication, NSImage
        if os.path.exists(ICNS):
            img = NSImage.alloc().initByReferencingFile_(ICNS)
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:
        pass


def _wait_up(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    server = subprocess.Popen([sys.executable, SERVER, "--port", str(PORT)])
    try:
        if not _wait_up(PORT):
            sys.stderr.write("[synapse-app] server did not come up in time\n")
            server.terminate()
            sys.exit(1)
        webview.create_window("Synapse", f"http://127.0.0.1:{PORT}",
                              width=1440, height=920, min_size=(1024, 680))
        _set_dock_icon()
        webview.start()          # blocks until the window is closed
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except Exception:
            server.kill()


if __name__ == "__main__":
    main()
