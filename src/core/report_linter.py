"""
report_linter.py — Three-state report-language enforcement (2026-04-21).

ANCHOR-6 enforcement: reports (last_briefing.json, last_sync.json,
.claude/reports/*) must tag each claim as [code] / [test] / [LIVE]
and must not use "全部闭环/闭环就位/fully wired" style victory claims.

Contract:
  - lint(text) → List[Violation]; empty list = pass
  - CLI `python -m src.core.report_linter [file|--stdin]`; exit 0 = clean,
    exit 2 = violations found (same as gate convention)
  - PostToolUse hook route: path matches last_briefing.json /
    last_sync.json / .claude/reports/*.md|json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List


# Banned victory-claim phrases (case-insensitive substring match).
BANNED_PHRASES = [
    # Chinese
    "闭环就位", "闭环了", "闭环完成", "全部跑通", "完全闭环",
    "全部闭环", "打通", "通跑",
    # English
    "production ready", "fully wired", "end-to-end working",
    "everything works", "all systems go", "shipped and working",
    "all green", "closed loop confirmed", "victory declared",
]

# Phrases that look like claims (trigger tri-state tag requirement).
# Case-insensitive; substring.
CLAIM_PREDICATES = [
    # English
    "tests passing", "tests passed", "passed", "shipped",
    "deployed", "completed", "verified", "works", "running",
    "live", "production",
    # Chinese
    "完成", "通过", "已跑", "实测", "部署", "投产",
    "验证通过", "验收通过", "测试通过",
]

# Required tags (any one of these on a claim sentence satisfies ANCHOR-6).
TRI_STATE_TAGS = ["[code]", "[test]", "[LIVE]"]


@dataclass
class Violation:
    kind: str  # "banned_phrase" | "untagged_claim"
    detail: str
    location: str = ""  # line:col or json-path


def _extract_plaintext(text: str, file_hint: str = "") -> str:
    """If text is JSON, pull out string values that are human-readable.
    Otherwise return text as-is.
    """
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            d = json.loads(stripped)

            def _walk(node) -> List[str]:
                out = []
                if isinstance(node, dict):
                    for v in node.values():
                        out.extend(_walk(v))
                elif isinstance(node, list):
                    for v in node:
                        out.extend(_walk(v))
                elif isinstance(node, str):
                    out.append(node)
                return out

            return "\n".join(_walk(d))
        except json.JSONDecodeError:
            return text
    return text


def _split_sentences(text: str) -> List[str]:
    """Simple sentence splitter. Not perfect but good enough for lint."""
    # Split on . ! ? Chinese 。 ！ ？ 、 plus newlines.
    parts = re.split(r"(?<=[。！？\.\!\?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def lint(text: str, file_hint: str = "") -> List[Violation]:
    """Scan text for violations. Returns empty list if clean."""
    violations: List[Violation] = []
    plaintext = _extract_plaintext(text, file_hint)

    # 1. Banned phrases
    plaintext_lower = plaintext.lower()
    for phrase in BANNED_PHRASES:
        if phrase.lower() in plaintext_lower:
            violations.append(Violation(
                kind="banned_phrase",
                detail=phrase,
            ))

    # 2. Untagged claims
    # #lint:ignore works at LINE level (not just sentence) so a line with
    # ". #lint:ignore" suppresses the preceding claim. Check line-by-line.
    for line in plaintext.splitlines():
        if "#lint:ignore" in line.lower():
            continue
        # Process sentences within the line
        for sent in _split_sentences(line):
            sent_lower = sent.lower()
            if len(sent_lower) < 10:
                continue
            has_claim_predicate = any(p.lower() in sent_lower for p in CLAIM_PREDICATES)
            if not has_claim_predicate:
                continue
            has_tag = any(tag.lower() in sent_lower for tag in TRI_STATE_TAGS)
            if not has_tag:
                violations.append(Violation(
                    kind="untagged_claim",
                    detail=sent[:120],
                ))
    return violations


def _cli(argv) -> int:
    parser = argparse.ArgumentParser(prog="report_linter")
    parser.add_argument("file", nargs="?", default=None)
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--json", action="store_true",
                        help="emit violations as JSON instead of human text")
    args = parser.parse_args(argv[1:])

    if args.stdin or args.file is None:
        text = sys.stdin.read()
        hint = "<stdin>"
    else:
        p = Path(args.file)
        text = p.read_text(encoding="utf-8", errors="replace")
        hint = str(p)

    viols = lint(text, file_hint=hint)
    if args.json:
        print(json.dumps([v.__dict__ for v in viols], ensure_ascii=False, indent=2))
    else:
        if not viols:
            print("OK: no violations")
            return 0
        print(f"VIOLATIONS ({len(viols)}):")
        for v in viols:
            print(f"  {v.kind}: {v.detail}")
    return 2 if viols else 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
