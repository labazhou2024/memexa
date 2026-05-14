"""TU-9 (plan_v2 SR-1, 2026-04-25) — CEO out-of-band HMAC-signed approval.

Closes the OWASP LLM01 self-attestation hole identified by security-reviewer
iter2 S1: trace.jsonl is writable by the same session that emits "CEO
approval" events, so a session can forge approval. This module provides
an HMAC-signed approval mechanism using a key only the CEO holds in their
shell environment.

Workflow:
  1. CEO sets `MEMEXA_CEO_HMAC_KEY` env (≥32 hex chars; one-time `setx`)
  2. CEO runs `python -m src.core.ceo_approve <task_id> [--reason "..."]`
  3. Module writes `<workspace>/.claude/harness/approvals/<task_id>.json`
     with HMAC-SHA256 over `task_id|ts|reason`
  4. Verifier (e.g. autopilot Stage 4 AC-8) calls `verify_approval(tid)`;
     re-computes HMAC with current env key; matches → True else False

Threat model:
  - Sessions WITHOUT the key cannot forge a valid record.
  - A session WITH the key can forge — but env key is operator-only by
    convention (we do not enforce; OS-level isolation is out of scope).
  - Tampering the JSON record after signing fails verification.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

_KEY_ENV = "MEMEXA_CEO_HMAC_KEY"
_MIN_KEY_BYTES = 32

# U6 TU-3: known bypass tokens enrolled with this approval system.
# Each token is HMAC-signed via MEMEXA_HMAC_KEY (separate from CEO key);
# bench_gate bypass via MEMEXA_BENCH_BYPASS_TOKEN env. Token format
# = first 32 hex chars of HMAC_SHA256(key, "bench_gate:YYYY-MM-DD").
# Verifier in src.core._hook_utils._verify_bench_bypass_token.
_KNOWN_BYPASS_TOKENS = (
    "BENCH_BYPASS",  # MEMEXA_BENCH_BYPASS_TOKEN; bench_gate (U6 TU-3)
)


def _workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _approvals_dir() -> Path:
    """Resolve approvals directory.

    Honors MEMEXA_CEO_APPROVALS_DIR env override (T-2 fix; per
    feedback_env_override_parent_allowlist.md HARD RULE — verify the override
    is under an allowed parent set before honoring). Allowed parents:
    workspace .claude/, $TEMP, $TMPDIR. Override outside the allowlist logs
    a stderr warning and falls through to default.
    """
    env_override = os.environ.get("MEMEXA_CEO_APPROVALS_DIR", "").strip()
    if env_override:
        try:
            override_path = Path(env_override).resolve()
        except (OSError, ValueError):
            override_path = None
        if override_path is not None:
            allowed_parents = []
            try:
                allowed_parents.append((_workspace_root() / ".claude").resolve())
            except (OSError, ValueError):
                pass
            for env_var in ("TEMP", "TMPDIR", "TMP"):
                p = os.environ.get(env_var, "").strip()
                if p:
                    try:
                        allowed_parents.append(Path(p).resolve())
                    except (OSError, ValueError):
                        continue
            override_str = str(override_path)
            for parent in allowed_parents:
                try:
                    parent_str = str(parent)
                except (OSError, ValueError):
                    continue
                # SEC-1 fix (Stage 4): use os.sep boundary to prevent
                # path-prefix attack (e.g. "Temp_evil".startswith("Temp")).
                # Match if override IS the parent OR is strictly under it.
                if override_str == parent_str or \
                   override_str.startswith(parent_str + os.sep):
                    override_path.mkdir(parents=True, exist_ok=True)
                    return override_path
            print(
                f"[ceo_approve] MEMEXA_CEO_APPROVALS_DIR={env_override!r} "
                f"outside allowed parent set; using default",
                file=sys.stderr,
            )
    d = _workspace_root() / ".claude" / "harness" / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _approval_path(task_id: str) -> Path:
    safe = "".join(c for c in task_id if c.isalnum() or c in "_-")
    return _approvals_dir() / f"{safe}.json"


def _key_or_die(exit_on_missing: bool = True) -> Optional[bytes]:
    raw = os.environ.get(_KEY_ENV, "").strip()
    if not raw:
        if exit_on_missing:
            print(
                f"[ceo_approve] ERROR: {_KEY_ENV} env var not set.\n"
                f"  Set it once with:  setx {_KEY_ENV} <hex-key-≥32-bytes>\n"
                f"  Or for current shell:  export {_KEY_ENV}=<hex>",
                file=sys.stderr,
            )
        return None
    if len(raw.encode("utf-8")) < _MIN_KEY_BYTES:
        if exit_on_missing:
            print(
                f"[ceo_approve] ERROR: {_KEY_ENV} too short ({len(raw)} chars; "
                f"need ≥{_MIN_KEY_BYTES})",
                file=sys.stderr,
            )
        return None
    return raw.encode("utf-8")


def _sign(key: bytes, task_id: str, ts: str, reason: str) -> str:
    msg = f"{task_id}|{ts}|{reason}".encode("utf-8")
    return _hmac.new(key, msg, hashlib.sha256).hexdigest()


def _key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def approve(task_id: str, reason: str = "") -> int:
    """CLI entry: write a signed approval record.

    Returns 0 on success, 2 if key missing/short, 3 on filesystem error.
    """
    key = _key_or_die(exit_on_missing=True)
    if key is None:
        return 2
    ts = datetime.now(tz=timezone.utc).isoformat()
    sig = _sign(key, task_id, ts, reason or "")
    record = {
        "task_id": task_id,
        "ts": ts,
        "approver": "ceo",
        "reason": reason or "",
        "hmac_sha256": sig,
        "key_fingerprint": _key_fingerprint(key),
    }
    try:
        path = _approval_path(task_id)
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[ceo_approve] write failed: {e}", file=sys.stderr)
        return 3
    print(
        f"[ceo_approve] APPROVED task {task_id}\n"
        f"  path: {path}\n"
        f"  fingerprint: {record['key_fingerprint']}"
    )
    return 0


_PHASE_REGEX = __import__("re").compile(r"\bphase=(\d+)\b")


def verify_approval(task_id: str, phase: Optional[int] = None) -> Tuple[bool, str]:
    """Verify a previously-written approval record.

    Returns (ok, reason). ok=True iff:
      - MEMEXA_CEO_HMAC_KEY is set (caller env)
      - approvals/<task_id>.json exists and parses
      - HMAC over (task_id, ts, reason) matches stored hmac_sha256
      - key_fingerprint matches current key (catches key rotation)

    U8 plan_v2 TU-5: when `phase` is set, additionally requires record's
    `reason` field to match regex `r"\\bphase=(\\d+)\\b"` AND captured group
    == str(phase) **case-sensitive** (logic-iter1-2 HIGH fix).

    SECURITY ORDERING INVARIANT (security-iter2-2 MED, load-bearing):
    The phase=N regex check MUST run AFTER HMAC verification passes,
    NOT before. The `reason` field is part of the HMAC-signed payload;
    checking the regex on an UNVERIFIED reason would let an attacker
    forge `phase=99` post-hoc. DO NOT REORDER.
    """
    key = _key_or_die(exit_on_missing=False)
    if key is None:
        return (False, f"{_KEY_ENV} env not set or too short in caller process")

    path = _approval_path(task_id)
    if not path.exists():
        return (False, f"no approval record at {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return (False, f"approval record unreadable: {e}")

    expected_fp = _key_fingerprint(key)
    if record.get("key_fingerprint") != expected_fp:
        # T-7 v4 redaction: never embed key_fingerprint hex in the reason
        # string (downstream callers may print/trace this; leaks the
        # fingerprint to log readers). Use opaque token instead.
        return (False, "key_rotation_detected")

    expected_sig = _sign(
        key,
        record.get("task_id", ""),
        record.get("ts", ""),
        record.get("reason", "") or "",
    )
    if not _hmac.compare_digest(expected_sig, record.get("hmac_sha256", "")):
        return (False, "HMAC signature mismatch (record tampered or wrong key)")

    # U8 TU-5: phase check happens AFTER HMAC verify (ordering invariant —
    # security-iter2-2 MED fix). Only the HMAC-VERIFIED reason field is
    # subject to the regex check, never the raw file content.
    if phase is not None:
        reason = record.get("reason", "") or ""
        m = _PHASE_REGEX.search(reason)
        if m is None or m.group(1) != str(phase):
            return (False, f"mode_b_no_phase_approval phase={phase}")
    return (True, "ok")


def _cli(argv: Optional[list] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="src.core.ceo_approve")
    p.add_argument("task_id", nargs="?", help="task_id to approve")
    p.add_argument("--reason", default="", help="optional reason text")
    p.add_argument("--verify", action="store_true",
                   help="verify existing record instead of creating one")
    args = p.parse_args(argv)
    if not args.task_id:
        print("[ceo_approve] ERROR: task_id required", file=sys.stderr)
        return 2
    if args.verify:
        ok, reason = verify_approval(args.task_id)
        print(f"verify: {ok} ({reason})")
        return 0 if ok else 1
    return approve(args.task_id, args.reason)


if __name__ == "__main__":
    raise SystemExit(_cli())
