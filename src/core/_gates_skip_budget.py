"""
Gates Skip Budget (AC-A3 + AC-A4)
==================================
Rolling 7-day skip counter with HMAC-signed one-shot override tokens.

Public API:
  record_skip(gate, reason, active_tid, autopilot, commit_sha) -> None
  should_escalate(window_days=7, threshold=4) -> (bool, count, level)
  mint_token(reason, expires_sec=3600) -> str
  verify_override_token(token) -> (ok, reason_str)

Internal helpers:
  _load_hmac_key() -> bytes
  _locked_append_jsonl(path, entry) -> None
  _data_dir() -> Path   (respects MEMEX_GATES_DATA_DIR env for tests)

CLI:
  python -m src.core._gates_skip_budget {show-budget|status}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.core.trace_sink import write_trace_event

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_LOCK_RETRIES = 200
_LOCK_RETRY_SLEEP = 0.05  # seconds


def _data_dir() -> Path:
    """Return the data directory, respecting MEMEX_GATES_DATA_DIR env override.

    The env override is validated against an allowlist of parent directories
    (workspace tree or system temp) per HARD RULE feedback_env_override_parent_allowlist.md
    and feedback_priority_inverted_fallback_2x2_matrix.md (exists/missing) x (set/empty).
    """
    override = os.environ.get("MEMEX_GATES_DATA_DIR")
    if override:
        try:
            import tempfile as _tf
            candidate = Path(override).resolve()
            tempdir = Path(_tf.gettempdir()).resolve()
            # Walk up from this file: core -> memex -> memex -> workspace
            workspace = Path(__file__).parent.parent.parent.parent.resolve()
            memex_root = workspace / "memex"
            allowed_parents = (workspace, tempdir, memex_root)
            is_safe = any(
                str(candidate).startswith(str(p) + os.sep) or str(candidate) == str(p)
                for p in allowed_parents
            )
            if is_safe:
                return candidate
            else:
                _LOG.warning(
                    "_gates_skip_budget: MEMEX_GATES_DATA_DIR %r outside allowlist; using default",
                    override,
                )
        except (OSError, ValueError) as exc:
            _LOG.warning("_gates_skip_budget: bad MEMEX_GATES_DATA_DIR (%s); using default", exc)

    # Default: memex/memex/data/
    try:
        from src.core._paths import DATA_DIR
        return DATA_DIR
    except ImportError:
        return Path(__file__).parent.parent / "data"


def _budget_file() -> Path:
    return _data_dir() / "_gates_skip_budget.jsonl"


def _used_tokens_file() -> Path:
    return _data_dir() / "_used_tokens.jsonl"


def _key_file() -> Path:
    return _data_dir() / ".gates_override_key"


# ---------------------------------------------------------------------------
# Sanitization helper (HARD RULE: feedback_sanitize_llm_reflected_text.md)
# ---------------------------------------------------------------------------

def _sanitize(s: str, maxlen: int = 200) -> str:
    """Strip control chars and truncate to maxlen."""
    return re.sub(r"[\x00-\x1f\x7f]", "", str(s))[:maxlen]


# ---------------------------------------------------------------------------
# Windows msvcrt locking helper
# ---------------------------------------------------------------------------

def _locked_append_jsonl(path: Path, entry: Dict[str, Any]) -> None:
    """Append a JSON entry to a JSONL file using Windows msvcrt.locking.

    Lock range covers (current_seek_pos=end_of_file, len(serialized)+1)
    per HARD RULE feedback_windows_file_lock_range.md.

    On non-Windows, falls back to fcntl.flock for correctness.
    The .lock sidecar file pattern is used to avoid the write-offset vs
    lock-offset mismatch inherent in append mode (same pattern as _file_lock.py).
    """
    serialized = json.dumps(entry, ensure_ascii=False) + "\n"
    encoded = serialized.encode("utf-8")
    n_bytes = len(encoded)

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")

    # Acquire sidecar mutex
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    locked = False

    try:
        if sys.platform == "win32":
            try:
                import msvcrt
                for _ in range(_LOCK_RETRIES):
                    try:
                        os.lseek(lock_fd, 0, os.SEEK_SET)
                        msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(_LOCK_RETRY_SLEEP)
                if not locked:
                    _LOG.warning("_locked_append_jsonl: could not acquire lock after 10s on %s", path)
            except ImportError:
                _LOG.warning("_locked_append_jsonl: msvcrt unavailable; running unlocked")
        else:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                locked = True
            except Exception as exc:
                _LOG.warning("_locked_append_jsonl: fcntl.flock unavailable: %s", exc)

        # Critical section: open target file, seek to end, write
        with open(path, "ab") as f:
            # On Windows with msvcrt, we want to lock the byte range we are
            # about to write. Seek to end first to discover current pos.
            f.seek(0, os.SEEK_END)
            write_pos = f.tell()
            if sys.platform == "win32" and locked:
                import msvcrt
                data_fd = f.fileno()
                os.lseek(data_fd, write_pos, os.SEEK_SET)
                try:
                    msvcrt.locking(data_fd, msvcrt.LK_NBLCK, n_bytes + 1)
                except OSError:
                    pass  # Proceed; sidecar mutex already serialises
            f.write(encoded)

    finally:
        # Release sidecar mutex
        if locked:
            if sys.platform == "win32":
                try:
                    import msvcrt
                    os.lseek(lock_fd, 0, os.SEEK_SET)
                    msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                try:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# AC-A3: Rolling 7-day skip budget
# ---------------------------------------------------------------------------

def record_skip(
    gate: str,
    reason: str,
    active_tid: Optional[str],
    autopilot: bool,
    commit_sha: Optional[str],
) -> None:
    """Record one gate skip event to the rolling budget JSONL.

    Emits a gate_skipped trace event (AC-A1 requirement also met here).
    Reason is sanitized per HARD RULE feedback_sanitize_llm_reflected_text.md.
    """
    clean_gate = _sanitize(gate)
    clean_reason = _sanitize(reason)
    clean_tid = _sanitize(active_tid or "")
    clean_sha = _sanitize(commit_sha or "")

    entry: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "gate": clean_gate,
        "reason": clean_reason,
        "active_tid": clean_tid,
        "autopilot": bool(autopilot),
        "commit_sha": clean_sha,
    }

    try:
        _locked_append_jsonl(_budget_file(), entry)
    except OSError as exc:
        write_trace_event("gate_infra_error", {
            "op": "record_skip",
            "gate": clean_gate,
            "error": _sanitize(str(exc)),
        })
        raise

    # Emit trace event (AC-A1 gate_skipped observability)
    write_trace_event("gate_skipped", {
        "gate": clean_gate,
        "reason": clean_reason,
        "active_tid": clean_tid,
        "autopilot": autopilot,
        "commit_sha": clean_sha,
    })


def _read_budget_entries(window_days: int = 7) -> list:
    """Read budget JSONL and filter to rolling window."""
    path = _budget_file()
    if not path.exists():
        return []

    cutoff_ts = time.time() - window_days * 86400
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    # Parse ts; accept both naive and aware ISO strings
                    ts_str = obj.get("ts", "")
                    try:
                        dt = datetime.fromisoformat(ts_str)
                        if dt.tzinfo is None:
                            epoch = dt.replace(tzinfo=timezone.utc).timestamp()
                        else:
                            epoch = dt.timestamp()
                    except (ValueError, TypeError):
                        continue
                    if epoch >= cutoff_ts:
                        entries.append(obj)
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError as exc:
        write_trace_event("gate_infra_error", {
            "op": "read_budget",
            "error": _sanitize(str(exc)),
        })
        raise

    return entries


def should_escalate(
    window_days: int = 7,
    threshold: int = 4,
) -> Tuple[bool, int, str]:
    """Check whether skip count in rolling window meets/exceeds threshold.

    Returns:
        (escalate: bool, count: int, escalation_level: str)
        escalation_level is one of "L0" (no action), "L2", "L3".

    When threshold is met, appends an L2 entry to pending_approvals.json.
    Count >= threshold * 2 escalates to L3 instead of L2.
    """
    try:
        entries = _read_budget_entries(window_days)
    except OSError:
        return False, 0, "L0"

    count = len(entries)
    if count < threshold:
        return False, count, "L0"

    # Determine level: >= 2x threshold -> L3, else L2
    level = "L3" if count >= threshold * 2 else "L2"

    # Append to pending_approvals.json
    _append_pending_approval(count, window_days, threshold, level)

    return True, count, level


def _append_pending_approval(
    count: int,
    window_days: int,
    threshold: int,
    level: str,
) -> None:
    """Append an escalation entry to pending_approvals.json."""
    try:
        from src.core._paths import DATA_DIR
        approvals_path = DATA_DIR / "pending_approvals.json"
    except ImportError:
        approvals_path = Path(__file__).parent.parent / "data" / "pending_approvals.json"

    entry_id = "apr_" + secrets.token_hex(4)
    new_entry = {
        "id": entry_id,
        "status": "pending",
        "level": level,
        "type": "gate_skip_budget_exceeded",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": f"Gate skip budget exceeded: {count} skips in {window_days}d (threshold={threshold})",
        "proposer": "gates_skip_budget auto-escalation",
        "summary": (
            f"{count} gate skips recorded in the last {window_days} days, "
            f"exceeding the {threshold}-skip threshold. "
            f"Review skip reasons in memex/memex/data/_gates_skip_budget.jsonl."
        ),
    }

    try:
        existing: list = []
        if approvals_path.exists():
            try:
                with open(approvals_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError):
                existing = []

        existing.append(new_entry)
        approvals_path.parent.mkdir(parents=True, exist_ok=True)
        with open(approvals_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        write_trace_event("gate_infra_error" if level == "L3" else "gate_skipped", {
            "op": "skip_budget_escalation",
            "level": level,
            "count": count,
            "approval_id": entry_id,
        })
    except OSError as exc:
        _LOG.error("_append_pending_approval: failed to write pending_approvals.json: %s", exc)
        write_trace_event("gate_infra_error", {
            "op": "append_pending_approval",
            "error": _sanitize(str(exc)),
        })


# ---------------------------------------------------------------------------
# AC-A4: HMAC one-shot override token
# ---------------------------------------------------------------------------

def _load_hmac_key() -> bytes:
    """Load HMAC key from file. FAIL-CLOSED if missing.

    HMAC key is NEVER read from env var per S-S4 security fix.
    """
    kf = _key_file()
    if not kf.exists():
        raise RuntimeError(
            f"HMAC key file missing; run: python -m src.cli.gates_override init-key"
            f" (expected at {kf})"
        )
    try:
        key = kf.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"Cannot read HMAC key file {kf}: {exc}") from exc
    if len(key) < 16:
        raise RuntimeError(
            f"HMAC key file too short ({len(key)} bytes); re-run init-key"
        )
    return key


def _current_commit_sha() -> str:
    """Return the current HEAD commit SHA (short 7-char fallback if git unavailable)."""
    import subprocess
    try:
        result = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.decode().strip()[:40]
    except Exception:
        return "0000000"


def mint_token(reason: str, expires_sec: int = 3600) -> str:
    """Create an HMAC-signed one-shot override token.

    Format: <base64url_payload>.<hex_hmac>

    Payload fields:
      commit_sha: current HEAD (full)
      expires_at: unix epoch float
      reason: sanitized, max 200 chars
      nonce: 16 hex chars (random)
    """
    import base64

    key = _load_hmac_key()
    clean_reason = _sanitize(reason, 200)
    commit_sha = _current_commit_sha()
    expires_at = time.time() + expires_sec
    nonce = secrets.token_hex(8)

    payload_dict = {
        "commit_sha": commit_sha,
        "expires_at": expires_at,
        "reason": clean_reason,
        "nonce": nonce,
    }
    payload_json = json.dumps(payload_dict, ensure_ascii=False, sort_keys=True)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii")

    sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _check_and_consume_token(used_path: Path, token_id: str, used_entry: Dict[str, Any]) -> Tuple[bool, str]:
    """Atomically check if token is unused and consume it (read-check-append under sidecar lock).

    Prevents TOCTOU race: two concurrent callers cannot both see the token as
    unused before either writes — the entire read+check+append sequence runs
    inside one sidecar-lock acquisition.

    Returns (True, "ok") on success, (False, "token_already_consumed") if
    already used, or raises OSError on I/O failure.
    """
    serialized = json.dumps(used_entry, ensure_ascii=False) + "\n"
    encoded = serialized.encode("utf-8")
    n_bytes = len(encoded)

    used_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = used_path.with_name(used_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    locked = False

    try:
        # Acquire sidecar lock
        if sys.platform == "win32":
            try:
                import msvcrt
                for _ in range(_LOCK_RETRIES):
                    try:
                        os.lseek(lock_fd, 0, os.SEEK_SET)
                        msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(_LOCK_RETRY_SLEEP)
                if not locked:
                    _LOG.warning("_check_and_consume_token: could not acquire lock after 10s on %s", used_path)
            except ImportError:
                _LOG.warning("_check_and_consume_token: msvcrt unavailable; running unlocked")
        else:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                locked = True
            except Exception as exc:
                _LOG.warning("_check_and_consume_token: fcntl.flock unavailable: %s", exc)

        # CRITICAL SECTION: read existing tokens, check for replay, then append
        if used_path.exists():
            try:
                with open(used_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if obj.get("token_id") == token_id:
                                return False, "token_already_consumed"
                        except json.JSONDecodeError:
                            continue
            except OSError:
                raise

        # Token not yet consumed — append consumption record
        with open(used_path, "ab") as f:
            f.seek(0, os.SEEK_END)
            write_pos = f.tell()
            if sys.platform == "win32" and locked:
                import msvcrt
                data_fd = f.fileno()
                os.lseek(data_fd, write_pos, os.SEEK_SET)
                try:
                    msvcrt.locking(data_fd, msvcrt.LK_NBLCK, n_bytes + 1)
                except OSError:
                    pass  # Proceed; sidecar mutex already serialises
            f.write(encoded)

        return True, "ok"

    finally:
        # Release sidecar lock
        if locked:
            if sys.platform == "win32":
                try:
                    import msvcrt
                    os.lseek(lock_fd, 0, os.SEEK_SET)
                    msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                try:
                    import fcntl
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def verify_override_token(token: str) -> Tuple[bool, str]:
    """Verify an HMAC-signed override token (one-shot: consumed on success).

    Returns (ok: bool, reason: str).
    Ratchet (used_tokens log) is incremented ONLY on successful verification.

    TOCTOU safety: the replay-check and ratchet-append run under one sidecar
    lock via _check_and_consume_token, preventing two threads from both seeing
    the token as unused before either writes.
    """
    import base64

    try:
        key = _load_hmac_key()
    except RuntimeError as exc:
        return False, f"key_unavailable: {_sanitize(str(exc))}"

    parts = token.split(".")
    if len(parts) != 2:
        return False, "malformed_token"

    payload_b64, provided_sig = parts[0], parts[1]

    # HMAC verification (constant-time)
    expected_sig = hmac.new(
        key, payload_b64.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return False, "hmac_mismatch"

    # Decode payload
    try:
        payload_json = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        return False, "payload_decode_error"

    # Check expiry
    expires_at = payload.get("expires_at", 0)
    if time.time() > expires_at:
        return False, "token_expired"

    # Check commit SHA matches HEAD
    token_sha = payload.get("commit_sha", "")
    current_sha = _current_commit_sha()
    if token_sha != current_sha:
        return False, f"commit_sha_mismatch (token={token_sha[:7]}, head={current_sha[:7]})"

    # Atomically check-and-consume: read-check-append under sidecar lock
    token_id = token[:64]  # Use prefix as lookup key
    used_path = _used_tokens_file()

    used_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "token_id": token_id,
        "commit_sha": token_sha,
        "reason": _sanitize(payload.get("reason", "")),
    }

    try:
        ok, reason = _check_and_consume_token(used_path, token_id, used_entry)
    except OSError as exc:
        write_trace_event("gate_infra_error", {
            "op": "verify_check_and_consume",
            "error": _sanitize(str(exc)),
        })
        raise

    if not ok:
        return False, reason

    return True, "ok"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli_show_budget() -> None:
    import sys
    try:
        entries = _read_budget_entries(7)
    except OSError as exc:
        print(f"ERROR reading budget: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Skip budget (last 7 days): {len(entries)} entries")
    for e in entries[-10:]:
        ts = e.get("ts", "?")[:19]
        gate = e.get("gate", "?")
        reason = e.get("reason", "?")[:60]
        print(f"  {ts}  gate={gate}  reason={reason}")


def _cli_status() -> None:
    try:
        escalate, count, level = should_escalate()
        key_ok = _key_file().exists()
        override_file = Path.home() / ".claude_gates_override"
        print(f"Skip budget: {count}/4 (7d window)  escalation={level}")
        print(f"HMAC key file: {'present' if key_ok else 'MISSING — run init-key'}")
        print(f"Override file (~/.claude_gates_override): {'present' if override_file.exists() else 'absent'}")
    except Exception as exc:
        import sys
        print(f"ERROR: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "show-budget":
        _cli_show_budget()
    elif cmd == "status":
        _cli_status()
    else:
        print(f"Unknown command: {cmd}. Use: show-budget | status", file=sys.stderr)
        sys.exit(1)
