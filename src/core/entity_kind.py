"""
entity_kind.py -- Lightweight entity type classifier for graph memory.

Hand-tuned heuristics over (canon, raw_forms). Conservative: falls through
to "other" whenever not confident. Never LLM-calls. Used by:
  - graph_memory.write_fact to set Entity.kind on first_seen
  - migrate_entity_kind() one-shot to backfill existing entities

Kinds:
  person      human actor (CEO, collaborators, advisors)
  project     named software or research initiative
  concept     abstract idea, method, theory
  constraint  rule/policy/HARD RULE
  tool        software library, CLI, API
  location    institution, physical place
  episode_ref memory-file pointer (fallback for file-like canons)
  other       not classifiable
"""
from __future__ import annotations

import re
from typing import Iterable, List

# ---------------------------------------------------------------------------
# Keyword banks (hand-labeled, additive)
# ---------------------------------------------------------------------------

_PERSON_TOKENS = {
    "Alice", "Alice", "demo_user", "ceo", "advisor",
    "collaborator", "user_remote_user", "student", "teacher",
    # 2026-04-22 Phase β expansion
    "mentor", "senior", "老师", "导师", "师兄", "师姐",
    "reviewer", "reviewers",
}

_LOCATION_TOKENS = {
    "ustc", "科大", "your-org", "hefei", "合肥",
    "shanghai", "beijing", "校园", "实验室",
    # 2026-04-22 Phase β expansion
    "workspace", "desktop", "桌面", "gpu server",
    "<remote-server-ip>", "28022",
}

_PROJECT_TOKENS = {
    "memex", "autopilot", "polymarket", "kairos",
    "wigner", "ene", "prl", "prb", "qccd", "paper",
    # 2026-04-22 Phase β expansion
    "manuscript", "submission", "bibliography", "revision",
    "benchmark", "campaign",
}

_TOOL_TOKENS = {
    "claude", "neo4j", "graphiti", "pytest", "opus", "sonnet", "haiku",
    "python", "bash", "git", "grep", "vllm", "qiskit", "qutip",
    "regex", "sdk", "api", "cli", "hook", "mcp",
    "graph_memory", "semantic_kb", "keyword_router",
    # 2026-04-22 Phase β expansion
    "powershell", "runas", "uac", "schtasks", "start transcript",
    "stdout", "stdin", "subprocess", "redirectstandardoutput",
    "pretool_gate", "session_gate", "plan_retro_gate",
    "agent_stall_detector", "_safe_fs", "_agent_spawns",
    "hook_pretool_agent", "hook_posttool_agent_complete",
    "memory_write_hook", "ac_verifier", "task_router",
    "chief-researcher", "security-reviewer", "logic-reviewer",
    "coverage-reviewer", "spec-reviewer", "planning-council",
    "knowledge-manager", "briefing-agent",
    "trace_sink", "event_bus", "pattern_extractor",
}

# L-05 R1 fix (2026-04-21): concept check runs BEFORE tool check so
# "memory retrieval", "RAG", etc. resolve to concept (their primary
# meaning) even if a tool-adjacent substring is present.
_CONCEPT_TOKENS = {
    "qubit", "spin", "wigner molecule", "charge defect",
    "memory retrieval", "retrieval", "embedding", "canonicalize",
    "rag", "finetuning", "qec", "decoherence",
    "graph memory", "knowledge graph",
    # 2026-04-22 Phase β expansion
    "feedback", "approval", "approval queue", "review gate",
    "plan gate", "persistent mode", "session", "automation",
    "workflow", "pipeline", "stage", "task_unit", "schema",
    "provenance", "plan_gap", "code_bug", "test_gap",
    "entity", "fact", "predicate", "episode",
    "solver", "simulation", "ensemble", "mc", "monte carlo",
    "figure", "caption", "figure legends", "plot",
    "survival oracle", "tripwire",
    "prose", "narrative", "reviewer schema",
    "ingestion", "extraction", "classifier",
    "research report", "md格式", "图解", "研究报告",
    "科研沟通风格", "组会",
}

_CONSTRAINT_TOKENS = {
    "hard rule", "must", "rule", "constraint", "policy",
    "permission", "deny", "forbidden", "禁止", "规则",
    # 2026-04-22 Phase β expansion
    "reinforcement", "quota", "timeout", "must not",
    "violation", "blocker", "retro patch", "enforcement",
    "exit code", "fail-soft", "fail-closed",
    "no session", "opt-out", "opt-in", "default-deny",
    "plan depth", "dont return early",
    "verify_cmd", "signal_hint", "axis_anchor",
    "hard-wired", "禁用", "不得", "必须",
    "must cite", "must include", "必要", "不可",
}

# file-like canons (common after bulk ingest)
_EPISODE_REF_PATTERNS = [
    re.compile(r"\.md$"),
    re.compile(r"/memory/"),
    re.compile(r"\\memory\\"),
]


def _any_token_in(tokens: Iterable[str], haystack: str) -> bool:
    """Case-insensitive substring membership."""
    h = haystack.lower()
    return any(t in h for t in tokens)


def classify_entity(canon: str, raw_forms: List[str] | None = None) -> str:
    """Return a kind label for an entity.

    Rules, short-circuit in order:
      1. file/path-like canon → episode_ref
      2. any CONSTRAINT token present → constraint
      3. any PERSON token present → person
      4. any LOCATION token present → location
      5. any PROJECT token present → project
      6. any TOOL token present → tool
      7. any CONCEPT token present → concept
      8. else → other
    """
    canon_l = (canon or "").lower().strip()
    if not canon_l:
        return "other"

    haystack_parts = [canon_l]
    if raw_forms:
        for rf in raw_forms[:8]:  # cap
            if isinstance(rf, str):
                haystack_parts.append(rf.lower())
    haystack = " | ".join(haystack_parts)

    for pat in _EPISODE_REF_PATTERNS:
        if pat.search(canon_l):
            return "episode_ref"

    if _any_token_in(_CONSTRAINT_TOKENS, haystack):
        return "constraint"
    if _any_token_in(_PERSON_TOKENS, haystack):
        return "person"
    if _any_token_in(_LOCATION_TOKENS, haystack):
        return "location"
    if _any_token_in(_PROJECT_TOKENS, haystack):
        return "project"
    # L-05 R1 reorder: concept runs BEFORE tool so "memory retrieval",
    # "knowledge graph" etc. classify as concept, not tool.
    if _any_token_in(_CONCEPT_TOKENS, haystack):
        return "concept"
    if _any_token_in(_TOOL_TOKENS, haystack):
        return "tool"

    return "other"


def main() -> int:
    import sys
    # L-10 R1 fix: `classify` without a canon argument previously raised
    # IndexError. Check arg count explicitly.
    if len(sys.argv) < 2:
        print("usage: python -m src.core.entity_kind classify <canon> [raw_form ...]",
              file=sys.stderr)
        return 1
    if sys.argv[1] == "classify":
        if len(sys.argv) < 3:
            print("usage: classify <canon> [raw_form ...]", file=sys.stderr)
            return 1
        canon = sys.argv[2]
        raws = sys.argv[3:]
    else:
        canon = sys.argv[1]
        raws = sys.argv[2:]
    print(classify_entity(canon, raws))
    return 0


if __name__ == "__main__":
    import sys as _s
    _s.exit(main())
