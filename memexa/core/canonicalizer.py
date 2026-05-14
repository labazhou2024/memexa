"""
canonicalizer.py -- Entity and predicate normalization for memexa knowledge graph.

Loads aliases.json (bootstrap map), normalizes raw strings to canonical names,
and logs unrecognized tokens to unknown_entities.log for later review.
"""


import json
import re
import threading
from pathlib import Path
from typing import Optional

from memexa.core._path_resolver import memory_dir

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ALIASES_PATH = (
    memory_dir()
    / "aliases.json"
)

_DATA_DIR = Path(__file__).parent.parent / "data"
_UNKNOWN_LOG = _DATA_DIR / "unknown_entities.log"

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------


_cache: Optional[dict] = None          # raw aliases.json content
_entity_map: Optional[dict] = None     # alias -> canonical (entities)
_predicate_map: Optional[dict] = None  # alias -> canonical (predicates)
_cache_mtime: float = 0.0              # A6 (2026-04-21): aliases.json mtime at cache build time
_seen_unknown: set = set()             # per-process dedup for unknown log
# LOG-R1-2 / SEC-R1-3 (2026-04-23 Stage 4 fix): reentrant lock guards
# check-then-act on _cache/_entity_map/_predicate_map/_cache_mtime.
# CPython GIL makes single-instruction access safe but compound rebuild
# (invalidate → read file → assign multiple globals) can interleave
# with a concurrent reader under no-GIL / threaded heartbeat workloads.
_cache_lock = threading.RLock()


def _cache_is_stale() -> bool:
    """A6 (2026-04-21): check if aliases.json has been updated on disk
    since we built _cache. Enables CEO's `alias_suggester approve ...` in a
    different terminal to be picked up by long-running heartbeat processes
    without restart — closes verifier R1-4 cross-process cache race.
    """
    global _cache_mtime
    if _cache is None:
        return True
    try:
        cur = _ALIASES_PATH.stat().st_mtime if _ALIASES_PATH.exists() else 0.0
    except OSError:
        # LOGIC-R1-03 fix (2026-04-21): returning False here would serve
        # stale data indefinitely if aliases.json is deleted/inaccessible.
        # Return True so the next load_aliases attempts a reload, surfacing
        # FileNotFoundError to the caller instead of silent data corruption.
        return True
    return cur > _cache_mtime


def _invalidate_cache() -> None:
    """A6: explicit invalidation point, also called by alias_suggester.approve
    for in-process immediate visibility (AC-A6-d)."""
    global _cache, _entity_map, _predicate_map, _cache_mtime
    with _cache_lock:
        _cache = None
        _entity_map = None
        _predicate_map = None
        _cache_mtime = 0.0


def load_aliases() -> dict:
    """Return the raw aliases dict, cached after first load. A6: reloads
    if aliases.json mtime has advanced since cache build.

    LOGIC-R2-NEW (2026-04-21): if aliases.json is deleted while the
    process is running, _cache_is_stale (post-LOGIC-R1-03 fix) returns
    True, we invalidate, then the open() would raise FileNotFoundError.
    Instead, degrade to an empty shell and log once so callers can keep
    running (canonicalize_entity will simply return normalized-raw).
    """
    global _cache, _cache_mtime
    with _cache_lock:
        if _cache is not None and _cache_is_stale():
            _invalidate_cache()
        if _cache is None:
            try:
                with open(_ALIASES_PATH, encoding="utf-8") as fh:
                    _cache = json.load(fh)
                try:
                    _cache_mtime = _ALIASES_PATH.stat().st_mtime
                except OSError:
                    _cache_mtime = 0.0
            except FileNotFoundError:
                import logging
                logging.getLogger(__name__).warning(
                    "canonicalizer: aliases.json missing at %s — "
                    "running with empty alias map", _ALIASES_PATH,
                )
                _cache = {"entities": {}, "predicates": {}}
                _cache_mtime = 0.0
        return _cache


def _build_entity_map() -> dict:
    """Build and return a fresh entity alias -> canonical dict from aliases.json.

    This is a pure rebuild: it reads through load_aliases() and constructs the
    mapping without checking or setting any module-level cache. The caching
    layer is handled by _get_entity_map().
    """
    aliases = load_aliases()
    result: dict = {}
    for canonical, alias_list in aliases.get("entities", {}).items():
        result[_normalize(canonical)] = canonical
        for alias in alias_list:
            result[_normalize(alias)] = canonical
    return result


def _build_predicate_map() -> dict:
    """Build and return a fresh predicate alias -> canonical dict from aliases.json.

    Pure rebuild; caching layer is _get_predicate_map().
    """
    aliases = load_aliases()
    result: dict = {}
    for canonical, syn_list in aliases.get("predicates", {}).items():
        result[_normalize(canonical)] = canonical
        for syn in syn_list:
            result[_normalize(syn)] = canonical
    return result


def _get_entity_map() -> dict:
    """Return entity map, calling _build_entity_map only when mtime has changed.

    AC-5 (TU-5): module-level _entity_map caches the built dict. On each call
    this function checks aliases.json mtime; only rebuilds (and re-caches) when
    the file has been modified since last build. Cache hit skips _build_entity_map
    entirely, satisfying AC-5-2 (exactly-once build per mtime epoch).
    """
    global _entity_map
    if _entity_map is not None and not _cache_is_stale():
        return _entity_map
    _entity_map = _build_entity_map()
    return _entity_map


def _get_predicate_map() -> dict:
    """Return predicate map, calling _build_predicate_map only when mtime changed.

    AC-5 (TU-5): same mtime-guard pattern as _get_entity_map.
    """
    global _predicate_map
    if _predicate_map is not None and not _cache_is_stale():
        return _predicate_map
    _predicate_map = _build_predicate_map()
    return _predicate_map


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _normalize(raw: str) -> str:
    """Lowercase, collapse spaces/hyphens/underscores to single space, strip."""
    s = raw.lower()
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def canonicalize_entity(raw: str) -> str:
    """Return canonical entity name for *raw*, or *raw* normalized if unknown.

    A6 (2026-04-21): when the lookup misses (returns normalized-raw), also
    notify alias_suggester so the unknown mention can be counted. After 3
    process-local sightings the suggester promotes the mention to
    pending_aliases.json awaiting CEO approval.
    """
    key = _normalize(raw)
    canonical = _get_entity_map().get(key)
    if canonical is None:
        # A6 record-unknown hook (best-effort, failure must not break
        # canonicalization path).
        try:
            from memexa.core import alias_suggester
            alias_suggester.record_unknown(raw)
        except Exception:
            pass
        return key
    return canonical


def canonicalize_predicate(raw: str) -> str:
    """Return canonical predicate name for *raw*, or *raw* normalized if unknown."""
    key = _normalize(raw)
    return _get_predicate_map().get(key, key)


def log_unknown_entity(raw: str) -> None:
    """Append *raw* to unknown_entities.log once per process lifetime."""
    if raw in _seen_unknown:
        return
    _seen_unknown.add(raw)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_UNKNOWN_LOG, "a", encoding="utf-8") as fh:
        fh.write(raw + "\n")


def canonicalize_fact(
    subject: str,
    predicate: str,
    object_: str,
) -> tuple:
    """Canonicalize a (subject, predicate, object) triple.

    Returns:
        (s_canon, p_canon, o_canon, unknown_list)
        where unknown_list contains raw values that were NOT found in aliases.
    """
    unknowns = []

    s_key = _normalize(subject)
    s_canon = _get_entity_map().get(s_key)
    if s_canon is None:
        s_canon = s_key
        unknowns.append(subject)
        log_unknown_entity(subject)

    p_key = _normalize(predicate)
    p_canon = _get_predicate_map().get(p_key)
    if p_canon is None:
        p_canon = p_key
        # predicates are not logged to unknown_entities (different domain)

    o_key = _normalize(object_)
    o_canon = _get_entity_map().get(o_key)
    if o_canon is None:
        o_canon = o_key
        unknowns.append(object_)
        log_unknown_entity(object_)

    return s_canon, p_canon, o_canon, unknowns
