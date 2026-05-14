"""TU-1 (U1, plan_v1, 2026-04-26) — vendored lint helpers.

Vendored from `.claude/harness/tasks/20260425_130622_industry_com/scripts/_lint_common.py`
(TU-8 9-lint sweep). Reason: memexa.core.* cannot import workspace task_dir scripts
(architect BLOCKER-A1).

Provenance:
  source_path: tasks/20260425_130622_industry_com/scripts/_lint_common.py
  source_sha256: 46dceb2d94f5444524f0a19c2a78ef2956892549b86f4e3fb687a0cf4882c27e
  source_size: 1890 bytes
  vendored_at: 2026-04-26

Drift detection (S2 fix from security-reviewer iter2): _PROVENANCE_SHA256 is
machine-readable; a separate audit script can verify drift periodically.

API contract: keep `runner: Callable[[Path], int]` signature (LR-1 fix);
TU-3 plan_uniformity_check.py uses `_runner_for_selftest` adapter.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Callable

# S2 fix (security-reviewer iter2): machine-readable provenance for drift audit
_PROVENANCE_SHA256 = "46dceb2d94f5444524f0a19c2a78ef2956892549b86f4e3fb687a0cf4882c27e"
_PROVENANCE_SOURCE = "tasks/20260425_130622_industry_com/scripts/_lint_common.py"


def selftest(
    name: str,
    runner: Callable[[Path], int],
    positive_fixture: str,
    negative_fixture: str,
) -> int:
    """Run positive + negative fixtures; return 0 iff both behave as expected.

    SR-5 fix (industry-comparison TU-8 origin): each --self-test runs
    >=1 positive (expect exit 0) + >=1 negative (expect non-0).
    """
    failures = []
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(positive_fixture)
        pos_path = Path(f.name)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as f:
        f.write(negative_fixture)
        neg_path = Path(f.name)
    try:
        pos_rc = runner(pos_path)
        if pos_rc != 0:
            failures.append(f"positive fixture expected exit 0, got {pos_rc}")
        neg_rc = runner(neg_path)
        if neg_rc == 0:
            failures.append("negative fixture expected non-0, got 0")
    finally:
        try:
            pos_path.unlink()
            neg_path.unlink()
        except OSError:
            pass
    if failures:
        for f in failures:
            print(f"[{name}] SELF-TEST FAIL: {f}", file=sys.stderr)
        return 1
    print(f"[{name}] SELF-TEST PASS")
    return 0


def read(path: Path) -> str:
    """Read file as UTF-8; return empty string if absent."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def main_dispatch(
    name: str,
    runner: Callable[[Path], int],
    pos_fix: str,
    neg_fix: str,
) -> int:
    """CLI dispatch helper; honors --self-test flag."""
    if "--self-test" in sys.argv:
        return selftest(name, runner, pos_fix, neg_fix)
    if len(sys.argv) < 2:
        print(f"Usage: {name} <input.md> | --self-test", file=sys.stderr)
        return 2
    return runner(Path(sys.argv[1]))


def verify_provenance() -> bool:
    """S2 fix: drift audit. Returns True iff vendored content matches source SHA-256.

    Returns False (with stderr message) if source file unreachable or sha mismatch.
    """
    import hashlib
    workspace = Path(__file__).resolve().parent.parent.parent.parent
    source = workspace / ".claude" / "harness" / "tasks" / \
        "20260425_130622_industry_com" / "scripts" / "_lint_common.py"
    if not source.exists():
        print(f"[provenance] source unreachable: {source}", file=sys.stderr)
        return False
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual != _PROVENANCE_SHA256:
        print(
            f"[provenance] DRIFT: source sha256={actual[:16]} "
            f"vendored sha256={_PROVENANCE_SHA256[:16]}",
            file=sys.stderr,
        )
        return False
    return True
