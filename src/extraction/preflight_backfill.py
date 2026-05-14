"""Pre-flight checks for backfill kickoff (2026-05-04 fix CAUTION #4).

Run BEFORE kicking off batch_chat_extract.main with a wide --since-days
window. Verifies the runtime dependencies are healthy so a 30h-wall
backfill doesn't fail at hour 1 from a fixable preflight gap.

Checks (each is independent; report all):
  1. mlx_lm.server :18080 (Qwen 4bit)            — paired_eval primary
  2. mlx_lm.server :18081 (Gemma 12B 4bit)       — paired_eval secondary
  3. ssh primary-host reachable                         — required by mlx_lifecycle
  4. WeChat DB enc_keys present + WeixinDir set  — wechat_db.WeChatDBReader
  5. data/win_keystone_outbox/ writable          — fact landing
  6. data/ disk free ≥ 2 GB                      — outbox + traces
  7. trace_sink JSONL writable                   — observability

CLI:
  python -m src.extraction.preflight_backfill        # 0 if all pass
  python -m src.extraction.preflight_backfill --json # JSON output
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data"
_OUTBOX = _DATA / "win_keystone_outbox"
_SSH_HOST = os.environ.get("MEMEXA_MAC_SSH_HOST", "primary-host")


def _emit(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def check_mlx_port(port: int, timeout: float = 5.0) -> Tuple[bool, str]:
    """Probe http://localhost:{port}/v1/models on Mac via ssh."""
    try:
        r = subprocess.run(
            ["ssh", "-o", f"ConnectTimeout={int(timeout)}", _SSH_HOST,
             f"curl -sf -m {int(timeout)} http://localhost:{port}/v1/models"],
            capture_output=True, text=True, timeout=timeout + 5,
            encoding="utf-8", errors="replace",
        )
        if r.returncode == 0 and "data" in (r.stdout or ""):
            return True, f"alive (response {len(r.stdout)} bytes)"
        return False, f"exit={r.returncode} stderr_tail={(r.stderr or '')[-120:]}"
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except FileNotFoundError:
        return False, "ssh binary missing"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_ssh_reachable(timeout: float = 5.0) -> Tuple[bool, str]:
    """Confirm `ssh primary-host echo ok` works."""
    if shutil.which("ssh") is None:
        return False, "ssh binary missing on this host"
    try:
        r = subprocess.run(
            ["ssh", "-o", f"ConnectTimeout={int(timeout)}", _SSH_HOST, "echo ok"],
            capture_output=True, text=True, timeout=timeout + 5,
            encoding="utf-8", errors="replace",
        )
        if r.returncode == 0 and (r.stdout or "").strip() == "ok":
            return True, f"ssh {_SSH_HOST} reachable"
        return False, f"exit={r.returncode} stderr={(r.stderr or '')[-120:]}"
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_wechat_db() -> Tuple[bool, str]:
    """Verify WeChatDBReader can initialize + enc_keys + wxid_dir present."""
    try:
        sys.path.insert(0, str(_REPO))
        from src.wechat_db import WeChatDBReader
        r = WeChatDBReader()
        r.initialize()
        if not r.enc_keys:
            return False, "enc_keys empty (need wechat_db_keys.json with valid keys)"
        if not r.wxid_dir:
            return False, "wxid_dir not found (WeChat may not be running)"
        n_keys = len(r.enc_keys) if hasattr(r.enc_keys, "__len__") else 1
        return True, f"enc_keys={n_keys}; wxid_dir={str(r.wxid_dir)[-50:]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_outbox_writable() -> Tuple[bool, str]:
    """Outbox dir exists/creatable + write probe."""
    try:
        _OUTBOX.mkdir(parents=True, exist_ok=True)
        probe = _OUTBOX / f".preflight_probe_{int(time.time())}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        n_existing = sum(1 for _ in _OUTBOX.iterdir())
        return True, f"writable; {n_existing} existing files"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_disk_free(min_gb: float = 2.0) -> Tuple[bool, str]:
    """Free disk space on data/ partition ≥ min_gb."""
    try:
        usage = shutil.disk_usage(str(_DATA))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < min_gb:
            return False, f"only {free_gb:.2f} GB free (need ≥ {min_gb})"
        return True, f"{free_gb:.2f} GB free on {_DATA.drive or _DATA.anchor}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_trace_sink_writable() -> Tuple[bool, str]:
    """Confirm trace_sink can write a smoke event."""
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("backfill_preflight_ok", {"probe": "smoke"})
        return True, "trace_sink write ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def run_all() -> Dict[str, Any]:
    """Run all checks; return aggregate dict (never raises)."""
    started = time.time()
    checks: List[Dict[str, Any]] = []

    def _add(name: str, ok: bool, detail: str, severity: str = "fatal") -> None:
        checks.append({
            "name": name, "ok": ok, "detail": detail, "severity": severity,
        })

    ok_ssh, d_ssh = check_ssh_reachable()
    _add("ssh_primary-host_reachable", ok_ssh, d_ssh, severity="fatal")
    if ok_ssh:
        ok_q, d_q = check_mlx_port(18080)
        _add("mlx_qwen_18080_alive", ok_q, d_q, severity="fatal")
        ok_g, d_g = check_mlx_port(18081)
        _add("mlx_gemma_18081_alive", ok_g, d_g, severity="fatal")
    else:
        _add("mlx_qwen_18080_alive", False,
             "skipped: ssh_primary-host failed", severity="fatal")
        _add("mlx_gemma_18081_alive", False,
             "skipped: ssh_primary-host failed", severity="fatal")
    ok_db, d_db = check_wechat_db()
    _add("wechat_db_initialize", ok_db, d_db, severity="fatal")
    ok_ob, d_ob = check_outbox_writable()
    _add("outbox_writable", ok_ob, d_ob, severity="fatal")
    ok_disk, d_disk = check_disk_free(min_gb=2.0)
    _add("disk_free_2gb", ok_disk, d_disk, severity="warn")
    ok_tr, d_tr = check_trace_sink_writable()
    _add("trace_sink_writable", ok_tr, d_tr, severity="warn")

    fatal_fails = [c for c in checks if not c["ok"] and c["severity"] == "fatal"]
    warn_fails = [c for c in checks if not c["ok"] and c["severity"] == "warn"]
    overall_ok = len(fatal_fails) == 0

    summary = {
        "ok": overall_ok,
        "n_checks": len(checks),
        "n_pass": sum(1 for c in checks if c["ok"]),
        "n_fatal_fail": len(fatal_fails),
        "n_warn_fail": len(warn_fails),
        "elapsed_sec": round(time.time() - started, 2),
        "checks": checks,
    }
    if overall_ok:
        _emit("backfill_preflight_ok", {
            "n_checks": len(checks), "n_warn_fail": len(warn_fails),
        })
    else:
        _emit("backfill_preflight_failed", {
            "n_fatal_fail": len(fatal_fails),
            "fatal_names": [c["name"] for c in fatal_fails],
        })
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true",
                   help="output full JSON; default is human-readable")
    args = p.parse_args(argv)
    summary = run_all()
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(f"=== backfill preflight ({summary['elapsed_sec']}s) ===")
        for c in summary["checks"]:
            mark = "OK  " if c["ok"] else ("FAIL" if c["severity"] == "fatal" else "WARN")
            print(f"  [{mark}] {c['name']:<28} {c['detail']}")
        print(f"--- pass {summary['n_pass']}/{summary['n_checks']}; "
              f"fatal_fail={summary['n_fatal_fail']}; "
              f"warn_fail={summary['n_warn_fail']}")
        if summary["ok"]:
            print("READY: backfill may proceed.")
        else:
            print("BLOCK: fix fatal failures before backfill kickoff.")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
