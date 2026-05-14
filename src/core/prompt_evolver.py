"""
[DEPRECATED 2026-05-12] KAIROS self-evolution frozen since 2026-04-04.
prompt_evolution.json last updated 2026-04-04 (2 prompts deployed, then dead).
Only KAIROS daemon called this; daemon hasn't run for 5+ weeks.

Prompt Evolver — TextGrad-inspired automatic prompt optimization.

Phase 3 of three-layer evolution architecture (Outer Loop).

Flow:
  1. Collect last N outcomes for an agent
  2. LLM generates "gradient" — concrete improvement suggestions
  3. LLM applies gradient to produce new prompt candidate
  4. Before/After evaluation on test cases
  5. If candidate wins by >= 2%, auto-deploy; else rollback

Inspired by: TextGrad (Nature), DSPy GEPA, EvoAgentX.

Storage: data/prompt_evolution.json (history + current prompts).
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"


def _extract_json(text: str) -> Optional[Dict]:
    """Extract JSON from LLM response that may contain markdown or other wrapping.

    Handles:
      - Pure JSON: {"suggestions": [...]}
      - Markdown code block: ```json\n{...}\n```
      - Text before/after JSON: "Here is the result: {...}"
    """
    import re
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try extracting from markdown code block
    md_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if md_match:
        try:
            return json.loads(md_match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # Try finding first { ... } block
    brace_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except (json.JSONDecodeError, ValueError):
            pass

    return None
_EVOLUTION_FILE = _DATA_DIR / "prompt_evolution.json"

MIN_OUTCOMES = 5          # Need at least this many outcomes to evolve
IMPROVEMENT_THRESHOLD = 0.02  # 2% improvement required to deploy (lowered from 5% — ECC/GEPA insight: strict thresholds block evolution startup)

# [L6 Phase 4 2026-04-18] Verdict sample guard per verifier R3.
# A/B decisions with <30 samples are noise (latitude-blog, EvoPrompt studies).
MIN_SAMPLES_FOR_VERDICT = 30

# [SEC-H1/H2/H3/M2 2026-04-19] Sanitization + safe-name validator.
# Defined EARLY so _load_eligible_agents (below) can use _SAFE_AGENT_NAME_RE.
import os as _os
import re as _re_mod

_SAFE_AGENT_NAME_RE = _re_mod.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_SECRET_PATTERNS = [
    (_re_mod.compile(r"(?i)\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{20,}"), "[REDACTED_SK_KEY]"),
    (_re_mod.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_GOOGLE]"),
    (_re_mod.compile(r"\bhf_[A-Za-z0-9]{20,}"), "[REDACTED_HF]"),
    (_re_mod.compile(r"\bAKIA[0-9A-Z]{16}"), "[REDACTED_AWS]"),
    (_re_mod.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{16,}"), "Bearer [REDACTED]"),
    (_re_mod.compile(r"(?i)authorization:\s*[^\r\n]+"), "Authorization: [REDACTED]"),
    (_re_mod.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s\r\n]+"),
     r"\1=[REDACTED]"),
    (_re_mod.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (_re_mod.compile(r"\b\d{11,}\b"), "[REDACTED_DIGITS]"),
]

_INJECTION_MARKERS_RE = _re_mod.compile(
    r"(?:<<<+|>>>+|###\s*(?:system|instruction|prompt|ignore)"
    r"|(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above)\s+"
    r"(?:instructions?|prompts?|rules?|constraints?))",
    flags=_re_mod.IGNORECASE,
)


def _sanitize_for_llm(text: str, max_len: int = 2000) -> str:
    """Scrub secrets + neutralize injection markers from untrusted agent output
    before embedding it in an LLM prompt. Defense-in-depth layer."""
    if not text:
        return ""
    out = text[:max_len]
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    out = _INJECTION_MARKERS_RE.sub("[FILTERED]", out)
    return out


def _is_safe_agent_name(name: str) -> bool:
    """Validate agent name before it touches any file-system path."""
    if not isinstance(name, str):
        return False
    return bool(_SAFE_AGENT_NAME_RE.match(name))


# Whitelist: only evolve high-frequency agents where sample count is sufficient
# and write-back risk is manageable. CEO can extend via env MEMEX_EVOLVE_AGENTS.
def _load_eligible_agents() -> set:
    raw = _os.environ.get(
        "MEMEX_EVOLVE_AGENTS",
        "code-reviewer,fix-agent,sonnet-executor",
    )
    out = set()
    for raw_entry in raw.split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if _is_safe_agent_name(entry):
            out.add(entry)
        else:
            logger.warning(
                "MEMEX_EVOLVE_AGENTS: dropping unsafe entry %r", raw_entry[:40]
            )
    return out


# [LOG-R1-015 2026-04-20] _ELIGIBLE_AGENTS computed at import time is visible
# via `from prompt_evolver import _ELIGIBLE_AGENTS` for backwards compatibility
# AND for direct monkeypatch in tests. But it only reflects the MEMEX_EVOLVE_AGENTS
# value at import time. For callers that can tolerate a function call, use
# _get_eligible_agents() which re-reads env each call (with a 30s TTL cache to
# protect hot paths).
_ELIGIBLE_AGENTS = _load_eligible_agents()

_ELIGIBLE_CACHE_TTL_SEC = 30.0
# Initialize cache to the module-load snapshot so monkeypatch detection
# (cache_sentinel is not module-level constant) fires on the first call
# after test monkeypatch. Fix 2026-04-20 for TestB2WhitelistEnvVarRespected.
_eligible_cache: tuple = (0.0, _ELIGIBLE_AGENTS)  # (expires_at_monotonic, value)


def _get_eligible_agents() -> set:
    """Dynamic accessor for the eligible-agents whitelist.

    Returns a *copy* of the current eligible-agents set. Honors later changes
    to the MEMEX_EVOLVE_AGENTS env var without requiring a process restart.

    Cache: 30s TTL on the parsed value, so 1k+ calls per minute from the
    hot path don't repeatedly parse the env string. TTL reset on every hit
    from the same env-var value so active sessions don't see stale data for
    more than 30s after a CEO change.

    Monkeypatch-friendly: if a test (or maintainer) directly rebinds
    ``_ELIGIBLE_AGENTS`` to a different set object, we detect that the
    module-level constant has diverged from our cached reference and return
    the monkeypatched value instead. This keeps legacy tests that override
    ``_ELIGIBLE_AGENTS`` working while still giving long-lived processes
    env-driven refresh.

    [LOG-R1-015] Replacement for a frozen module-level constant.
    """
    import sys as _sys
    import time as _time
    global _eligible_cache, _ELIGIBLE_AGENTS
    mod = _sys.modules.get(__name__)
    monkeypatched_value = getattr(mod, "_ELIGIBLE_AGENTS", None)
    now = _time.monotonic()
    expires, cached = _eligible_cache
    # Detect monkeypatch: module constant no longer identical to the cache
    # sentinel means someone rebound it; honor that override.
    if cached is not None and monkeypatched_value is not cached:
        return set(monkeypatched_value) if monkeypatched_value is not None else set()
    if cached is not None and now < expires:
        return set(cached)
    fresh = _load_eligible_agents()
    _eligible_cache = (now + _ELIGIBLE_CACHE_TTL_SEC, fresh)
    # Keep the module-level constant in sync so legacy imports reflect truth.
    _ELIGIBLE_AGENTS = fresh
    return set(fresh)


def _invalidate_eligible_cache() -> None:
    """Force next _get_eligible_agents() call to re-read env. Test helper."""
    global _eligible_cache
    _eligible_cache = (0.0, None)

# Track auto-disabled agents (2 failures in a row → disable)
_DISABLED_AGENTS_FILE = _DATA_DIR / "evolution_disabled_agents.json"
_FAILURE_STREAK_LIMIT = 2


def _load_disabled() -> set:
    if not _DISABLED_AGENTS_FILE.exists():
        return set()
    try:
        return set(json.loads(_DISABLED_AGENTS_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_disabled(disabled: set) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _DISABLED_AGENTS_FILE.write_text(
            json.dumps(sorted(disabled), ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def is_agent_eligible(agent_name: str) -> bool:
    """[L6] True if agent is in whitelist AND not auto-disabled.

    [LOG-R1-015 2026-04-20] Use _get_eligible_agents() to pick up MEMEX_EVOLVE_AGENTS
    env changes after import. 30s TTL cache keeps the hot path cheap.
    """
    if agent_name not in _get_eligible_agents():
        return False
    if agent_name in _load_disabled():
        return False
    return True


@dataclass
class EvolutionRecord:
    """Record of one evolution attempt."""
    agent_name: str
    timestamp: str
    old_prompt_hash: str
    new_prompt_hash: str
    old_score: float
    new_score: float
    improvement: float       # (new - old) / old
    deployed: bool
    gradient: str            # The improvement suggestions
    reason: str
    # [Round2 LOG-H5 2026-04-19] Distinguish actual evolve attempts from
    # telemetry-only rows (gate blocks). "attempt" = real A/B evaluation.
    # "blocked" = gate rejected before any LLM work. deployment_rate should
    # exclude "blocked" rows from its denominator.
    record_type: str = "attempt"

    def to_dict(self) -> dict:
        return asdict(self)


class PromptEvolver:
    """Manages prompt evolution for Agent definitions."""

    def __init__(self, evolution_file: Path = None):
        self._file = evolution_file or _EVOLUTION_FILE
        self._history: List[EvolutionRecord] = []
        self._current_prompts: Dict[str, str] = {}  # agent_name → current prompt
        self._load()

    def _load(self):
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self._history = [
                    EvolutionRecord(**r) for r in data.get("history", [])
                ]
                self._current_prompts = data.get("current_prompts", {})
            except Exception as e:
                logger.warning("Failed to load evolution data: %s", e)

    def _save(self):
        data = {
            "version": "1.0",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "current_prompts": self._current_prompts,
            "history": [r.to_dict() for r in self._history[-50:]],  # Keep last 50
        }
        atomic_write_json(self._file, data)

    def check_baseline(
        self,
        old_score: float,
        agent_name: str = "unknown",
        current_prompt: str = "",
    ) -> Optional[Dict]:
        """[AC-10 2026-04-20] Check whether the baseline score is sufficient for evolution.

        Returns a verdict dict if the baseline is too low (deployment blocked),
        or None if the baseline is acceptable (caller may proceed).

        Verdict dict schema:
          {"verdict": "needs_more_data", "reason": "baseline too low",
           "old_score": <float>, "agent_name": <str>}

        Side-effect: appends a record_type="blocked" entry to self._history
        and saves when baseline is insufficient. This ensures observability
        without requiring a full async evolve() call.
        """
        if old_score < 0.5:
            logger.info(
                "Zero-baseline guard: %s has degenerate old_score=%.3f; "
                "refusing to deploy (need real baseline data)",
                agent_name, old_score,
            )
            record = EvolutionRecord(
                agent_name=agent_name,
                timestamp=datetime.utcnow().isoformat() + "Z",
                old_prompt_hash=str(hash(current_prompt))[-8:] if current_prompt else "n/a",
                new_prompt_hash="n/a",
                old_score=round(old_score, 3),
                new_score=0.0,
                improvement=0.0,
                deployed=False,
                gradient="",
                reason=f"zero_baseline (old_score={old_score:.3f} < 0.5)",
                record_type="blocked",
            )
            self._history.append(record)
            self._save()
            return {
                "verdict": "needs_more_data",
                "reason": "baseline too low",
                "old_score": round(old_score, 3),
                "agent_name": agent_name,
            }
        return None

    async def evolve(
        self,
        agent_name: str,
        current_prompt: str,
        outcomes: List[Dict],
        test_fn=None,
    ) -> Optional[str]:
        """Attempt to evolve an agent's prompt.

        Args:
            agent_name: Name of the agent being evolved
            current_prompt: The current system prompt / agent instructions
            outcomes: List of {task, score, output_summary} from recent executions
            test_fn: Optional async callable(prompt) -> float score for validation

        Returns:
            New prompt if improved, None if no improvement or error.
        """
        # [SEC-H2 Round2 2026-04-19] Defense-in-depth: reject unsafe agent
        # name before any work. Write-back path also validates, but early
        # rejection also prevents wasted LLM calls on invalid targets.
        if not _is_safe_agent_name(agent_name):
            logger.warning("evolve() refused unsafe agent_name: %r", agent_name[:60])
            return None

        if len(outcomes) < MIN_OUTCOMES:
            logger.info("Not enough outcomes (%d < %d) to evolve %s",
                       len(outcomes), MIN_OUTCOMES, agent_name)
            return None

        # [Gap 2d + LOG-M1 Round2 2026-04-19] Verdict-grade gate.
        # MUST be checked BEFORE any LLM round-trip (gradient / apply / test).
        # <30 samples has stddev too large to detect 2% improvement above noise.
        _skip_sample_gate = _os.environ.get("MEMEX_SKIP_SAMPLE_GATE", "0") == "1"
        _samples_ok = len(outcomes) >= MIN_SAMPLES_FOR_VERDICT
        if not _samples_ok and not _skip_sample_gate:
            logger.info(
                "Verdict-grade gate: %s has %d samples < MIN_SAMPLES_FOR_VERDICT=%d; "
                "skipping deploy decision (set MEMEX_SKIP_SAMPLE_GATE=1 to override)",
                agent_name, len(outcomes), MIN_SAMPLES_FOR_VERDICT,
            )
            # [LOG-H5 Round2] Mark as "blocked" so deployment_rate / score
            # averages can exclude it. Scores remain 0.0 but consumers key on
            # record_type, not the sentinel values.
            record = EvolutionRecord(
                agent_name=agent_name,
                timestamp=datetime.utcnow().isoformat() + "Z",
                old_prompt_hash=str(hash(current_prompt))[-8:],
                new_prompt_hash="n/a",
                old_score=0.0,
                new_score=0.0,
                improvement=0.0,
                deployed=False,
                gradient="",
                reason=f"insufficient_samples ({len(outcomes)}/{MIN_SAMPLES_FOR_VERDICT})",
                record_type="blocked",
            )
            self._history.append(record)
            self._save()
            return None

        # [LOG-H2 Round2 + LOG2-M1 Round3 2026-04-19] Zero-baseline trap guard.
        # If all outcomes have score 0, old_score=0.0; max(old_score,0.01)
        # floors divisor to 0.01, any positive new_score yields huge
        # improvement % and auto-deploys. Require a real baseline.
        # Default=0 (safer: missing score treated as zero; matches improvement
        # calc below — both use default=0 for consistency).
        _raw_old = sum(o.get("score", 0) for o in outcomes) / max(len(outcomes), 1)
        # [AC-10 2026-04-20] Use check_baseline() for observable verdict.
        _baseline_check = self.check_baseline(_raw_old, agent_name, current_prompt)
        if _baseline_check is not None:
            # Record was already appended inside check_baseline; just return None.
            return None

        from .llm_router import get_router, TaskType
        from .event_bus import log_event

        router = get_router()
        client = router.get_client()
        if not client:
            return None

        # Step 1: Generate gradient (improvement suggestions)
        gradient = await self._generate_gradient(
            router, agent_name, current_prompt, outcomes
        )
        if not gradient:
            return None

        # Step 2: Apply gradient to produce new prompt
        new_prompt = await self._apply_gradient(
            router, current_prompt, gradient
        )
        if not new_prompt or new_prompt == current_prompt:
            return None

        # Step 3: Before/After evaluation
        # [LOG2-M1 Round3 2026-04-19] default=0 consistent with zero-baseline
        # guard above (missing score = zero, not neutral-3 — safer + guard-aware)
        old_score = sum(o.get("score", 0) for o in outcomes) / len(outcomes)

        if test_fn is None:
            test_fn = self._make_default_test_fn(agent_name, outcomes)

        try:
            new_score = await test_fn(new_prompt)
        except Exception as e:
            logger.warning("Test function failed: %s", e)
            new_score = old_score

        improvement = (new_score - old_score) / max(old_score, 0.01)

        # Step 4: Deploy decision
        deployed = improvement >= IMPROVEMENT_THRESHOLD
        record = EvolutionRecord(
            agent_name=agent_name,
            timestamp=datetime.utcnow().isoformat() + "Z",
            old_prompt_hash=str(hash(current_prompt))[-8:],
            new_prompt_hash=str(hash(new_prompt))[-8:],
            old_score=round(old_score, 3),
            new_score=round(new_score, 3),
            improvement=round(improvement, 4),
            deployed=deployed,
            gradient=gradient[:500],
            reason="auto-deployed" if deployed else f"insufficient improvement ({improvement:.1%} < {IMPROVEMENT_THRESHOLD:.0%})",  # threshold lowered from 5% to 2%
        )
        self._history.append(record)

        if deployed:
            self._current_prompts[agent_name] = new_prompt
            # Write evolved prompt back to .claude/agents/{name}.md
            if self._write_back_to_agent_file(agent_name, new_prompt):
                logger.info("Evolved %s: %.2f -> %.2f (+%.1f%%), deployed + written to .md",
                           agent_name, old_score, new_score, improvement * 100)
            else:
                logger.info("Evolved %s: %.2f -> %.2f (+%.1f%%), deployed (write-back failed)",
                           agent_name, old_score, new_score, improvement * 100)
        else:
            logger.info("Evolution rejected for %s: improvement %.1f%% < %.0f%%",
                       agent_name, improvement * 100, IMPROVEMENT_THRESHOLD * 100)

        log_event("prompt_evolution", agent=agent_name, details={
            "old_score": record.old_score,
            "new_score": record.new_score,
            "improvement": record.improvement,
            "deployed": deployed,
        })

        self._save()
        return new_prompt if deployed else None

    def _make_default_test_fn(self, agent_name: str, outcomes: List[Dict]):
        """Create a default test function using LLM as A/B evaluator.

        [Gap 2c fix 2026-04-19] Output-aware grounded evaluation.
        Instead of asking the LLM "is this prompt good?" (which creates
        meta-judge bias toward verbose/templated writing), we feed it the
        ACTUAL failure and success outputs from recent runs and ask whether
        the candidate prompt would have produced better outputs.

        Primary mode: output-grounded score (1-5) — sees real raw_output
        Fallback mode: output-grounded pairwise A/B
        """
        # Capture current_prompt for pairwise fallback
        current_prompt = self._current_prompts.get(agent_name, "")

        # [Gap 2c + SEC-H1 Round2] Pull REAL exemplars BUT sanitize every
        # untrusted raw_output before embedding it in the LLM prompt. Scrubs
        # secrets AND neutralizes prompt-injection markers.
        # Bound context: 3 failures + 3 successes × 400 chars = ~2.4K chars.
        failures = [o for o in outcomes if o.get("score", 0) <= 2][:3]
        successes = [o for o in outcomes if o.get("score", 0) >= 4][:3]

        def _fmt_exemplar(o, label):
            task = _sanitize_for_llm((o.get("task") or ""), max_len=120)
            raw_src = o.get("raw_output")
            if raw_src is None:
                raw_src = o.get("output_summary") or ""
            raw = _sanitize_for_llm(raw_src.strip(), max_len=400)
            score = o.get("score", 0)
            if raw:
                return f"[{label} score={score}] TASK: {task}\n  OUTPUT: {raw}"
            return f"[{label} score={score}] TASK: {task}  (no output captured)"

        exemplars = []
        for o in failures:
            exemplars.append(_fmt_exemplar(o, "FAIL"))
        for o in successes:
            exemplars.append(_fmt_exemplar(o, "OK"))
        exemplars_text = "\n\n".join(exemplars) if exemplars else "(no exemplars available)"

        async def _test_fn(new_prompt: str) -> float:
            from .llm_router import get_router, TaskType
            router = get_router()
            client = router.get_client()
            if not client:
                return 3.0

            test_cases = [o.get("task", "") for o in outcomes[:3] if o.get("task")]
            if not test_cases:
                return 3.0

            # --- Primary: output-grounded score-based evaluation ---
            eval_prompt = (
                f"Evaluate whether this CANDIDATE prompt would improve agent '{agent_name}'.\n\n"
                f"## CANDIDATE PROMPT\n{new_prompt[:2000]}\n\n"
                f"## REAL HISTORICAL EXEMPLARS (from past runs of this agent)\n"
                f"{exemplars_text}\n\n"
                "## Your Task\n"
                "Judge whether the CANDIDATE prompt, if used, would have:\n"
                "  - Prevented the FAIL outputs (or produced clearly better ones)?\n"
                "  - Preserved / replicated the OK outputs?\n\n"
                "Score 1-5:\n"
                "  1 = would make things worse (adds wrong instructions)\n"
                "  2 = misses the patterns in the failures\n"
                "  3 = neutral — same failures would likely recur\n"
                "  4 = addresses most failure patterns, preserves successes\n"
                "  5 = directly addresses all observed failure modes with specific rules\n\n"
                'Return JSON: {"score": N, "reason": "..."}'
            )

            try:
                response = router.call(
                    task_type=TaskType.CHAT,
                    messages=[{"role": "user", "content": eval_prompt}],
                    temperature=0.1,
                    max_tokens=200,
                    response_format={"type": "json_object"},
                )
                data = _extract_json(response)
                if data is not None:
                    score = float(data.get("score", 3.0))
                    logger.info("Default test_fn scored %s prompt: %.1f (%s)",
                               agent_name, score, data.get("reason", "")[:80])
                    return score

                # JSON parse failed — log raw response and fall through to pairwise
                logger.warning(
                    "Default test_fn: could not parse score JSON for %s; "
                    "raw response: %s",
                    agent_name, response[:300],
                )
            except Exception as e:
                logger.warning("Default test_fn score-based call failed: %s", e)

            # --- Fallback: pairwise comparison ---
            if not current_prompt:
                logger.warning(
                    "Default test_fn: no current_prompt for pairwise fallback on %s",
                    agent_name,
                )
                return 3.0

            # [Gap 2c] Output-grounded pairwise: judge on real outputs, not
            # on prompt stylistics.
            pairwise_prompt = (
                f"Compare two system prompts for agent '{agent_name}'. Decide which "
                f"would produce BETTER outputs on this agent's real tasks.\n\n"
                f"## PROMPT A (current)\n{current_prompt[:1500]}\n\n"
                f"## PROMPT B (candidate)\n{new_prompt[:1500]}\n\n"
                f"## REAL HISTORICAL OUTPUTS\n{exemplars_text}\n\n"
                "Focus on: which prompt would have prevented the FAIL outputs "
                "while preserving the OK outputs? Ignore length / style / politeness.\n"
                'Return JSON: {"winner": "A" or "B", "reason": "..."}'
            )

            try:
                pairwise_response = router.call(
                    task_type=TaskType.CHAT,
                    messages=[{"role": "user", "content": pairwise_prompt}],
                    temperature=0.1,
                    max_tokens=200,
                    response_format={"type": "json_object"},
                )
                pairwise_data = _extract_json(pairwise_response)
                if pairwise_data is not None:
                    winner = pairwise_data.get("winner", "A").strip().upper()
                    reason = pairwise_data.get("reason", "")[:80]
                    # B wins → new prompt scores above baseline; A wins → below
                    score = 4.0 if winner == "B" else 2.5
                    logger.info(
                        "Default test_fn pairwise: %s winner=%s (%.1f) — %s",
                        agent_name, winner, score, reason,
                    )
                    return score

                logger.warning(
                    "Default test_fn: pairwise JSON parse also failed for %s; "
                    "raw response: %s",
                    agent_name, pairwise_response[:300],
                )
            except Exception as e:
                logger.warning("Default test_fn pairwise call failed: %s", e)

            return 3.0

        return _test_fn

    async def _generate_gradient(
        self, router, agent_name: str, prompt: str, outcomes: List[Dict]
    ) -> str:
        """Generate improvement suggestions (the 'gradient')."""
        from .llm_router import TaskType
        # Separate successes and failures
        successes = [o for o in outcomes if o.get("score", 0) >= 4]
        failures = [o for o in outcomes if o.get("score", 0) <= 2]

        # [Gap 2e + SEC-H1 Round2] Include raw_output (sanitized!) so gradient
        # LLM can see WHY each run failed/succeeded. Budget ~5KB total.
        # Sanitization scrubs secrets and neutralizes injection markers.
        def _fmt(o):
            score = o.get("score", 0)
            task = _sanitize_for_llm(o.get("task", ""), max_len=80)
            raw_src = o.get("raw_output")
            if raw_src is None:
                raw_src = o.get("output_summary") or ""
            out = _sanitize_for_llm(raw_src.strip(), max_len=400)
            if out:
                return f"- [score={score}] {task}\n    OUTPUT: {out}"
            return f"- [score={score}] {task}"

        outcomes_text = ""
        if failures:
            outcomes_text += "FAILURES:\n" + "\n".join(_fmt(o) for o in failures[:5])
        if successes:
            outcomes_text += "\n\nSUCCESSES:\n" + "\n".join(_fmt(o) for o in successes[:5])

        gradient_prompt = f"""You are optimizing an AI agent's system prompt.

## Agent: {agent_name}

## Current Prompt (excerpt)
{prompt[:2000]}

## Recent Outcomes
{outcomes_text}

## Your Job
Generate 2-4 specific, concrete improvements to the prompt.
Focus on:
1. Patterns in failures — what instruction is missing or unclear?
2. Patterns in successes — what should be reinforced?
3. Remove vague instructions, add specific "if X then Y" rules.

Be CONCRETE: "Add the sentence 'Always check return values'" not "Improve error handling".
Return JSON: {{"suggestions": ["suggestion1", "suggestion2"]}}"""

        try:
            response = router.call(
                task_type=TaskType.CHAT,
                messages=[{"role": "user", "content": gradient_prompt}],
                temperature=0.3,
                max_tokens=500,
                response_format={"type": "json_object"},
            )
            if not response or not response.strip():
                logger.warning("Gradient generation: empty response from LLM")
                return ""
            data = _extract_json(response)
            if not data:
                logger.warning("Gradient generation: could not extract JSON from: %s", response[:200])
                return ""
            suggestions = data.get("suggestions", [])
            return "\n".join(f"- {s}" for s in suggestions) if suggestions else ""
        except Exception as e:
            logger.warning("Gradient generation failed: %s", e)
            return ""

    async def _apply_gradient(self, router, prompt: str, gradient: str) -> str:
        """Apply gradient to produce new prompt."""
        from .llm_router import TaskType
        apply_prompt = f"""Apply these improvements to the prompt.

## Original Prompt
{prompt[:3000]}

## Improvements to Apply
{gradient}

## Rules
- Keep the same overall structure and length
- Only add/modify/remove what the improvements specify
- Do not add commentary or explanation
- Output the complete new prompt, ready to use"""

        try:
            response = router.call(
                task_type=TaskType.CHAT,
                messages=[{"role": "user", "content": apply_prompt}],
                temperature=0.2,
                max_tokens=3000,
            )
            return response.strip()
        except Exception as e:
            logger.warning("Gradient application failed: %s", e)
            return ""

    def _write_back_to_agent_file(self, agent_name: str, new_prompt: str) -> bool:
        """Write evolved prompt back to .claude/agents/{name}.md, preserving YAML frontmatter.

        [SEC-H2 Round2 2026-04-19] Validate agent_name against strict regex
        AND verify resolved path stays under the agents_dir (belt + braces).
        """
        import re as _re
        if not _is_safe_agent_name(agent_name):
            logger.error("Write-back refused: unsafe agent_name %r", agent_name[:60])
            return False

        # Find agents dir relative to workspace
        agents_dir = (self._file.parent.parent.parent.parent / ".claude" / "agents").resolve()
        agent_file = (agents_dir / f"{agent_name}.md").resolve()

        # Path-traversal check: resolved path must remain under agents_dir.
        try:
            agent_file.relative_to(agents_dir)
        except ValueError:
            logger.error("Write-back refused: path escapes agents_dir: %s", agent_file)
            return False

        if not agent_file.exists():
            logger.warning("Agent file not found for write-back: %s", agent_file)
            return False

        try:
            old_content = agent_file.read_text(encoding="utf-8")

            # Create backup
            backup = agent_file.with_suffix(".md.bak")
            backup.write_text(old_content, encoding="utf-8")

            # Preserve YAML frontmatter (--- ... ---)
            fm_match = _re.match(r'^(---\n.*?\n---)\n', old_content, _re.DOTALL)
            if fm_match:
                new_content = fm_match.group(1) + "\n\n" + new_prompt
            else:
                new_content = new_prompt

            agent_file.write_text(new_content, encoding="utf-8")
            logger.info("Wrote evolved prompt to %s (backup: %s)", agent_file.name, backup.name)
            return True
        except Exception as e:
            logger.error("Write-back failed for %s: %s", agent_name, e)
            return False

    # [LOG-H1 + LOG-H5 Round2 2026-04-19] Count only real attempt rows.
    # Blocked (gate-telemetry) rows must NOT dilute deployment_rate.
    def _real_attempts(self) -> List[EvolutionRecord]:
        return [r for r in self._history
                if getattr(r, "record_type", "attempt") == "attempt"]

    @property
    def evolution_count(self) -> int:
        """Total REAL evolve attempts (excluding gate-blocked telemetry)."""
        return len(self._real_attempts())

    @property
    def total_records(self) -> int:
        """All rows incl. blocked telemetry — for dashboards."""
        return len(self._history)

    @property
    def deployment_rate(self) -> float:
        real = self._real_attempts()
        if not real:
            return 0.0
        deployed = sum(1 for r in real if r.deployed)
        return deployed / len(real)

    @property
    def stats(self) -> Dict:
        blocked = [r for r in self._history
                   if getattr(r, "record_type", "attempt") == "blocked"]
        return {
            "total_evolutions": self.evolution_count,
            "total_records": self.total_records,
            "blocked_count": len(blocked),
            "deployment_rate": round(self.deployment_rate, 3),
            "agents_evolved": list(self._current_prompts.keys()),
            "last_evolution": self._history[-1].timestamp if self._history else None,
        }


# Singleton
_instance: Optional[PromptEvolver] = None


def get_prompt_evolver() -> PromptEvolver:
    global _instance
    if _instance is None:
        _instance = PromptEvolver()
    return _instance


def _cli_main():
    """arch_v2 §F P4 CLI: `python -m src.core.prompt_evolver --status`."""
    import argparse as _ap
    import json as _json
    import os as _os
    import sys as _sys
    ap = _ap.ArgumentParser(description="prompt_evolver status inspector")
    ap.add_argument("--status", action="store_true",
                    help="print current evolver stats + pending approvals")
    args = ap.parse_args()
    if not args.status:
        ap.print_help()
        return 0
    ev = get_prompt_evolver()
    out = {
        "autorun_enabled": _os.environ.get("MEMEX_L6_AUTORUN", "0") == "1",
        "evolution_enabled": _os.environ.get("MEMEX_L6_EVOLUTION", "0") == "1",
        "eligible_agents": sorted(list(_ELIGIBLE_AGENTS)),
        "stats": ev.stats,
    }
    print(_json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli_main())
