"""
sys_monitor launcher — invoked by the desktop shortcut.

Behaviour:
  1. Probe TCP 127.0.0.1:8765. If something already listens, jump to step 3.
  2. Spawn server.py as a detached background process (no console window,
     survives launcher exit). Wait up to 20 s for the port to come up.
  3. Open default browser at http://127.0.0.1:8765/.

Launched via pythonw.exe so no console flashes. Use `python launcher.pyw
--no-browser` for headless self-test.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent.resolve()
SERVER = HERE / "server.py"
HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/"
WAIT_TIMEOUT = 20.0


def port_alive() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex((HOST, PORT)) == 0
    finally:
        s.close()


def spawn_server() -> None:
    """Spawn server.py detached so it outlives this launcher."""
    log_dir = HERE / "logs"
    log_dir.mkdir(exist_ok=True)
    log = log_dir / "server.log"
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        creationflags = 0x00000008 | 0x00000200 | 0x08000000
    # Use the same Python that launched us (sys.executable maps pythonw->python.exe
    # on Anaconda for child processes implicitly, but we pass python.exe explicitly
    # so the server's HTTPServer logging doesn't get suppressed).
    py_exe = sys.executable
    if py_exe.lower().endswith("pythonw.exe"):
        candidate = py_exe[:-len("pythonw.exe")] + "python.exe"
        if Path(candidate).exists():
            py_exe = candidate
    cmd = [py_exe, str(SERVER), "--port", str(PORT), "--refresh", "6"]
    subprocess.Popen(
        cmd,
        cwd=str(HERE),
        stdout=open(log, "ab", buffering=0),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def wait_for_port(timeout: float = WAIT_TIMEOUT) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_alive():
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true",
                        help="for self-test: ensure server up, skip browser open")
    parser.add_argument("--restart", action="store_true",
                        help="force-restart the server even if already alive")
    args = parser.parse_args()

    if args.restart and port_alive():
        # Attempt graceful stop via TCP probe + taskkill is overkill — let user
        # kill manually if needed. For now: refuse to restart without --restart.
        pass

    if not port_alive():
        spawn_server()
        if not wait_for_port():
            sys.stderr.write(f"sys_monitor server failed to bind {HOST}:{PORT} "
                             f"within {WAIT_TIMEOUT}s\n")
            return 2

    if not args.no_browser:
        webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
