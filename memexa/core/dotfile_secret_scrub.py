"""Strip secret-bearing lines before mirroring dotfiles to OneDrive.

Per plan §3 U3 Action 4 + security-iter2-5:
- Mirror ~/.zshrc + ~/.condarc + xfer.py to OneDrive workspace WITHOUT secrets
- Patterns scrubbed: API_KEY, TOKEN, PASSWORD, SECRET, sk-, ghp_, AKIA, AIza, ey<JWT>

Usage:
    python dotfile_secret_scrub.py <input_path> <output_path>
    python dotfile_secret_scrub.py --check <input_path>      # exit 1 if any secret pattern present
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SECRET_PATTERNS = (
    # iter1 sec-1 fix: drop \b on left side so ANTHROPIC_API_KEY=... matches.
    # Variable-name pattern: any *_API_KEY / *_SECRET_KEY / *_ACCESS_KEY etc with assignment.
    re.compile(r"(API_?KEY|ACCESS_?KEY|SECRET_?KEY|AUTH_?TOKEN|BEARER_?TOKEN)\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\b(PASSWORD|PASSWD|PASSPHRASE)\s*[=:]\s*\S+", re.IGNORECASE),
    # iter1 sec-2 fix: include sk_ underscore variants (Stripe sk_live_/sk_test_, etc.)
    re.compile(r"\bsk[-_][A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    # iter1 sec-3 fix: TOKEN wildcard restricted to assignment with non-path value (rejects
    # TOKENIZER_PATH=/models/bert and OAUTH_TOKEN_FILE=/etc/secret since /-prefixed paths
    # do not match). Requires value to NOT start with / (path) — assumes secrets are not paths.
    re.compile(r"\b[A-Z][A-Z0-9_]*TOKEN[A-Z0-9_]*\s*[=:]\s*['\"]?(?!/)\S{8,}"),
)

REDACT_LINE = "# [REDACTED by dotfile_secret_scrub: line matched secret pattern]\n"


def scrub_text(text: str) -> tuple[str, int]:
    """Return (scrubbed_text, lines_redacted_count)."""
    out_lines = []
    redacted = 0
    for line in text.splitlines(keepends=True):
        if any(p.search(line) for p in SECRET_PATTERNS):
            out_lines.append(REDACT_LINE)
            redacted += 1
        else:
            out_lines.append(line)
    return "".join(out_lines), redacted


def scrub_file(src: Path, dst: Path) -> int:
    """Scrub src → dst. Return count of lines redacted."""
    text = src.read_text(encoding="utf-8", errors="replace")
    scrubbed, count = scrub_text(text)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(scrubbed, encoding="utf-8")
    return count


def check_file(src: Path) -> int:
    """Return count of lines that WOULD be redacted; non-zero exit means secrets present."""
    text = src.read_text(encoding="utf-8", errors="replace")
    _, count = scrub_text(text)
    return count


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    if argv[1] == "--check":
        if len(argv) < 3:
            print("usage: --check <path>")
            return 2
        n = check_file(Path(argv[2]))
        print(f"SECRET_LINES_FOUND={n}")
        return 1 if n > 0 else 0
    if len(argv) < 3:
        print(__doc__)
        return 2
    src = Path(argv[1])
    dst = Path(argv[2])
    n = scrub_file(src, dst)
    print(f"SCRUB OK {src} -> {dst} (redacted={n})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
