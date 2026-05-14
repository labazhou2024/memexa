"""
Feedback Collector — Bridge between KAIROS execution and evolution stack.

Wires W1 (result → semantic_memory), W2 (success → metrics), W5 (quality → evolver trigger).

After KAIROS executes a project, this module:
  1. Computes a quality_score (1-5) from objective signals
  2. Records an episode to semantic_memory (source="kairos")
  3. Logs a scored event for evolution_metrics consumption
  4. Tags low-quality results for prompt_evolver targeting

Usage:
    from src.core.feedback_collector import collect_feedback
    collect_feedback(project, result)
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DATA = Path(__file__).parent.parent / "data"
_FEEDBACK_FILE = _DATA / "kairos_feedback.jsonl"


def _compute_quality_score(project: Dict, result: Dict) -> int:
    """Compute 1-5 quality score from objective signals.

    Scoring rubric (binary signals, not LLM subjective):
      +1 base (attempted)
      +1 if success (exit code 0 + subtype success)
      +1 if output mentions "passed" or "commit" (real work done)
      +1 if cost < budget (efficient)
      +1 if turns < max_turns (didn't exhaust context)
    """
    score = 1  # Base: attempted

    if result.get("success"):
        score += 1

    output = (result.get("output") or "").lower()
    if "passed" in output or "commit" in output or "committed" in output:
        score += 1

    budget = project.get("max_budget_usd", 1.0)
    cost = result.get("cost_usd", 0)
    if budget > 0 and cost < budget * 0.8:
        score += 1

    max_turns = project.get("max_turns", 25)
    turns = result.get("num_turns", 0)
    if max_turns > 0 and turns < max_turns * 0.9:
        score += 1

    return min(5, max(1, score))


def _extract_summary(result: Dict) -> str:
    """Extract a concise summary from project output."""
    output = result.get("output", "")
    if not output:
        return result.get("error", "no output")

    # Take last meaningful lines (usually the summary)
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    summary_lines = lines[-5:] if len(lines) > 5 else lines
    return "\n".join(summary_lines)[:500]


def _extract_causal_fields(project: Dict, result: Dict) -> Dict[str, Any]:
    """Extract causal annotation fields from project+result.

    Returns:
        action: what the project attempted (from title / prompt first line)
        outcome: success/failure + key metrics
        reason: why it succeeded/failed (extracted from output)
        git_diff_stat: count of files changed if mentioned in output
    """
    # --- action ---
    title = project.get("title", "")
    prompt = project.get("prompt", "")
    if title:
        action = title.strip()[:120]
    elif prompt:
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
        action = first_line[:120]
    else:
        action = "unknown"

    # --- outcome ---
    success = result.get("success", False)
    cost = result.get("cost_usd", 0)
    turns = result.get("num_turns", 0)
    outcome = f"{'success' if success else 'failure'} | cost=${cost:.3f} | turns={turns}"

    # --- reason ---
    output = result.get("output") or ""
    error = result.get("error") or ""
    reason = ""
    if not success:
        # Look for error/failure indicators
        for line in reversed(output.strip().splitlines()):
            line_s = line.strip().lower()
            if any(kw in line_s for kw in ("error", "fail", "exception", "traceback", "refused", "timeout")):
                reason = line.strip()[:200]
                break
        if not reason and error:
            reason = error[:200]
        if not reason:
            reason = "unknown failure"
    else:
        # For success, grab last meaningful line as reason
        lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
        if lines:
            reason = lines[-1][:200]
        else:
            reason = "completed successfully"

    # --- git_diff_stat ---
    git_diff_stat: Optional[int] = None
    # Match patterns like "3 files changed", "changed 5 files", "modified 2 files"
    diff_match = re.search(r'(\d+)\s+files?\s+changed', output)
    if not diff_match:
        diff_match = re.search(r'(?:changed|modified|updated)\s+(\d+)\s+files?', output, re.IGNORECASE)
    if diff_match:
        git_diff_stat = int(diff_match.group(1))

    return {
        "action": action,
        "outcome": outcome,
        "reason": reason,
        "git_diff_stat": git_diff_stat,
    }


def _detect_agent_role(project: Dict) -> str:
    """Detect which agent role this project corresponds to.

    [Gap 2a + Round2 LOG-M2/L1 2026-04-19] Lookup order:
      1. project["agent_role"] | ["agent_name"]       (explicit top-level)
      2. project["metadata"]["agent_name"] | ["agent_role"]
      3. title prefix (startswith — not substring)
      4. "kairos-general"

    All resolved names are normalized via .strip().lower() so
    "Fix-Agent" matches whitelist entry "fix-agent".
    """
    def _norm(v):
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
        return None

    # --- explicit fields (preferred) ---
    for key in ("agent_role", "agent_name"):
        n = _norm(project.get(key))
        if n:
            return n

    meta = project.get("metadata") or {}
    if isinstance(meta, dict):
        for key in ("agent_name", "agent_role"):
            n = _norm(meta.get(key))
            if n:
                return n

    # --- title heuristics (legacy) ---
    title = project.get("title", "") or ""
    if "[BigLoop" in title:
        match = re.search(r'\[BigLoop\s+(\S+)\]', title)
        if match:
            name = match.group(1)
            if name == "FULL":
                return "big-loop-orchestrator"
            return name.lower()

    # [LOG-L1 Round2] Use startswith so a body mentioning both "REVIEW:"
    # and "FIX:" is not accidentally classified by the more-common prefix.
    stripped_title = title.lstrip()
    _PREFIX_MAP = (
        ("CODE-REVIEW:", "code-reviewer"),
        ("REVIEW:", "code-reviewer"),
        ("FIX:", "fix-agent"),
        ("TEST:", "test-runner"),
        ("EVOLVE:", "evolution"),
        ("INVESTIGATE:", "investigator"),
        ("IMPL:", "sonnet-executor"),
        ("EXECUTE:", "sonnet-executor"),
        ("RESEARCH:", "research-assistant"),
        ("ARCHITECT:", "architect"),
        ("VERIFY:", "verifier"),
    )
    for prefix, role in _PREFIX_MAP:
        if stripped_title.startswith(prefix):
            return role

    return "kairos-general"


# [SEC-H3 Round2 2026-04-19] Scrub patterns for raw_output BEFORE persistence
# to kairos_feedback.jsonl. Mirrors prompt_evolver._sanitize_for_llm — kept
# separate to avoid circular imports and so the feedback file on disk is
# already clean.
_FB_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{20,}"), "[REDACTED_SK_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_GOOGLE]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "[REDACTED_HF]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}"), "[REDACTED_AWS]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{16,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)authorization:\s*[^\r\n]+"), "Authorization: [REDACTED]"),
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*[^\s\r\n]+"),
     r"\1=[REDACTED]"),
]


def _scrub_raw_output(text: str, max_len: int = 2000) -> str:
    if not text:
        return ""
    out = text[:max_len]
    for pat, repl in _FB_SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


# [SEC-M3 Round3 final 2026-04-19] Per-process verifier call cap. Guards
# against runaway external LLM spend when MEMEXA_AUTO_VERIFY_QUALITY=1 is
# combined with a high-throughput feedback stream. Default 50 calls/process;
# CEO can override via MEMEXA_AUTO_VERIFY_MAX.
import threading as _thr_mod
_VERIFIER_CALL_LOCK = _thr_mod.Lock()
_VERIFIER_CALL_COUNT = 0
# [AC-8 2026-04-20] Track quota-exhaustion events per process for observability.
_VERIFIER_BUDGET_EXHAUSTED_COUNT = 0


def _verifier_quota_remaining() -> int:
    max_calls = int(os.environ.get("MEMEXA_AUTO_VERIFY_MAX", "50"))
    with _VERIFIER_CALL_LOCK:
        return max(0, max_calls - _VERIFIER_CALL_COUNT)


def _consume_verifier_quota() -> bool:
    """Returns True if this call is within budget; atomically increments."""
    global _VERIFIER_CALL_COUNT
    max_calls = int(os.environ.get("MEMEXA_AUTO_VERIFY_MAX", "50"))
    with _VERIFIER_CALL_LOCK:
        if _VERIFIER_CALL_COUNT >= max_calls:
            return False
        _VERIFIER_CALL_COUNT += 1
        return True


def _reset_verifier_quota_for_tests() -> None:
    """Test hook only — not called in production code path."""
    global _VERIFIER_CALL_COUNT, _VERIFIER_BUDGET_EXHAUSTED_COUNT
    with _VERIFIER_CALL_LOCK:
        _VERIFIER_CALL_COUNT = 0
        _VERIFIER_BUDGET_EXHAUSTED_COUNT = 0


def get_verifier_budget_exhausted_count() -> int:
    """Return how many times verifier quota was exhausted in this process (for tests/dashboard)."""
    with _VERIFIER_CALL_LOCK:
        return _VERIFIER_BUDGET_EXHAUSTED_COUNT


def collect_feedback(project: Dict, result: Dict) -> Dict[str, Any]:
    """Core function: process KAIROS result into evolution signals.

    Args:
        project: The project dict (title, prompt, priority, etc.)
        result: The execution result (success, output, cost, turns, etc.)

    Returns:
        Feedback dict with quality_score, agent_role, summary.
    """
    # [Gap 2b + LOG-H3 Round2 2026-04-19] Prefer real_quality_verifier
    # (low-noise multi-signal) over legacy rule-based score. Do NOT mutate
    # caller's result dict — use a local variable.
    import os as _os
    quality_report = result.get("quality_report")
    score_source = "pre_computed"  # default when caller attached quality_report
    if not (quality_report and isinstance(quality_report, dict)):
        if _os.environ.get("MEMEXA_AUTO_VERIFY_QUALITY", "0") == "1":
            # [SEC-M3 Round3 final] Rate-cap: if quota exhausted, fall through
            # to compute-based score instead of making another external call.
            if not _consume_verifier_quota():
                # [AC-8 2026-04-20] Emit observable trace event + increment counter
                global _VERIFIER_BUDGET_EXHAUSTED_COUNT
                with _VERIFIER_CALL_LOCK:
                    _VERIFIER_BUDGET_EXHAUSTED_COUNT += 1
                    _exhausted_count = _VERIFIER_BUDGET_EXHAUSTED_COUNT
                try:
                    from src.core.trace_sink import write_trace_event
                    write_trace_event("hook_outcome", {
                        "event": "budget_exhausted",
                        "module": "feedback_collector",
                        "max_calls": int(_os.environ.get("MEMEXA_AUTO_VERIFY_MAX", "50")),
                        "exhausted_count": _exhausted_count,
                    })
                except Exception:
                    pass  # trace_sink must never break the feedback path
                logger.warning(
                    "Auto-verify quota exhausted (MEMEXA_AUTO_VERIFY_MAX=%s); "
                    "using compute fallback for this call (exhausted_count=%d)",
                    _os.environ.get("MEMEXA_AUTO_VERIFY_MAX", "50"),
                    _exhausted_count,
                )
                score_source = "compute_quota_exhausted"
            else:
                try:
                    from .real_quality_verifier import verify_quality_sync
                    report = verify_quality_sync(project, result)
                    from dataclasses import asdict as _asdict
                    quality_report = _asdict(report)
                    # DO NOT mutate caller's result dict (LOG-H3).
                    score_source = "verifier"
                except Exception as e:
                    logger.warning("Auto-verify failed, falling back to rule score: %s", e)
                    quality_report = None
                    score_source = "compute_fallback"
        else:
            score_source = "compute"

    if quality_report and isinstance(quality_report, dict):
        quality = quality_report.get("score_1_5", _compute_quality_score(project, result))
        if score_source == "pre_computed":
            score_source = "verifier"  # caller attached a verifier report
    else:
        quality = _compute_quality_score(project, result)
    agent_role = _detect_agent_role(project)
    summary = _extract_summary(result)
    # [Gap 2e + SEC-H3 Round2 2026-04-19] Preserve agent raw output but
    # scrub secrets (tokens/keys/credentials) BEFORE persisting to jsonl
    # and before it ever reaches an external LLM via gradient/test_fn.
    raw_output_full = result.get("output", "") or ""
    raw_output_truncated = _scrub_raw_output(raw_output_full, max_len=2000)
    causal = _extract_causal_fields(project, result)
    proj_id = project.get("id", "unknown")
    title = project.get("title", "")

    # Compute verdict label
    if quality >= 4:
        _verdict = "good"
    elif quality == 3:
        _verdict = "acceptable"
    elif quality == 2:
        _verdict = "poor"
    else:
        _verdict = "failed"

    feedback = {
        "project_id": proj_id,
        "title": title[:80],
        "agent_role": agent_role,
        "quality_score": quality,
        "score_source": score_source,
        "verdict": _verdict,
        "success": result.get("success", False),
        "cost_usd": result.get("cost_usd", 0),
        "num_turns": result.get("num_turns", 0),
        "duration_seconds": result.get("duration_seconds", 0),
        "summary": summary,
        "raw_output": raw_output_truncated,
        "action": causal["action"],
        "outcome": causal["outcome"],
        "reason": causal["reason"],
        "git_diff_stat": causal["git_diff_stat"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Lazy import for semantic_memory (avoid circular imports, used in W1+W3)
    try:
        from .semantic_memory import get_semantic_memory
        sm = get_semantic_memory()
    except Exception:
        sm = None

    # --- W1: Record episode to semantic_memory ---
    try:
        if sm is None:
            raise RuntimeError("semantic_memory unavailable")
        causal_tags = [
            "kairos", agent_role,
            "success" if result.get("success") else "failure",
            f"action:{causal['action'][:60]}",
            f"outcome:{causal['outcome'][:60]}",
        ]
        if causal["git_diff_stat"] is not None:
            causal_tags.append(f"diff_stat:{causal['git_diff_stat']}")
        sm.record_episode(
            task=f"[KAIROS] {title[:60]}",
            output=f"{causal['reason'][:200]}\n---\n{summary[:200]}",
            score=quality,
            agent=agent_role,
            tags=causal_tags,
        )
        feedback["w1_episode_recorded"] = True
        logger.info("W1: Episode recorded for %s (score=%d)", proj_id, quality)
    except Exception as e:
        feedback["w1_episode_recorded"] = False
        logger.warning("W1 failed: %s", e)

    # --- Pattern injection A/B tracking ---
    injected_ids = project.get("injected_pattern_ids", [])
    feedback["patterns_injected_count"] = len(injected_ids)
    feedback["had_patterns"] = len(injected_ids) > 0

    # --- W3 outcome: Record pattern application outcomes ---
    if injected_ids:
        try:
            if sm is None:
                raise RuntimeError("semantic_memory unavailable")
            recorded = 0
            for pid in injected_ids:
                if sm.record_application_outcome(
                    pattern_id=pid,
                    project_id=proj_id,
                    success=result.get("success", False),
                    quality_score=quality,
                ):
                    recorded += 1
            feedback["w3_patterns_tracked"] = recorded
            logger.info("W3: Tracked %d/%d pattern outcomes for %s",
                        recorded, len(injected_ids), proj_id)
        except Exception as e:
            feedback["w3_patterns_tracked"] = 0
            logger.warning("W3 outcome tracking failed: %s", e)
    else:
        feedback["w3_patterns_tracked"] = 0

    # --- W2: Log scored event for evolution_metrics ---
    try:
        from .event_bus import log_event
        # Map quality score to verdict for evolution consumption
        log_event("kairos_feedback", agent=agent_role, details={
            "project_id": proj_id,
            "quality_score": quality,
            "verdict": _verdict,
            "success": result.get("success", False),
            "cost_usd": result.get("cost_usd", 0),
            "turns": result.get("num_turns", 0),
            "title": title[:80],
            "summary": summary[:300],
        })
        feedback["w2_event_logged"] = True
        logger.info("W2: Feedback event logged for %s", proj_id)
    except Exception as e:
        feedback["w2_event_logged"] = False
        logger.warning("W2 failed: %s", e)

    # --- W5: Tag low-quality for prompt_evolver targeting ---
    if quality <= 2 and agent_role != "kairos-general":
        feedback["w5_low_quality_tagged"] = True
        feedback["w5_target_agent"] = agent_role
        try:
            from .event_bus import log_event
            log_event("low_quality_agent", agent=agent_role, details={
                "project_id": proj_id,
                "quality_score": quality,
                "reason": "KAIROS execution quality <= 2",
            })
            logger.warning("W5: Low quality tagged for agent=%s (score=%d)", agent_role, quality)
        except Exception:
            pass
    else:
        feedback["w5_low_quality_tagged"] = False

    # --- W4: Parse big_loop output → structured results ---
    if "[BigLoop" in title:
        parsed = _parse_bigloop_output(result)
        feedback["w4_bigloop_parsed"] = parsed
        if parsed.get("regressions") is not None:
            try:
                from .event_bus import log_event
                log_event("bigloop_result", agent="kairos", details=parsed)
                logger.info("W4: BigLoop result parsed: %s", parsed)
            except Exception:
                pass

    # --- W6: Knowledge compilation for high-quality results ---
    # Karpathy-style: high-quality execution results are compiled into
    # knowledge articles and written back to knowledge_base/articles/
    if quality >= 4 and result.get("success"):
        try:
            from .auto_dream import get_auto_dream
            ad = get_auto_dream()
            episode_for_compile = [{
                "task": title,
                "output": summary,
                "score": quality,
                "agent": agent_role,
            }]
            # Accumulate high-quality episodes for batch compilation
            # (actual compilation happens during autoDream cycle)
            feedback["w6_kb_candidate"] = True
            logger.info("W6: High-quality result marked for KB compilation (score=%d)", quality)
        except Exception as e:
            feedback["w6_kb_candidate"] = False
            logger.debug("W6 skipped: %s", e)
    else:
        feedback["w6_kb_candidate"] = False

    # --- Lesson generation from execution outcome ---
    try:
        from .lesson_store import get_lesson_store
        ls = get_lesson_store()
        if result.get("success"):
            lesson_id = ls.generate_lesson_from_success(project, result)
            if lesson_id:
                feedback["lesson_generated"] = lesson_id
                logger.info("Lesson generated from success: %s", lesson_id)
        else:
            error_cat = result.get("error_category", "")
            lesson_id = ls.generate_lesson_from_failure(
                project, result.get("error") or causal["reason"], error_cat
            )
            if lesson_id:
                feedback["lesson_generated"] = lesson_id
                logger.info("Lesson generated from failure: %s", lesson_id)
    except Exception as e:
        logger.debug("Lesson generation skipped: %s", e)

    # Append to feedback log
    _append_feedback(feedback)

    return feedback


def _parse_bigloop_output(result: Dict) -> Dict:
    """W4: Extract structured data from big_loop project output.

    Parses text like:
      Q1: 182 passed, 0 failed
      Q4: 0 regressions
    """
    output = result.get("output", "")
    parsed = {}

    # Q1 results
    q1_match = re.search(r'Q1:\s*(\d+)\s*passed.*?(\d+)\s*failed', output)
    if q1_match:
        parsed["q1_passed"] = int(q1_match.group(1))
        parsed["q1_failed"] = int(q1_match.group(2))

    # Q4 regressions
    q4_match = re.search(r'Q4:\s*(\d+)\s*regression', output)
    if q4_match:
        parsed["regressions"] = int(q4_match.group(1))

    # Health score if present
    health_match = re.search(r'[Hh]ealth[:\s]+([0-9.]+)', output)
    if health_match:
        try:
            parsed["health_score"] = float(health_match.group(1))
        except ValueError:
            pass

    return parsed


_MAX_FEEDBACK_BYTES = 2 * 1024 * 1024  # 2 MB cap


def _append_feedback(feedback: Dict):
    """Append feedback entry to JSONL file with size-based rotation."""
    _DATA.mkdir(parents=True, exist_ok=True)
    try:
        # Rotate if file exceeds size cap (prevents unbounded OOM growth)
        if _FEEDBACK_FILE.exists() and _FEEDBACK_FILE.stat().st_size > _MAX_FEEDBACK_BYTES:
            archive_dir = _DATA / "_archive"
            archive_dir.mkdir(exist_ok=True)
            from datetime import datetime as _dt
            ts = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
            _FEEDBACK_FILE.rename(archive_dir / f"kairos_feedback_{ts}.jsonl.bak")

        with open(_FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(feedback, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Failed to write feedback: %s", e)


def get_recent_feedback(last_n: int = 20) -> list:
    """Read recent feedback entries (for dashboard)."""
    if not _FEEDBACK_FILE.exists():
        return []
    try:
        lines = _FEEDBACK_FILE.read_text(encoding="utf-8").strip().splitlines()
        entries = []
        for line in lines[-last_n:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except Exception:
        return []


def get_feedback_summary() -> Dict:
    """Aggregate feedback stats (for dashboard)."""
    entries = get_recent_feedback(100)
    if not entries:
        return {"total": 0, "avg_quality": 0, "success_rate": 0, "total_cost": 0}

    total = len(entries)
    avg_quality = sum(e.get("quality_score", 0) for e in entries) / total
    success_count = sum(1 for e in entries if e.get("success"))
    total_cost = sum(e.get("cost_usd", 0) for e in entries)

    # Per-agent breakdown
    agent_stats = {}
    for e in entries:
        role = e.get("agent_role", "unknown")
        if role not in agent_stats:
            agent_stats[role] = {"count": 0, "total_score": 0, "successes": 0}
        agent_stats[role]["count"] += 1
        agent_stats[role]["total_score"] += e.get("quality_score", 0)
        if e.get("success"):
            agent_stats[role]["successes"] += 1

    for role, stats in agent_stats.items():
        stats["avg_score"] = round(stats["total_score"] / stats["count"], 2)
        stats["success_rate"] = round(stats["successes"] / stats["count"], 3)

    # --- Pattern injection A/B comparison ---
    with_patterns = [e for e in entries if e.get("had_patterns")]
    without_patterns = [e for e in entries if not e.get("had_patterns")]

    def _group_stats(group: list) -> Dict:
        if not group:
            return {"count": 0, "avg_quality": 0, "success_rate": 0}
        count = len(group)
        avg_q = sum(e.get("quality_score", 0) for e in group) / count
        succ = sum(1 for e in group if e.get("success")) / count
        return {
            "count": count,
            "avg_quality": round(avg_q, 2),
            "success_rate": round(succ, 3),
        }

    pattern_ab = {
        "with_patterns": _group_stats(with_patterns),
        "without_patterns": _group_stats(without_patterns),
    }

    return {
        "total": total,
        "avg_quality": round(avg_quality, 2),
        "success_rate": round(success_count / total, 3),
        "total_cost": round(total_cost, 4),
        "agent_breakdown": agent_stats,
        "pattern_ab": pattern_ab,
    }
