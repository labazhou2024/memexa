"""Single-source-of-truth for project version numbers.

Before 2026-04-23 P1: three version fields drifted independently:
  - CLAUDE.md header "v3.6"
  - harness_state.infrastructure.claude_md_version "3.5" (stale)
  - harness_state.infrastructure.workflow_version "5.1" (stale vs WORKFLOW.md v5.2)
  - harness_state.autopilot_system.version "4.4"

Now: this module is authoritative. Any doc citing a version should import
from here (or run the CLI) so drift surfaces at CI time, not runtime.

CLI:
  python -m memexa.core.versions        # print all versions as JSON
  python -m memexa.core.versions check  # exit 1 if any doc header disagrees
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parents[3]

# AUTHORITATIVE VERSIONS (2026-04-23)
# When editing CLAUDE.md / WORKFLOW.md headers, bump the number here too.
VERSIONS = {
    "claude_md": "3.6",
    "workflow": "5.3",
    "autopilot_system": "4.4",
    "architecture": "3.0",
    "cold_start_schema": "1",  # cold_start_meter LIMITS schema version
    "env_allowlist_schema": "1",  # env_allowlist.json schema version
}


def get(key: str) -> str:
    """Return the authoritative version string."""
    return VERSIONS[key]


def check() -> int:
    """Scan doc headers for version disagreement. Return 0 = OK, 1 = drift."""
    drifts = []
    # CLAUDE.md
    cmd = _WORKSPACE / "CLAUDE.md"
    if cmd.exists():
        text = cmd.read_text(encoding="utf-8")[:500]
        m = re.search(r"CLAUDE\.md\s*(?:[—-]|--)\s*[^v]*v([\d.]+)", text)
        if m and m.group(1) != VERSIONS["claude_md"]:
            drifts.append((
                "CLAUDE.md", m.group(1), VERSIONS["claude_md"],
            ))
    # WORKFLOW.md
    wf = _WORKSPACE / "memexa" / "WORKFLOW.md"
    if wf.exists():
        text = wf.read_text(encoding="utf-8")[:500]
        m = re.search(r"Workflow.*?v([\d.]+)", text)
        if m and m.group(1) != VERSIONS["workflow"]:
            drifts.append((
                "memexa/WORKFLOW.md", m.group(1), VERSIONS["workflow"],
            ))
    # harness_state.json
    hs = _WORKSPACE / ".claude" / "config" / "harness_state.json"
    if hs.exists():
        try:
            d = json.loads(hs.read_text(encoding="utf-8"))
            infra = d.get("infrastructure", {})
            if infra.get("claude_md_version") != VERSIONS["claude_md"]:
                drifts.append((
                    "harness_state.infrastructure.claude_md_version",
                    infra.get("claude_md_version"),
                    VERSIONS["claude_md"],
                ))
            if infra.get("workflow_version") != VERSIONS["workflow"]:
                drifts.append((
                    "harness_state.infrastructure.workflow_version",
                    infra.get("workflow_version"),
                    VERSIONS["workflow"],
                ))
            ap = d.get("autopilot_system", {})
            if ap.get("version") != VERSIONS["autopilot_system"]:
                drifts.append((
                    "harness_state.autopilot_system.version",
                    ap.get("version"),
                    VERSIONS["autopilot_system"],
                ))
        except Exception:
            pass

    if drifts:
        print("VERSION DRIFT (authoritative in memexa/core/versions.py):")
        for loc, actual, authoritative in drifts:
            print(f"  {loc}: doc has {actual!r}, authoritative is {authoritative!r}")
        return 1
    print("OK: all version sources agree.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="versions")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("check")
    args = p.parse_args(argv)
    if args.cmd == "check":
        return check()
    print(json.dumps(VERSIONS, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
