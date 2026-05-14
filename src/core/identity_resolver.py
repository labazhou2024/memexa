"""identity_resolver — surface_form ↔ canonical_id query interface.

Backed by `data/identity_aliases.json` (built by tools/auto_manifest_learn.py).

Public API:
  resolve(query)       surface_form → canonical_id (or list if ambiguous, None if unresolved)
  aliases_of(canon)    canonical_id → list of all surface forms
  expand_query(q)      query → [query, alias1, alias2, ...] for BGE recall enrichment
  tag_filter_for(q)    query → list of "canon:<id>" tag strings for hindsight filter

CLI:
  python -m src.core.identity_resolver list                 # list canonical_ids
  python -m src.core.identity_resolver show <name>          # full info one entity
  python -m src.core.identity_resolver resolve <name>       # → canonical_id(s)
  python -m src.core.identity_resolver expand <name>        # → [aliases...]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_REPO = Path(__file__).resolve().parent.parent.parent
_ALIASES_PATH = _REPO / "data" / "identity_aliases.json"
_MANIFEST_PATH = _REPO / "data" / "identity_manifest.yaml"

# Module-level caches (lazy-load)
_aliases_cache: Optional[Dict[str, Any]] = None
_manifest_cache: Optional[Dict[str, Any]] = None


def _load_aliases() -> Dict[str, Any]:
    global _aliases_cache
    if _aliases_cache is None:
        if not _ALIASES_PATH.exists():
            _aliases_cache = {}
        else:
            with open(_ALIASES_PATH, "r", encoding="utf-8") as f:
                _aliases_cache = json.load(f)
    return _aliases_cache


def _load_manifest() -> Dict[str, Any]:
    global _manifest_cache
    if _manifest_cache is None:
        if not _MANIFEST_PATH.exists():
            _manifest_cache = {"persons": {}}
        else:
            try:
                import yaml
                with open(_MANIFEST_PATH, "r", encoding="utf-8") as f:
                    _manifest_cache = yaml.safe_load(f) or {"persons": {}}
            except ImportError:
                _manifest_cache = {"persons": {}}
    return _manifest_cache


def reload_caches() -> None:
    """Force-reload caches (after manifest update)."""
    global _aliases_cache, _manifest_cache
    _aliases_cache = None
    _manifest_cache = None


def resolve(query: str) -> Union[str, List[str], None]:
    """Surface form → canonical_id (or list of cids if ambiguous, None if not found).

    Tries exact match first, then lowercase fallback (for emails).
    """
    if not query:
        return None
    aliases = _load_aliases()
    target = aliases.get(query)
    if target is None:
        target = aliases.get(query.lower())
    if target is None:
        # Try without space normalization
        target = aliases.get(query.strip())
    return target


def aliases_of(canonical_id: str) -> List[str]:
    """Reverse: canonical_id → all surface forms."""
    if not canonical_id:
        return []
    aliases = _load_aliases()
    out = []
    for sf, cid in aliases.items():
        if cid == canonical_id:
            out.append(sf)
        elif isinstance(cid, list) and canonical_id in cid:
            out.append(sf)
    return out


def expand_query(query: str, max_aliases: int = 8) -> List[str]:
    """For BGE recall enrichment: query → [query, alias1, ...].

    If query resolves to ambiguous (multiple cids), returns aliases of ALL candidates.
    Result list always starts with original query and is dedup-preserving.
    """
    if not query:
        return []
    target = resolve(query)
    if target is None:
        return [query]
    result = [query]
    if isinstance(target, list):
        for cid in target:
            result.extend(aliases_of(cid))
    else:
        result.extend(aliases_of(target))
    # dedup preserving order
    seen = set()
    uniq = []
    for s in result:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
        if len(uniq) >= max_aliases:
            break
    return uniq


def tag_filter_for(query: str) -> Optional[List[str]]:
    """For hindsight tag filter (preferred over BGE for entity queries).

    Returns ["canon:person_xxx"] or None if query unresolved.
    Multiple cids → list of multiple canon: tags (hindsight will OR them).
    """
    target = resolve(query)
    if target is None:
        return None
    if isinstance(target, list):
        return [f"canon:{cid}" for cid in target]
    return [f"canon:{target}"]


def show(query: str) -> Dict[str, Any]:
    """Detailed info: aliases + canonical_id + manifest entry."""
    target = resolve(query)
    if target is None:
        return {"query": query, "resolved": None}
    cids = target if isinstance(target, list) else [target]
    manifest = _load_manifest()
    persons = manifest.get("persons") or {}
    out: Dict[str, Any] = {"query": query, "candidates": []}
    for cid in cids:
        p = persons.get(cid, {})
        out["candidates"].append({
            "canonical_id": cid,
            "primary_name": p.get("primary_name") or "",
            "is_self": p.get("is_self", False),
            "merged_into": p.get("merged_into"),
            "n_aka": len(p.get("aka") or []),
            "wxid_hashes": p.get("identifiers", {}).get("wxid_hashes") or [],
            "emails": p.get("identifiers", {}).get("emails") or [],
            "all_aliases": aliases_of(cid),
        })
    return out


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def _cli(argv: List[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "list":
        manifest = _load_manifest()
        persons = manifest.get("persons") or {}
        active = {cid: p for cid, p in persons.items() if p.get("status") != "merged"}
        print(f"Total active persons: {len(active)}")
        for cid, p in sorted(active.items(), key=lambda x: x[1].get("primary_name") or "")[:50]:
            wxids = len(p.get("identifiers", {}).get("wxid_hashes") or [])
            emails = len(p.get("identifiers", {}).get("emails") or [])
            akas = len(p.get("aka") or [])
            print(f"  {cid:42s} primary={p.get('primary_name','?'):16s} aka={akas} wxid={wxids} email={emails}")
        if len(active) > 50:
            print(f"  ... and {len(active)-50} more (use 'show' for details)")
        return 0
    elif cmd == "show":
        if len(argv) < 3:
            print("usage: show <query>", file=sys.stderr)
            return 2
        print(json.dumps(show(argv[2]), ensure_ascii=False, indent=2))
        return 0
    elif cmd == "resolve":
        if len(argv) < 3:
            return 2
        r = resolve(argv[2])
        if r is None:
            print(f"UNRESOLVED: {argv[2]!r}")
            return 1
        print(r if isinstance(r, str) else f"AMBIGUOUS: {r}")
        return 0
    elif cmd == "expand":
        if len(argv) < 3:
            return 2
        r = expand_query(argv[2])
        for x in r:
            print(x)
        return 0
    elif cmd == "tag":
        if len(argv) < 3:
            return 2
        r = tag_filter_for(argv[2])
        if r is None:
            print("(unresolved)")
            return 1
        for t in r:
            print(t)
        return 0
    else:
        print(f"unknown cmd: {cmd}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
