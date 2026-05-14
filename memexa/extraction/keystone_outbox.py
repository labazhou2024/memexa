"""Win-side keystone outbox writer + PII scrub utility (mac_win_integration U6 TU-4).

Provides shared atomic-write + bare-PII scrub for U6 wechat_read / outlook_read /
schedule_poll. Mac-side U7 will pull JSON envelopes via xfer.py.

Schema (envelope JSON written to memexa/data/win_keystone_outbox/<utc-iso>__<source>.json):

    ENVELOPE_SCHEMA = {
        "schema_version": int,                 # = 1
        "writer_id": str,                      # = "win_keystone3"
        "source": str,                         # ∈ {"wechat","outlook","schedule"}
        "captured_at_utc": str,                # ISO-8601 with Z suffix
        "payload": list,                       # source-specific dataclass dicts
        "scrubbed_count": int,                 # NUMBER OF LINES replaced (NOT secret occurrences)
        "tempfile_replaced_atomically": bool,  # True after successful os.replace
    }

scrub_pii(text) returns (scrubbed_text, scrubbed_lines_count). Counts LINES
redacted, not secret occurrences (per logic-iter1-5).

Bare-PII patterns (security-iter2-2): mobile / national-id / OTP / bank-card /
email / sk- / JWT — all bare patterns NOT requiring KEY=value form.

HARD RULE: feedback_secret_scrub_assignment_anchor + feedback_writer_reader_schema_contract.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Add workspace root to sys.path for dotfile_secret_scrub import
_WS_ROOT = Path(__file__).resolve().parents[3]
if str(_WS_ROOT / "research" / "mac_primary-host_xfer_test") not in sys.path:
    sys.path.insert(0, str(_WS_ROOT / "research" / "mac_primary-host_xfer_test"))
from memexa.core.dotfile_secret_scrub import scrub_text as _scrub_assignment_form  # noqa: E402

ENVELOPE_SCHEMA = {
    "schema_version": int,
    "writer_id": str,
    "source": str,
    "captured_at_utc": str,
    "payload": list,
    "scrubbed_count": int,
    "tempfile_replaced_atomically": bool,
}

ALLOWED_SOURCES = frozenset({"wechat", "email", "schedule"})  # v2: outlook -> email per CEO 2026-04-30

# Per security-iter1-1/2 HIGH: Python `\b` doesn't fire at CJK↔ASCII boundary
# because CJK chars ARE \w (Unicode word chars). Use explicit non-digit / non-letter
# lookarounds instead of \b for digit and alphanumeric secrets.
BARE_PII_PATTERNS = (
    # Chinese mobile (with optional +86 prefix; security-iter1-2)
    re.compile(r"(?<!\d)(?:\+?86)?1[3-9]\d{9}(?!\d)"),
    # 18-char Chinese ID
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    # bank card 16-19 digits
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    # 6-digit OTP standalone
    re.compile(r"(?<!\d)\d{6}(?!\d)"),
    # email
    re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"),
    # OpenAI/Anthropic-like
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}(?![A-Za-z0-9])"),
    # JWT (3 dot-separated b64 segments, eyJ prefix; min 10 chars per segment to allow short payloads)
    re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9])"),
    # v3 U3: Chinese street address with door number — covers "建国路88号" / "5栋101室" /
    # "3楼B座" pattern per chat_to_graph plan_v3_FINAL §3 U3 (multi-person PII risk
    # in group chat). Matches a Chinese address indicator (路/街/巷/里/弄/道/号/楼/栋/室)
    # followed (within 30 non-newline chars) by a multi-digit door number.
    re.compile(r"(?:[省市区县](?:[\u4e00-\u9fff]{1,12}?)?)?[\u4e00-\u9fff]{1,15}?(?:路|街|巷|里|弄|道)\s*\d{1,5}号(?:\s*\d{1,4}(?:楼|栋|号楼|号|室|门))?"),
    re.compile(r"\d{1,3}(?:楼|栋|号楼)\d{1,4}(?:室|号|门)"),
)

REDACT_LINE = "[REDACTED by keystone_outbox: line matched bare-PII or assignment-form secret]"


class OutboxWriteFailed(Exception):
    """Raised when atomic write to outbox fails (disk full, replace-cross-fs, etc.)."""


def scrub_pii(text: str) -> tuple[str, int]:
    """Scrub PII from free-text. Returns (scrubbed_text, redacted_line_count).

    Applies BOTH the dotfile assignment-form scrubber AND the bare-PII regex set.
    Count = lines that matched ANY pattern (not occurrences).
    """
    # Pass 1: assignment-form (KEY=value)
    after_assignment, count_a = _scrub_assignment_form(text)
    # Pass 2: bare-PII patterns on already-partially-scrubbed text
    out_lines: list[str] = []
    count_b = 0
    for line in after_assignment.splitlines(keepends=True):
        if any(p.search(line) for p in BARE_PII_PATTERNS):
            ending = "\n" if line.endswith("\n") else ""
            out_lines.append(REDACT_LINE + ending)
            count_b += 1
        else:
            out_lines.append(line)
    return "".join(out_lines), count_a + count_b


def _outbox_dir() -> Path:
    """Resolve outbox directory; create if missing."""
    p = _WS_ROOT / "memexa" / "data" / "win_keystone_outbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _emit_trace(event: str, payload: dict) -> None:
    """Best-effort trace emission; silent if trace_sink unavailable."""
    try:
        from memexa.core.trace_sink import emit  # type: ignore
        emit(event, payload)
    except Exception:
        pass


def write_envelope(
    source: str,
    payload: list,
    scrubbed_count: int,
) -> Path:
    """Atomically write a typed envelope to outbox.

    Returns the final file path. Raises OutboxWriteFailed on disk/replace failure.
    """
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"source must be in {ALLOWED_SOURCES}, got {source!r}")
    if not isinstance(payload, list):
        raise ValueError(f"payload must be list, got {type(payload).__name__}")

    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    envelope = {
        "schema_version": 1,
        "writer_id": "win_keystone3",
        "source": source,
        "captured_at_utc": captured_at,
        "payload": payload,
        "scrubbed_count": int(scrubbed_count),
        "tempfile_replaced_atomically": False,  # flipped True after os.replace
    }

    outbox = _outbox_dir()
    safe_iso = captured_at.replace(":", "-")
    final_path = outbox / f"{safe_iso}__{source}.json"

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(outbox),
        suffix=".json.tmp",
        prefix="keystone_outbox_",
        delete=False,
        encoding="utf-8",
    )
    tmp_path = tmp.name
    try:
        envelope["tempfile_replaced_atomically"] = True
        json_text = json.dumps(envelope, ensure_ascii=False, indent=2)
        tmp.write(json_text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass  # Windows may reject chmod; advisory only
        os.replace(tmp_path, final_path)
    except OSError as e:
        # Best-effort cleanup. On Windows, file handle may still be open from
        # a partial write — broaden except to OSError to catch ERROR_SHARING_VIOLATION
        # (per logic-iter1-1 HIGH fix; FileNotFoundError alone misses Windows case).
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise OutboxWriteFailed(f"atomic write to {final_path} failed: {e}") from e
    finally:
        try:
            tmp.close()
        except Exception:
            pass

    # security-iter1-3 MED fix: emit only filename, NOT full Windows absolute path
    _emit_trace("outbox_written", {
        "source": source,
        "scrubbed_count": scrubbed_count,
        "filename": final_path.name,
    })
    return final_path


# ─── Real-time append-mode (2026-04-30 CEO directive) ────────────────────────


_APPEND_LOCK = __import__("threading").Lock()  # in-process serialization

# TU-7 (2026-04-30): rotation thresholds. Override via env for tests / different deployments.
_ROTATE_MAX_BYTES = int(os.environ.get("MEMEXA_OUTBOX_ROTATE_MAX_BYTES", str(100 * 1024 * 1024)))
_ROTATE_MAX_AGE_SEC = float(os.environ.get("MEMEXA_OUTBOX_ROTATE_MAX_AGE_SEC", str(7 * 24 * 3600)))


def _should_rotate(target: Path) -> tuple:
    """Return (should_rotate: bool, reason: str). reason ∈ {'size','age','no'}."""
    if not target.exists():
        return False, "no"
    try:
        st = target.stat()
    except OSError:
        return False, "no"
    if st.st_size > _ROTATE_MAX_BYTES:
        return True, "size"
    import time as _time
    age = _time.time() - st.st_mtime
    if age > _ROTATE_MAX_AGE_SEC:
        return True, "age"
    return False, "no"


def _rotate_outbox(target: Path):
    """Atomic-rename target → realtime__<source>.jsonl.<n> (next free n).

    Returns archive Path or None if target absent. Caller MUST hold _APPEND_LOCK.
    """
    if not target.exists():
        return None
    base_name = target.name  # e.g. realtime__wechat.jsonl
    parent = target.parent
    n = 1
    while True:
        candidate = parent / f"{base_name}.{n}"
        if not candidate.exists():
            break
        n += 1
        if n > 9999:
            raise RuntimeError(f"rotation index exhausted for {target}")
    os.rename(str(target), str(candidate))
    _emit_trace("outbox_rotated", {
        "old_path": target.name, "archive": candidate.name,
        "archive_size": candidate.stat().st_size,
    })
    return candidate


def append_message(source: str, message: dict) -> Path:
    """Append a single message to realtime__<source>.jsonl (one JSON line).

    Complement to write_envelope (which writes single envelope per pull).
    Used by WeChatDBWatcher streaming callback.

    Schema per line: {_writer_id, _appended_at_utc, source, message: {...}}.

    Concurrency: in-process threading.Lock protects against torn writes;
    cross-process portalocker.Lock used when available (multi-process safety).

    TU-7: pre-write check rotates target if size>100MB or age>7d (tunable via env).
    """
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"source must be in {ALLOWED_SOURCES}, got {source!r}")
    if not isinstance(message, dict):
        raise ValueError(f"message must be dict, got {type(message).__name__}")

    outbox = _outbox_dir()
    target = outbox / f"realtime__{source}.jsonl"

    line_text = json.dumps({
        "_writer_id": "win_keystone3",
        "_appended_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "source": source,
        "message": message,
    }, ensure_ascii=False) + "\n"

    # Try cross-process lock; fallback to in-process only
    try:
        import portalocker  # type: ignore
        cross_proc_lock = portalocker.Lock(str(target) + ".lock", timeout=2.0,
                                            fail_when_locked=False)
    except ImportError:
        cross_proc_lock = None

    with _APPEND_LOCK:  # in-process serialize first (covers thread case)
        # TU-7: rotate inside the lock so concurrent appenders don't see a torn file
        should, reason = _should_rotate(target)
        if should:
            try:
                _rotate_outbox(target)
            except OSError as e:
                # Rotation failure is non-fatal: continue appending to original.
                _emit_trace("outbox_rotate_failed", {"target": target.name, "error": str(e)})
        if cross_proc_lock:
            with cross_proc_lock:
                with target.open("a", encoding="utf-8") as f:
                    f.write(line_text)
                    f.flush()
                    try: os.fsync(f.fileno())
                    except OSError: pass
        else:
            with target.open("a", encoding="utf-8") as f:
                f.write(line_text)
                f.flush()
                try: os.fsync(f.fileno())
                except OSError: pass

    _emit_trace("outbox_appended", {
        "source": source,
        "filename": target.name,
    })
    return target
