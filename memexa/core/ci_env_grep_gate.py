"""TU-R7-lite (2026-04-23): CI env-var grep gate.

Policy: all `os.environ.get("MEMEXA_*")` reads must be pre-declared in
`memexa/data/env_allowlist.json`. New env reads require CEO approval via
the `add-allowed` CLI.

Rationale (deep audit B2 + security-reviewer B2): 64+ MEMEXA_* env reads
across 37 files form an OWASP LLM01 attack surface. Full runtime migration
to a single `overrides` module is 500+ LoC — out of scope. This lite gate
instead prevents NEW env reads from being added without CEO signoff, via
a grep-based CI check + pretool_gate Rule 13 that blocks Write to
`env_allowlist.json` directly (only the CLI with --ceo-approved flag can
modify it).

Usage:
  python -m memexa.core.ci_env_grep_gate check          # scan + verdict
  python -m memexa.core.ci_env_grep_gate list           # print current allowlist
  python -m memexa.core.ci_env_grep_gate add-allowed NAME --ceo-approved [--reason TEXT]

Exit codes:
  0 = PASS (no unknown env reads, or check mode with allowlist fresh)
  1 = FAIL (unknown env var read found, or other violation)
  2 = missing --ceo-approved flag for add-allowed
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_MEMEXA = Path(__file__).resolve().parents[2]
_ALLOWLIST = _MEMEXA / "memexa" / "data" / "env_allowlist.json"

_ENV_RE = re.compile(r'os\.environ\.get\s*\(\s*[\'"](MEMEXA_[A-Z0-9_]+)[\'"]')


def _scan_workspace() -> set:
    """Scan memexa/ for all MEMEXA_* env var names."""
    names = set()
    for root, _, files in os.walk(_MEMEXA):
        if "__pycache__" in root or "worktree" in root:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            p = Path(root) / f
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                for m in _ENV_RE.finditer(text):
                    names.add(m.group(1))
            except Exception:
                pass
    return names


def _load_allowlist() -> dict:
    if not _ALLOWLIST.exists():
        return {"names": [], "entries": []}
    try:
        return json.loads(_ALLOWLIST.read_text(encoding="utf-8"))
    except Exception:
        return {"names": [], "entries": []}


def check() -> int:
    found = _scan_workspace()
    allow = set(_load_allowlist().get("names", []))
    new = sorted(found - allow)
    removed = sorted(allow - found)
    print(f"allowlist: {len(allow)} entries; scan found {len(found)} unique")
    if new:
        print(f"VIOLATION: {len(new)} new env var reads not in allowlist:")
        for n in new:
            print(f"  - {n}")
        print(
            f"To add: python -m memexa.core.ci_env_grep_gate "
            f"add-allowed <NAME> --ceo-approved --reason '<why>'"
        )
        return 1
    if removed:
        print(f"INFO: {len(removed)} allowlist entries no longer used:")
        for n in removed:
            print(f"  - {n}")
    print("PASS: all env reads accounted for.")
    return 0


def list_allowed() -> int:
    data = _load_allowlist()
    for n in sorted(data.get("names", [])):
        print(n)
    return 0


def add_allowed(name: str, ceo_approved: bool, reason: str = "") -> int:
    if not ceo_approved:
        print("ERROR: add-allowed requires --ceo-approved flag.", file=sys.stderr)
        return 2
    if not re.fullmatch(r"MEMEXA_[A-Z0-9_]+", name):
        print(f"ERROR: name {name!r} must match MEMEXA_[A-Z0-9_]+.", file=sys.stderr)
        return 1
    data = _load_allowlist()
    names = set(data.get("names", []))
    if name in names:
        print(f"already present: {name}")
        return 0
    names.add(name)
    data["names"] = sorted(names)
    data["n_entries"] = len(names)
    entries = data.setdefault("entries", [])
    entries.append({
        "name": name,
        "file": "<manual>",
        "line": 0,
        "reason": reason,
        "added": "2026-04-23",
    })
    _ALLOWLIST.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"added: {name} (reason: {reason or '<none>'})")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ci_env_grep_gate")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("list")
    aa = sub.add_parser("add-allowed")
    aa.add_argument("name")
    aa.add_argument("--ceo-approved", action="store_true", default=False)
    aa.add_argument("--reason", default="")
    args = p.parse_args(argv)
    if args.cmd == "check":
        return check()
    if args.cmd == "list":
        return list_allowed()
    if args.cmd == "add-allowed":
        return add_allowed(args.name, args.ceo_approved, args.reason)
    return 1


if __name__ == "__main__":
    sys.exit(main())
