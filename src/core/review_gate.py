"""
Review Gate -- Agent-level code review enforced via command hook.

Since type:"agent" hooks don't work in practice, this module implements
the same functionality as a type:"command" hook that spawns `claude -p`.

Flow:
  1. Check task_spec.json: complex + review_approved=false?
  2. If no review needed: exit 0 (allow commit)
  3. If review needed: run `claude -p` with review prompt + staged diff
  4. Parse review result, write last_review.json
  5. Exit 0 (approve) or exit 1 (block)

Called by: PreToolUse hook on Bash(git commit*) in settings.json
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_DATA = Path(__file__).parent.parent / "data"
_SPEC_FILE = _DATA / "task_spec.json"
_REVIEW_FILE = _DATA / "last_review.json"


def _needs_review() -> bool:
    """Check if current task requires agent review."""
    if not _SPEC_FILE.exists():
        return False
    try:
        spec = json.loads(_SPEC_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if spec.get("complexity") != "complex":
        return False
    if spec.get("status") != "in_progress":
        return False

    criteria = {c["id"]: c.get("verified", False)
                for c in spec.get("acceptance_criteria", [])}
    if criteria.get("review_approved", False):
        return False

    # Check if a recent review already exists (don't re-review)
    if _REVIEW_FILE.exists():
        try:
            age = time.time() - os.path.getmtime(str(_REVIEW_FILE))
            if age < 600:  # < 10 minutes
                review = json.loads(_REVIEW_FILE.read_text(encoding="utf-8"))
                if review.get("verdict") == "APPROVED":
                    return False
        except (OSError, json.JSONDecodeError):
            pass

    return True


def _get_staged_diff() -> str:
    """Get git diff --cached output."""
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
        stat = r.stdout.strip()
        r2 = subprocess.run(
            ["git", "diff", "--cached"],
            capture_output=True, text=True, timeout=10,
        )
        # Truncate large diffs to prevent memory exhaustion in claude -p subprocess
        # 4000 chars ~ 1000 tokens, safe for review without OOM
        diff = r2.stdout[:4000]
        if len(r2.stdout) > 4000:
            diff += f"\n\n[TRUNCATED: {len(r2.stdout)} total chars, showing first 4000]"
        return f"STAT:\n{stat}\n\nDIFF:\n{diff}"
    except Exception:
        return ""


def _run_claude_review(diff: str) -> dict:
    """Spawn claude -p to review the staged diff."""
    # Write diff to temp file (avoids shell escaping issues with long content)
    diff_file = _DATA / "_review_diff.txt"
    _DATA.mkdir(parents=True, exist_ok=True)
    diff_file.write_text(diff, encoding="utf-8")

    prompt = (
        "Read memexa/memexa/data/_review_diff.txt which contains a git diff. "
        "Review for: logic errors, security issues, API breaking changes, "
        "missing error handling, untested code paths. "
        "Output ONLY a JSON object with no other text: "
        '{"verdict":"APPROVED" or "CHANGES_REQUIRED", "score":0-100, '
        '"findings":[{"severity":"high/medium/low","file":"path","issue":"desc"}], '
        '"summary":"one line"}'
    )

    # F2 fix (2026-04-20 autopilot, SEC-R1-003): shell=True with list arg on
    # Windows routes through cmd.exe and allows PATH hijacking via a rogue
    # npx.bat/npx.cmd. Resolve to absolute path and run with shell=False.
    import shutil as _shutil
    _npx = _shutil.which("npx") or _shutil.which("npx.cmd") or "npx"
    _env = os.environ.copy()
    _env["CLAUDE_CODE_DISABLE_THINKING"] = "1"
    try:
        r = subprocess.run(
            [
                _npx, "@anthropic-ai/claude-code", "-p", prompt,
                "--output-format", "text",
                "--bare",                        # skip hooks/MCP/LSP for fast startup
                "--fallback-model", "sonnet",    # auto-fallback if Opus overloaded
                "--max-budget-usd", "0.50",      # cap cost per review
            ],
            capture_output=True, timeout=120,
            shell=False,
            env=_env,
        )
        # Decode with utf-8 fallback (Windows may default to GBK)
        try:
            r.stdout = r.stdout.decode("utf-8") if isinstance(r.stdout, bytes) else (r.stdout or "")
        except UnicodeDecodeError:
            r.stdout = r.stdout.decode("utf-8", errors="replace") if isinstance(r.stdout, bytes) else ""
        output = r.stdout.strip()

        # Try to extract JSON from output
        # claude -p may wrap in markdown or add text
        import re
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', output, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))

        # Fallback: if output contains "APPROVED"
        if "APPROVED" in output.upper():
            return {"verdict": "APPROVED", "score": 75, "findings": [], "summary": "Auto-approved"}

        return {"verdict": "CHANGES_REQUIRED", "score": 50,
                "findings": [{"severity": "medium", "file": "unknown", "issue": "Could not parse review"}],
                "summary": "Review parsing failed"}

    except subprocess.TimeoutExpired:
        print("[REVIEW GATE] claude -p timed out (120s), allowing commit", file=sys.stderr)
        return {"verdict": "APPROVED", "score": 60, "findings": [], "summary": "Timeout, auto-approved"}
    except FileNotFoundError:
        print("[REVIEW GATE] claude CLI not found, skipping review", file=sys.stderr)
        return {"verdict": "APPROVED", "score": 60, "findings": [], "summary": "CLI not found, skipped"}
    except Exception as e:
        print(f"[REVIEW GATE] Error: {e}", file=sys.stderr)
        return {"verdict": "APPROVED", "score": 60, "findings": [], "summary": f"Error: {e}"}
    finally:
        # Clean up temp diff file
        try:
            diff_file.unlink(missing_ok=True)
        except Exception:
            pass


def main():
    """Hook entry point. Exit 0=allow, exit 1=block."""
    if not _needs_review():
        print("[REVIEW GATE] No review needed (simple task or already reviewed)")
        sys.exit(0)

    print("[REVIEW GATE] Complex task detected, running agent review...")
    diff = _get_staged_diff()
    if not diff:
        print("[REVIEW GATE] No staged diff found, allowing commit")
        sys.exit(0)

    review = _run_claude_review(diff)

    # Write review result
    _DATA.mkdir(parents=True, exist_ok=True)
    review["timestamp"] = datetime.now().isoformat()
    _REVIEW_FILE.write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Wire 4: Feed findings into knowledge base (non-blocking)
    if review.get("findings"):
        try:
            from src.core.pattern_extractor import extract_pattern_from_review, save_patterns
            entries = extract_pattern_from_review(
                json.dumps(review), source="review_gate",
                reference=f"commit-{datetime.now():%Y%m%d}",
            )
            if entries:
                added = save_patterns(entries)
                print(f"[REVIEW GATE] Extracted {added} patterns to knowledge base")
        except Exception:
            pass  # KB extraction failure must not affect commit flow

    verdict = review.get("verdict", "CHANGES_REQUIRED")
    score = review.get("score", 0)
    summary = review.get("summary", "")
    findings_count = len(review.get("findings", []))

    print(f"[REVIEW GATE] Verdict: {verdict} (score={score}, {findings_count} findings)")
    print(f"[REVIEW GATE] Summary: {summary}")

    # [Item #2] Emit gate_decision event
    try:
        from src.core._hook_utils import log_gate_decision
        log_gate_decision(
            gate="review_gate",
            rule="agent_review",
            decision="allow" if verdict == "APPROVED" else "block",
            target="staged_diff",
            reason=summary[:200],
            extra={"verdict": verdict, "score": score, "findings_count": findings_count},
        )
    except Exception:
        pass

    if verdict == "APPROVED":
        print("[REVIEW GATE] COMMIT ALLOWED")
        sys.exit(0)
    else:
        print(f"[REVIEW GATE] COMMIT BLOCKED: {summary}")
        for f in review.get("findings", [])[:5]:
            print(f"  [{f.get('severity','?')}] {f.get('file','?')}: {f.get('issue','?')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
