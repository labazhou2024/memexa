"""hook_audit — inspect .claude/config/settings.json Claude Code hook wiring.

Problem this solves (2026-04-23): Rule 10/11 sat dead in pretool_gate.py for
6 weeks because settings.json PreToolUse had `matcher: "Write|Edit"` — Bash
invocations never reached the gate. Unit tests passed; production never
triggered. This CLI makes "hooks aren't wired where you think" observable.

Usage:
    python -m memexa.core.hook_audit list               # table output
    python -m memexa.core.hook_audit list --json        # JSON for scripts
    python -m memexa.core.hook_audit list --strict      # exit 1 if any issue
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# Tools that PreToolUse SHOULD cover. Missing coverage = FAIL in strict mode.
REQUIRED_PRETOOL_COVERAGE = ["Bash", "Write", "Edit"]


@dataclass
class HookEntry:
    event: str
    matcher: str
    command: str
    description: str
    script_path: Optional[Path]
    script_exists: bool

    def status(self) -> str:
        if not self.script_path:
            return "INLINE"  # not a file-based hook; opaque
        return "OK" if self.script_exists else "MISSING_SCRIPT"


@dataclass
class AuditReport:
    entries: List[HookEntry] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)   # tool_name -> bool
    missing_scripts: List[str] = field(default_factory=list)
    missing_coverage: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_scripts and not self.missing_coverage

    def as_json(self) -> str:
        return json.dumps({
            "ok": self.ok,
            "total_hooks": len(self.entries),
            "coverage": self.coverage,
            "missing_scripts": self.missing_scripts,
            "missing_coverage": self.missing_coverage,
            "entries": [
                {
                    "event": e.event, "matcher": e.matcher,
                    "command": e.command, "status": e.status(),
                    "description": e.description[:80],
                }
                for e in self.entries
            ],
        }, ensure_ascii=False, indent=2)

    def as_text(self) -> str:
        rows = []
        for e in self.entries:
            badge = {
                "OK": "[OK]      ",
                "MISSING_SCRIPT": "[MISSING] ",
                "INLINE": "[INLINE]  ",
            }.get(e.status(), "[?]       ")
            rows.append(f"{badge} {e.event:14s} {e.matcher:28s} → {e.command[:70]}")
        rows.append("")
        for tool, covered in sorted(self.coverage.items()):
            rows.append(f"{'[OK]  ' if covered else '[FAIL]'} PreToolUse coverage for {tool}: {covered}")
        verdict = "PASS" if self.ok else "FAIL"
        rows.append(f"\n--- hook_audit {verdict} | {len(self.entries)} entries | "
                    f"{len(self.missing_scripts)} missing | "
                    f"{len(self.missing_coverage)} coverage gaps ---")
        return "\n".join(rows)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _workspace_root() -> Path:
    # hook_audit.py sits at memexa/memexa/core/
    return Path(__file__).resolve().parent.parent.parent.parent


def _default_settings_path() -> Path:
    return _workspace_root() / ".claude" / "config" / "settings.json"


def _resolve_script(command: str, workspace: Path) -> tuple[Optional[Path], str]:
    """Extract first whitespace-split path-looking arg after `python`, if any.
    Returns (resolved_path_or_None, command_verbatim).

    Handles common shapes:
      'python memexa/memexa/core/foo.py'
      'python -m memexa.core.foo'
      'python -m memexa.core.foo arg1'
    """
    tokens = command.strip().split()
    if not tokens:
        return None, command
    if tokens[0].endswith("python") or tokens[0].endswith("python3"):
        if len(tokens) >= 2 and tokens[1] == "-m":
            # Module form: dotted path → look up a file under memexa/...
            if len(tokens) >= 3:
                module = tokens[2]
                parts = module.split(".")
                # Try memexa/<parts>.py
                candidate = workspace / "memexa" / Path(*parts).with_suffix(".py")
                if candidate.exists():
                    return candidate.resolve(), command
                # Fallback: <parts>/__main__.py
                candidate2 = workspace / "memexa" / Path(*parts) / "__main__.py"
                if candidate2.exists():
                    return candidate2.resolve(), command
                # No file found
                return None, command
        # File form: next token is the script path
        if len(tokens) >= 2:
            raw = tokens[1]
            p = Path(raw)
            if not p.is_absolute():
                p = workspace / raw
            return p, command
    return None, command


def load_hooks(settings_path: Optional[Path] = None,
               settings_data: Optional[dict] = None) -> AuditReport:
    """Return an AuditReport by parsing settings.json (or given dict)."""
    workspace = _workspace_root()
    if settings_data is None:
        path = settings_path or _default_settings_path()
        if not path.exists():
            return AuditReport()
        try:
            settings_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return AuditReport()
    report = AuditReport()
    hooks = (settings_data or {}).get("hooks", {}) or {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            matcher = str(e.get("matcher", "")) if isinstance(e, dict) else ""
            desc = str(e.get("description", "")) if isinstance(e, dict) else ""
            hlist = e.get("hooks") if isinstance(e, dict) else None
            if not isinstance(hlist, list):
                continue
            for h in hlist:
                if not isinstance(h, dict):
                    continue
                cmd = str(h.get("command", ""))
                sp, _ = _resolve_script(cmd, workspace)
                exists = bool(sp and sp.exists())
                report.entries.append(HookEntry(
                    event=event, matcher=matcher, command=cmd,
                    description=desc,
                    script_path=sp,
                    script_exists=exists,
                ))
    # Build coverage map for required tool names: does any PreToolUse matcher
    # containing this tool route to pretool_gate.py AND script exists?
    # S4 fix: require `pretool_gate.py` (with .py suffix) — a bogus command
    # `echo pretool_gate` would otherwise satisfy the substring check while
    # the hook is dead.
    workspace = _workspace_root()
    canonical_gate = (workspace / "memexa" / "memexa" / "core" / "pretool_gate.py").resolve()
    for tool in REQUIRED_PRETOOL_COVERAGE:
        pattern = re.compile(rf"(^|\|){re.escape(tool)}(\||$|\()")
        hit = False
        for e in report.entries:
            if e.event != "PreToolUse":
                continue
            # S4: require `.py` suffix anchoring + resolved-path equality
            if "pretool_gate.py" not in (e.command or ""):
                continue
            if not e.script_exists:
                continue
            if e.script_path and e.script_path.resolve() != canonical_gate:
                continue
            if pattern.search(e.matcher):
                hit = True
                break
        report.coverage[tool] = hit
        if not hit:
            report.missing_coverage.append(tool)
    # Collect missing scripts
    for e in report.entries:
        if e.script_path and not e.script_exists:
            report.missing_scripts.append(f"{e.event}/{e.matcher}: {e.command}")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="memexa.core.hook_audit")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list", help="list all hook entries with coverage report")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--strict", action="store_true",
                    help="exit 1 if any missing script or coverage gap")
    args = p.parse_args(argv)

    if args.cmd == "list":
        rep = load_hooks()
        if args.json:
            print(rep.as_json())
        else:
            print(rep.as_text())
        if args.strict and not rep.ok:
            return 1
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
