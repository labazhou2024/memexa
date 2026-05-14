"""hindsight_outbox.py — Outbox primitive for PostToolUse Hook → Hindsight retain.

U2 (2026-04-26): JSONL append-only queue + cron-style reconciler.

Design (per plan_v2):
- Hook calls `enqueue(file_path, content_sha256, content_bytes)` → ≤5ms file write.
- Background `python -m src.core.hindsight_outbox drain` reads pending,
  POSTs to Hindsight retain (via hindsight_client), records operation_id.
- At-most-once-on-success / at-least-once-on-crash delivery semantics.
- File-lock sidecar (filelock library, timeout=5.0s) on whole enqueue
  critical section. NOT msvcrt range-lock on append (per
  feedback_windows_file_lock_range.md).
- Bounded growth: rotate at >5MB or >10000 entries; refuse enqueue at
  >10MB (returns False → hook falls back to legacy ingest).
- Crash safety: `_reclaim_zombies` resets in_flight entries older than
  TTL=300s (configurable via MEMEXA_OUTBOX_ZOMBIE_TTL).
- Security: outbox dir env override REQUIRES allowlisted parent set
  (per feedback_env_override_parent_allowlist.md HARD RULE).
  Drain re-validates file_path with `_is_memory_file` before reading.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Configuration — paths & limits
# --------------------------------------------------------------------------

# Zombie reclaim TTL aligned with 30-min cron schedule (2026-05-01 cadence change).
# Prior 300s assumed 5-min cron; raised to 1800s to avoid premature reclaim of
# in_flight entries that legitimately wait through one 30-min drain cycle.
# Override via MEMEXA_OUTBOX_ZOMBIE_TTL env if a faster cron is reinstated.
_DEFAULT_ZOMBIE_TTL_SEC = 1800.0
_DEFAULT_LOCK_TIMEOUT_SEC = 5.0
_DEFAULT_ROTATE_BYTES = 5 * 1024 * 1024  # 5 MB
_DEFAULT_ROTATE_ENTRIES = 10_000
_DEFAULT_REFUSE_BYTES = 10 * 1024 * 1024  # 10 MB
_DEFAULT_OUTBOX_FILE = "outbox.jsonl"
_DEFAULT_PID_FILE_NAME = "hindsight_outbox.pid"
_OUTBOX_SUBDIR = "hindsight_outbox"  # under .claude/harness/

# 5-step exponential backoff schedule (seconds). Configurable via injection
# in tests (per logic-reviewer L-3 — avoid 4+ hour test wall time).
_BACKOFF_SCHEDULE = [0, 30, 300, 1800, 14400]
_MAX_RETRIES = len(_BACKOFF_SCHEDULE)

# Status enum
STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_DONE = "done"
STATUS_DEAD_LETTER = "dead_letter"
_VALID_STATUSES = (STATUS_PENDING, STATUS_IN_FLIGHT, STATUS_DONE, STATUS_DEAD_LETTER)

# Required JSONL keys (schema validation on read; per coverage M-2)
_REQUIRED_KEYS = ("outbox_id", "file_path", "content_sha256", "status",
                  "retry_count", "enqueued_at")


class OutboxDirRejected(Exception):
    """Raised when MEMEXA_OUTBOX_DIR env points outside allowlisted parents."""


@dataclass
class OutboxEntry:
    """Single outbox row.

    Schema is round-trippable via asdict() / from_dict().
    """
    outbox_id: str
    file_path: str
    content_sha256: str
    status: str = STATUS_PENDING
    retry_count: int = 0
    enqueued_at: float = field(default_factory=time.time)
    in_flight_started_at: Optional[float] = None
    last_attempt_at: Optional[float] = None
    op_id: Optional[str] = None
    last_error: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OutboxEntry":
        return cls(
            outbox_id=str(d["outbox_id"]),
            file_path=str(d["file_path"]),
            content_sha256=str(d["content_sha256"]),
            status=str(d.get("status", STATUS_PENDING)),
            retry_count=int(d.get("retry_count", 0)),
            enqueued_at=float(d.get("enqueued_at", time.time())),
            in_flight_started_at=d.get("in_flight_started_at"),
            last_attempt_at=d.get("last_attempt_at"),
            op_id=d.get("op_id"),
            last_error=d.get("last_error"),
        )


# --------------------------------------------------------------------------
# Path resolution + parent allowlist (per security-reviewer S-1, S-4)
# --------------------------------------------------------------------------


def _workspace_root() -> Path:
    """Return claude workspace root: 3 parents up from this file."""
    # this file: <ws>/memexa/memexa/core/hindsight_outbox.py
    return Path(__file__).resolve().parents[3]


def _allowlisted_parents() -> List[Path]:
    """Allowed parent directories for outbox / pid file paths.

    Per feedback_env_override_parent_allowlist.md HARD RULE:
    env-var path overrides MUST enumerate allowed parent set.

    Per Stage-4 security-iter1-1 (HIGH) finding: do NOT trust os.environ
    for {TEMP, TMP, TMPDIR} — those env vars are also subprocess-env-
    inheritable, so an attacker who controls TEMP=/etc could bypass the
    allowlist. Instead use `tempfile.gettempdir()` which resolves the
    OS-level temp dir at first call (cached), and is not subprocess-
    overridable in the same way.
    """
    import tempfile
    out: List[Path] = []
    try:
        out.append((_workspace_root() / ".claude" / "harness").resolve())
    except Exception:
        pass
    # OS-resolved temp dir — Python's tempfile module computes this
    # from a hardened search (TMPDIR/TEMP env BUT also OS defaults like
    # /tmp on POSIX, %WINDIR%\TEMP on Windows). Using gettempdir() at
    # module-call time means the allowlist parent is locked at first
    # invocation and not re-read from env on each call.
    try:
        out.append(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass
    # POSIX /tmp explicit (covers WSL where tempfile may resolve to
    # something else if TMPDIR is set)
    try:
        if os.name != "nt" and os.path.isdir("/tmp"):
            out.append(Path("/tmp").resolve())
    except Exception:
        pass
    return out


def _is_under_allowlisted_parent(p: Path) -> bool:
    """Return True iff p is subpath of an allowlisted parent."""
    try:
        rp = p.resolve()
    except Exception:
        return False
    for parent in _allowlisted_parents():
        try:
            rp.relative_to(parent)
            return True
        except ValueError:
            continue
    return False


def _resolve_outbox_dir(env_dir: Optional[str] = None) -> Path:
    """Resolve target outbox directory, enforcing parent allowlist.

    Per security S-1: if env-dir provided, REQUIRE allowlist match.
    Default: <workspace>/.claude/harness/hindsight_outbox/.

    Raises OutboxDirRejected if env-dir falls outside allowlist.
    """
    chosen = env_dir or os.environ.get("MEMEXA_OUTBOX_DIR")
    if chosen:
        p = Path(chosen)
        if not _is_under_allowlisted_parent(p):
            _emit_trace(
                "outbox_dir_rejected_unsafe_parent",
                {"requested_dir": str(p)[:200],
                 "allowlisted_parents": [str(x)[:200]
                                         for x in _allowlisted_parents()]},
            )
            raise OutboxDirRejected(
                f"outbox dir {p} not under allowlisted parents "
                f"{[str(x) for x in _allowlisted_parents()]}"
            )
        return p
    # Default — guaranteed under workspace/.claude/harness/
    return _workspace_root() / ".claude" / "harness" / _OUTBOX_SUBDIR


def _resolve_pid_dir() -> Path:
    """Resolve PID file directory.

    Per security S-4: PID file is at FIXED workspace path, NOT
    user-controlled MEMEXA_OUTBOX_DIR. Test isolation uses
    MEMEXA_PID_FILE_DIR env (also allowlist-restricted).
    """
    env_dir = os.environ.get("MEMEXA_PID_FILE_DIR")
    if env_dir:
        p = Path(env_dir)
        if not _is_under_allowlisted_parent(p):
            raise OutboxDirRejected(
                f"PID file dir {p} not under allowlisted parents"
            )
        return p
    return _workspace_root() / ".claude" / "harness"


# --------------------------------------------------------------------------
# Trace event emission (best-effort, fail-soft)
# --------------------------------------------------------------------------


def _emit_trace(event: str, payload: Dict[str, Any]) -> None:
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:  # allow-silent
        pass


# --------------------------------------------------------------------------
# File lock helper
# --------------------------------------------------------------------------


@contextmanager
def _outbox_filelock(outbox_dir: Path,
                     timeout: float = _DEFAULT_LOCK_TIMEOUT_SEC):
    """Acquire FileLock sidecar (per L-5: explicit timeout).

    Yields True if acquired; False if timeout.
    Falls back to no-op (with trace) if filelock package unavailable.
    """
    lock_path = outbox_dir / ".outbox.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _emit_trace("outbox_lock_dir_mkdir_failed",
                    {"error": str(e)[:100]})
        yield False
        return
    try:
        from filelock import FileLock, Timeout  # type: ignore
    except ImportError:
        _emit_trace("outbox_filelock_missing", {"timeout": timeout})
        yield True  # advisory degrade
        return
    lock = FileLock(str(lock_path), timeout=timeout)
    try:
        with lock:
            yield True
    except Timeout:
        _emit_trace("outbox_filelock_timeout", {"timeout": timeout})
        yield False


# --------------------------------------------------------------------------
# Public API: enqueue
# --------------------------------------------------------------------------


def _outbox_id(file_path: str, content_sha256: str) -> str:
    """64-bit (16 hex char) outbox ID = sha256(file_path||content_sha256)."""
    h = hashlib.sha256()
    h.update(file_path.encode("utf-8"))
    h.update(b"\x00")
    h.update(content_sha256.encode("ascii"))
    return h.hexdigest()[:16]


def _outbox_path(outbox_dir: Path) -> Path:
    return outbox_dir / _DEFAULT_OUTBOX_FILE


def _file_size_bytes(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _refuse_due_to_size(p: Path) -> bool:
    return _file_size_bytes(p) >= _DEFAULT_REFUSE_BYTES


def enqueue(
    file_path: str,
    content_sha256: str,
    content_bytes: bytes,
    dir: Optional[str] = None,
) -> bool:
    """Append a pending outbox entry. Returns True on success, False on refuse.

    Semantics:
    - Holds outbox-global FileLock for write (timeout 5s; per L-5).
    - Refuses if outbox file >= 10 MB (caller should fall back to legacy).
    - Idempotent on duplicate (outbox_id, status=pending|in_flight): if
      a non-terminal entry with the same outbox_id already exists, skips
      append (returns True — not an error; per coverage AC-2 phrasing).
    - Always uses json.dumps (no f-string injection; per S-5).
    """
    try:
        outbox_dir = _resolve_outbox_dir(dir)
    except OutboxDirRejected as e:
        logger.warning("enqueue rejected: %s", e)
        return False
    try:
        outbox_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _emit_trace("outbox_enqueue_mkdir_failed",
                    {"dir": str(outbox_dir)[:200],
                     "error": str(e)[:100]})
        return False

    outbox_file = _outbox_path(outbox_dir)

    # Refuse-due-to-size check BEFORE acquiring lock (cheap)
    if _refuse_due_to_size(outbox_file):
        _emit_trace("outbox_full_refuse",
                    {"size_bytes": _file_size_bytes(outbox_file),
                     "limit_bytes": _DEFAULT_REFUSE_BYTES})
        return False

    obx_id = _outbox_id(file_path, content_sha256)
    entry = OutboxEntry(
        outbox_id=obx_id,
        file_path=file_path,
        content_sha256=content_sha256,
        status=STATUS_PENDING,
    )

    with _outbox_filelock(outbox_dir) as got:
        if not got:
            return False
        # Rotate-on-size if needed. Skip the count-based scan when file is
        # small (under 1 MB) — count scan is O(N), and only the byte-based
        # rotate threshold matters at small sizes. This keeps enqueue
        # latency bounded for AC-4 (p99 < 50ms on Windows NTFS).
        try:
            cur_size = _file_size_bytes(outbox_file)
            if cur_size >= _DEFAULT_ROTATE_BYTES:
                rotate_if_needed(outbox_dir)
        except Exception as e:  # never block enqueue on rotate failure
            _emit_trace("outbox_rotate_failed",
                        {"error": str(e)[:120]})

        # Dedup: O(1) per call via streaming targeted scan of recent entries
        # only. The hot path (PostToolUse) cares about idempotent enqueue
        # within a short burst window; older pending entries are reconciler-
        # owned. Bounded scan keeps p99 < 50ms even when outbox has thousands
        # of entries (per logic-reviewer iter1 #9 concern).
        try:
            if outbox_file.exists():
                # Tail-only scan: read last 64 KB, parse lines, check ids
                if cur_size > 65536:
                    with outbox_file.open("rb") as f:
                        f.seek(-65536, 2)
                        f.readline()  # discard partial first line
                        tail_bytes = f.read()
                    tail_text = tail_bytes.decode("utf-8", errors="replace")
                else:
                    tail_text = outbox_file.read_text(encoding="utf-8")
                # Substring containment of obx_id is sufficient — the id is
                # 16-hex unique enough that false-positive within a 64KB
                # window is negligible. Confirm with a json.loads of the
                # matching line(s).
                if obx_id in tail_text:
                    for raw in tail_text.splitlines():
                        if obx_id not in raw:
                            continue
                        try:
                            d = json.loads(raw)
                            if (d.get("outbox_id") == obx_id
                                    and d.get("status") in
                                    (STATUS_PENDING, STATUS_IN_FLIGHT)):
                                _emit_trace("outbox_enqueue_dedup",
                                            {"outbox_id": obx_id})
                                return True
                        except (json.JSONDecodeError, ValueError):
                            continue
        except Exception:
            pass  # corrupted file → fall through to append

        # Append the JSONL line via json.dumps (per S-5; no f-strings)
        try:
            line = json.dumps(asdict(entry), ensure_ascii=False)
            with outbox_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # platforms without fsync: best-effort
        except OSError as e:
            _emit_trace("outbox_enqueue_write_failed",
                        {"error": str(e)[:120]})
            return False

    _emit_trace("outbox_enqueued",
                {"outbox_id": obx_id,
                 "file_path": file_path[:200],
                 "content_sha256": content_sha256[:16]})
    return True


# --------------------------------------------------------------------------
# Iteration helpers
# --------------------------------------------------------------------------


def _iter_all_entries(outbox_file: Path) -> Iterator[OutboxEntry]:
    """Yield all entries (any status). Skip-with-warn on malformed lines."""
    if not outbox_file.exists():
        return
    try:
        with outbox_file.open("r", encoding="utf-8") as f:
            for ln_no, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                    if not all(k in d for k in _REQUIRED_KEYS):
                        _emit_trace("outbox_line_missing_keys",
                                    {"line": ln_no})
                        continue
                    if d.get("status") not in _VALID_STATUSES:
                        _emit_trace("outbox_line_bad_status",
                                    {"line": ln_no})
                        continue
                    yield OutboxEntry.from_dict(d)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    _emit_trace("outbox_line_malformed",
                                {"line": ln_no,
                                 "error": str(e)[:80]})
                    continue
    except OSError as e:
        _emit_trace("outbox_read_failed", {"error": str(e)[:100]})
        return


def _read_all_entries(outbox_file: Path) -> List[OutboxEntry]:
    return list(_iter_all_entries(outbox_file))


def _atomic_rewrite_entries(outbox_file: Path,
                            entries: List[OutboxEntry]) -> None:
    """Write entries back to outbox file atomically (tmp + replace)."""
    outbox_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = outbox_file.with_suffix(outbox_file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(str(tmp), str(outbox_file))


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------


def rotate_if_needed(outbox_dir: Path) -> Optional[Path]:
    """Rotate outbox.jsonl if size > 5 MB or count > 10k.

    Returns the rotated path on rotation, None otherwise.
    """
    outbox_file = _outbox_path(outbox_dir)
    if not outbox_file.exists():
        return None
    size = _file_size_bytes(outbox_file)
    count = sum(1 for _ in _iter_all_entries(outbox_file))
    if size < _DEFAULT_ROTATE_BYTES and count < _DEFAULT_ROTATE_ENTRIES:
        return None
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    rotated = outbox_dir / f"outbox.{ts}.jsonl"
    # Avoid clobber on rapid rotation in tests
    if rotated.exists():
        rotated = outbox_dir / f"outbox.{ts}.{int(time.time()*1000)%1000}.jsonl"
    os.replace(str(outbox_file), str(rotated))
    _emit_trace("outbox_rotated",
                {"rotated_path": str(rotated)[:200],
                 "size_bytes": size,
                 "entries": count})
    return rotated


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


def stats(dir: Optional[str] = None) -> Dict[str, Any]:
    """Return counts by status. Always returns 4 keys.

    Per AC-1 / TU1-pass-1: pending / in_flight / done / dead_letter.
    """
    try:
        outbox_dir = _resolve_outbox_dir(dir)
    except OutboxDirRejected:
        return {"pending": 0, "in_flight": 0, "done": 0, "dead_letter": 0,
                "error": "outbox_dir_rejected"}
    counts = {STATUS_PENDING: 0, STATUS_IN_FLIGHT: 0,
              STATUS_DONE: 0, STATUS_DEAD_LETTER: 0}
    if not outbox_dir.exists():
        return {"pending": 0, "in_flight": 0, "done": 0, "dead_letter": 0}
    try:
        for e in _iter_all_entries(_outbox_path(outbox_dir)):
            if e.status in counts:
                counts[e.status] += 1
    except Exception as ex:
        logger.warning("stats read failed: %s", ex)
    return {"pending": counts[STATUS_PENDING],
            "in_flight": counts[STATUS_IN_FLIGHT],
            "done": counts[STATUS_DONE],
            "dead_letter": counts[STATUS_DEAD_LETTER]}


# --------------------------------------------------------------------------
# Zombie reclamation (per logic-reviewer M-1 + MED-1)
# --------------------------------------------------------------------------


def _zombie_ttl() -> float:
    raw = os.environ.get("MEMEXA_OUTBOX_ZOMBIE_TTL")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _DEFAULT_ZOMBIE_TTL_SEC


def _reclaim_zombies(outbox_dir: Path,
                     ttl: Optional[float] = None,
                     now_fn: Callable[[], float] = time.time) -> int:
    """Reset entries stuck in_flight beyond TTL → pending.

    Returns the number of reclaimed entries.
    """
    outbox_file = _outbox_path(outbox_dir)
    if not outbox_file.exists():
        return 0
    if ttl is None:
        ttl = _zombie_ttl()
    now = now_fn()
    entries = _read_all_entries(outbox_file)
    reclaimed = 0
    for e in entries:
        if (e.status == STATUS_IN_FLIGHT
                and e.in_flight_started_at is not None
                and (now - e.in_flight_started_at) > ttl):
            e.status = STATUS_PENDING
            e.in_flight_started_at = None
            e.last_error = (e.last_error or "") + " | reclaimed_zombie"
            reclaimed += 1
    if reclaimed:
        _atomic_rewrite_entries(outbox_file, entries)
        _emit_trace("outbox_reclaim_zombie",
                    {"count": reclaimed, "ttl": ttl})
    return reclaimed


# --------------------------------------------------------------------------
# PID-file lock (per security S-4)
# --------------------------------------------------------------------------


@contextmanager
def _acquire_pid_lock():
    """Acquire singleton PID-file lock for drain.

    PID file at FIXED `<workspace>/.claude/harness/hindsight_outbox.pid`
    (NOT user-controlled MEMEXA_OUTBOX_DIR; per security S-4).

    Yields True if acquired (caller should drain), False if another
    instance holds it.
    """
    try:
        pid_dir = _resolve_pid_dir()
    except OutboxDirRejected as e:
        _emit_trace("outbox_pid_dir_rejected", {"error": str(e)[:100]})
        yield False
        return
    try:
        pid_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _emit_trace("outbox_pid_dir_mkdir_failed",
                    {"error": str(e)[:100]})
        yield False
        return
    pid_file = pid_dir / _DEFAULT_PID_FILE_NAME
    # Try O_CREAT|O_EXCL
    fd: Optional[int] = None
    try:
        fd = os.open(str(pid_file),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
    except FileExistsError:
        # Stale PID? Read content; if PID not running → reclaim
        try:
            stale_pid = int(pid_file.read_text(encoding="ascii").strip())
            if not _pid_alive(stale_pid):
                try:
                    os.unlink(str(pid_file))
                except OSError:
                    pass
                # Retry once
                try:
                    fd = os.open(str(pid_file),
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                                 0o600)
                    os.write(fd, str(os.getpid()).encode("ascii"))
                    os.close(fd)
                except FileExistsError:
                    _emit_trace("outbox_pid_lock_held",
                                {"existing_pid": stale_pid,
                                 "reason": "race_after_reclaim"})
                    yield False
                    return
            else:
                _emit_trace("outbox_pid_lock_held",
                            {"existing_pid": stale_pid})
                yield False
                return
        except (OSError, ValueError) as e:
            _emit_trace("outbox_pid_lock_held",
                        {"error": str(e)[:100]})
            yield False
            return
    except OSError as e:
        _emit_trace("outbox_pid_lock_open_failed",
                    {"error": str(e)[:100]})
        yield False
        return

    try:
        yield True
    finally:
        try:
            os.unlink(str(pid_file))
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """Return True iff PID corresponds to a running process."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # Windows: tasklist check via os.popen is brittle; use
            # OpenProcess via ctypes
            import ctypes  # type: ignore
            PROCESS_QUERY_INFORMATION = 0x0400
            kern = ctypes.windll.kernel32  # type: ignore
            h = kern.OpenProcess(PROCESS_QUERY_INFORMATION, 0, pid)
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                kern.GetExitCodeProcess(h, ctypes.byref(exit_code))
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kern.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except (PermissionError, OSError):
        # If we got a permission error, the PID exists
        if os.name != "nt":
            err = sys.exc_info()[1]
            if isinstance(err, OSError) and err.errno == errno.EPERM:
                return True
            return False
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------
# File-path security re-check (per security S-2)
# --------------------------------------------------------------------------


def _is_safe_memory_file_at_drain(file_path: str) -> bool:
    """Re-validate file_path BEFORE drain reads bytes.

    Per S-2: outbox JSONL might be tampered (writable by attacker via
    MEMEXA_OUTBOX_DIR override). Drain re-checks via the same allowlist
    used at hook-write time.

    Per HARD RULE feedback_ntfs_junction_reparse_point.md (SEC-5 fix):
    on Windows, explicit reparse-point check before reading the target.
    Even if realpath in _is_memory_file() resolves the junction, an
    attacker who replaced the file with a junction AFTER hook-write +
    BEFORE drain can still cause read of an unintended target. The
    `st_file_attributes & 0x400` (FILE_ATTRIBUTE_REPARSE_POINT) check
    is the authoritative gate.
    """
    try:
        from src.core.memory_write_hook import _is_memory_file
        if not _is_memory_file(file_path):
            return False
        # SEC-5: NTFS reparse-point check (Windows only)
        if sys.platform == "win32":
            try:
                st = os.stat(file_path, follow_symlinks=False)
                # FILE_ATTRIBUTE_REPARSE_POINT = 0x400 = 1024
                attrs = getattr(st, "st_file_attributes", 0)
                if attrs & 0x400:
                    _emit_trace("outbox_drain_rejected_reparse_point",
                                {"file_path": file_path[:200],
                                 "attrs": hex(attrs)})
                    return False
            except OSError:
                # Cannot stat → safest to reject
                return False
        return True
    except Exception as e:
        _emit_trace("outbox_drain_path_check_failed",
                    {"error": str(e)[:100]})
        return False


# --------------------------------------------------------------------------
# Drain (reconciler)
# --------------------------------------------------------------------------


def _attempt_retain(client: Any,
                    entry: OutboxEntry,
                    content_bytes: bytes) -> Tuple[bool, Optional[str], Optional[str]]:
    """Attempt one retain call.

    Returns (success, op_id_or_fallback, error_message).

    Per Stage-4 security-iter1-3 (MED) finding: recompute content_sha256
    from the actual bytes we just read from the validated file path, not
    from entry.content_sha256 (which came from the JSONL — attacker-
    writable). After this fix, the provenance audit trail in Hindsight
    matches the actually-retained content, even if a tampered JSONL line
    declared a different sha.
    """
    import hashlib
    actual_sha = hashlib.sha256(content_bytes).hexdigest()
    try:
        result = client.retain(
            content=content_bytes.decode("utf-8", errors="replace"),
            metadata={
                "source_file": entry.file_path,
                "content_sha256": actual_sha,
                "outbox_id": entry.outbox_id,
            },
            tags=["v2_outbox_drain"],
        )
        op_id = None
        if isinstance(result, dict):
            op_id = result.get("operation_id") or result.get("op_id")
        if not op_id:
            # Per L-4 fallback: use outbox_id as op_id
            op_id = entry.outbox_id
            _emit_trace("outbox_op_id_fallback",
                        {"outbox_id": entry.outbox_id})
        return True, str(op_id), None
    except Exception as e:
        return False, None, str(e)[:200]


def drain(
    once: bool = False,
    max_iter: int = 100,
    dir: Optional[str] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    client: Optional[Any] = None,
    backoff_schedule: Optional[List[int]] = None,
) -> Dict[str, int]:
    """Process pending entries.

    Args:
        once: if True, exits after one full sweep (no inner sleep loops).
        max_iter: max entries to process per call.
        dir: outbox dir override.
        sleep_fn: injectable for tests (per L-3).
        client: HindsightHttpClient instance. If None, creates default.
        backoff_schedule: injectable retry delay schedule.

    Returns counts dict {drained, succeeded, retried, dead_lettered, rejected_path}.
    """
    counts = {"drained": 0, "succeeded": 0, "retried": 0,
              "dead_lettered": 0, "rejected_path": 0}
    schedule = backoff_schedule or _BACKOFF_SCHEDULE
    try:
        outbox_dir = _resolve_outbox_dir(dir)
    except OutboxDirRejected:
        _emit_trace("outbox_drain_dir_rejected", {})
        return counts
    if not outbox_dir.exists():
        return counts

    with _acquire_pid_lock() as got_pid:
        if not got_pid:
            return counts

        # Reclaim zombies first
        _reclaim_zombies(outbox_dir)

        outbox_file = _outbox_path(outbox_dir)
        if not outbox_file.exists():
            return counts

        # Lazy client (only if anything pending)
        client_obj = client
        for iteration in range(max_iter):
            entries = _read_all_entries(outbox_file)
            pending = [e for e in entries if e.status == STATUS_PENDING]
            if not pending:
                break
            entry = pending[0]
            counts["drained"] += 1

            # Per S-2: re-validate file_path
            if not _is_safe_memory_file_at_drain(entry.file_path):
                _emit_trace("outbox_drain_rejected_path",
                            {"file_path": entry.file_path[:200],
                             "outbox_id": entry.outbox_id})
                counts["rejected_path"] += 1
                # Mark as dead-letter (security event)
                _mark_status(outbox_file, entry.outbox_id, STATUS_DEAD_LETTER,
                             extra={"last_error": "rejected_path_at_drain"})
                continue

            # Read content
            try:
                content_bytes = Path(entry.file_path).read_bytes()
            except OSError as e:
                _mark_status(outbox_file, entry.outbox_id, STATUS_DEAD_LETTER,
                             extra={"last_error": f"read_failed: {e}"[:200]})
                counts["dead_lettered"] += 1
                continue

            # Mark in_flight
            _mark_status(outbox_file, entry.outbox_id, STATUS_IN_FLIGHT,
                         extra={"in_flight_started_at": time.time()})

            # Lazy client init
            if client_obj is None:
                try:
                    from src.core.hindsight_client import get_client
                    client_obj = get_client()
                except Exception as e:
                    # Cannot reach Hindsight; mark back to pending for retry
                    _mark_status(outbox_file, entry.outbox_id, STATUS_PENDING,
                                 extra={"in_flight_started_at": None,
                                        "last_error": f"client init: {e}"[:200]})
                    counts["retried"] += 1
                    if once:
                        break
                    continue

            ok, op_id, err = _attempt_retain(client_obj, entry, content_bytes)
            if ok:
                _mark_status(outbox_file, entry.outbox_id, STATUS_DONE,
                             extra={"op_id": op_id,
                                    "in_flight_started_at": None,
                                    "last_attempt_at": time.time()})
                counts["succeeded"] += 1
                _emit_trace("outbox_drained_one",
                            {"outbox_id": entry.outbox_id,
                             "op_id": op_id})
            else:
                # retry-or-dead-letter
                next_retry = entry.retry_count + 1
                if next_retry >= _MAX_RETRIES:
                    _mark_status(outbox_file, entry.outbox_id,
                                 STATUS_DEAD_LETTER,
                                 extra={"retry_count": next_retry,
                                        "last_error": err,
                                        "in_flight_started_at": None,
                                        "last_attempt_at": time.time()})
                    counts["dead_lettered"] += 1
                    _emit_trace("outbox_dead_letter",
                                {"outbox_id": entry.outbox_id,
                                 "retry_count": next_retry,
                                 "last_error": (err or "")[:120]})
                else:
                    _mark_status(outbox_file, entry.outbox_id, STATUS_PENDING,
                                 extra={"retry_count": next_retry,
                                        "last_error": err,
                                        "in_flight_started_at": None,
                                        "last_attempt_at": time.time()})
                    counts["retried"] += 1
                    delay = schedule[next_retry] if next_retry < len(schedule) else schedule[-1]
                    if not once and delay > 0:
                        sleep_fn(delay)

            if once:
                break
    return counts


def _mark_status(outbox_file: Path,
                 outbox_id: str,
                 new_status: str,
                 extra: Optional[Dict[str, Any]] = None) -> bool:
    """Atomically update one entry's status.

    Per Stage-4 security-iter1-2 (MED) finding: hold the outbox FileLock
    across the read+modify+rewrite triple to prevent concurrent enqueue
    from writing a new line that gets clobbered by our rewrite.
    """
    outbox_dir = outbox_file.parent
    with _outbox_filelock(outbox_dir) as got:
        if not got:
            # Lock contention — abort safely; caller (drain) will retry
            # next iteration.
            return False
        entries = _read_all_entries(outbox_file)
        found = False
        for e in entries:
            if e.outbox_id == outbox_id:
                e.status = new_status
                if extra:
                    for k, v in extra.items():
                        if hasattr(e, k):
                            setattr(e, k, v)
                found = True
                break
        if found:
            _atomic_rewrite_entries(outbox_file, entries)
        return found


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cli(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="src.core.hindsight_outbox")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_e = sub.add_parser("enqueue", help="enqueue one entry")
    p_e.add_argument("--file-path", required=True)
    p_e.add_argument("--sha", dest="sha", required=True)
    p_e.add_argument("--content", default="")
    p_e.add_argument("--dir", default=None)

    p_d = sub.add_parser("drain", help="drain pending entries")
    p_d.add_argument("--once", action="store_true")
    p_d.add_argument("--max-iter", type=int, default=100)
    p_d.add_argument("--dir", default=None)

    p_s = sub.add_parser("stats", help="report counts by status")
    p_s.add_argument("--dir", default=None)

    p_r = sub.add_parser("rotate", help="force-rotate outbox file")
    p_r.add_argument("--dir", default=None)

    p_c = sub.add_parser("clear-dead-letter",
                         help="purge dead-letter entries")
    p_c.add_argument("--dir", default=None)

    args = parser.parse_args(argv[1:])
    try:
        if args.cmd == "enqueue":
            ok = enqueue(args.file_path, args.sha,
                         args.content.encode("utf-8"),
                         dir=args.dir)
            print(json.dumps({"ok": ok}, ensure_ascii=False))
            return 0 if ok else 1
        elif args.cmd == "drain":
            counts = drain(once=args.once, max_iter=args.max_iter,
                           dir=args.dir)
            print(json.dumps(counts, ensure_ascii=False))
            return 0
        elif args.cmd == "stats":
            s = stats(dir=args.dir)
            print(json.dumps(s, ensure_ascii=False))
            return 0
        elif args.cmd == "rotate":
            try:
                ob_dir = _resolve_outbox_dir(args.dir)
            except OutboxDirRejected as e:
                print(json.dumps({"error": str(e)}))
                return 2
            r = rotate_if_needed(ob_dir)
            print(json.dumps({"rotated": str(r) if r else None},
                             ensure_ascii=False))
            return 0
        elif args.cmd == "clear-dead-letter":
            try:
                ob_dir = _resolve_outbox_dir(args.dir)
            except OutboxDirRejected as e:
                print(json.dumps({"error": str(e)}))
                return 2
            ob_file = _outbox_path(ob_dir)
            if not ob_file.exists():
                print(json.dumps({"removed": 0}))
                return 0
            entries = _read_all_entries(ob_file)
            kept = [e for e in entries if e.status != STATUS_DEAD_LETTER]
            removed = len(entries) - len(kept)
            _atomic_rewrite_entries(ob_file, kept)
            print(json.dumps({"removed": removed}, ensure_ascii=False))
            return 0
    except Exception as e:
        print(json.dumps({"error": str(e)[:200]}, ensure_ascii=False))
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
