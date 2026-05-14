"""
[DEPRECATED 2026-05-12] KAIROS self-evolution frozen since 2026-04-04.
0 production callers. Tests preserved. New code: use src.core.memory_query.

LLM-as-Judge — Automated output quality scoring.

Uses a cheap LLM (Kimi 8k) to evaluate Agent outputs on a 1-5 scale.
Part of the Inner Loop Reflexion system (v5.0).

Correlation with human judgment: ~80% (industry benchmark).
Cost: ~0.001 USD per evaluation (Kimi 8k, ~200 tokens).
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Scoring rubric injected into every evaluation
_RUBRIC = """Score the output on a 1-5 scale. Be STRICT — bias toward 3 when uncertain.

5 = Outstanding: Verifiably correct, complete, AND you can name a specific positive outcome
4 = Good: Correct with concrete evidence (tests pass, code compiles, specific problem solved)
3 = Acceptable: Task attempted, plausible output, but no strong evidence of correctness
2 = Poor: Major errors, incomplete, or misunderstands the task
1 = Failed: Wrong output, crashes, security issues, or empty

IMPORTANT: Score 3 is the DEFAULT for ordinary outputs. Reserve 4-5 ONLY for outputs
where you can point to specific, verifiable evidence of quality. "Looks reasonable" = 3, not 4.

Return JSON: {"score": N, "reason": "one sentence justification with specific evidence"}
"""


async def judge(
    task_description: str,
    output: str,
    *,
    context: str = "",
    rubric: str = "",
) -> Dict[str, Any]:
    """Score an Agent output using LLM-as-Judge.

    Args:
        task_description: What the agent was asked to do
        output: The agent's output to evaluate
        context: Optional additional context (e.g., target files)
        rubric: Optional custom rubric (overrides default)

    Returns:
        {"score": 1-5, "reason": str, "raw": str}
    """
    from .llm_router import get_router, TaskType

    router = get_router()
    client = router.get_client()
    if not client:
        logger.warning("LLM Judge: no client available, returning default score 3")
        return {"score": 3, "reason": "No LLM client available", "raw": ""}

    prompt = f"""You are a strict quality evaluator. Evaluate this agent output.

## Task
{task_description}

{f"## Context{chr(10)}{context}" if context else ""}

## Agent Output
{output[:3000]}

## Rubric
{rubric or _RUBRIC}
"""

    try:
        response = router.call(
            task_type=TaskType.CHAT,  # Use cheap 8k model
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            response_format={"type": "json_object"},
        )

        # Extract JSON from response (may be markdown-wrapped or contain extra text)
        result = None
        if response and response.strip():
            try:
                result = json.loads(response)
            except (json.JSONDecodeError, ValueError):
                # Try extracting from markdown code blocks or embedded JSON
                import re
                md_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', response, re.DOTALL)
                if md_match:
                    try:
                        result = json.loads(md_match.group(1).strip())
                    except (json.JSONDecodeError, ValueError):
                        pass
                if not result:
                    brace_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
                    if brace_match:
                        try:
                            result = json.loads(brace_match.group(0))
                        except (json.JSONDecodeError, ValueError):
                            pass
        if not result:
            logger.warning("LLM Judge: could not parse response: %s", (response or "")[:200])
            return {"score": 3, "reason": "Unparseable response", "raw": response or ""}

        score = int(result.get("score", 3))
        score = max(1, min(5, score))  # Clamp to 1-5
        reason = result.get("reason", "")

        return {"score": score, "reason": reason, "raw": response}

    except Exception as e:
        logger.warning("LLM Judge failed: %s, returning default score 3", e)
        return {"score": 3, "reason": f"Judge error: {e}", "raw": ""}


def judge_sync(task_description: str, output: str, **kwargs) -> Dict[str, Any]:
    """Synchronous wrapper for judge()."""
    import asyncio
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, judge(task_description, output, **kwargs)).result(timeout=30)
    except RuntimeError:
        return asyncio.run(judge(task_description, output, **kwargs))
