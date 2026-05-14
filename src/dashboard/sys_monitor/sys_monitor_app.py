"""
sys_monitor_app — single-process .exe entry point.

Lifecycle:
  - When the .exe (or python script) is launched, a tiny Tk window appears
    showing server status + buttons "Open in browser" and "Quit".
  - The HTTP server and the metrics refresh loop run as DAEMON threads of
    this same process. There is NO detached background process.
  - When the user closes the Tk window (X button or Quit), the process exits
    and TCP 8765 is released. Nothing is left running.

Bundling:
  pyinstaller --onefile --windowed --name sys_monitor \
              --add-data "index.html;." \
              --hidden-import psutil \
              sys_monitor_app.py
"""
from __future__ import annotations

import argparse
import logging
import socket
import subprocess as _sp
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

# 2026-05-12 FIX: sys_monitor.exe is built console=False (GUI subsystem). Every
# ssh.exe/powershell.exe subprocess (server.py spawns ~5 per 6s refresh) without
# CREATE_NO_WINDOW flag triggers Windows to allocate a NEW console window for
# each child → flurry of popups every refresh. Monkey-patch Popen globally so
# every subprocess from server.py and downstream inherits CREATE_NO_WINDOW.
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = _sp.Popen.__init__

    def _silent_popen_init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        return _orig_popen_init(self, *args, **kwargs)

    _sp.Popen.__init__ = _silent_popen_init

# Resolve resource path for both frozen .exe (PyInstaller _MEIPASS) and dev mode.
if getattr(sys, "frozen", False):
    _BASE = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    _BASE = Path(__file__).parent

sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(Path(__file__).parent))

# Import after path setup. `server` is the core engine module.
import server as srv_mod  # noqa: E402

# Override the HTML path so frozen-mode resolves correctly.
srv_mod.INDEX_HTML = _BASE / "index.html"

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/"


def port_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex((host, port)) != 0
    finally:
        s.close()


class AppState:
    def __init__(self) -> None:
        self.httpd: srv_mod._ReusableServer | None = None
        self.http_thread: threading.Thread | None = None
        self.cache: srv_mod._Cache | None = None

    def start_server(self, port: int = PORT, refresh_s: float = 6.0) -> None:
        # Bind HTTP first so the port is live within milliseconds. /api/metrics
        # returns 503 "warming up" until the cache's first gather() finishes.
        # 2026-05-13: this PyInstaller entry path bypasses srv_mod.serve(),
        # so we replicate auth-token init here. Without this _AUTH_TOKEN
        # stays "" and /api/kill rejects every request (and UI can't kill).
        try:
            srv_mod._init_auth_token()
        except Exception as exc:
            sys.stderr.write(f"auth_token init warn: {exc}\n")
        self.cache = srv_mod._Cache(refresh_s=refresh_s)
        srv_mod.CACHE = self.cache
        self.httpd = srv_mod._ReusableServer((HOST, port), srv_mod.Handler)
        self.http_thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="sys-monitor-http",
            daemon=True,
        )
        self.http_thread.start()
        # Start cache refresh loop in its own thread; first gather happens async.
        threading.Thread(
            target=self.cache._loop,
            name="sys-monitor-refresh",
            daemon=True,
        ).start()

    def shutdown(self) -> None:
        if self.cache is not None:
            self.cache._stop.set()
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass


def build_ui(state: AppState) -> None:
    root = tk.Tk()
    root.title("memex sys_monitor")
    root.geometry("360x180")
    root.resizable(False, False)
    try:
        root.iconbitmap(default="")  # fall back to Python icon
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    container = ttk.Frame(root, padding=14)
    container.pack(fill="both", expand=True)

    ttk.Label(container, text="memex sys_monitor", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    ttk.Label(container, text="监控 Win + Mac Studio + your-org GPU server", foreground="#555").pack(anchor="w", pady=(0, 8))

    status_var = tk.StringVar(value="启动中…")
    age_var = tk.StringVar(value="")
    ttk.Label(container, textvariable=status_var, font=("Consolas", 9)).pack(anchor="w")
    ttk.Label(container, textvariable=age_var, font=("Consolas", 9), foreground="#777").pack(anchor="w", pady=(0, 10))

    btn_row = ttk.Frame(container)
    btn_row.pack(fill="x")

    def open_browser() -> None:
        webbrowser.open(URL)

    open_btn = ttk.Button(btn_row, text="在浏览器中打开", command=open_browser)
    open_btn.pack(side="left")

    def quit_app() -> None:
        root.after(0, root.destroy)

    ttk.Button(btn_row, text="退出 (Quit)", command=quit_app).pack(side="right")

    def on_close() -> None:
        # Window X — same as Quit
        quit_app()
    root.protocol("WM_DELETE_WINDOW", on_close)

    def refresh_status() -> None:
        if state.cache is None or state.httpd is None:
            status_var.set("server not started")
        else:
            snap = state.cache.get()
            if snap is None:
                status_var.set(f"listening on {URL}  (warming up)")
                age_var.set("")
            else:
                up = []
                down = []
                for h in ("win", "mac", "ustc"):
                    if "error" in snap.get(h, {}):
                        down.append(h)
                    else:
                        up.append(h)
                status_var.set(f"listening on {URL}")
                age = time.time() - snap["ts"]
                age_var.set(f"up: {', '.join(up) or '-'}    down: {', '.join(down) or '-'}    age: {age:.1f}s")
        root.after(1000, refresh_status)

    root.after(200, refresh_status)
    # Auto-open browser the first time, only after server is bound.
    def auto_open_once() -> None:
        if state.cache is not None and state.cache.get() is not None:
            open_browser()
        else:
            root.after(500, auto_open_once)
    root.after(1500, auto_open_once)

    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--refresh", type=float, default=6.0)
    parser.add_argument("--no-ui", action="store_true",
                        help="self-test mode: start server, wait 8 s, exit")
    parser.add_argument("--no-auto-open", action="store_true",
                        help="don't auto-open browser at startup")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

    if not port_free(HOST, args.port):
        # Another instance already running. Just open browser and exit so
        # double-clicking the .exe doesn't accumulate processes.
        webbrowser.open(URL)
        return 0

    state = AppState()
    try:
        state.start_server(port=args.port, refresh_s=args.refresh)
    except OSError as exc:
        sys.stderr.write(f"failed to bind {HOST}:{args.port}: {exc}\n")
        return 2

    if args.no_ui:
        time.sleep(8)
        state.shutdown()
        return 0

    try:
        build_ui(state)
    finally:
        state.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
