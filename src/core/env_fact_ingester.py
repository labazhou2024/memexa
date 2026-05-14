"""EnvFact: vendor-specific environment facts persisted in graph memory.

Purpose (plan_v1 M1): cache facts like "Docker Desktop 4.37.1 on Windows Home
defaults engine=stopped after GUI launch" so future sessions don't re-discover
them on every LIVE-class task.

Design:
  - source_episode_id = f"envfact:{slug(context)}"  (guaranteed no collision
    with real episodes; NOT matched by _TMP_PATTERNS in write_fact)
  - source_path = None                               (explicit; guard bypasses
    source_path-None records, so this is safe-by-construction, not safe-by-prefix)
  - entity.kind remains whatever classify_entity decides (often "other" for
    new vendor-specific canons). Phase β other_ratio is monitored upstream.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.core import graph_memory
# v2 facade for query path (2026-04-30 daemon repair); v1 still used by write
# path until env_fact_ingester is fully migrated to outbox.enqueue.
from src.core import graph_memory_v2 as _gm_v2


_MAX_FIELD_LEN = 1024  # S3: cap context/claim to prevent bloat/injection


def _slug(s: str) -> str:
    """Make a short stable slug from a context string."""
    base = re.sub(r"[^a-z0-9_]", "_", (s or "").lower().strip())[:60]
    base = base.strip("_") or "ctx"
    # Append a short hash so distinct contexts with same slug don't collide.
    h = hashlib.sha1((s or "").encode("utf-8", errors="replace")).hexdigest()[:6]
    return f"{base}_{h}"


def _source_episode_id(context: str) -> str:
    return f"envfact:{_slug(context)}"


def register_env_fact(
    context: str,
    claim: str,
    *,
    predicate: str = "is_true_of",
    confidence: float = 0.9,
) -> Optional[str]:
    """Persist a single env fact to the graph.

    Returns the fact_id from graph_memory.write_fact, or None on failure.
    """
    if not context or not claim:
        return None
    # S3 fix: cap lengths defensively so a crafted seed file can't grow
    # graph storage unboundedly.
    context = context[:_MAX_FIELD_LEN]
    claim = claim[:_MAX_FIELD_LEN]
    # Build a minimal fact dict matching graph_memory.write_fact's dict input shape.
    source_span = f"{context} :: {claim}"
    fact_dict: Dict[str, Any] = {
        "subject_raw": context,
        "subject_canon": context,
        "predicate_raw": predicate,
        "predicate_canon": predicate,
        "object_raw": claim,
        "object_canon": claim,
        "source_span": source_span,
        "confidence": float(confidence),
    }
    return graph_memory.write_fact(
        fact_dict,
        source_episode_id=_source_episode_id(context),
        source_path=None,  # explicit; guard sees None and skips tmpfs check
    )


def query_env_fact(context: str, limit: int = 10) -> List[Any]:
    """Return FactRows where source_episode_id is the envfact slug for
    this context. Falls back to substring entity query if exact slug misses.
    """
    try:
        # 2026-04-30 daemon repair: route through v2 (Hindsight) not v1 (Neo4j dead)
        rows = _gm_v2.query_entity(context, limit=limit * 3) or []
    except Exception:
        return []
    slug_id = _source_episode_id(context)
    # Prefer exact source_episode_id match; fall back to any envfact:* row.
    exact = [r for r in rows if getattr(r, "source_episode_id", "") == slug_id]
    if exact:
        return exact[:limit]
    env_only = [
        r for r in rows
        if str(getattr(r, "source_episode_id", "")).startswith("envfact:")
    ]
    return env_only[:limit]


def seed_from_file(path: Path, dry_run: bool = False) -> int:
    """Load seed env facts from a JSONL file.

    Each line: {"context": "...", "claim": "..."}
    Returns count of facts registered (or, with dry_run, count that WOULD be).
    """
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ctx = rec.get("context") or ""
        claim = rec.get("claim") or ""
        if not (ctx and claim):
            continue
        if dry_run:
            count += 1
            continue
        fid = register_env_fact(ctx, claim)
        if fid:
            count += 1
    return count


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="env_fact_ingester")
    sub = p.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("seed", help="load seed facts from JSONL")
    s1.add_argument("--file", default=None,
                    help="path to seed jsonl (default: .claude/harness/seed_env_facts.jsonl)")
    s1.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.cmd == "seed":
        # S3 fix: if --file is user-supplied, restrict it to the workspace
        # root to prevent `--file /etc/passwd` style path-traversal into
        # graph_memory.write_fact.
        workspace = Path(__file__).resolve().parent.parent.parent.parent
        if args.file:
            src = Path(args.file).resolve()
            if not str(src).startswith(str(workspace.resolve())):
                print(f"error: --file must be under workspace ({workspace})", file=sys.stderr)
                return 2
        else:
            src = workspace / ".claude" / "harness" / "seed_env_facts.jsonl"
        n = seed_from_file(src, dry_run=args.dry_run)
        print(f"env_fact_ingester seed: {n} facts ({'dry-run' if args.dry_run else 'applied'}) from {src}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
