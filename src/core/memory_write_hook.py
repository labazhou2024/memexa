"""
memory_write_hook.py -- PostToolUse hook that auto-ingests memory/*.md
writes into the Neo4j graph.

Triggered by Claude Code's PostToolUse for Edit|Write tool calls.
Reads the tool_use JSON from stdin, validates the file_path is a true
memory file, then forks a non-blocking `graph_memory ingest` subprocess.

Verifier hardening (2026-04-21):
  - realpath containment check vs. canonical MEMORY_DIR
  - allowlist regex on filename (only feedback_/project_/reference_/
    user_profile/constraints/frozen_manifest types)
  - per-file lock (5s TTL) so bursts on different files all proceed and
    bursts on the SAME file coalesce
  - subprocess is invoked with list-form argv only, no shell interpolation
  - fail-soft: any exception -> exit 0; never blocks the parent Edit/Write
"""
from __future__ import annotations


import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from src.core._path_resolver import memory_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MEMORY_DIR = (
    memory_dir()
).resolve()

_LOCK_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / ".claude" / "harness"
)
_LOCK_TTL_SECONDS = 5.0

_FILENAME_ALLOWLIST = re.compile(
    # Match either:
    #   (a) prefix + "_" + suffix  (feedback_x.md, project_y.md, reference_z.md)
    #   (b) bare core file (user_profile.md, constraints.md, frozen_manifest.md)
    # L-02 fix (2026-04-21 R1 review): bare-file forms have no underscore
    # suffix so the original regex never matched them.
    r"^(feedback|project|reference)_.+\.md$"
    r"|^(user_profile|constraints|frozen_manifest)\.md$"
)
_FILENAME_DENY_PREFIX = ("MEMORY.md",)  # name match
_FILENAME_DENY_REGEX = re.compile(r"^\.|\.swp$|\.tmp$|~$")


def _is_memory_file(path_str: str) -> bool:
    """Return True iff path_str is a real memory file we should ingest.

    Containment check uses os.path.realpath on BOTH sides to defeat
    symlink + .. traversal. Allowlist regex enforces naming convention,
    keeping the surface area to the actual semantic memory files.
    """
    if not path_str:
        return False
    try:
        p = Path(path_str)
        rp = Path(os.path.realpath(str(p)))
    except Exception:
        return False
    try:
        if not rp.is_relative_to(_MEMORY_DIR):
            return False
    except AttributeError:
        # Py < 3.9 fallback
        try:
            rp.relative_to(_MEMORY_DIR)
        except ValueError:
            return False
    name = rp.name
    if name in _FILENAME_DENY_PREFIX:
        return False
    if _FILENAME_DENY_REGEX.match(name):
        return False
    if not _FILENAME_ALLOWLIST.match(name):
        return False
    return True


def _per_file_lock_path(real_path: str) -> Path:
    h = hashlib.sha256(real_path.encode("utf-8")).hexdigest()[:8]
    return _LOCK_DIR / f".memory_ingest_lock_{h}"


def _try_acquire_lock(real_path: str) -> bool:
    """Atomic lock acquisition with TTL. Returns True if acquired.

    SEC-01/L-06 hardening (R1 review): the original
    `exists -> stat -> unlink -> O_CREAT|O_EXCL` sequence had three
    distinct FS calls between the staleness check and the unlink,
    allowing two concurrent processes to both observe a stale lock,
    both unlink it, and both successfully O_CREAT|O_EXCL — silently
    duplicating ingest subprocesses.

    Hardened pattern: O_CREAT|O_EXCL is the SOLE arbitrator. If the
    create fails, only THEN do we evaluate staleness — if the lock is
    stale we attempt one rotation by re-creating with the same primitive
    after an os.unlink that itself can race; we treat a second
    FileExistsError as "another process won the rotation race", which
    is the safe outcome (we just skip — the other process will spawn).
    """
    try:
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return True  # if we can't create dir, don't block ingest
    lock = _per_file_lock_path(real_path)
    now = time.time()

    def _create_lock() -> bool:
        try:
            fd = os.open(
                str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
            )
            os.write(fd, str(time.time()).encode("utf-8"))
            os.close(fd)
            return True
        except FileExistsError:
            return False
        except Exception as e:
            logger.warning("lock acquire error (proceeding ungated): %s", e)
            return True  # fail-open

    # Fast path: try create directly. Wins if no concurrent lock holder.
    if _create_lock():
        return True

    # Slow path: existing lock — check staleness via mtime.
    try:
        mtime = lock.stat().st_mtime
    except OSError:
        # Lock vanished between O_CREAT failure and stat — retry once.
        return _create_lock()
    if now - mtime <= _LOCK_TTL_SECONDS:
        return False  # fresh lock, another ingest in flight

    # Stale: ATOMIC rotation. Try to unlink + recreate. The unlink may
    # race with another GC; if we fail to unlink, treat as "lost the
    # race" (someone else handled it). If we succeed, retry create —
    # if THAT fails we lost the race after rotation; either way, exit
    # without spawning to avoid duplicate ingests.
    try:
        os.unlink(str(lock))
    except OSError:
        return False  # someone else beat us to rotation
    return _create_lock()


_SUBPROCESS_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "PYTHONPATH", "PYTHONIOENCODING",
    "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA",
    "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
    "MEMEXA_HOOK_FAST", "MEMEXA_GRAPHITI_ENABLED",
    "MEMEXA_GRAPH_RETRIEVE", "MEMEXA_DUAL_LLM_MOCK",
    "ANTHROPIC_API_KEY",
    # U2 (2026-04-26): per security-reviewer S-3, allowlist new env vars
    "MEMEXA_MEMORY_HOOK_LEGACY_ONLY",
    "MEMEXA_OUTBOX_DIR",
    "MEMEXA_OUTBOX_ZOMBIE_TTL",
    "MEMEXA_AUDIT_THRESHOLD",
    "MEMEXA_PID_FILE_DIR",
    "MEMEXA_HINDSIGHT_URL", "MEMEXA_HINDSIGHT_BANK", "MEMEXA_HINDSIGHT_TIMEOUT",
)


def _filtered_subprocess_env() -> dict:
    """SEC-13 fix: only forward the env vars graph_memory ingest actually
    needs. Caps blast radius if parent env contains tokens unrelated to
    Neo4j ingest (CI tokens, OAuth secrets, etc.).
    """
    return {k: os.environ[k] for k in _SUBPROCESS_ENV_ALLOWLIST if k in os.environ}


def _spawn_ingest(file_path: str) -> bool:
    """Fork legacy subprocess; never blocks. Returns True if Popen succeeded.

    LEGACY (kept as last-resort fallback during U2 7-day dual-write).
    Primary path is now `_enqueue_via_outbox` → outbox + reconciler.
    """
    cmd = [
        sys.executable, "-m", "src.core.graph_memory",
        "ingest", file_path,
    ]
    try:
        # Critical: list form (no shell interpolation), close_fds where
        # supported, devnull I/O, env allowlist (SEC-13 R1 review).
        kwargs = dict(
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=_filtered_subprocess_env(),
        )
        if os.name == "posix":
            kwargs["close_fds"] = True
        # Detach from parent so the hook returns instantly.
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        return True
    except Exception as e:
        logger.warning("memory ingest spawn failed for %s: %s", file_path, e)
        return False


def _enqueue_via_outbox(file_path: str) -> bool:
    """U2 PRIMARY PATH: read file bytes → write_fact_async → outbox enqueue.

    Returns True iff successfully enqueued (caller emits trace
    `memory_write_hook_enqueued`). Returns False on any failure path
    (caller falls back to `_spawn_ingest`).
    """
    try:
        # Read file bytes (we accept that the read may capture a later
        # edit's bytes if Edit/Write races — content_sha256 binds to
        # whatever we actually saw, per L-1).
        content_bytes = Path(file_path).read_bytes()
    except OSError as e:
        logger.warning("memory_write_hook outbox read failed for %s: %s",
                       file_path, e)
        return False
    try:
        from src.core.graph_memory_v2 import write_fact_async
    except Exception as e:
        logger.warning("write_fact_async import failed: %s", e)
        return False
    try:
        return bool(write_fact_async(file_path, content_bytes))
    except Exception as e:
        logger.warning("write_fact_async raised: %s", e)
        return False


def _emit_hook_trace(event: str, payload: dict) -> None:
    """TU-7 (plan_v3 AC-8): emit explicit trace event on every fail-soft
    exit path. Prior behavior swallowed exceptions silently.

    AC-9 fix: when run as raw script (python memexa/memexa/core/memory_
    write_hook.py), memexa/ isn't on sys.path by default. Insert it
    before attempting the trace_sink import so observability works
    regardless of how the hook was invoked.
    """
    try:
        import sys as _sys
        _jarvis_dir = Path(__file__).resolve().parent.parent.parent
        _jarvis_dir_str = str(_jarvis_dir)
        if _jarvis_dir_str not in _sys.path:
            _sys.path.insert(0, _jarvis_dir_str)
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        # allow-silent: trace is best-effort; hook must not block parent tool
        pass


def main() -> int:
    """Hook entry. Reads stdin JSON; ALWAYS exits 0 (never blocks tool).

    TU-7 observability: every exit path now emits a trace event so the
    silent-fail pattern (5 memory writes went to queue, not direct
    ingest) becomes observable.
    """
    try:
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            _emit_hook_trace("memory_write_hook_skip", {"reason": "empty_stdin"})
            return 0
        try:
            data = json.loads(raw)
        except Exception as je:
            _emit_hook_trace("memory_write_hook_error",
                             {"reason": "malformed_json",
                              "error": str(je)[:120]})
            return 0

        # Claude Code PostToolUse payload shape:
        #   {"tool_name": "Edit"|"Write", "tool_input": {"file_path": "..."}}
        tool_input = data.get("tool_input") or {}
        file_path = tool_input.get("file_path") or ""

        if not _is_memory_file(file_path):
            _emit_hook_trace("memory_write_hook_skip",
                             {"reason": "not_memory_file",
                              "file_path": (file_path or "")[:200]})
            return 0

        try:
            real_path = os.path.realpath(file_path)
        except Exception as re:
            _emit_hook_trace("memory_write_hook_error",
                             {"reason": "realpath_failed",
                              "file_path": (file_path or "")[:200],
                              "error": str(re)[:120]})
            return 0

        if not _try_acquire_lock(real_path):
            _emit_hook_trace("memory_write_hook_skip",
                             {"reason": "lock_held",
                              "file_path": real_path[:200]})
            return 0

        # U2 (2026-04-26): primary path is outbox enqueue.
        # Kill-switch MEMEXA_MEMORY_HOOK_LEGACY_ONLY=1 → bypass outbox.
        if os.environ.get("MEMEXA_MEMORY_HOOK_LEGACY_ONLY") == "1":
            _emit_hook_trace("memory_write_hook_kill_switch",
                             {"file_path": real_path[:200],
                              "reason": "env_override"})
            spawned = _spawn_ingest(real_path)
            if not spawned:
                _emit_hook_trace("memory_write_hook_error",
                                 {"reason": "spawn_failed",
                                  "file_path": real_path[:200]})
            return 0

        # Primary: outbox enqueue (≤5ms file write under filelock)
        enqueued = _enqueue_via_outbox(real_path)
        if enqueued:
            _emit_hook_trace("memory_write_hook_enqueued",
                             {"file_path": real_path[:200]})
            return 0

        # Fallback: legacy ingest spawn
        _emit_hook_trace("memory_write_hook_legacy_fallback",
                         {"file_path": real_path[:200],
                          "reason": "outbox_unavailable"})
        spawned = _spawn_ingest(real_path)
        if not spawned:
            _emit_hook_trace("memory_write_hook_error",
                             {"reason": "spawn_failed",
                              "file_path": real_path[:200]})
        return 0
    except Exception as e:
        # Hook contract: must NEVER block the parent tool. Log + trace.
        _emit_hook_trace("memory_write_hook_error",
                         {"reason": "top_level_exception",
                          "error": str(e)[:200]})
        # allow-silent
        try:
            logger.warning("memory_write_hook unexpected: %s", e)
        except Exception:
            pass  # allow-silent
        return 0


if __name__ == "__main__":
    sys.exit(main())
