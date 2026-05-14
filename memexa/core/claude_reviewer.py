"""
Claude-powered code review — uses Claude CLI (Max subscription).
Called by code-reviewer agent and MCP memexa_code_review tool.

Migration (2026-04-03): Replaced Kimi API with `claude -p` subprocess.

Usage:
    python -m memexa.core.claude_reviewer path/to/file.py
    python -m memexa.core.claude_reviewer --diff  (reviews staged git diff)
"""

import json
import logging
import asyncio
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


async def review_file(filepath: str, language: str = "python") -> Dict:
    """Review a single file using Claude CLI."""
    path = Path(filepath)
    if not path.exists():
        return {"error": f"File not found: {filepath}"}

    code = path.read_text(encoding="utf-8", errors="replace")
    if len(code.strip()) < 10:
        return {"file": filepath, "verdict": "SKIP", "reason": "too short"}

    return await review_code(code, language, context=f"File: {filepath}")


async def review_code(code: str, language: str = "python", context: str = "", model: str = None) -> Dict:
    """Review code string using Claude CLI."""
    from .llm_router import get_router, TaskType

    router = get_router()

    # Truncate very long code
    code_truncated = code[:12000]

    prompt = f"""You are a senior code reviewer. Review this {language} code strictly.

{f"Context: {context}" if context else ""}

```{language}
{code_truncated}
```

Check for:
1. **Security**: SQL injection, command injection, hardcoded secrets, eval/exec, path traversal
2. **Logic**: dead code paths, unreachable branches, missing error handling, bare except
3. **Performance**: O(n^2) loops, repeated computation, memory leaks
4. **Style**: mutable defaults, unused imports, print() in library code

Return JSON:
{{
  "verdict": "APPROVED" or "CHANGES_REQUIRED",
  "score": 0-100,
  "findings": [
    {{
      "severity": "critical|high|medium|low",
      "line": "line number or range",
      "description": "what is wrong",
      "suggestion": "how to fix"
    }}
  ],
  "summary": "one sentence overall assessment"
}}

Only report REAL issues. Empty findings array if code is clean."""

    try:
        response = router.call(
            task_type=TaskType.CODE_REVIEW,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=3000,
            response_format={"type": "json_object"},
        )

        result = json.loads(response)
        if not isinstance(result, dict):
            result = {"verdict": "ERROR", "raw": response[:500]}
        return result

    except Exception as e:
        logger.error("Claude review failed: %s", e)
        return {"verdict": "ERROR", "error": str(e)}


async def review_diff() -> Dict:
    """Review staged git diff."""
    import subprocess

    try:
        diff = subprocess.check_output(
            ["git", "diff", "--cached"], encoding="utf-8", errors="replace"
        )
        if not diff.strip():
            diff = subprocess.check_output(
                ["git", "diff"], encoding="utf-8", errors="replace"
            )
        if not diff.strip():
            return {"verdict": "SKIP", "reason": "no changes to review"}
        return await review_code(diff, language="diff", context="Git diff")
    except Exception as e:
        return {"verdict": "ERROR", "error": str(e)}


async def review_files(filepaths: List[str]) -> List[Dict]:
    """Review multiple files in parallel.

    If any file gets CHANGES_REQUIRED, auto-submits a fix project to KAIROS.
    """
    tasks = [review_file(f) for f in filepaths]
    results = await asyncio.gather(*tasks)

    # Auto-wire: CHANGES_REQUIRED → submit KAIROS fix project
    changes_required = []
    for filepath, result in zip(filepaths, results):
        if result.get("verdict") == "CHANGES_REQUIRED":
            findings = result.get("findings", [])
            changes_required.append((filepath, findings))

    if changes_required:
        try:
            from .kairos_daemon import submit_project
            findings_text = "\n".join(
                f"- {fp}: {len(findings)} findings — "
                + "; ".join(f"[{f.get('severity','')}] {f.get('description','')[:80]}" for f in findings[:3])
                for fp, findings in changes_required
            )
            submit_project(
                title=f"[AutoFix] Claude review: {len(changes_required)} files need fixes",
                prompt=(
                    f"Claude code review found issues in {len(changes_required)} files. Fix them:\n\n"
                    f"{findings_text}\n\n"
                    f"For each finding: read the file, understand the issue, apply minimal fix, "
                    f"verify syntax with ast.parse. Then run pytest."
                ),
                priority=4,
                mode="workflow",
            )
            logger.info("Auto-submitted KAIROS fix for %d files with CHANGES_REQUIRED",
                        len(changes_required))
        except Exception as e:
            logger.debug("KAIROS auto-fix submission skipped: %s", e)

    return results


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Claude-powered code review")
    parser.add_argument("files", nargs="*", help="Files to review")
    parser.add_argument("--diff", action="store_true", help="Review git diff")
    args = parser.parse_args()

    async def _run():
        if args.diff:
            result = await review_diff()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.files:
            results = await review_files(args.files)
            for r in results:
                print(json.dumps(r, ensure_ascii=False, indent=2))
                print()
        else:
            parser.print_help()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
