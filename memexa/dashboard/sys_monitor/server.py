"""
sys_monitor — multi-host CPU/RAM/GPU dashboard with per-pid GPU attribution.

Pulls metrics from
  - localhost via psutil
  - any host listed in ``MEMEXA_DASHBOARD_HOSTS`` via embedded ssh probe

The host list is a JSON array of ``{label, ssh_args}`` records (env value
example below) so the dashboard runs out-of-the-box with localhost only,
and lights up the remote panels as the operator wires them in.

  MEMEXA_DASHBOARD_HOSTS='[
    {"label":"mac",  "ssh_args":["mac-host"]},
    {"label":"gpu",  "ssh_args":["-J","mac-host","-p","22","user@gpu-host"]}
  ]'

Per host the snapshot carries CPU, RAM, top processes, per-user aggregate,
and (when the probe sees ``nvidia-smi``) a per-GPU breakdown with
PID→memory ranking and cross-referenced cmdline/user. macOS reports
MLX/vllm/ollama process residents since the M-series GPU shares memory
with CPU (unified memory).

Endpoints:
  GET /            → dashboard HTML
  GET /api/metrics → cached JSON snapshot (background refresh every N s)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

LOG = logging.getLogger("sys_monitor")

HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"
SSH_TIMEOUT = 12
KILL_LOG = HERE / "logs" / "kill_audit.log"

# ── SSH routing ───────────────────────────────────────────────────────────
# Each route is (label, ssh-args-list). The first viable route wins; on
# failure the next is tried and the successful one is cached for ROUTE_TTL_S.
ROUTE_TTL_S = 60.0


def _load_ssh_routes() -> Dict[str, List[Tuple[str, List[str]]]]:
    """Read SSH_ROUTES from ``MEMEXA_DASHBOARD_HOSTS`` (JSON array).

    Empty / unset env → empty dict (dashboard will only show localhost).
    Malformed JSON → log + empty dict (graceful degradation).

    JSON schema (per host can declare multiple ssh routes for failover):

        [
          {"label": "mac",
           "routes": [
             {"label": "primary",  "ssh_args": ["mac-host"]},
             {"label": "fallback", "ssh_args": ["mac-host.local"]}
           ]},
          {"label": "gpu",
           "routes": [
             {"label": "direct",  "ssh_args": ["-p","28022","user@gpu"]},
             {"label": "jump",    "ssh_args": ["-J","mac-host","-p","28022","user@gpu"]}
           ]}
        ]
    """
    raw = os.environ.get("MEMEXA_DASHBOARD_HOSTS", "").strip()
    if not raw:
        return {}
    try:
        records = json.loads(raw)
    except Exception as exc:
        LOG.warning("MEMEXA_DASHBOARD_HOSTS is not valid JSON: %s", exc)
        return {}
    out: Dict[str, List[Tuple[str, List[str]]]] = {}
    for rec in records or []:
        host_label = str(rec.get("label", "")).strip()
        routes_raw = rec.get("routes") or []
        if not host_label or not routes_raw:
            continue
        out[host_label] = [
            (str(r.get("label", "default")), list(r.get("ssh_args") or []))
            for r in routes_raw
            if r.get("ssh_args")
        ]
    return out


SSH_ROUTES: Dict[str, List[Tuple[str, List[str]]]] = _load_ssh_routes()


class HostRouter:
    """Picks an SSH route per host, cached for ROUTE_TTL_S after success."""

    def __init__(self, host: str, routes: List[Tuple[str, List[str]]]) -> None:
        self.host = host
        self.routes = routes
        self._lock = threading.Lock()
        self._sticky_idx: Optional[int] = None
        self._sticky_ts: float = 0.0
        self._last_label: Optional[str] = None

    def _start_order(self) -> List[int]:
        with self._lock:
            if (self._sticky_idx is not None
                    and (time.time() - self._sticky_ts) < ROUTE_TTL_S):
                start = self._sticky_idx
            else:
                start = 0
        order = [start] + [i for i in range(len(self.routes)) if i != start]
        return order

    def mark_success(self, idx: int) -> None:
        with self._lock:
            self._sticky_idx = idx
            self._sticky_ts = time.time()
            self._last_label = self.routes[idx][0]

    def mark_failure(self, idx: int) -> None:
        with self._lock:
            if self._sticky_idx == idx:
                self._sticky_idx = None

    def last_label(self) -> Optional[str]:
        with self._lock:
            return self._last_label


ROUTERS: Dict[str, HostRouter] = {
    name: HostRouter(name, routes) for name, routes in SSH_ROUTES.items()
}

# V5 ingest source layout: (key, input_rel, cards_rel, posted_rel, pg_source_name)
# Single source of truth, consumed by gather_memory_system pending and
# _parse_source_progress (cron_activity per-source cards display).
V5_SOURCE_SPECS: List[Tuple[str, str, str, str, str]] = [
    ("wechat",      "data/l0_v5/input_batches",
                     "data/l0_v5/work/cards_v2_wechat",   "data/l0_v5/work/posted_v5_wechat",  "wechat"),
    ("qq",          "data/l0_v5_qq/input_batches",
                     "data/l0_v5/work/cards_v2_qq",       "data/l0_v5/work/posted_v5_qq",      "qq"),
    ("email",       "data/l0_v5/input_batches_email",
                     "data/l0_v5/work/cards_v2_email",    "data/l0_v5/work/posted_v5_email",   "email"),
    ("browser",     "data/l0_v5/input_batches_browser",
                     "data/l0_v5/work/cards_v2_browser",  "data/l0_v5/work/posted_v5_browser", "browser_session"),
    ("claude_code", "data/l0_v5/input_batches_claude_full",
                     "data/l0_v5/work/cc_cards",          "data/l0_v5/work/cc_posted",         "claude_code"),
    ("audio",       "data/l0_v5/input_batches_audio",
                     "data/l0_v5/work/cards_v2_audio",    "data/l0_v5/work/posted_v5_audio",   "audio"),
]

# --------------------------------------------------------------------------
# Remote probe (Mac OR your-org). Shipped over ssh as stdin to `python3 -`.
# Self-contained, stdlib only, ~5 KB. Returns one JSON object on stdout.
# --------------------------------------------------------------------------
REMOTE_PROBE = r"""
import json, os, subprocess, sys, time, collections

def sh(cmd, timeout=4):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, shell=isinstance(cmd, str))
        return r.stdout
    except Exception:
        return ""

uname = sh(["uname"]).strip().lower()
is_mac = uname == "darwin"
is_linux = uname == "linux"

# ---- CPU% (instant) ------------------------------------------------------
cpu_pct = 0.0
if is_mac:
    out = sh(["top", "-l", "1", "-s", "0"])
    for line in out.splitlines():
        if "CPU usage" in line:
            try:
                parts = line.replace("%", "").split(":")[1].split(",")
                u = float(parts[0].strip().split()[0])
                s = float(parts[1].strip().split()[0])
                cpu_pct = round(u + s, 1)
            except Exception:
                pass
            break
elif is_linux:
    def stat():
        with open("/proc/stat") as f:
            v = f.readline().split()
        nums = [int(x) for x in v[1:]]
        idle = nums[3] + nums[4]
        total = sum(nums)
        return idle, total
    i1, t1 = stat(); time.sleep(0.25); i2, t2 = stat()
    dt = t2 - t1
    cpu_pct = round(100.0 * (1 - (i2 - i1) / dt), 1) if dt > 0 else 0.0

# ---- Memory --------------------------------------------------------------
mem_used = mem_total = mem_wired = mem_compressed = 0
if is_mac:
    page = int(sh("vm_stat | awk '/page size of/ {print $8}'").strip() or 4096)
    def vm(field):
        line = [l for l in sh(["vm_stat"]).splitlines() if field in l]
        if not line: return 0
        return int(line[0].split(":")[1].strip().rstrip(".").replace(",", ""))
    active = vm("Pages active")
    wired = vm("Pages wired down")
    compressed = vm("Pages occupied by compressor")
    used_pages = active + wired + compressed
    mem_used = used_pages * page // 1024 // 1024
    mem_wired = wired * page // 1024 // 1024  # rough proxy for "GPU + kernel"
    mem_compressed = compressed * page // 1024 // 1024
    mem_total = int(sh(["sysctl", "-n", "hw.memsize"]).strip() or 0) // 1024 // 1024
elif is_linux:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])  # kB
    mem_total = info.get("MemTotal", 0) // 1024
    mem_avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
    mem_used = mem_total - mem_avail

# ---- Load + ncpu ---------------------------------------------------------
try:
    load_1 = round(os.getloadavg()[0], 2)
except Exception:
    load_1 = 0.0
ncpu = os.cpu_count() or 0

# ---- Processes -----------------------------------------------------------
# On macOS, `ps -o rss` reports resident pages but EXCLUDES IOSurface / Metal
# GPU allocations (unified memory). Activity Monitor uses phys_footprint
# (~ dirty+swapped+wired-per-proc), which `top -stats mem` exposes. We build
# a pid->phys_footprint map and prefer it over ps RSS on Mac so the dashboard
# matches Activity Monitor for MLX / BGE / Metal-heavy processes.
top_mem_by_pid = {}
if is_mac:
    raw_top = sh(["top", "-l", "1", "-n", "9999", "-stats", "pid,mem"], timeout=5)
    for line in raw_top.splitlines():
        parts = line.split()
        if len(parts) < 2: continue
        try:
            pid_i = int(parts[0])
        except ValueError:
            continue  # skip header / counter rows
        token = parts[1]
        try:
            if token.endswith("T"):
                mem_mb_i = int(float(token[:-1]) * 1024 * 1024)
            elif token.endswith("G"):
                mem_mb_i = int(float(token[:-1]) * 1024)
            elif token.endswith("M"):
                mem_mb_i = int(float(token[:-1]))
            elif token.endswith("K"):
                mem_mb_i = max(0, int(float(token[:-1]) // 1024))
            elif token.endswith("B"):
                mem_mb_i = 0  # tiny bytes
            else:
                mem_mb_i = int(float(token) // 1024 // 1024)
        except ValueError:
            continue
        top_mem_by_pid[pid_i] = mem_mb_i

if is_mac:
    ps_cmd = ["ps", "-A", "-o", "pid=,user=,pcpu=,rss=,args="]
else:
    ps_cmd = ["ps", "-eo", "pid=,user=,pcpu=,rss=,args="]
out = sh(ps_cmd, timeout=4)
rows = []
pid_to_proc = {}
for line in out.splitlines():
    line = line.strip()
    if not line: continue
    p = line.split(None, 4)
    if len(p) < 5: continue
    try:
        pid = int(p[0])
        ps_rss_mb = int(p[3]) // 1024
        mem_mb = top_mem_by_pid.get(pid, ps_rss_mb)
        rec = {
            "pid": pid,
            "user": p[1][:16],
            "cpu": float(p[2]),
            "mem_mb": mem_mb,
            "rss_mb": ps_rss_mb,
            "cmd": p[4][:140],
        }
        rows.append(rec)
        pid_to_proc[pid] = rec
    except (ValueError, IndexError):
        continue

# Linux: refine mem_mb from RSS to PSS for top-100 RSS owners.
# Rationale: `ps -o rss` (= /proc/PID/status VmRSS) counts every shared page
# under every process that maps it, so summing per-user totals over-states
# memory by 2-4x for vllm/python workloads. PSS (proportional set size) from
# /proc/PID/smaps_rollup is kernel-computed: shared pages are split among
# their mappers, so PSS is what `smem` reports and what really fits in RAM.
# Falls back to RSS when smaps_rollup is unreadable (kernel < 4.14 or
# permission denied on cross-user procs).
if is_linux and rows:
    rows.sort(key=lambda r: r["rss_mb"], reverse=True)
    for r in rows[:100]:
        try:
            with open(f"/proc/{r['pid']}/smaps_rollup", "r") as fh:
                for line in fh:
                    if line.startswith("Pss:"):
                        # "Pss:    12345 kB"
                        kb = int(line.split()[1])
                        r["mem_mb"] = kb // 1024
                        break
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            pass
        except Exception:
            pass

# Per-user aggregate
user_agg = collections.defaultdict(lambda: {"cpu": 0.0, "mem_mb": 0, "n": 0})
for r in rows:
    u = user_agg[r["user"]]
    u["cpu"] += r["cpu"]; u["mem_mb"] += r["mem_mb"]; u["n"] += 1
users = sorted([{"user": k, **v, "cpu": round(v["cpu"], 1)} for k, v in user_agg.items()],
               key=lambda x: x["cpu"] + x["mem_mb"]/1024, reverse=True)[:8]

top_cpu = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:15]
top_mem = sorted(rows, key=lambda r: r["mem_mb"], reverse=True)[:15]

# Counters
workers = sum(1 for r in rows if "l0_worker_v2" in r["cmd"] or "phase_split_worker" in r["cmd"])
vllms   = sum(1 for r in rows if "vllm_simple" in r["cmd"] or "VLLM::EngineCore" in r["cmd"])
mlxs    = sum(1 for r in rows if "mlx_lm" in r["cmd"])

# ---- GPU (NVIDIA on Linux) ------------------------------------------------
# rich: per-card name/util/mem/temp/power + per-pid mem ranking
gpu_section = {"kind": "none", "cards": [], "card_count": 0}
if is_linux:
    raw_gpu = sh([
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu,"
        "temperature.gpu,power.draw,power.limit",
        "--format=csv,noheader,nounits",
    ])
    cards = []
    uuid_to_idx = {}
    for line in raw_gpu.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9: continue
        try:
            i = int(parts[0]); uuid = parts[1]
            cards.append({
                "i": i, "uuid": uuid, "name": parts[2],
                "mem_used_mb": int(parts[3]), "mem_total_mb": int(parts[4]),
                "util": int(parts[5]),
                "temp_c": int(parts[6]) if parts[6] not in ("[N/A]","") else None,
                "power_w": float(parts[7]) if parts[7] not in ("[N/A]","") else None,
                "power_limit_w": float(parts[8]) if parts[8] not in ("[N/A]","") else None,
                "procs": [],
            })
            uuid_to_idx[uuid] = len(cards) - 1
        except ValueError:
            continue

    raw_apps = sh([
        "nvidia-smi",
        "--query-compute-apps=pid,used_memory,gpu_uuid",
        "--format=csv,noheader,nounits",
    ])
    for line in raw_apps.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3: continue
        try:
            pid = int(parts[0]); mem_mb = int(parts[1]); uuid = parts[2]
        except ValueError:
            continue
        idx = uuid_to_idx.get(uuid)
        if idx is None: continue
        proc_meta = pid_to_proc.get(pid, {})
        cards[idx]["procs"].append({
            "pid": pid,
            "user": proc_meta.get("user", "?"),
            "mem_mb": mem_mb,
            "cmd": proc_meta.get("cmd", "(no cmdline)")[:120],
        })
    # Sort each card's procs by mem desc
    for c in cards:
        c["procs"].sort(key=lambda x: x["mem_mb"], reverse=True)
    gpu_section = {"kind": "nvidia", "cards": cards, "card_count": len(cards)}

# ---- Mac unified-memory GPU-heavy processes ------------------------------
mac_gpu = None
if is_mac:
    # Apple Silicon GPU utilization via ioreg IOAccelerator (no sudo).
    # This is the same data Activity Monitor reads — Device Utilization %,
    # Renderer/Tiler %, GPU memory in-use vs allocated, last-submission PID.
    import re as _re
    gpu_stats = None
    raw_io = sh(["ioreg", "-r", "-c", "IOAccelerator", "-w", "0"], timeout=4)
    if raw_io:
        def _grab_int(key, default=0):
            m = _re.search(r'"' + _re.escape(key) + r'"\s*=\s*(-?\d+)', raw_io)
            if not m: return default
            try: return int(m.group(1))
            except ValueError: return default
        def _grab_str(key, default=""):
            m = _re.search(r'"' + _re.escape(key) + r'"\s*=\s*"([^"]+)"', raw_io)
            return m.group(1) if m else default
        gpu_stats = {
            "model": _grab_str("model"),
            "cores": _grab_int("gpu-core-count"),
            "util_pct": _grab_int("Device Utilization %"),
            "renderer_pct": _grab_int("Renderer Utilization %"),
            "tiler_pct": _grab_int("Tiler Utilization %"),
            "mem_alloc_mb": _grab_int("Alloc system memory") // 1024 // 1024,
            "mem_in_use_mb": _grab_int("In use system memory") // 1024 // 1024,
            "last_submission_pid": _grab_int("fLastSubmissionPID"),
            "recovery_count": _grab_int("recoveryCount"),
        }
        # Annotate the last-submitter with its cmd/user for the dashboard.
        last_pid = gpu_stats["last_submission_pid"]
        if last_pid:
            meta = pid_to_proc.get(last_pid)
            if meta:
                gpu_stats["last_submission_user"] = meta["user"]
                gpu_stats["last_submission_cmd"] = meta["cmd"][:120]

    # Heuristic: pick procs that hold meaningful memory AND match known GPU/LLM patterns
    patterns = ("mlx_lm", "mlx-lm", "vllm", "ollama", "llama-server", "llama.cpp",
                "bge_m3", "bge_reranker", "bge_", "torch", "Metal",
                "WindowServer", "WebKit", "Mediaanalysisd")
    cand = []
    for r in rows:
        cmd = r["cmd"]
        if any(pat in cmd for pat in patterns) and r["mem_mb"] >= 50:
            cand.append(r)
    cand.sort(key=lambda x: x["mem_mb"], reverse=True)
    mac_gpu = {
        "kind": "apple_unified",
        "procs": cand[:15],
        "wired_mb": mem_wired,
        "compressed_mb": mem_compressed,
        "stats": gpu_stats,
    }

out_obj = {
    "ok": True,
    "cpu": {
        "pct": cpu_pct,
        "load_1": load_1,
        "ncpu": ncpu,
        "mem_used_mb": mem_used,
        "mem_total_mb": mem_total,
    },
    "gpu": gpu_section,
    "mac_gpu": mac_gpu,
    "procs": {
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "users": users,
    },
    "counters": {
        "workers": workers,
        "vllms": vllms,
        "mlxs": mlxs,
    },
}
print(json.dumps(out_obj, ensure_ascii=False))
"""


def _no_window_flag() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _run_ssh_cmd(route_args: List[str], remote_cmd: List[str],
                 stdin: Optional[str] = None,
                 timeout: float = SSH_TIMEOUT) -> Tuple[int, str, str]:
    """Run a single ssh command via the given route args. Returns (rc, stdout, stderr).
    Forces UTF-8 decoding because Windows default codepage (GBK) chokes on remote
    Chinese error messages and on the embedded probe's Unicode output."""
    if not shutil.which("ssh"):
        return 127, "", "ssh not in PATH"
    try:
        proc = subprocess.run(
            ["ssh", *route_args, *remote_cmd],
            input=stdin.encode("utf-8") if stdin is not None else None,
            capture_output=True,
            timeout=timeout,
            creationflags=_no_window_flag(),
        )
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return -1, "", "ssh timeout"
    except Exception as exc:
        return -2, "", f"{type(exc).__name__}: {exc}"


def _run_ssh_probe(host_name: str) -> Dict[str, Any]:
    """Probe a host using the host router, trying each route in priority order."""
    router = ROUTERS[host_name]
    last_err: Dict[str, Any] = {"ok": False, "error": "no route tried"}
    for idx in router._start_order():
        label, args = router.routes[idx]
        rc, stdout, stderr = _run_ssh_cmd(args, ["python3", "-"], stdin=REMOTE_PROBE)
        if rc != 0:
            last_err = {"ok": False, "error": f"ssh rc={rc} via {label}",
                        "stderr": stderr.strip()[-200:], "route_tried": label}
            router.mark_failure(idx)
            continue
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                    payload["_route"] = label
                    router.mark_success(idx)
                    return payload
                except json.JSONDecodeError as exc:
                    last_err = {"ok": False, "error": f"json decode via {label}: {exc}",
                                "raw": stdout[-300:], "route_tried": label}
                    router.mark_failure(idx)
                    break
        else:
            last_err = {"ok": False, "error": f"no JSON line via {label}",
                        "route_tried": label}
            router.mark_failure(idx)
    return last_err


def ssh_kill(host_name: str, pid: int, signal: str) -> Dict[str, Any]:
    """Send a signal to a PID on a remote host via the cached route.
    Returns {"ok": True/False, "output"|"error", "route"}. The remote 'kill'
    sets exit code 1 when the PID doesn't exist — we capture stdout + the
    echo'd RC so the caller can tell the difference between connectivity
    failure (rc -1 timeout) and "PID not found" (rc 1, stdout has detail)."""
    router = ROUTERS[host_name]
    safe_pid = int(pid)
    safe_sig = {"TERM": "TERM", "KILL": "KILL", "INT": "INT", "HUP": "HUP"}.get(
        signal.upper(), "TERM")
    last: Dict[str, Any] = {"ok": False, "error": "no route attempted"}
    for idx in router._start_order():
        label, args = router.routes[idx]
        # Pass the kill as a single remote command string. If we used
        # ["bash","-c", "kill ..."] then ssh would split the args and bash
        # would only see "kill" as the command (the rest become $1, $2…).
        remote_shell = f"kill -{safe_sig} {safe_pid} 2>&1; echo RC=$?"
        rc, stdout, stderr = _run_ssh_cmd(args, [remote_shell], timeout=6.0)
        out = (stdout or "").strip()
        err = (stderr or "").strip()
        if rc == 0:
            router.mark_success(idx)
            # Check if `kill` itself reported success (RC=0 in echoed tail)
            if "RC=0" in out:
                return {"ok": True, "route": label,
                        "output": f"sent SIG{safe_sig} to PID {safe_pid} on {host_name}"}
            return {"ok": False, "route": label,
                    "error": f"remote kill failed: {out or 'no output'}"}
        router.mark_failure(idx)
        last = {"ok": False, "error": f"ssh rc={rc} via {label}",
                "stderr": err[-200:]}
    return last


def probe_win() -> Dict[str, Any]:
    # Two-pass scan so the dashboard mem column matches Task Manager.
    # Pass 1: cheap memory_info().rss (Working Set) for every process.
    # Pass 2: for the top ~80 by RSS, refine to memory_full_info().uss
    #   (Unique Set Size). USS strips shared DLL/page contribution and matches
    #   Task Manager → Processes → "Memory (active private working set)".
    # RSS over-counts shared pages (e.g. Weixin shows 515MB RSS vs 211MB USS),
    # which made the dashboard mem column read 1.2–2.5× higher than Task Mgr.
    # Pass 2 cost ≈ 1.2s for top-80; AccessDenied (SYSTEM-owned procs) falls
    # back to RSS.
    procs: List[psutil.Process] = []
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)
            procs.append(p)
        except Exception:
            continue
    cpu_pct = psutil.cpu_percent(interval=0.4)
    mem = psutil.virtual_memory()

    # Pass 1
    rows: List[Dict[str, Any]] = []
    proc_by_pid: Dict[int, psutil.Process] = {}
    for p in procs:
        if p.pid in (0, 4):
            continue
        try:
            with p.oneshot():
                cpu = p.cpu_percent(None)
                rss = p.memory_info().rss
                try:
                    user = (p.username() or "").split("\\")[-1][:16]
                except Exception:
                    user = ""
                try:
                    cmd = " ".join(p.cmdline()) or (p.name() or "")
                except Exception:
                    cmd = p.name() if hasattr(p, "name") else ""
                row = {
                    "pid": p.pid,
                    "user": user,
                    "cpu": round(cpu, 1),
                    "mem_mb": int(rss / 1024 / 1024),  # provisional, refined below
                    "rss_mb": int(rss / 1024 / 1024),  # kept for reference
                    "cmd": cmd[:140],
                }
                rows.append(row)
                proc_by_pid[p.pid] = p
        except Exception:
            continue

    # Pass 2: refine to USS for top 80 RSS owners (covers all display tables
    # and most of the per-user aggregate weight).
    rows.sort(key=lambda r: r["rss_mb"], reverse=True)
    for r in rows[:80]:
        p = proc_by_pid.get(r["pid"])
        if p is None:
            continue
        try:
            uss_mb = int(p.memory_full_info().uss / 1024 / 1024)
            r["mem_mb"] = uss_mb
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            # Keep RSS fallback for SYSTEM-owned (MemCompression, dwm, etc.)
            pass
        except Exception:
            pass
    from collections import defaultdict
    agg = defaultdict(lambda: {"cpu": 0.0, "mem_mb": 0, "n": 0})
    for r in rows:
        a = agg[r["user"] or "?"]
        a["cpu"] += r["cpu"]; a["mem_mb"] += r["mem_mb"]; a["n"] += 1
    users = sorted([{"user": k, **v, "cpu": round(v["cpu"], 1)} for k, v in agg.items()],
                   key=lambda x: x["mem_mb"], reverse=True)[:8]
    counters = {
        "workers": sum(1 for r in rows if "l0_worker_v2" in r["cmd"]
                       or "phase_split_worker" in r["cmd"]),
        "vllms": sum(1 for r in rows if "vllm_simple" in r["cmd"]),
        "mlxs": sum(1 for r in rows if "mlx_lm" in r["cmd"]),
    }
    return {
        "ok": True,
        "cpu": {
            "pct": round(cpu_pct, 1),
            "load_1": round(getattr(psutil, "getloadavg", lambda: (0.0,))()[0], 2),
            "ncpu": psutil.cpu_count(logical=True) or 0,
            "mem_used_mb": int(mem.used / 1024 / 1024),
            "mem_total_mb": int(mem.total / 1024 / 1024),
        },
        "gpu": {"kind": "none", "cards": [], "card_count": 0},
        "mac_gpu": None,
        "procs": {
            "top_cpu": sorted(rows, key=lambda r: r["cpu"], reverse=True)[:15],
            "top_mem": sorted(rows, key=lambda r: r["mem_mb"], reverse=True)[:15],
            "users": users,
        },
        "counters": counters,
    }


def gather() -> Dict[str, Any]:
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        fut_mac = ex.submit(_run_ssh_probe, "mac")
        fut_ustc = ex.submit(_run_ssh_probe, "ustc")
        fut_api = ex.submit(gather_api_usage)
        fut_mem = ex.submit(gather_memory_system)
        win = probe_win()
        mac = fut_mac.result()
        ustc = fut_ustc.result()
        api_usage = fut_api.result()
        memory_system = fut_mem.result()
    win["_route"] = "local"
    return {
        "ts": time.time(),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "win": win,
        "mac": mac,
        "ustc": ustc,
        "api_usage": api_usage,
        "memory_system": memory_system,
        "routes": {
            "mac": ROUTERS["mac"].last_label(),
            "ustc": ROUTERS["ustc"].last_label(),
        },
    }


# --------------------------------------------------------------------------
# API usage & memory system probes (added 2026-05-12)
# --------------------------------------------------------------------------
def _find_repo_root() -> Optional[Path]:
    """Walk up to find memexa repo root (has data/l0_v5 and tools/).

    When running as PyInstaller .exe, __file__ is in a temp extract dir.
    sys.executable points to the actual .exe at memexa/tools/sys_monitor/dist/sys_monitor.exe
    — walking up from there finds memexa/. Also accept env override.
    """
    env_override = os.environ.get("MEMEXA_REPO_ROOT")
    if env_override:
        p = Path(env_override).resolve()
        if (p / "data" / "l0_v5").is_dir():
            return p
    candidates: List[Path] = []
    try:
        candidates.append(Path(__file__).resolve())
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve())
    candidates.append(Path.cwd().resolve())
    for entry in candidates:
        for parent in [entry, entry.parent, *entry.parents]:
            try:
                if (parent / "data" / "l0_v5").is_dir() and (parent / "tools").is_dir():
                    return parent
            except (OSError, PermissionError):
                continue
    return None


# Lazy-computed (re-evaluated each gather call so missing repo isn't permanently cached as None)
def _repo() -> Optional[Path]:
    global _REPO_ROOT
    if _REPO_ROOT is None:
        _REPO_ROOT = _find_repo_root()
    return _REPO_ROOT


_REPO_ROOT: Optional[Path] = None

# 2026-05-13: per-startup auth token, set in _init_auth_token() called from serve().
# Consumed by /api/kill handler. Empty token rejects all destructive requests
# (used by --once mode and tests where no token file is written).
_AUTH_TOKEN: str = ""

_API_USAGE_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_MEM_SYS_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_API_USAGE_TTL_S = 25.0   # heavier scan, refresh less often
_MEM_SYS_TTL_S = 20.0


def gather_api_usage() -> Dict[str, Any]:
    """Aggregate your-org LiteLLM API usage from recent cards_v2_<src>/*.json.

    Each card meta has `gk_usage`+`ext_usage` token counts. We scan recent
    files (last 24h) to compute per-source and total tokens, request count,
    verdict distribution, and last-call timestamp.

    Cached for _API_USAGE_TTL_S.
    """
    now = time.time()
    if _API_USAGE_CACHE["data"] is not None and (now - _API_USAGE_CACHE["ts"]) < _API_USAGE_TTL_S:
        return _API_USAGE_CACHE["data"]
    repo = _repo()
    if repo is None:
        return {"ok": False, "error": "repo root not found"}
    cards_root = repo / "data" / "l0_v5" / "work"
    parse_fail_dir = cards_root / "parse_fail"
    if not cards_root.exists():
        return {"ok": False, "error": f"no cards root: {cards_root}"}

    cutoff_24h = now - 86400
    cutoff_1h = now - 3600
    by_source: Dict[str, Dict[str, Any]] = {}
    last_call_ts = 0.0
    last_call_source = ""
    total_24h = {"req": 0, "tok_in": 0, "tok_out": 0, "cards": 0}
    total_1h = {"req": 0, "tok_in": 0, "tok_out": 0, "cards": 0}
    verdict_dist = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "EMPTY": 0, "FAIL": 0}

    n_files_scanned = 0
    n_files_total = 0
    # 2026-05-13: cc driver uses legacy `cc_cards/` dir (not `cards_v2_cc/`).
    # Add it explicitly so API usage panel reflects ALL 6 sources, not just 3.
    # CEO observed: panel showed only qq/audio/wechat; cc/email/browser hidden.
    scan_dirs = list(cards_root.glob("cards_v2_*"))
    cc_cards_dir = cards_root / "cc_cards"
    if cc_cards_dir.is_dir():
        scan_dirs.append(cc_cards_dir)
    try:
        for src_dir in scan_dirs:
            if not src_dir.is_dir():
                continue
            files_with_mtime = []
            for entry in os.scandir(src_dir):
                if not entry.name.endswith(".json"):
                    continue
                try:
                    mt = entry.stat().st_mtime
                except OSError:
                    continue
                n_files_total += 1
                if mt >= cutoff_24h:
                    files_with_mtime.append((mt, entry.path))
            # Sort by recency, cap 500 per source (avoids 10k-file walks)
            files_with_mtime.sort(reverse=True)
            for mt, fpath in files_with_mtime[:500]:
                n_files_scanned += 1
                try:
                    d = json.loads(Path(fpath).read_text(encoding="utf-8"))
                except Exception:
                    continue
                meta = d.get("meta", {}) or {}
                src = meta.get("source") or src_dir.name.replace("cards_v2_", "")
                gk = meta.get("gk_usage", {}) or {}
                ext = meta.get("ext_usage", {}) or {}
                tok_in = int(gk.get("prompt_tokens", 0) or 0) + int(ext.get("prompt_tokens", 0) or 0)
                tok_out = int(gk.get("completion_tokens", 0) or 0) + int(ext.get("completion_tokens", 0) or 0)
                cards_n = len(d.get("cards", []) or [])
                v = meta.get("verdict") or ("EMPTY" if "skipped" in meta else "FAIL")
                bucket = by_source.setdefault(src, {
                    "req_24h": 0, "tok_in_24h": 0, "tok_out_24h": 0,
                    "cards_24h": 0, "high_24h": 0, "low_24h": 0, "medium_24h": 0,
                    "zero_card_on_nonlow_24h": 0,
                })
                bucket["req_24h"] += 1
                bucket["tok_in_24h"] += tok_in
                bucket["tok_out_24h"] += tok_out
                bucket["cards_24h"] += cards_n
                if v == "HIGH":
                    bucket["high_24h"] += 1
                elif v == "MEDIUM":
                    bucket["medium_24h"] += 1
                elif v == "LOW":
                    bucket["low_24h"] += 1
                # Audit 'extractor broke' signal: HIGH/MEDIUM verdict but 0 cards out.
                if v in ("HIGH", "MEDIUM") and cards_n == 0:
                    bucket["zero_card_on_nonlow_24h"] += 1
                total_24h["req"] += 1
                total_24h["tok_in"] += tok_in
                total_24h["tok_out"] += tok_out
                total_24h["cards"] += cards_n
                if v in verdict_dist:
                    verdict_dist[v] += 1
                if mt >= cutoff_1h:
                    total_1h["req"] += 1
                    total_1h["tok_in"] += tok_in
                    total_1h["tok_out"] += tok_out
                    total_1h["cards"] += cards_n
                if mt > last_call_ts:
                    last_call_ts = mt
                    last_call_source = src
    except Exception as exc:
        LOG.exception("gather_api_usage scan failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # Parse-fail dump count (LLM output quality indicator)
    parse_fail_recent_24h = 0
    parse_fail_total = 0
    try:
        if parse_fail_dir.exists():
            for entry in os.scandir(parse_fail_dir):
                if entry.name.endswith(".txt"):
                    parse_fail_total += 1
                    try:
                        if entry.stat().st_mtime >= cutoff_24h:
                            parse_fail_recent_24h += 1
                    except OSError:
                        pass
    except Exception:
        pass

    # Read models from env (loaded by cron_silent or directly)
    gk_model = os.environ.get("MEMEXA_your-org_GATEKEEPER_MODEL", "?")
    ext_model = os.environ.get("MEMEXA_your-org_EXTRACTOR_MODEL", "?")
    # If env not set, try reading the env file
    if gk_model == "?" or ext_model == "?":
        try:
            env_path = repo / "data" / "secrets" / "ustc_llm.env"
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k == "MEMEXA_your-org_GATEKEEPER_MODEL" and gk_model == "?":
                            gk_model = v
                        elif k == "MEMEXA_your-org_EXTRACTOR_MODEL" and ext_model == "?":
                            ext_model = v
        except Exception:
            pass

    # Pipeline health: 0-card rate on non-LOW verdicts (audit's canonical signal)
    nonlow_total = verdict_dist["HIGH"] + verdict_dist["MEDIUM"]
    zero_card_nonlow = sum(b.get("zero_card_on_nonlow_24h", 0) for b in by_source.values())
    zero_card_rate = (zero_card_nonlow / nonlow_total) if nonlow_total else 0.0
    # Health enum: green <10% zero-card, yellow 10-30%, red >30% (per audit §1.1 thresholds)
    if zero_card_rate < 0.10:
        health = "green"
    elif zero_card_rate < 0.30:
        health = "yellow"
    else:
        health = "red"

    result = {
        "ok": True,
        "endpoint": "https://api.llm.ustc.edu.cn/v1",
        "gatekeeper_model": gk_model,
        "extractor_model": ext_model,
        # NB: total_24h counts WIN-LOCAL extractions only — Phase B cards
        # extracted on your-org (4738 batches) do NOT show here. They're tracked
        # via the bank growth in memory_system.bank.total_nodes.
        "total_24h": total_24h,
        "total_1h": total_1h,
        "verdict_24h": verdict_dist,
        "by_source": by_source,
        "parse_fail_total": parse_fail_total,
        "parse_fail_24h": parse_fail_recent_24h,
        "last_call_ts": last_call_ts,
        "last_call_source": last_call_source,
        # 2026-05-12: renamed to avoid "sampling" misread. The numbers mean:
        #   _24h = cards_v2_*.json with mtime in last 24h (full count, no cap below 500/src)
        #   _total = total cards_v2_*.json files (historical, all-time)
        "n_files_24h": n_files_scanned,
        "n_files_total_alltime": n_files_total,
        # legacy aliases for backward compat
        "n_files_scanned": n_files_scanned,
        "n_files_total": n_files_total,
        "scope_note": "Win-local extraction calls only. your-org Phase B post bypasses Win.",
        "health": health,
        "zero_card_nonlow_24h": zero_card_nonlow,
        "zero_card_rate_24h": round(zero_card_rate, 3),
        "nonlow_total_24h": nonlow_total,
        "scanned_at": now,
    }
    _API_USAGE_CACHE.update({"ts": now, "data": result})
    return result


def _http_get_json(url: str, timeout: float = 4.0) -> Tuple[bool, Any]:
    """Fetch JSON via urllib (stdlib only). Returns (ok, value)."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, json.loads(body)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def gather_memory_system() -> Dict[str, Any]:
    """Aggregate the memexa memory system control plane:
      - hindsight :8888 banks/memory_full_v5 stats (PG node count)
      - outbox queue depth (data/outbox/*)
      - schtask_health (read cached data/schtask_health.json)
      - driver cursor freshness (data/backfill_v5_*_progress.json)
      - cards pending per source (input vs .posted markers approximate)

    Cached for _MEM_SYS_TTL_S.
    """
    now = time.time()
    if _MEM_SYS_CACHE["data"] is not None and (now - _MEM_SYS_CACHE["ts"]) < _MEM_SYS_TTL_S:
        return _MEM_SYS_CACHE["data"]
    repo = _repo()
    if repo is None:
        return {"ok": False, "error": "repo root not found"}

    out: Dict[str, Any] = {"ok": True, "scanned_at": now}

    # 1. Hindsight bank stats (HTTP GET, fast)
    # 2026-05-13: align with rest of codebase — accept MEMEXA_HINDSIGHT_URL env override
    # (other 9 callsites do this; dashboard was the only outlier hardcoded).
    bank_base = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
    bank_id = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")
    bank_url = f"{bank_base}/v1/default/banks/{bank_id}/stats"
    ok, val = _http_get_json(bank_url, timeout=3.0)
    if ok and isinstance(val, dict):
        # 2026-05-12: 暴露 consolidation 状态 (USAGE_MANUAL §B.5 / §C.-3 #3 自陈
        # "by design disabled in bank cutover phase"). 但 dashboard 之前完全不显示
        # last_consolidated_at + failed_consolidation 计数, 监控盲区.
        # consolidation_mode = "by_design_disabled" 时, failed=total 视为正常.
        out["bank"] = {
            "ok": True,
            "url": bank_url,
            "total_nodes": val.get("total_nodes") or val.get("nodes") or val.get("count") or 0,
            "total_links": val.get("total_links") or 0,
            "pending_operations": val.get("pending_operations") or 0,
            "failed_operations": val.get("failed_operations") or 0,
            "last_consolidated_at": val.get("last_consolidated_at"),
            "pending_consolidation": val.get("pending_consolidation") or 0,
            "failed_consolidation": val.get("failed_consolidation") or 0,
            # by_design_disabled: 整合 daemon 当前 disabled, recall 走 BGE 向量 + 自动 link
            "consolidation_mode": ("by_design_disabled"
                                   if val.get("last_consolidated_at") is None
                                   else "active"),
            "raw": val,
        }
    else:
        out["bank"] = {"ok": False, "url": bank_url, "error": str(val)}

    # 2. Outbox queue depth
    outbox_dir = repo / "data" / "outbox"
    # POSIX path to avoid GBK mojibake in JSON for Chinese-path display
    out["outbox"] = {"path": outbox_dir.as_posix(), "count": 0}
    try:
        if outbox_dir.exists():
            n = 0
            for entry in os.scandir(outbox_dir):
                if entry.is_file():
                    n += 1
            out["outbox"]["count"] = n
    except Exception as exc:
        out["outbox"]["error"] = f"{type(exc).__name__}: {exc}"

    # 2c. PG snapshot health — polls $MEMEXA_PG_SNAPSHOT_DIR on the remote host
    # for latest *.sql.gz and reports age + size + count. SSH adds ~1-2 s
    # latency; cached 5 min. Skipped if MEMEXA_PG_SNAPSHOT_DIR is unset.
    snap_cache = _PG_SNAPSHOT_CACHE
    snap_dir = os.environ.get("MEMEXA_PG_SNAPSHOT_DIR", "").strip()
    snap_host = os.environ.get("MEMEXA_PG_SNAPSHOT_HOST", "").strip()
    if not (snap_dir and snap_host):
        out["pg_snapshot"] = {"ok": False, "skipped": True,
                              "reason": "MEMEXA_PG_SNAPSHOT_DIR/HOST unset"}
    elif snap_cache["data"] is not None and (now - snap_cache["ts"]) < _PG_SNAPSHOT_TTL_S:
        out["pg_snapshot"] = snap_cache["data"]
    else:
        snap_data: Dict[str, Any] = {"ok": False, "checked_at": now}
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", snap_host,
                 f"ls -la {snap_dir}/v5_*.sql.gz 2>/dev/null | "
                 "awk '{print $5, $6, $7, $8, $9}'"],
                capture_output=True, timeout=10,
                # 2026-05-13: every other SSH subprocess uses _no_window_flag;
                # this PG snapshot poll was the lone exception, flashing console
                # windows every 5 min when cache expires.
                creationflags=_no_window_flag(),
            )
            lines = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
            snapshots = []
            for line in lines:
                parts = line.strip().split(None, 4)
                if len(parts) >= 5:
                    try:
                        size_b = int(parts[0])
                    except ValueError:
                        continue
                    snapshots.append({"size_b": size_b, "name": parts[-1]})
            if snapshots:
                # Latest = last in sorted ls order (timestamps in name sort lexicographically)
                snapshots.sort(key=lambda s: s["name"])
                latest = snapshots[-1]
                # Parse ts from filename "v5_YYYYMMDDTHHMMSSZ.sql.gz"
                import re as _re
                m = _re.search(r"v5_(\d{8})T(\d{6})Z", latest["name"])
                age_h = None
                if m:
                    import datetime as _dt
                    try:
                        snap_ts = _dt.datetime.strptime(
                            m.group(1) + m.group(2), "%Y%m%d%H%M%S"
                        ).replace(tzinfo=_dt.timezone.utc).timestamp()
                        age_h = round((now - snap_ts) / 3600, 1)
                    except Exception:
                        pass
                snap_data.update({
                    "ok": True,
                    "n_snapshots": len(snapshots),
                    "latest_name": latest["name"].split("/")[-1],
                    "latest_size_mb": round(latest["size_b"] / 1048576, 1),
                    "age_h": age_h,
                    # Health: green <24h, yellow 24-72h, red >72h or missing
                    "health": ("green" if age_h is not None and age_h < 24
                               else "yellow" if age_h is not None and age_h < 72
                               else "red"),
                })
            else:
                snap_data["error"] = "no snapshots found"
                snap_data["health"] = "red"
        except subprocess.TimeoutExpired:
            snap_data["error"] = "ssh timeout"
        except Exception as exc:
            snap_data["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        snap_cache["ts"] = now
        snap_cache["data"] = snap_data
        out["pg_snapshot"] = snap_data

    # 2b. v5 bank 24h growth tracker (NEW 2026-05-12)
    # Persist last-seen bank size + ts so we can compute delta over time.
    # This is what user actually cares about for "cards posted in last 24h".
    growth_path = repo / "memexa" / "data" / ".bank_size_history.jsonl"
    out["bank_growth"] = {"history_path": growth_path.as_posix()}
    try:
        current_total = (out.get("bank") or {}).get("total_nodes") or 0
        if current_total > 0:
            growth_path.parent.mkdir(parents=True, exist_ok=True)
            # Append current point
            with growth_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({"ts": now, "total": current_total}) + "\n")
            # 2026-05-13: prior bug — keep 2000 lines × 6s = 200 min retention,
            # but cutoff_24h below tried to find a sample 24h old → never had
            # data older than 3.3h, so "delta_24h" was actually "delta_3.3h".
            # Fix: keep enough for 24h+ at 6s cadence (~15k lines = ~1.5 MB).
            try:
                lines = growth_path.read_text(encoding="utf-8").strip().splitlines()
                MAX_LINES = 15000  # ≥ 25 h coverage at 6 s refresh
                if len(lines) > MAX_LINES:
                    growth_path.write_text("\n".join(lines[-MAX_LINES:]) + "\n",
                                            encoding="utf-8")
                    lines = lines[-MAX_LINES:]
            except Exception as exc:
                LOG.debug("bank_growth rotate failed: %s", exc)
                lines = []
            # Compute delta: oldest point within 24h vs current
            cutoff_24h = now - 86400
            cutoff_1h = now - 3600
            t0_24h = None
            t0_1h = None
            try:
                for line in lines:
                    try:
                        p = json.loads(line)
                    except Exception:
                        continue
                    t = p.get("ts", 0)
                    v = p.get("total", 0)
                    if t >= cutoff_24h and t0_24h is None:
                        t0_24h = (t, v)
                    if t >= cutoff_1h and t0_1h is None:
                        t0_1h = (t, v)
            except Exception:
                pass
            out["bank_growth"]["current"] = current_total
            if t0_24h:
                out["bank_growth"]["delta_24h"] = current_total - t0_24h[1]
                out["bank_growth"]["window_24h_s"] = int(now - t0_24h[0])
            if t0_1h:
                out["bank_growth"]["delta_1h"] = current_total - t0_1h[1]
                out["bank_growth"]["window_1h_s"] = int(now - t0_1h[0])
    except Exception as exc:
        out["bank_growth"]["error"] = f"{type(exc).__name__}: {exc}"

    # 3. schtask health — LIVE via _read_cron_health() (the 30s-cached PS query).
    # 2026-05-12 FIX: switched from data/schtask_health.json (refresher cron
    # 7.5h stale) to live Get-ScheduledTask. Old path-based snapshot kept as
    # fallback if PS query fails (e.g. on Mac dev test).
    try:
        live = _read_cron_health()
        tasks = live.get("tasks") or []
        # 2026-05-12 truth-align: 3-bucket (healthy / failed / disabled).
        # disabled 不算入 active 总数, 与 _read_cron_health 的 n_disabled 一致.
        healthy = failed = disabled = 0
        items = []
        for t in tasks:
            is_disabled = bool(t.get("is_disabled"))
            ok = bool(t.get("last_result_ok"))
            if is_disabled:
                disabled += 1
                bucket = "disabled"
                status = "disabled"
            elif ok:
                healthy += 1
                bucket = "healthy"
                status = "ok"
            else:
                failed += 1
                bucket = "failed"
                status = f"fail_rc={t.get('last_result')}"
            items.append({
                "name": t.get("name"),
                "status": status,
                "bucket": bucket,
                "last_result": t.get("last_result"),
                "state": t.get("state"),
                "is_disabled": is_disabled,
                "last_run": t.get("last_run"),
                "next_run": t.get("next_run"),
            })
        active_total = len(items) - disabled
        out["schtasks"] = {
            "ok": "error" not in live,
            "source": "live_powershell",
            "healthy": healthy, "failed": failed, "disabled": disabled,
            "critical": failed,  # critical = active failures only
            "total": len(items),
            "active_total": active_total,
            "items": items[:14],
            "snapshot_age_s": int(now - (live.get("fetched_at") or now)),
            "error": live.get("error"),
        }
    except Exception as exc:
        # Fallback to cached file if live PS fails
        schtask_path = repo / "data" / "schtask_health.json"
        out["schtasks"] = {
            "ok": False,
            "source": "live_failed_fallback_file",
            "error": f"{type(exc).__name__}: {exc}",
            "path": str(schtask_path),
        }

    # 4. Driver cursor freshness — prefer last_run_ts (driver heartbeat) over
    # last_built_ts (content-build cursor). They equal when content keeps flowing,
    # but diverge when a source has no new data for days (e.g. browser: no new URL
    # built in 4d → last_built_ts stale, but driver still runs every 6h and last_run_ts
    # stays fresh). Previously last_built_ts won → browser falsely flagged as 98h-dead.
    from datetime import datetime as _dt
    drivers: List[Dict[str, Any]] = []
    try:
        for progress in (repo / "data").glob("backfill_v5_*_progress.json"):
            try:
                d = json.loads(progress.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = progress.stem.replace("backfill_v5_", "").replace("_progress", "")
            last_f = 0.0
            # Driver heartbeat (ISO) takes priority; epoch fields are fallback.
            for key in ("last_run_ts", "last_run_epoch", "last_built_ts"):
                val = d.get(key)
                if val is None:
                    continue
                try:
                    last_f = float(val)
                    break
                except (TypeError, ValueError):
                    # Try ISO
                    try:
                        s = str(val).replace("Z", "+00:00")
                        last_f = _dt.fromisoformat(s).timestamp()
                        break
                    except Exception:
                        continue
            drivers.append({
                "name": name,
                "last_run_ts": last_f,
                "age_h": round((now - last_f) / 3600, 2) if last_f > 0 else None,
            })
    except Exception:
        pass
    drivers.sort(key=lambda x: x.get("age_h") if x.get("age_h") is not None else 99999)
    out["drivers"] = drivers

    # 5. Pending per source — proper SET DIFFERENCE, matching driver's
    # _list_pending_batches() in cron_e2e_sop §6.1:
    #   pending = input_bids - posted_local - cards_local - pg_truth
    # (input from rglob, not the buggy 2-level iterdir; PG truth via
    # pg_bid_cache.query_pg_existing_bids per cron's authoritative path).
    pending: List[Dict[str, Any]] = []
    # 2026-05-13: source_specs moved to module-level V5_SOURCE_SPECS so
    # cron_activity per-source cards display reuses the same single source of truth.
    source_specs = V5_SOURCE_SPECS

    def _scan_bids(dir_path: Path, suffix: str) -> set:
        """Collect batch_ids by stripping `suffix` from filenames in dir."""
        bids: set = set()
        if not dir_path.exists():
            return bids
        try:
            for entry in os.scandir(dir_path):
                if entry.name.endswith(suffix):
                    bids.add(entry.name[: -len(suffix)])
        except Exception:
            pass
        return bids

    # PG truth queried via the same cache module the driver uses.
    # Load via absolute path import to bypass memexa/__init__.py chain
    # (PyInstaller bundles strip transitive deps like asyncio/aiosqlite when
    # `memexa.core` is loaded as a package — direct file-path import avoids
    # the heavy package init).
    pg_bids_by_src: Dict[str, set] = {}
    pg_import_err: Optional[str] = None
    pbc_path = repo / "memexa" / "core" / "pg_bid_cache.py"
    _pbc = None
    if pbc_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_sysmon_pg_bid_cache", str(pbc_path)
            )
            if spec and spec.loader:
                _pbc = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(_pbc)
        except Exception as exc:
            pg_import_err = f"{type(exc).__name__}: {exc}"
            LOG.warning("pg_bid_cache direct-load failed: %s", pg_import_err)
    else:
        pg_import_err = f"pg_bid_cache.py not found at {pbc_path}"
    if _pbc is not None:
        for spec_tup in source_specs:
            pg_src = spec_tup[4]
            try:
                pg_bids_by_src[pg_src] = set(_pbc.query_pg_existing_bids(pg_src))
            except Exception as exc:
                pg_bids_by_src[pg_src] = set()
                LOG.warning("pg_bid_cache query failed for %s: %s", pg_src, exc)
    out["pg_import_err"] = pg_import_err

    for src, in_rel, cards_rel, post_rel, pg_src in source_specs:
        in_dir = repo / in_rel
        if not in_dir.exists():
            pending.append({"source": src, "input": 0, "posted": 0, "cards": 0,
                            "pg": 0, "pending": 0, "input_dir_missing": True})
            continue
        # input bids via rglob (handles nested <date>/<bid>/prompt.json AND
        # any deeper nesting the builders might produce)
        input_bids: set = set()
        try:
            for pj in in_dir.rglob("prompt.json"):
                input_bids.add(pj.parent.name)
        except Exception:
            pass
        posted_bids = _scan_bids(repo / post_rel, ".posted")
        cards_bids = _scan_bids(repo / cards_rel, ".json")
        pg_bids = pg_bids_by_src.get(pg_src, set())
        # Set difference: input that is NOT yet posted, NOT in cards, NOT in PG
        truly_pending = input_bids - posted_bids - cards_bids - pg_bids
        pending.append({
            "source": src,
            "input": len(input_bids),
            "posted": len(posted_bids),
            "cards": len(cards_bids),
            "pg": len(pg_bids),
            "pending": len(truly_pending),
            "pg_source_name": pg_src,
        })
    out["pending"] = pending

    _MEM_SYS_CACHE.update({"ts": now, "data": out})
    return out



def local_kill(pid: int, signal: str) -> Dict[str, Any]:
    """Kill a process on the local Windows machine."""
    try:
        p = psutil.Process(pid)
        cmd_preview = " ".join(p.cmdline())[:120]
        if signal.upper() == "KILL":
            p.kill()
        else:
            p.terminate()
        return {"ok": True, "route": "local", "output": f"sent {signal} to {pid}: {cmd_preview}"}
    except psutil.NoSuchProcess:
        return {"ok": False, "error": f"no such PID {pid}"}
    except psutil.AccessDenied:
        return {"ok": False, "error": f"access denied for PID {pid} "
                                       f"(SYSTEM-owned or admin required)"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def audit_kill(host: str, pid: int, signal: str, result: Dict[str, Any]) -> None:
    try:
        KILL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with KILL_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "host": host,
                "pid": pid,
                "signal": signal,
                "result": result,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Cache + HTTP
# --------------------------------------------------------------------------
class _Cache:
    def __init__(self, refresh_s: float) -> None:
        self.refresh_s = refresh_s
        self._lock = threading.Lock()
        self._snapshot: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()

    def get(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._snapshot

    def _set(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._snapshot = payload

    def refresh_once(self) -> None:
        try:
            self._set(gather())
        except Exception as exc:
            LOG.exception("gather failed: %s", exc)
            self._set({"ts": time.time(), "error": str(exc),
                       "win": {"ok": False, "error": "gather crashed"},
                       "mac": {"ok": False, "error": "gather crashed"},
                       "ustc": {"ok": False, "error": "gather crashed"}})

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            self.refresh_once()
            dt = time.time() - t0
            wait = max(0.5, self.refresh_s - dt)
            self._stop.wait(wait)

    def start(self) -> None:
        self.refresh_once()
        t = threading.Thread(target=self._loop, daemon=True, name="sys-monitor-refresh")
        t.start()


CACHE: Optional[_Cache] = None


# ── Graph memory query monitoring (2026-05-12) ───────────────────────────
# Reads memexa.core.memory_query CLI invocation log so dashboard can show
# 谁正在查询 / 最近查了什么 / 结果数 / latency.
#
# 2026-05-13 fix: PyInstaller frozen exe 把 __file__ 解析到 PyInstaller temp
# 提取目录 → _MEMEXA_ROOT = HERE.parent.parent 拿到 AppData\Local\memexa 假根.
# 结果 log_exists=False 永远空面板. 改用 _find_repo_root() 找真 repo (扫描
# data/l0_v5 + tools 标志目录).
def _resolve_log_paths() -> tuple[Path, Path]:
    """Resolve real workspace root for log files. Works in frozen exe."""
    root = _find_repo_root()
    if root is None:
        # 兜底: 用 HERE.parent.parent (开发模式可用)
        root = HERE.parent.parent
    return (
        root / "memexa" / "data" / "memory_query_log.jsonl",
        root / "data" / "phaseB_monitor" / "status.json",
    )


_QUERY_LOG, _PHASEB_STATUS = _resolve_log_paths()


def _read_graph_query_state(tail: int = 200) -> Dict[str, Any]:
    """Tail-read memory_query_log.jsonl, compute live + aggregate stats.

    Returns:
        {
          "queries_recent": List[{ts, subcmd, query, n_results, latency_ms, ok}],
          "active_now": bool,  # latest query started <60s ago
          "active_subcmd": str | None,
          "by_subcmd": List[{subcmd, n_calls, success_pct, zero_pct, avg_n,
                             p50_ms, p95_ms, last_ts}],
          "phaseb": {cards_count, expected_cards, pct_done, rate_per_sec, eta_iso,
                     gpu3_util_pct, last_check_ts},
          "log_path": str,
          "log_exists": bool,
        }
    """
    out: Dict[str, Any] = {
        "queries_recent": [],
        "active_now": False,
        "active_subcmd": None,
        "by_subcmd": [],
        "phaseb": {},
        "log_path": str(_QUERY_LOG),
        "log_exists": _QUERY_LOG.exists(),
    }

    if _QUERY_LOG.exists():
        try:
            lines = _QUERY_LOG.read_text(encoding="utf-8", errors="replace") \
                              .strip().splitlines()
            recent_raw = lines[-tail:]
            parsed: List[Dict[str, Any]] = []
            for line in recent_raw:
                try:
                    parsed.append(json.loads(line))
                except Exception:
                    continue

            # Newest first display, cap to 30 for the table
            out["queries_recent"] = list(reversed(parsed))[:30]

            # Active-now: latest record's ts within last 60s OR latency in flight
            if parsed:
                latest = parsed[-1]
                try:
                    import datetime as _dt
                    ts_str = (latest.get("ts") or "").strip()
                    if ts_str:
                        # ts is ISO local-time, no tz suffix
                        t_log = _dt.datetime.fromisoformat(ts_str)
                        # We don't know the tz, treat as local naive
                        age_s = (_dt.datetime.now() - t_log).total_seconds()
                        if 0 <= age_s < 60:
                            out["active_now"] = True
                            out["active_subcmd"] = latest.get("subcmd")
                except Exception:
                    pass

            # Per-subcmd aggregate
            buckets: Dict[str, Dict[str, Any]] = {}
            for e in parsed:
                sub = e.get("subcmd") or "?"
                b = buckets.setdefault(sub, {
                    "n": 0, "ok": 0, "zero": 0, "n_results_total": 0,
                    "latencies": [], "last_ts": "",
                })
                b["n"] += 1
                if e.get("ok"):
                    b["ok"] += 1
                n_r = int(e.get("n_results") or 0)
                b["n_results_total"] += n_r
                if n_r == 0:
                    b["zero"] += 1
                lat = int(e.get("latency_ms") or 0)
                if lat > 0:
                    b["latencies"].append(lat)
                if (e.get("ts") or "") > b["last_ts"]:
                    b["last_ts"] = e.get("ts") or ""

            def _pct(latencies: List[int], p: float) -> Optional[int]:
                if not latencies:
                    return None
                s = sorted(latencies)
                idx = max(0, min(len(s) - 1,
                                 int(round((p / 100.0) * (len(s) - 1)))))
                return s[idx]

            agg = []
            for sub, b in buckets.items():
                n = b["n"] or 1
                agg.append({
                    "subcmd": sub,
                    "n_calls": b["n"],
                    "success_pct": round(100.0 * b["ok"] / n, 1),
                    "zero_pct": round(100.0 * b["zero"] / n, 1),
                    "avg_n": round(b["n_results_total"] / n, 1),
                    "p50_ms": _pct(b["latencies"], 50),
                    "p95_ms": _pct(b["latencies"], 95),
                    "last_ts": b["last_ts"],
                })
            agg.sort(key=lambda x: -x["n_calls"])
            out["by_subcmd"] = agg
        except Exception:
            pass

    # Phase B progress (best-effort)
    if _PHASEB_STATUS.exists():
        try:
            pb = json.loads(_PHASEB_STATUS.read_text(encoding="utf-8"))
            out["phaseb"] = {
                "cards_count": pb.get("cards_count"),
                "expected_cards": pb.get("expected_cards"),
                "pct_done": pb.get("pct_done"),
                "rate_per_sec": pb.get("rate_per_sec"),
                "eta_iso": pb.get("eta_iso"),
                "gpu3_util_pct": pb.get("gpu3_util_pct"),
                "last_check_ts": pb.get("last_check_ts"),
            }
        except Exception:
            pass

    return out


_CRON_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_CRON_TTL = 30.0  # 30s — schtasks query is ~1-2s, cache to avoid hammer

_PG_SNAPSHOT_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_PG_SNAPSHOT_TTL_S = 300.0  # 5min — SSH to Mac is slow, snapshot only updates rarely

_CALENDAR_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_CALENDAR_TTL = 20.0

_DEAD_LETTER_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_DEAD_LETTER_TTL = 30.0

_IDENTITY_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_IDENTITY_TTL = 60.0


def _read_calendar_state() -> Dict[str, Any]:
    """Calendar daemon state snapshot (2026-05-13 NEW B1 panel).

    Combines:
      - calendar_index.json: active/cancelled/pinned counts
      - daemon_heartbeat.json: last tick stats (filter drops, llm_calls)
      - daemon_log.jsonl tail: recent veto hits (cancel_persistent /
        manually_pinned / origin_drops / responsibility_drops)
    """
    now = time.time()
    if (_CALENDAR_CACHE["data"] is not None and
            (now - _CALENDAR_CACHE["ts"]) < _CALENDAR_TTL):
        return _CALENDAR_CACHE["data"]
    repo = _repo()
    if repo is None:
        return {"ok": False, "error": "repo not found"}
    plan_dir = repo / "data" / "calendar_planning"

    out: Dict[str, Any] = {"ok": True, "scanned_at": now}

    # Index breakdown
    idx_path = plan_dir / "calendar_index.json"
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
            counts = {"active": 0, "pinned": 0, "persistent_cancel": 0,
                      "cancelled": 0, "deleted_via_reconcile": 0,
                      "resolved": 0, "other": 0}
            upcoming: List[Dict[str, Any]] = []
            for canon, rec in idx.items():
                status = rec.get("status") or "other"
                if status == "active":
                    counts["active"] += 1
                    if rec.get("manually_pinned"):
                        counts["pinned"] += 1
                    due = rec.get("due_iso", "")
                    summary = rec.get("summary", "")
                    upcoming.append({
                        "due": due, "summary": summary[:60],
                        "pinned": bool(rec.get("manually_pinned")),
                        "canon": canon,
                    })
                elif rec.get("cancel_persistent"):
                    counts["persistent_cancel"] += 1
                elif status == "cancelled_by_user":
                    counts["cancelled"] += 1
                elif status == "deleted_via_reconcile":
                    counts["deleted_via_reconcile"] += 1
                elif status == "resolved":
                    counts["resolved"] += 1
                else:
                    counts["other"] += 1
            upcoming.sort(key=lambda x: x["due"])
            out["counts"] = counts
            out["upcoming"] = upcoming[:20]
        except Exception as e:
            out["index_err"] = str(e)[:200]

    # Heartbeat
    hb_path = plan_dir / "daemon_heartbeat.json"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            last_ts = hb.get("ts", "")
            try:
                from datetime import datetime as _dt
                last = _dt.fromisoformat(last_ts.replace("Z", "+00:00"))
                age_s = (_dt.utcnow().replace(tzinfo=last.tzinfo) - last).total_seconds()
            except Exception:
                age_s = -1
            out["last_tick"] = {
                "ts": last_ts, "age_s": round(age_s, 1) if age_s >= 0 else None,
                "tick_n": hb.get("tick_n"),
                "ok": hb.get("ok"),
                "queries_made": (hb.get("queries") or {}).get("queries_made"),
                "queries_failed": (hb.get("queries") or {}).get("queries_failed"),
                "llm_calls": (hb.get("aggregation") or {}).get("llm_calls"),
                "cached_hits": (hb.get("aggregation") or {}).get("cached_hits"),
                "audience_drops": (hb.get("aggregation") or {}).get("audience_drops", 0),
                "responsibility_drops": (hb.get("aggregation") or {}).get("responsibility_drops", 0),
                "origin_drops": (hb.get("aggregation") or {}).get("origin_drops", 0),
                "unique_commitments": (hb.get("aggregation") or {}).get("unique_commitments"),
                "reconcile": hb.get("reconcile"),
            }
        except Exception as e:
            out["heartbeat_err"] = str(e)[:200]

    # Recent veto events (last 100 lines of daemon_log.jsonl)
    log_path = plan_dir / "daemon_log.jsonl"
    if log_path.exists():
        try:
            with log_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()[-500:]
            recent_evts: Dict[str, int] = {}
            from datetime import datetime, timezone, timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            for line in lines:
                try:
                    e = json.loads(line)
                    if e.get("ts", "") < cutoff:
                        continue
                    evt = e.get("event", "?")
                    recent_evts[evt] = recent_evts.get(evt, 0) + 1
                except Exception:
                    continue
            out["recent_24h_events"] = recent_evts
        except Exception as e:
            out["log_err"] = str(e)[:200]

    _CALENDAR_CACHE["data"] = out
    _CALENDAR_CACHE["ts"] = now
    return out


def _read_dead_letter_state() -> Dict[str, Any]:
    """Dead-letter audit across all 6 sources (B1 panel 2026-05-13).

    For each posted_v5_<source> dir, count .dead markers + classify by
    age. Read ext_raw of paired card if exists for diagnostic.
    """
    now = time.time()
    if (_DEAD_LETTER_CACHE["data"] is not None and
            (now - _DEAD_LETTER_CACHE["ts"]) < _DEAD_LETTER_TTL):
        return _DEAD_LETTER_CACHE["data"]
    repo = _repo()
    if repo is None:
        return {"ok": False, "error": "repo not found"}
    work_dir = repo / "data" / "l0_v5" / "work"

    by_source: List[Dict[str, Any]] = []
    total_dead = 0
    recent_dead: List[Dict[str, Any]] = []

    for marker_dir in work_dir.glob("posted_v5_*"):
        if not marker_dir.is_dir():
            continue
        src = marker_dir.name.replace("posted_v5_", "")
        dead_files = list(marker_dir.glob("*.dead"))
        recovered_files = list(marker_dir.glob("*.recovered"))
        n_dead = len(dead_files)
        n_recovered = len(recovered_files)
        by_source.append({
            "source": src, "dead": n_dead, "recovered": n_recovered,
        })
        total_dead += n_dead
        for df in dead_files[:5]:
            try:
                mt = df.stat().st_mtime
                age_h = round((now - mt) / 3600, 1)
                recent_dead.append({
                    "source": src, "batch_id": df.stem,
                    "age_h": age_h,
                    "reason": df.read_text(encoding="utf-8", errors="replace")[:80],
                })
            except OSError:
                continue
    # cc_posted is a separate naming (legacy)
    cc_dir = work_dir / "cc_posted"
    if cc_dir.is_dir():
        cc_dead = list(cc_dir.glob("*.dead"))
        if cc_dead:
            by_source.append({"source": "cc", "dead": len(cc_dead), "recovered": 0})
            total_dead += len(cc_dead)
            for df in cc_dead[:5]:
                try:
                    mt = df.stat().st_mtime
                    age_h = round((now - mt) / 3600, 1)
                    recent_dead.append({
                        "source": "cc", "batch_id": df.stem,
                        "age_h": age_h,
                        "reason": df.read_text(encoding="utf-8", errors="replace")[:80],
                    })
                except OSError:
                    continue

    recent_dead.sort(key=lambda x: x.get("age_h", 0))
    out = {
        "ok": True, "scanned_at": now,
        "total_dead": total_dead,
        "by_source": sorted(by_source, key=lambda x: -x["dead"]),
        "recent_dead": recent_dead[:10],
        "health": "ok" if total_dead == 0 else "lag" if total_dead <= 5 else "BACKLOG",
    }
    _DEAD_LETTER_CACHE["data"] = out
    _DEAD_LETTER_CACHE["ts"] = now
    return out


def _read_identity_state() -> Dict[str, Any]:
    """Identity manifest + canon: tag coverage (B1 panel 2026-05-13).

    Stats:
      - manifest entries (persons / organizations / inanimate / public_figures)
      - aliases.json reverse index size
      - PG canon: tag coverage % (queries hindsight stats)
    """
    now = time.time()
    if (_IDENTITY_CACHE["data"] is not None and
            (now - _IDENTITY_CACHE["ts"]) < _IDENTITY_TTL):
        return _IDENTITY_CACHE["data"]
    repo = _repo()
    if repo is None:
        return {"ok": False, "error": "repo not found"}
    data_dir = repo / "data"

    out: Dict[str, Any] = {"ok": True, "scanned_at": now}

    # Aliases JSON (faster to parse than yaml)
    aliases_path = data_dir / "identity_aliases.json"
    if aliases_path.exists():
        try:
            aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
            unique_canons = set()
            ambiguous = 0
            for v in aliases.values():
                if isinstance(v, str):
                    unique_canons.add(v)
                elif isinstance(v, list):
                    ambiguous += 1
                    for vv in v:
                        unique_canons.add(vv)
            out["aliases"] = {
                "n_surface_forms": len(aliases),
                "n_unique_canons": len(unique_canons),
                "n_ambiguous": ambiguous,
            }
        except Exception as e:
            out["aliases_err"] = str(e)[:200]

    # Manifest yaml (count keys roughly)
    manifest_path = data_dir / "identity_manifest.yaml"
    if manifest_path.exists():
        try:
            txt = manifest_path.read_text(encoding="utf-8")
            n_persons = txt.count("primary_name:")
            n_orgs = sum(1 for line in txt.splitlines() if line.strip().startswith("organizations:"))
            out["manifest"] = {
                "n_persons": n_persons,
                "size_bytes": manifest_path.stat().st_size,
                "mtime_h_ago": round((now - manifest_path.stat().st_mtime) / 3600, 1),
            }
        except Exception as e:
            out["manifest_err"] = str(e)[:200]

    _IDENTITY_CACHE["data"] = out
    _IDENTITY_CACHE["ts"] = now
    return out


def _read_cron_health() -> Dict[str, Any]:
    """Query Windows Task Scheduler for all memexa\\* tasks + cache 30s.

    Returns:
        {
          "tasks": [{name, state, last_run, last_result_hex, last_result_ok,
                     next_run, duration_min}],
          "n_total": int, "n_failing": int, "ts": iso,
          "fetched_at": float,
        }
    Failing = LastTaskResult not in (0, 0x00041301, 0x00041303).
    0x00041301 = SCHED_S_TASK_RUNNING (info, treat as ok — task is currently running)
    0x00041303 = SCHED_S_TASK_HAS_NOT_RUN (新注册 task 还没首跑, 良性)

    2026-05-12 truth-align: 之前把 0x40010004 算 ok 导致 BackfillPipeline 长期"老 issue"
    (HANDOFF §C.-12) 被 dashboard 沉默通过。现统一: 仅 rc=0 (成功完成) / 0x00041301
    (运行中) / 0x00041303 (新 task 未首跑) 算健康. 其他 info 码 (TERMINATED 0x00041306 /
    NOT_RUNNING 0x40010004) 都视为 failed, 与 schtask_health.py 判定一致.
    state=Disabled 任务进 disabled bucket, 不参与 ok/fail 计数.
    """
    now = time.time()
    if _CRON_CACHE["data"] is not None and (now - _CRON_CACHE["ts"]) < _CRON_TTL:
        return _CRON_CACHE["data"]

    OK_CODES = {0, 0x00041301, 0x00041303}
    out_data: Dict[str, Any] = {
        "tasks": [], "n_total": 0, "n_failing": 0,
        "ts": _dt_iso_now(), "fetched_at": now,
    }
    try:
        ps_cmd = (
            "Get-ScheduledTask -TaskPath '\\memexa\\*' -ErrorAction SilentlyContinue | "
            "ForEach-Object { $t=$_; $i=Get-ScheduledTaskInfo -InputObject $_; "
            "[PSCustomObject]@{"
            "Name=$t.TaskName; State=$t.State.ToString(); "
            "LastRun=$i.LastRunTime.ToString('o'); "
            "LastResult=('0x{0:X8}' -f $i.LastTaskResult); "
            "LastResultInt=$i.LastTaskResult; "
            "NextRun=$i.NextRunTime.ToString('o') "
            "} } | ConvertTo-Json -Compress -Depth 3"
        )
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, timeout=20,
        )
        out = r.stdout.decode("utf-8", errors="replace").strip()
        if not out:
            out_data["error"] = "empty powershell output"
            # 2026-05-12: DO NOT cache failed result (prevents 30s cache poison)
            return out_data
        # ConvertTo-Json may emit a single object or array
        raw = json.loads(out)
        if isinstance(raw, dict):
            raw = [raw]
        for t in raw:
            rc = int(t.get("LastResultInt") or 0)
            state = (t.get("State") or "").strip()
            is_disabled = state.lower() == "disabled"
            # ok 判定: 任务必须没被 Disabled, 且 rc 是真正的 success 码
            ok = (not is_disabled) and (rc in OK_CODES)
            out_data["tasks"].append({
                "name": t.get("Name"),
                "state": state,
                "last_run": t.get("LastRun") or "",
                "last_result": t.get("LastResult"),
                "last_result_ok": ok,
                "is_disabled": is_disabled,
                "next_run": t.get("NextRun") or "",
            })
        out_data["n_total"] = len(out_data["tasks"])
        out_data["n_disabled"] = sum(1 for t in out_data["tasks"] if t["is_disabled"])
        # n_failing 只算 active (非 Disabled) 且 rc 不在 OK_CODES 的
        out_data["n_failing"] = sum(
            1 for t in out_data["tasks"]
            if (not t["is_disabled"]) and (not t["last_result_ok"])
        )
    except Exception as exc:
        out_data["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    # 2026-05-12: only cache successful non-empty results to avoid poison
    if out_data.get("n_total", 0) > 0 and "error" not in out_data:
        _CRON_CACHE["ts"] = now
        _CRON_CACHE["data"] = out_data
    return out_data


def _dt_iso_now() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="seconds")


# ──────────────────────────────────────────────────────────────────────
# Cron Activity — 实时进度面板 (2026-05-13 NEW per user request)
# tails latest cron_orchestrator + audio_driver logs, exposes:
#   - current orchestrator driver in flight + per-driver durations of done
#   - audio session count + last sync ts (from on-disk artefacts)
# ──────────────────────────────────────────────────────────────────────
_ACTIVITY_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_ACTIVITY_TTL_S = 8.0  # cheap file tail, can refresh often


def _parse_orch_log(log_path: Path) -> Dict[str, Any]:
    """Parse latest cron_orchestrator log → drivers list + which is running."""
    import re
    out: Dict[str, Any] = {"path": str(log_path), "drivers": [],
                            "current": None, "start_ts": None, "end_ts": None,
                            "summary": None}
    if not log_path.exists():
        return out
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    # START 2026-05-13T06:30:01
    m = re.search(r"=== cron_silent START (\S+)", text)
    if m:
        out["start_ts"] = m.group(1)
    m = re.search(r"=== cron_silent END (\S+)", text)
    if m:
        out["end_ts"] = m.group(1)
    # FAILED drivers: ['qq', 'cc', 'structured']
    m = re.search(r"FAILED drivers:\s*(\[[^\]]+\])", text)
    if m:
        out["failed_drivers"] = m.group(1)
    # run_all host=win n=10 ok=7 fail=3
    m = re.search(r"run_all host=\S+ n=(\d+) ok=(\d+) fail=(\d+)", text)
    if m:
        out["summary"] = {"n": int(m.group(1)), "ok": int(m.group(2)),
                          "fail": int(m.group(3))}
    # Parse all dispatch/exit pairs to compute per-driver duration + current
    dispatches: Dict[str, Any] = {}
    for m in re.finditer(r"\[cron_orchestrator\] dispatch '(\S+?)'", text):
        dispatches[m.group(1)] = {"id": m.group(1), "started": True,
                                   "exit": None, "duration_ms": None}
    for m in re.finditer(r"\[cron_orchestrator\] '(\S+?)' exit=(-?\d+) duration=(\d+)ms", text):
        d = dispatches.setdefault(m.group(1), {"id": m.group(1)})
        d["exit"] = int(m.group(2))
        d["duration_ms"] = int(m.group(3))
    for m in re.finditer(r"\[cron_orchestrator\] SKIP (\S+)", text):
        d = dispatches.setdefault(m.group(1), {"id": m.group(1)})
        d["skipped"] = True
        d["exit"] = 0
    out["drivers"] = list(dispatches.values())
    # Current = dispatched but no exit + log no END marker
    if not out["end_ts"]:
        running = [d for d in out["drivers"] if d.get("started") and d.get("exit") is None]
        if running:
            out["current"] = running[-1]["id"]
    return out


def _parse_source_progress(repo: Path, src: str, in_rel: str,
                            cards_rel: str, post_rel: str,
                            input_count_override: Optional[int] = None) -> Dict[str, Any]:
    """Per-v5-source filesystem snapshot for Cron Activity panel.

    Returns input batches count + cards extracted (with latest mtime) +
    posted markers count. Lightweight (no PG query).

    input_batches dir layouts diverge per source:
      - wechat: input_batches/<wechat-tier>/<date>/<bid>/prompt.json (3-level)
      - qq/email/browser/cc/audio: <date>/<bid>/prompt.json (2-level)
    To avoid rglob (slow at 5k+ batches → stalls 8s TTL), the caller passes
    input_count_override from gather_memory_system's already-cached pending data.
    Falls back to bounded scandir up to depth 3 if no override.
    """
    out: Dict[str, Any] = {"source": src, "ok": True}
    try:
        if input_count_override is not None:
            out["input_batches"] = input_count_override
        else:
            in_dir = repo / in_rel
            # Bounded scandir to 3 levels: counts <bid> dirs (one containing prompt.json)
            in_count = 0

            def _scan_for_bids(p: Path, depth: int) -> int:
                if depth <= 0:
                    return 0
                n = 0
                try:
                    has_prompt = False
                    sub_dirs = []
                    for e in os.scandir(p):
                        if e.is_file() and e.name == "prompt.json":
                            has_prompt = True
                        elif e.is_dir():
                            sub_dirs.append(e.path)
                    if has_prompt:
                        return 1  # this is a bid dir
                    for sd in sub_dirs:
                        n += _scan_for_bids(Path(sd), depth - 1)
                except OSError:
                    pass
                return n

            if in_dir.exists():
                in_count = _scan_for_bids(in_dir, depth=3)
            out["input_batches"] = in_count
        cards_dir = repo / cards_rel
        cards_count = 0
        latest_card_mtime = 0.0
        if cards_dir.exists():
            for entry in os.scandir(cards_dir):
                if entry.name.endswith(".json"):
                    cards_count += 1
                    try:
                        latest_card_mtime = max(latest_card_mtime, entry.stat().st_mtime)
                    except OSError:
                        pass
        out["cards_extracted"] = cards_count
        out["latest_card_mtime"] = latest_card_mtime
        posted_dir = repo / post_rel
        posted = 0
        if posted_dir.exists():
            for entry in os.scandir(posted_dir):
                if entry.name.endswith(".posted"):
                    posted += 1
        out["posted_markers"] = posted
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _parse_audio_progress() -> Dict[str, Any]:
    """Audio-specific snapshot — extends per-source basics with transcripts info.

    Kept as separate function (vs purely calling _parse_source_progress) because
    audio is the only source with Win-side transcripts/ subdir for raw Whisper
    output. Other sources have no equivalent.
    """
    repo = _repo()
    if repo is None:
        return {"ok": False}
    out = _parse_source_progress(
        repo, "audio",
        "data/l0_v5/input_batches_audio",
        "data/l0_v5/work/cards_v2_audio",
        "data/l0_v5/work/posted_v5_audio",
    )
    try:
        trans_dir = repo / "data" / "audio" / "transcripts"
        n_sessions = 0
        latest_session = None
        latest_mtime = 0.0
        if trans_dir.exists():
            for entry in os.scandir(trans_dir):
                if entry.is_dir():
                    n_sessions += 1
                    try:
                        m = entry.stat().st_mtime
                        if m > latest_mtime:
                            latest_mtime = m
                            latest_session = entry.name
                    except OSError:
                        pass
        out["transcripts_n_sessions"] = n_sessions
        out["transcripts_latest_session"] = latest_session
        out["transcripts_latest_mtime"] = latest_mtime
    except Exception as exc:
        out.setdefault("error", f"{type(exc).__name__}: {exc}")
    return out


def _read_cron_activity() -> Dict[str, Any]:
    """Combined activity snapshot. Cached for _ACTIVITY_TTL_S."""
    now = time.time()
    if _ACTIVITY_CACHE["data"] is not None and (now - _ACTIVITY_CACHE["ts"]) < _ACTIVITY_TTL_S:
        return _ACTIVITY_CACHE["data"]
    repo = _repo()
    if repo is None:
        return {"ok": False, "error": "no repo"}
    log_dir = repo / "data" / "maintenance_logs"
    # find newest orchestrator log
    orch = {"drivers": [], "current": None}
    audio_log = None
    if log_dir.exists():
        orch_logs = sorted(log_dir.glob("memexa_core_cron_orchestrator_*.log"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        if orch_logs:
            orch = _parse_orch_log(orch_logs[0])
            orch["age_s"] = int(now - orch_logs[0].stat().st_mtime)
        audio_logs = sorted(log_dir.glob("tools_backfill_v5_audio_driver_*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if audio_logs:
            audio_log = {"path": str(audio_logs[0]),
                         "age_s": int(now - audio_logs[0].stat().st_mtime),
                         "size_bytes": audio_logs[0].stat().st_size}
    audio_progress = _parse_audio_progress()
    # 2026-05-13: per-source cards display (user req "我要看每一个来源的 cards 数量")
    # input batch counts borrowed from gather_memory_system pending data (cached,
    # accurate rglob count). Avoids redoing 5k+ file scans inside cron_activity's
    # 8s TTL. If mem_system cache is cold, fall back to bounded scandir.
    input_count_by_src: Dict[str, int] = {}
    try:
        mem_data = _MEM_SYS_CACHE.get("data")
        if mem_data and mem_data.get("ok"):
            for p in mem_data.get("pending", []):
                if isinstance(p, dict) and "source" in p:
                    input_count_by_src[p["source"]] = p.get("input", 0)
    except Exception:
        pass
    per_source: List[Dict[str, Any]] = []
    for src, in_rel, cards_rel, post_rel, _pg in V5_SOURCE_SPECS:
        override = input_count_by_src.get(src)
        per_source.append(_parse_source_progress(
            repo, src, in_rel, cards_rel, post_rel,
            input_count_override=override,
        ))
    result = {
        "ok": True,
        "scanned_at": now,
        "orchestrator": orch,
        "audio_driver_log": audio_log,
        "audio": audio_progress,
        "per_source": per_source,
    }
    _ACTIVITY_CACHE.update({"ts": now, "data": result})
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/metrics"):
            snap = CACHE.get() if CACHE else None
            if snap is None:
                self._send_json({"error": "cache warming up"}, code=503)
            else:
                self._send_json(snap)
            return
        if self.path.startswith("/api/cron_health"):
            try:
                self._send_json(_read_cron_health())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, code=500)
            return
        if self.path.startswith("/api/cron_activity"):
            # 2026-05-13 NEW: live per-driver progress + audio pipeline state
            # (request: "需要在控制面板上显示现在进行到哪里了，数据量如何")
            try:
                self._send_json(_read_cron_activity())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, code=500)
            return
        if self.path.startswith("/api/graph_queries"):
            # Live tail of memory_query_log.jsonl + active-now detection +
            # per-subcmd aggregate (last 200 records). 2026-05-12 added so the
            # sys_monitor panel can show 谁正在查询/最近查了什么/结果如何.
            try:
                self._send_json(_read_graph_query_state())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, code=500)
            return
        if self.path.startswith("/api/calendar_state"):
            # 2026-05-13 B1: calendar daemon state + active commitments +
            # 24h filter drop counters (audience/responsibility/origin/veto)
            try:
                self._send_json(_read_calendar_state())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, code=500)
            return
        if self.path.startswith("/api/dead_letter"):
            # 2026-05-13 B1: dead-letter audit (count by source + recent reasons)
            try:
                self._send_json(_read_dead_letter_state())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, code=500)
            return
        if self.path.startswith("/api/identity"):
            # 2026-05-13 B1: identity manifest coverage + aliases stats
            try:
                self._send_json(_read_identity_state())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)[:200]}, code=500)
            return
        if self.path == "/api/auth_token":
            # 2026-05-13: same-host-only token fetch for UI. server binds 127.0.0.1
            # by default, but be explicit — reject if Host header doesn't look like
            # localhost (paranoid; bind already enforces this).
            remote = self.client_address[0] if self.client_address else ""
            if remote not in ("127.0.0.1", "::1", "localhost"):
                self._send_json({"error": "forbidden: localhost only"}, code=403)
                return
            self._send_json({"token": _AUTH_TOKEN, "expires_at": None})
            return
        if self.path == "/api/kill_log":
            try:
                tail = []
                if KILL_LOG.exists():
                    lines = KILL_LOG.read_text(encoding="utf-8").splitlines()[-30:]
                    tail = [json.loads(l) for l in lines if l.strip()]
                self._send_json({"ok": True, "entries": tail})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, code=500)
            return
        if self.path in ("/", "/index.html"):
            try:
                body = INDEX_HTML.read_bytes()
            except FileNotFoundError:
                self._send_json({"error": "index.html missing"}, code=500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json({"error": "not found", "path": self.path}, code=404)

    def do_POST(self) -> None:
        if self.path != "/api/kill":
            self._send_json({"error": "not found"}, code=404)
            return
        # 2026-05-13: gate destructive endpoint behind startup-time random token.
        # Prior auth was only `confirm=yes-<host>-<pid>` — PID is public via
        # /api/metrics top_cpu so anyone reaching loopback could kill it.
        # Token stored at data/dashboard.token (0600 perm, regenerated each restart).
        global _AUTH_TOKEN
        provided = self.headers.get("X-Auth-Token", "")
        if not _AUTH_TOKEN or not _consteq(provided, _AUTH_TOKEN):
            self._send_json({"ok": False, "error":
                             "unauthorized: missing/invalid X-Auth-Token header"},
                            code=401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            req = json.loads(body) if body else {}
            host = str(req.get("host", "")).lower().strip()
            pid = int(req.get("pid", 0))
            signal = str(req.get("signal", "TERM")).upper()
            confirm = str(req.get("confirm", ""))
        except Exception as exc:
            self._send_json({"ok": False, "error": f"bad request: {exc}"}, code=400)
            return
        if host not in ("win", "mac", "ustc"):
            self._send_json({"ok": False, "error": f"unknown host '{host}'"}, code=400)
            return
        if pid <= 0:
            self._send_json({"ok": False, "error": "pid must be > 0"}, code=400)
            return
        if signal not in ("TERM", "KILL", "INT", "HUP"):
            self._send_json({"ok": False, "error": f"signal '{signal}' not allowed"}, code=400)
            return
        if confirm != f"yes-{host}-{pid}":
            self._send_json({"ok": False, "error":
                             "missing confirm token (expected yes-<host>-<pid>)"}, code=400)
            return
        LOG.warning("KILL request: host=%s pid=%s sig=%s", host, pid, signal)
        if host == "win":
            result = local_kill(pid, signal)
        else:
            result = ssh_kill(host, pid, signal)
        audit_kill(host, pid, signal, result)
        self._send_json(result, code=200 if result.get("ok") else 500)


class _ReusableServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _init_auth_token() -> None:
    """Generate per-startup random token, write 0600 to data/dashboard.token.

    Read by /api/kill handler. Anyone reaching loopback who can read this file
    can call kill (same trust boundary). Stored under workspace data/ so
    sys_monitor.exe + manual CLI can both read it.
    """
    import secrets
    global _AUTH_TOKEN
    _AUTH_TOKEN = secrets.token_urlsafe(24)
    repo = _repo()
    if repo is None:
        LOG.warning("auth_token: no repo root — destructive ops will reject all")
        return
    tok_path = repo / "data" / "dashboard.token"
    try:
        tok_path.parent.mkdir(parents=True, exist_ok=True)
        tok_path.write_text(_AUTH_TOKEN + "\n", encoding="utf-8")
        try:
            os.chmod(tok_path, 0o600)  # Win ignores, *nix enforces
        except OSError:
            pass
        LOG.info("auth token written to %s", tok_path.as_posix())
    except Exception as exc:
        LOG.warning("auth_token persist failed: %s", exc)


def _consteq(a: str, b: str) -> bool:
    """Constant-time string compare to avoid leaking via timing."""
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def serve(port: int, host: str = "127.0.0.1", refresh_s: float = 6.0) -> None:
    global CACHE
    _init_auth_token()
    CACHE = _Cache(refresh_s=refresh_s)
    CACHE.start()
    with _ReusableServer((host, port), Handler) as srv:
        LOG.info("sys_monitor up at http://%s:%d/  refresh=%.1fs", host, port, refresh_s)
        srv.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--refresh", type=float, default=6.0)
    parser.add_argument("--once", action="store_true", help="probe once, print JSON, exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.once:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print(json.dumps(gather(), indent=2, ensure_ascii=False))
        return 0
    try:
        serve(args.port, args.host, refresh_s=args.refresh)
    except KeyboardInterrupt:
        LOG.info("shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
