"""TU-4 (autopilot_pi 2026-04-30): Stage 5 staging-area whitelist gate.

Compares git index (`git diff --cached --name-only`) against the union of
each TU's `tu_outputs[]` list, sourced from:
  (1) state.json (primary, populated by task_unit_scheduler.mark_done)
  (2) plan_v<latest>.md regex parse (fallback if state.json empty)

Per security-iter1-2 HIGH fix in plan_v0_review_security.json.

CLI:
  python -m src.core.stage5_staging_gate check --task-id <tid>
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Set, Tuple


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _task_dir(task_id: str) -> Path:
    return _workspace_root() / ".claude" / "harness" / "tasks" / task_id


def _read_tu_outputs_from_state(task_id: str) -> Optional[Set[str]]:
    """Try to read tu_outputs[] from state.json. Returns None if empty/missing."""
    p = _task_dir(task_id) / "state.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    units = data.get("units") or []
    if not units:
        return None  # signal fallback to plan parse
    out: Set[str] = set()
    for u in units:
        outs = u.get("outputs") or u.get("tu_outputs") or []
        for o in outs:
            if isinstance(o, str):
                out.add(o)
    if not out:
        return None
    return out


_TU_OUTPUTS_RE = re.compile(
    r"\*\*tu_outputs\*\*\s*:\s*\[([^\]]+)\]",
    re.MULTILINE,
)


def _read_tu_outputs_from_plan(task_id: str) -> Set[str]:
    """Fallback: parse plan_v<latest>.md for **tu_outputs**: [...] declarations."""
    td = _task_dir(task_id)
    candidates = sorted(td.glob("plan_v*.md"),
                        key=lambda p: int(re.search(r"plan_v(\d+)", p.stem).group(1))
                                      if re.search(r"plan_v(\d+)", p.stem) else -1,
                        reverse=True)
    plan_path = None
    for c in candidates:
        if c.name == "plan_v_latest.md":
            continue
        plan_path = c
        break
    if plan_path is None:
        return set()
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    out: Set[str] = set()
    for m in _TU_OUTPUTS_RE.finditer(text):
        body = m.group(1)
        # Body looks like: "a.py", "b.py", "c.py"
        for piece in re.findall(r'"([^"]+)"', body):
            piece = piece.strip()
            if piece:
                out.add(piece)
    return out


def _git_staged_files(repo_dir: Path, *, timeout: int = 15) -> List[str]:
    """Return list of staged file paths (relative to repo_dir)."""
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, cwd=str(repo_dir), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"git diff --cached timeout after {timeout}s — check repo state at {repo_dir}"
        )
    if r.returncode != 0:
        raise RuntimeError(f"git diff failed exit={r.returncode}: {r.stderr}")
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _normalize(p: str) -> str:
    """Forward-slash + lowercase comparator-safe form."""
    return p.replace("\\", "/").strip().lower()


def _check_override_env() -> Tuple[bool, str]:
    """Inspect MEMEXA_STAGE5_WHITELIST_BYPASS_REASON. Returns (allowed, reason_stripped)."""
    raw = os.environ.get("MEMEXA_STAGE5_WHITELIST_BYPASS_REASON", "")
    stripped = raw.strip()
    if stripped and len(stripped) >= 80:
        return True, stripped
    return False, ""


def _emit_trace(task_id: str, event: str, payload: dict) -> None:
    """Best-effort trace emission via task_dir_layout.append_trace."""
    try:
        from src.core.task_dir_layout import append_trace
        append_trace(task_id, event, payload)
    except Exception:
        pass


def check_index_against_whitelist(
    task_id: str,
    *,
    repo_dir: Optional[Path] = None,
    state_outputs: Optional[Set[str]] = None,
) -> Tuple[bool, list, str]:
    """Compare git index to whitelist. Returns (allowed, violations, source).

    `state_outputs` injectable for tests.
    `source` is one of: "state.json", "plan_fallback", "override".
    """
    if repo_dir is None:
        repo_dir = _workspace_root() / "memexa"

    # Override path
    auth, _reason = _check_override_env()
    if auth:
        _emit_trace(task_id, "stage5_whitelist_override", {
            "task_id": task_id,
            "reason_len_post_strip": len(_reason),
        })
        return True, [], "override"

    # Build whitelist
    if state_outputs is None:
        state_outputs = _read_tu_outputs_from_state(task_id)
    if state_outputs:
        whitelist = state_outputs
        source = "state.json"
    else:
        whitelist = _read_tu_outputs_from_plan(task_id)
        source = "plan_fallback"

    if not whitelist:
        # Empty whitelist + non-empty index = BLOCK with explicit msg
        try:
            staged = _git_staged_files(repo_dir)
        except RuntimeError as exc:
            staged = []
            _emit_trace(task_id, "stage5_whitelist_violation", {
                "task_id": task_id, "violations_count": -1,
                "source": source, "error": str(exc),
            })
            return False, [f"git error: {exc}"], source
        if not staged:
            return True, [], source  # empty commit OK
        violations = [f"out-of-whitelist: {f} (whitelist empty for task {task_id})" for f in staged]
        _emit_trace(task_id, "stage5_whitelist_violation", {
            "task_id": task_id,
            "violations_count": len(violations),
            "source": source,
        })
        return False, violations, source

    try:
        staged = _git_staged_files(repo_dir)
    except RuntimeError as exc:
        return False, [f"git error: {exc}"], source

    norm_white = {_normalize(p) for p in whitelist}
    violations: List[str] = []
    for f in staged:
        if _normalize(f) not in norm_white and _normalize("memexa/" + f) not in norm_white:
            violations.append(f"out-of-whitelist: {f}")
    if violations:
        _emit_trace(task_id, "stage5_whitelist_violation", {
            "task_id": task_id,
            "violations_count": len(violations),
            "source": source,
            "files": violations[:10],  # cap forensic payload
        })
        return False, violations, source
    return True, [], source


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m src.core.stage5_staging_gate",
        description="Stage 5 staging-area whitelist gate (TU-4 autopilot_pi).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="Verify git index matches tu_outputs whitelist")
    p_check.add_argument("--task-id", required=True)
    args = p.parse_args(argv)
    if args.cmd == "check":
        try:
            ok, violations, source = check_index_against_whitelist(args.task_id)
        except Exception as exc:
            sys.stderr.write(f"stage5_staging_gate uncaught: {exc!s}\n")
            return 2
        if ok:
            print(f"OK ({source})")
            return 0
        print(f"BLOCKED ({source}): {len(violations)} violations")
        for v in violations[:20]:
            print(f"  {v}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
