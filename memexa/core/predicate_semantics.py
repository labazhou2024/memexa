"""
predicate_semantics.py -- Predicate arity registry for graph memory.

A predicate is either:
  functional      single-valued at a given time (has exactly one object;
                  a new fact with different object supersedes the old one)
  multi_valued    set-valued (many simultaneously-true objects OK)
  unknown         not in either registry -> DEFAULT TO multi_valued
                  (conservative: unknown predicates never trigger
                   supersession, preventing silent mass-invalidation
                   when graph ingests new predicate vocabulary)

Used by graph_memory.write_fact to decide whether a new (S,P,O') that
collides with an existing (S,P,O) should invalidate the old one.

Design note (verifier blocker 2026-04-21): naive "same (S,P) different O
= supersession" destroys legitimate set-valued predicates
(uses_feature, has_component, works_on, coauthored_with, mentions...).
70%+ of project_*.md facts use such predicates. Defaulting unknown to
multi_valued is the safe floor.
"""
from __future__ import annotations

from typing import Literal

# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------
# Stored lowercased + underscores, matching canonicalize_predicate output.

FUNCTIONAL_PREDICATES = {
    "located_in",
    "current_advisor",
    "current_status",
    "birthdate",
    "os",
    "primary_role",
    "belongs_to_project",
    "version",
    "deadline_on",
    "assigned_to",
    "occurred_on",
    "scheduled_for",
    "has_canonical_name",
    "resolves_to",
    "is_a",
    "has_email",
    "has_workspace",
    "has_password",
    "has_port",
    "has_host",
    "has_username",
    "has_priority",
    "has_phase",
    "current_commit",
    "current_version",
    "current_model",
    "default_mode",
    "sunset_date",
    "replaced_by",
    "supersedes",
}

MULTI_VALUED_PREDICATES = {
    "uses_feature",
    "has_component",
    "works_on",
    "coauthored_with",
    "mentions",
    "depends_on",
    "references",
    "contains",
    "implements",
    "has_tag",
    "is_related_to",
    "cites",
    "produces",
    "consumes",
    "triggered_by",
    "calls",
    "imports",
    "exports",
    "authored_by",
    "studies",
    "collaborates_with",
    "tested_by",
    "covered_by",
    "reviewed_by",
    "applied_to",
    "linked_to",
    "observed_in",
    "includes",
    "has_example",
    "has_metric",
    "has_alias",
}

Classification = Literal["functional", "multi_valued", "unknown"]


def normalize_predicate_canon(raw: str) -> str:
    """TU-α3 (2026-04-21): canonicalize predicate to snake_case lower form.

    Extracted from classify_predicate so write_fact can normalize predicates
    at ingest time, eliminating the "sample pred" / "connected to" / hyphen
    violations found in production. Also re-used by classify_predicate
    itself (DRY).

    Rules: strip, lowercase, collapse hyphens+spaces → single underscore,
    collapse multi-underscore runs, strip leading/trailing underscores.
    Returns empty string for empty/None input.
    """
    if not raw:
        return ""
    import re as _re
    key = raw.strip().lower()
    key = _re.sub(r"[-\s]+", "_", key)
    key = _re.sub(r"_+", "_", key).strip("_")
    return key


def classify_predicate(pred_canon: str) -> Classification:
    """Return the arity class for a canonical predicate.

    Lookup is case-insensitive on the normalized form; unknown -> unknown
    (caller must treat unknown as multi_valued for the supersession
    decision). Centralised here so runtime tests + audit CLIs share a
    single truth.

    L-08 R1 fix (2026-04-21): collapse double separators so inputs like
    "located  in" or "located__in" still match the registry.
    TU-α3 (2026-04-21): now delegates to normalize_predicate_canon for
    the normalization step.
    """
    key = normalize_predicate_canon(pred_canon or "")
    if not key:
        return "unknown"
    if key in FUNCTIONAL_PREDICATES:
        return "functional"
    if key in MULTI_VALUED_PREDICATES:
        return "multi_valued"
    return "unknown"


def is_supersession_eligible(pred_canon: str) -> bool:
    """True iff a new fact with this predicate should invalidate the old
    (same S,P) fact. Returns True ONLY for classified-functional
    predicates. Unknown and multi_valued both return False.
    """
    return classify_predicate(pred_canon) == "functional"


def main() -> int:
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m memexa.core.predicate_semantics "
              "<classify|list> [args]", file=sys.stderr)
        return 1
    cmd = sys.argv[1]
    if cmd == "classify":
        if len(sys.argv) < 3:
            print("usage: classify <predicate>", file=sys.stderr)
            return 1
        print(classify_predicate(sys.argv[2]))
        return 0
    if cmd == "list":
        print(f"functional ({len(FUNCTIONAL_PREDICATES)}):")
        for p in sorted(FUNCTIONAL_PREDICATES):
            print(f"  {p}")
        print(f"multi_valued ({len(MULTI_VALUED_PREDICATES)}):")
        for p in sorted(MULTI_VALUED_PREDICATES):
            print(f"  {p}")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
