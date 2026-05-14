"""
alias_suggester.py -- A6 (2026-04-21) — Phase A alias-table growth mechanism.

Records unknown entity mentions, promotes frequent ones to pending_aliases.json
for CEO review, and exposes an `approve` CLI that moves entries into the
authoritative aliases.json.

Design notes:
- Cross-process safety: pending_aliases.json + aliases.json writes both
  go through filelock. Verifier R1-4 called out that simple atomic
  temp+rename is not enough when two terminals invoke `approve` at once.
  SEC-R1-01 (round 1): filelock is a HARD requirement, not a soft fallback.
- LOCK-ORDER (SEC-R1-05): approve() acquires aliases.lock OUTER, then
  pending.lock INNER. record_unknown() acquires pending.lock only. No code
  path acquires pending then aliases — keep it that way.
- In-process counter: a Counter tracks how many times we've seen each raw
  mention in THIS process. Persistence happens only after threshold=3 to
  avoid thrashing the JSON. Lost counts between process restarts are
  acceptable.
- Cache invalidation: canonicalizer has mtime-based reload (_cache_is_stale),
  so any process observing aliases.json mtime bump picks up the new
  canonical on next call. approve CLI also invokes _invalidate_cache for
  in-process immediate visibility (AC-A6-d).
- Fail-soft: all errors in record_unknown are swallowed to avoid breaking
  the canonicalization path.
- Caller-auth (SEC-R1-09): approve() is intended for the CLI only. Do NOT
  call from automated pipelines; the single-user local threat model relies
  on CEO intent at approval time.
"""
from __future__ import annotations


import json
import logging
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional
from memexa.core._path_resolver import memory_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ALIASES_PATH = (
    memory_dir()
    / "aliases.json"
)

_PENDING_PATH = _ALIASES_PATH.parent / "pending_aliases.json"

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_unknown_counter: Counter = Counter()
_PROMOTION_THRESHOLD = 3  # sightings before writing to pending_aliases


# ---------------------------------------------------------------------------
# Filelock helper (optional dep)
# ---------------------------------------------------------------------------

def _acquire_lock(path: Path, timeout: float = 10.0):
    """Return filelock context manager.

    SEC-R1-01 fix (2026-04-21): filelock is now a HARD requirement. The
    previous silent _Noop fallback made approve() and record_unknown()
    race-unsafe on any machine missing the package. If filelock import
    fails, raise ImportError with an actionable install hint.
    """
    try:
        import filelock
    except ImportError as e:
        raise ImportError(
            "alias_suggester requires the `filelock` package for "
            "cross-process safety. Install: pip install filelock."
        ) from e
    return filelock.FileLock(str(path) + ".lock", timeout=timeout)


# ---------------------------------------------------------------------------
# Pending file I/O
# ---------------------------------------------------------------------------

def _load_pending() -> dict:
    if not _PENDING_PATH.exists():
        return {"entities": {}}
    try:
        with _PENDING_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "entities" not in data:
            return {"entities": {}}
        return data
    except (OSError, json.JSONDecodeError):
        return {"entities": {}}


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomic JSON write: temp file + os.replace. Caller should hold filelock.

    SEC-R1-07 fix (2026-04-21): resolve parent dir so a symlinked target
    cannot divert the temp file outside the intended memory tree.
    """
    parent = path.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".alias_", suffix=".json", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_unknown(raw: str) -> Optional[str]:
    """Increment counter for *raw*. On reaching threshold, append to
    pending_aliases.json under a canonical_id derived from the normalized
    raw. Returns the pending canonical_id if promotion fired this call,
    else None. Fail-soft: all exceptions swallowed.
    """
    try:
        if not raw or not isinstance(raw, str):
            return None
        _unknown_counter[raw] += 1
        if _unknown_counter[raw] != _PROMOTION_THRESHOLD:
            return None
        # Threshold hit: promote to pending.
        canonical_id = _raw_to_canonical_id(raw)
        with _acquire_lock(_PENDING_PATH):
            data = _load_pending()
            entities = data.setdefault("entities", {})
            if canonical_id in entities:
                # Already pending; just add this surface form.
                if raw not in entities[canonical_id]:
                    entities[canonical_id].append(raw)
                    _atomic_write_json(_PENDING_PATH, data)
                return canonical_id
            entities[canonical_id] = [raw]
            _atomic_write_json(_PENDING_PATH, data)
        return canonical_id
    except Exception as e:
        logger.debug("alias_suggester.record_unknown swallowed: %s", e)
        return None


def list_pending() -> Dict[str, List[str]]:
    """Return dict of canonical_id -> [surface_forms] awaiting approval."""
    return _load_pending().get("entities", {})


def approve(
    canonical_id: str,
    merge_into: Optional[str] = None,
) -> bool:
    """CEO approval step. Move entry from pending_aliases.json into
    aliases.json, then invalidate canonicalizer cache for immediate
    in-process visibility. Other processes pick up via mtime-reload.

    If merge_into is given, surface forms are appended to that existing
    canonical instead of creating a new one.

    Returns True if approved, False if canonical_id not found in pending.
    """
    pending = _load_pending()
    entities_pending = pending.get("entities", {})
    if canonical_id not in entities_pending:
        return False
    surface_forms = list(entities_pending[canonical_id])

    # Write both files under locks (acquire alias lock FIRST to stay
    # consistent with lock-order: pending is the inner resource).
    with _acquire_lock(_ALIASES_PATH):
        # Load aliases.json
        if _ALIASES_PATH.exists():
            try:
                with _ALIASES_PATH.open(encoding="utf-8") as fh:
                    aliases = json.load(fh)
            except (OSError, json.JSONDecodeError):
                aliases = {"entities": {}, "predicates": {}}
        else:
            aliases = {"entities": {}, "predicates": {}}
        if not isinstance(aliases.get("entities"), dict):
            aliases["entities"] = {}
        target_canonical = merge_into if merge_into else canonical_id
        existing = aliases["entities"].setdefault(target_canonical, [])
        for sf in surface_forms:
            if sf not in existing:
                existing.append(sf)
        # Sort alias lists for deterministic diff.
        existing.sort()
        _atomic_write_json(_ALIASES_PATH, aliases)

        # Pending: remove the approved entry.
        with _acquire_lock(_PENDING_PATH):
            pending2 = _load_pending()
            if canonical_id in pending2.get("entities", {}):
                del pending2["entities"][canonical_id]
                _atomic_write_json(_PENDING_PATH, pending2)

    # In-process cache invalidation for immediate visibility.
    try:
        from memexa.core import canonicalizer
        canonicalizer._invalidate_cache()
        # Also clear our per-process counter for this raw so a fresh
        # sighting after approval does not prematurely re-promote.
        for raw in surface_forms:
            _unknown_counter.pop(raw, None)
    except Exception:
        pass
    return True


def _raw_to_canonical_id(raw: str) -> str:
    """Convert a raw surface form to a stable canonical_id.

    Rule: lowercase + spaces/hyphens → underscores + strip.
    This mirrors canonicalizer._normalize but produces an id-form
    (underscores not spaces).

    SEC-R1-06 fix (2026-04-21): length cap at 128 chars so a pathological
    multi-KB mention from Haiku cannot bloat aliases.json keys.
    """
    import re
    s = raw.lower()
    s = re.sub(r"[-\s]+", "_", s)
    s = re.sub(r"[^\w]+", "", s, flags=re.UNICODE)
    return (s.strip("_") or "unknown")[:128]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: List[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "list":
        entries = list_pending()
        if not entries:
            print("(pending_aliases.json is empty)")
            return 0
        for cid, forms in sorted(entries.items()):
            print(f"{cid}: {forms}")
        return 0
    if cmd == "approve":
        if len(argv) < 3:
            print("usage: approve <canonical_id> [--merge-into <existing>]",
                  file=sys.stderr)
            return 1
        cid = argv[2]
        merge_into = None
        if "--merge-into" in argv:
            idx = argv.index("--merge-into")
            if idx + 1 >= len(argv):
                print("--merge-into requires a canonical target", file=sys.stderr)
                return 1
            merge_into = argv[idx + 1]
        if approve(cid, merge_into=merge_into):
            print(f"approved: {cid}" + (f" (merged into {merge_into})" if merge_into else ""))
            return 0
        print(f"not found in pending: {cid}", file=sys.stderr)
        return 1
    print("usage: python -m memexa.core.alias_suggester [list|approve <id> [--merge-into X]]",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
