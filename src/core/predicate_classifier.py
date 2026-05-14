"""predicate_classifier — classify graph predicates as
functional / multi_valued / unknown.

Today's audit: 5257/5413 predicates in graph are `unknown` (97%). The
semantic layer is raw extracted strings, not typed. This impairs
`_maybe_supersede`'s ability to automatically invalidate stale facts
when a new one arrives — functional predicates (`born_on`, `married_to`)
should supersede, multi_valued ones (`knows`, `uses_tool`) accumulate.

This module ships the infrastructure:
  - `classify(predicate) -> {'functional','multi_valued','unknown'}`
  - Stub mode for tests (heuristic lookup table, no LLM)
  - Real mode via `llm_provider.call_llm` (costs tokens; use sparingly)

Full migration (run on all 5257 unknowns) is deferred to a separate
CEO-approved task; this commit validates the classifier + does a
LIVE sample run on 10 predicates.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Literal, Optional


Classification = Literal["functional", "multi_valued", "unknown"]


# Stub table — deterministic for tests. Keys are predicate canonical
# (lowercase, underscore-joined).
_STUB_LOOKUP: Dict[str, Classification] = {
    "occurred_on": "functional",
    "born_on": "functional",
    "died_on": "functional",
    "married_to": "functional",
    "has_birthday": "functional",
    "located_in": "functional",
    "knows": "multi_valued",
    "uses": "multi_valued",
    "uses_tool": "multi_valued",
    "includes": "multi_valued",
    "prohibits": "multi_valued",
    "requires": "multi_valued",
    "relates_to": "multi_valued",
    "depends_on": "multi_valued",
    "connected_to": "multi_valued",
    "was": "unknown",
}


def _canonicalize(predicate: str) -> str:
    """Normalize: lowercase, spaces/hyphens→underscore, strip."""
    return predicate.strip().lower().replace(" ", "_").replace("-", "_")


def classify(predicate: str, mode: str = "stub") -> Classification:
    """Classify a predicate. mode='stub' uses heuristic lookup; 'real'
    calls the LLM provider (only used in explicit migrations)."""
    canon = _canonicalize(predicate)
    if mode == "stub":
        return _STUB_LOOKUP.get(canon, "unknown")

    # Real mode: LLM call via llm_provider
    try:
        from src.core.llm_provider import call_llm
    except Exception:
        return "unknown"

    prompt = (
        f"Classify the relational predicate `{predicate}` as one of:\n"
        f"  functional    (subject has at most ONE value; e.g. born_on, located_in)\n"
        f"  multi_valued  (subject may have many; e.g. knows, uses_tool)\n"
        f"  unknown       (cannot decide from the word alone)\n\n"
        f"Respond with exactly one word (no punctuation, no explanation)."
    )
    try:
        resp = call_llm(user=prompt, system="", tier="cheap_fast", timeout=8)
        ans = (resp or "").strip().lower()
        if ans in ("functional", "multi_valued", "unknown"):
            return ans  # type: ignore[return-value]
    except Exception:
        pass
    return "unknown"


def classify_batch(predicates: List[str], mode: str = "stub") -> Dict[str, Classification]:
    """Batch classify; preserves input order."""
    return {p: classify(p, mode=mode) for p in predicates}


def _cli(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="src.core.predicate_classifier")
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("one", help="classify a single predicate")
    po.add_argument("predicate")
    po.add_argument("--real", action="store_true",
                    help="use LLM (costs tokens)")

    pr = sub.add_parser("run", help="classify unknowns from graph")
    pr.add_argument("--limit", type=int, default=10)
    pr.add_argument("--real", action="store_true",
                    help="use LLM (costs tokens; default stub)")

    args = p.parse_args(argv)
    mode = "real" if getattr(args, "real", False) else "stub"

    if args.cmd == "one":
        r = classify(args.predicate, mode=mode)
        print(json.dumps({"predicate": args.predicate, "classification": r}))
        return 0

    if args.cmd == "run":
        # Pull unknown predicates from graph; classify N
        try:
            from src.core.graph_memory import _get_driver
            d = _get_driver()
            with d.session() as s:
                rs = s.run(
                    "MATCH (f:Fact) "
                    "WHERE f.predicate_canon IS NOT NULL "
                    "RETURN DISTINCT f.predicate_canon as pc LIMIT $lim",
                    lim=args.limit,
                )
                preds = [rec["pc"] for rec in rs if rec["pc"]]
        except Exception as e:
            print(json.dumps({"error": f"graph unreachable: {e}"}))
            return 1
        out = classify_batch(preds, mode=mode)
        print(json.dumps({
            "mode": mode,
            "sampled": len(preds),
            "classifications": out,
            "summary": {
                "functional": sum(1 for v in out.values() if v == "functional"),
                "multi_valued": sum(1 for v in out.values() if v == "multi_valued"),
                "unknown": sum(1 for v in out.values() if v == "unknown"),
            },
        }, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
