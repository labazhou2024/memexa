"""silent_fail_audit — TU-2 of plan_v3. Detect `try/except Exception: pass`
(and bare except: pass) blocks in STAGED .py files that do NOT emit a
trace event in the 5 lines before the except.

Silent-fail audit prevents the exact failure mode CEO identified: hook
fires, exception swallowed, operator has no signal. Rule: every fail-
soft `except: pass` must be preceded by a `_emit_trace(...)`, or
`logger.warning(...)`, or `write_trace_event(...)`, or an explicit
`# allow-silent` marker comment.

Allowlist: `.claude/harness/silent_fail_allowlist.json`. Entries must be
HMAC-signed via security_scanner._sign_allowlist_entry (B3 fix from
plan_v3). Unsigned entries are REJECTED — an LLM cannot self-exempt.

Uniform `check(task_id) -> (allow: bool, reason: str)`.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


_GATES_DIR = Path(__file__).resolve().parent
_WORKSPACE = _GATES_DIR.parent.parent.parent.parent
_MEMEX_ROOT = _WORKSPACE / "memex"
_ALLOWLIST_PATH = _WORKSPACE / ".claude" / "harness" / "silent_fail_allowlist.json"
_ALLOWLIST_KEY_ENV = "MEMEX_SILENT_FAIL_HMAC_KEY"

# Trace-emit markers that count as "not silent" — if any of these
# strings appears in the 5 lines before an except: pass block, the
# block is considered properly annotated.
_TRACE_MARKERS = (
    "_emit_trace(",
    "write_trace_event(",
    "logger.warning(",
    "logger.error(",
    "log_gate_decision(",
    "# allow-silent",
)
_LOOKBACK_LINES = 5


@dataclass
class Violation:
    path: str
    lineno: int
    block_sha256: str
    reason: str


def _sha256_of_block(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_silent_blocks_in_file(file_path: Path) -> List[Violation]:
    """AST-walk one .py; return violations."""
    try:
        source = file_path.read_text(encoding="utf-8")
    except Exception:
        return []

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    lines = source.splitlines()
    violations: List[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # Filter: we want `except Exception:` or bare `except:` with ONLY
        # a `pass` in body (classic silent-swallow).
        if len(node.body) != 1 or not isinstance(node.body[0], ast.Pass):
            continue
        # Broad exception filter:
        # - bare except (node.type is None)
        # - except Exception
        # - except BaseException
        is_silent_scope = False
        if node.type is None:
            is_silent_scope = True
        elif isinstance(node.type, ast.Name) and node.type.id in (
            "Exception", "BaseException"
        ):
            is_silent_scope = True
        # Tuple forms (except (A, B)) — skip; likely narrower
        if not is_silent_scope:
            continue

        # Look back 5 lines from the except line
        except_lineno = node.lineno
        start = max(1, except_lineno - _LOOKBACK_LINES)
        lookback_text = "\n".join(
            lines[i - 1] for i in range(start, except_lineno + 1)
            if 0 <= i - 1 < len(lines)
        )

        if any(m in lookback_text for m in _TRACE_MARKERS):
            continue  # annotated — OK

        # Construct block text for SHA256 (stable allowlist key)
        block_text = lookback_text + "\n" + (lines[except_lineno - 1] if except_lineno - 1 < len(lines) else "")
        if except_lineno < len(lines):
            block_text += "\n" + lines[except_lineno]  # the `pass` line

        violations.append(Violation(
            path=str(file_path.relative_to(_MEMEX_ROOT)) if str(file_path).startswith(str(_MEMEX_ROOT)) else str(file_path),
            lineno=except_lineno,
            block_sha256=_sha256_of_block(block_text),
            reason="except Exception: pass without preceding trace_emit",
        ))
    return violations


def _load_allowlist() -> List[dict]:
    """Return allowlist entries with VALID HMAC only.
    Returns [] if file missing, key unset, JSON malformed, or all entries
    fail HMAC verification.
    B3 fix (plan_v3): enforces signed-only entries. LLM cannot exempt itself.
    """
    if not _ALLOWLIST_PATH.exists():
        return []
    key = os.environ.get(_ALLOWLIST_KEY_ENV, "")
    if not key:
        return []
    try:
        data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    import hmac
    import hashlib

    valid: List[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        required = ("path", "block_sha256", "reason", "hmac", "signed_at")
        if any(k not in entry for k in required):
            continue
        # SEC-3 fix: reject non-string hmac values (int, list, null etc.)
        if not isinstance(entry["hmac"], str):
            continue
        msg = json.dumps([entry["path"], entry["block_sha256"],
                          entry["reason"], int(entry["signed_at"])],
                         ensure_ascii=False)
        expected = hmac.new(
            key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(expected, entry["hmac"]):
            valid.append(entry)
    return valid


def _is_allowlisted(violation: Violation, allowlist: List[dict]) -> bool:
    for entry in allowlist:
        if (entry.get("path") == violation.path
                and entry.get("block_sha256") == violation.block_sha256):
            return True
    return False


def _staged_py_files() -> List[Path]:
    """git diff --cached --name-only → list of staged .py files."""
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--diff-filter=d", "--name-only"],
            capture_output=True, text=True,
            cwd=str(_MEMEX_ROOT), timeout=10, shell=False,
        )
    except Exception:
        return []
    files: List[Path] = []
    for name in (r.stdout or "").strip().splitlines():
        name = name.strip()
        if not name.endswith(".py"):
            continue
        p = _MEMEX_ROOT / name
        if p.is_file():
            files.append(p)
    return files


def check(task_id: str) -> Tuple[bool, str]:
    """Uniform gate entry. Run silent_fail_audit on staged .py files.
    Return (allow, reason).
    """
    staged = _staged_py_files()
    if not staged:
        return (True, "silent_fail_audit: no staged .py files")

    allowlist = _load_allowlist()
    all_violations: List[Violation] = []
    for f in staged:
        all_violations.extend(_find_silent_blocks_in_file(f))

    unpardoned = [v for v in all_violations if not _is_allowlisted(v, allowlist)]
    if not unpardoned:
        return (True, f"silent_fail_audit: {len(staged)} files clean "
                      f"({len(all_violations)} blocks, "
                      f"{len(all_violations) - len(unpardoned)} allowlisted)")

    sample = unpardoned[0]
    return (False, f"silent_fail_audit BLOCK: {len(unpardoned)} "
                   f"silent except:pass blocks in staged diff. "
                   f"first: {sample.path}:{sample.lineno}")


def _cli(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        # allow-silent: fail-soft observability path
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    import argparse
    p = argparse.ArgumentParser(prog="silent_fail_audit")
    sub = p.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check")
    pc.add_argument("task_id")
    ps = sub.add_parser("scan")
    ps.add_argument("file")
    args = p.parse_args(argv)

    if args.cmd == "check":
        ok, reason = check(args.task_id)
        print(json.dumps({"allow": ok, "reason": reason}, ensure_ascii=False))
        return 0 if ok else 1

    if args.cmd == "scan":
        vs = _find_silent_blocks_in_file(Path(args.file))
        for v in vs:
            print(f"{v.path}:{v.lineno} sha={v.block_sha256[:12]} {v.reason}")
        return 0 if not vs else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
