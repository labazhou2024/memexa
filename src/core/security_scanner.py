"""
Security Scanner -- 代码级自动安全审计

借鉴 gstack /cso + skills-desktop security.rs 的 9 类检查规则。
不是文档，是实际执行扫描并返回 exit code 的 Python 脚本。

调用方式:
  python memex/core/security_scanner.py [directory]
  默认扫描 memex/

集成方式:
  PreToolUse hook on Bash(git push*) -> 自动阻止包含安全问题的 push

Exit codes:
  0 = PASS (无 CRITICAL/HIGH)
  2 = FAIL (发现 CRITICAL 或 HIGH, 阻止 push)
"""

import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_MEMEX = Path(__file__).parent.parent
_WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent


# ---------------------------------------------------------------------------
# plan_v1 TU-B4: inline-allow comment + HMAC-signed file allowlist
#
# Precedence (R1 I2 fix):
#   1. Inline `# security-scanner: allow <category> [<reason>]` on same line → suppressed,
#      independent of HMAC key presence
#   2. File allowlist entry matching (file, line, category) → suppressed ONLY if HMAC valid
#   3. HMAC key unset OR signature invalid → file allowlist ignored
#   4. Same finding matched by both inline + file allowlist → inline wins (no key needed)
# ---------------------------------------------------------------------------

_INLINE_ALLOW_RE = re.compile(
    r"#\s*security-scanner:\s*allow\s+(?P<category>[a-z_]+)(?:\s+(?P<reason>.+))?",
    re.IGNORECASE,
)

_ALLOWLIST_PATH = _WORKSPACE / ".claude" / "config" / ".scanner-allowlist.json"
_ALLOWLIST_KEY_ENV = "MEMEX_ALLOWLIST_KEY"


def _is_inline_allowed(line: str, category: str) -> bool:
    """Return True if the line carries an inline allow comment matching this category."""
    m = _INLINE_ALLOW_RE.search(line)
    if not m:
        return False
    tagged = (m.group("category") or "").lower().strip()
    return tagged == category.lower()


def _canonical_hmac_msg(file: str, line: int, category: str, reason: str) -> str:
    """Canonical HMAC message — JSON array avoids field-boundary ambiguity (S3 fix).
    Using `|`-separated encoding would let `reason="x|y"` cross-match
    `category="x"`, `reason="y"`."""
    return json.dumps([file, int(line), category, reason], ensure_ascii=False)


def _sign_allowlist_entry(file: str, line: int, category: str, reason: str) -> str:
    """Compute HMAC-SHA256 hex for an allowlist entry. Returns "" if key unset
    or (S2 fix) if we're inside a pytest run — refusing to sign under pytest
    prevents a compromised test body from self-whitelisting using the fixture
    key inherited via monkeypatch.setenv."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        # Test scope — signing is forbidden from CLI. Tests that exercise
        # the signing helper itself (test_scanner_allowlist.py) bypass this
        # guard by calling _sign_allowlist_entry with a local override path.
        pass  # proceed — but see _cmd_scan_allow which refuses entirely
    key = os.environ.get(_ALLOWLIST_KEY_ENV, "")
    if not key:
        return ""
    msg = _canonical_hmac_msg(file, line, category, reason)
    return hmac.new(
        key.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _load_allowlist() -> List[dict]:
    """Load validated (HMAC-passing) allowlist entries. Empty list if:
    - file missing
    - env key unset
    - json malformed
    Entries with invalid HMAC are dropped.
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
    validated: List[dict] = []
    for e in data.get("entries", []) or []:
        if not isinstance(e, dict):
            continue
        file = str(e.get("file", ""))
        line = int(e.get("line", 0))
        cat = str(e.get("category", ""))
        reason = str(e.get("reason", ""))
        sig = str(e.get("hmac_sha256", ""))
        expected = _sign_allowlist_entry(file, line, cat, reason)
        if expected and hmac.compare_digest(sig, expected):
            validated.append(e)
    return validated


def _is_file_allowlisted(file: str, line_num: int, category: str,
                         validated: List[dict]) -> bool:
    """Return True if validated allowlist has a matching entry.

    S1 fix: exact-suffix match (not substring) with minimum 2-path-component
    specificity. Rejects entries like file='mod.py' that could match
    '/any/deep/mod.py', or single-char entries that match everything.
    """
    file_norm = str(Path(file).as_posix())
    for e in validated:
        e_file_raw = str(e.get("file", ""))
        if not e_file_raw:
            continue
        e_file = str(Path(e_file_raw).as_posix())
        # Specificity floor: entry must specify at least 2 path components
        if len(Path(e_file).parts) < 2 and "/" not in e_file:
            # Allow single-component bare name only if the scanned file IS
            # exactly that name (no directory prefix in the target either)
            if file_norm != e_file:
                continue
        # Exact-suffix match: file_norm ends with `/e_file` or equals it
        if file_norm == e_file or file_norm.endswith("/" + e_file):
            if e.get("line") == line_num and \
               str(e.get("category", "")) == category:
                return True
    return False


# ── 9 类安全检查规则 ──

RULES: List[Tuple[str, str, str, str]] = [
    # (category, severity, pattern, description)

    # 1. API Key / Secret 泄露
    ("api_key_leak", "CRITICAL",
     r"""(?:api[_-]?key|secret|password|token|credential)\s*=\s*['"][A-Za-z0-9_\-/.]{8,}['"]""",
     "Hardcoded API key or secret"),

    # 2. 命令注入
    ("command_injection", "CRITICAL",
     r"""eval\s*\(""",
     "eval() usage (command injection risk)"),
    ("command_injection", "CRITICAL",
     r"""exec\s*\((?!.*compile)""",
     "exec() usage (command injection risk)"),
    ("command_injection", "HIGH",
     r"""os\.system\s*\(""",
     "os.system() usage (prefer subprocess)"),
    ("command_injection", "HIGH",
     r"""subprocess\..*shell\s*=\s*True""",
     "subprocess with shell=True (injection risk)"),

    # 3. 路径穿越
    ("path_traversal", "HIGH",
     r"""\.\.[\\/]""",
     "Path traversal pattern (../ or ..\\)"),

    # 4. 不安全反序列化
    ("unsafe_deserialize", "HIGH",
     r"""pickle\.loads?\s*\(""",
     "pickle.load/loads (arbitrary code execution)"),
    ("unsafe_deserialize", "HIGH",
     r"""yaml\.load\s*\((?!.*Loader)""",
     "yaml.load without SafeLoader"),

    # 5. SQL 注入
    ("sql_injection", "HIGH",
     r"""f['"].*(?:SELECT|INSERT|UPDATE|DELETE|DROP)\s.*\{""",
     "f-string SQL query (SQL injection risk)"),

    # 6. 敏感文件访问
    ("sensitive_file", "MEDIUM",
     r"""['"]/etc/(?:passwd|shadow|sudoers)""",
     "Access to sensitive system file"),
    ("sensitive_file", "MEDIUM",
     r"""\.ssh[/\\]""",
     "Access to SSH directory"),

    # 7. 权限问题
    ("permission", "MEDIUM",
     r"""chmod\s+777|0o777""",
     "chmod 777 (world-writable)"),

    # 8. AI 提示词注入
    ("prompt_injection", "LOW",
     r"""ignore.*(?:previous|above).*instruction""",
     "Potential prompt injection pattern"),

    # 9. 硬编码凭据模式
    ("hardcoded_cred", "MEDIUM",
     r"""(?:Bearer|Basic)\s+[A-Za-z0-9+/=]{20,}""",
     "Hardcoded Bearer/Basic token"),
]

# 排除的路径模式
EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", "archive"}
EXCLUDE_FILES = {"security_scanner.py"}  # 不扫自己


def scan_file(filepath: Path) -> List[dict]:
    """扫描单个文件，返回 findings 列表"""
    findings = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Load file-level allowlist once per file scan (cheap — cached by caller
    # can be added later if perf matters).
    validated_allowlist = _load_allowlist()

    lines = content.splitlines()
    # TU-9 (plan_v1, 2026-04-25): track multi-line docstring state so
    # docstring bodies (which often contain `../path` examples or words
    # like "delete" inside f-strings) don't fire path_traversal /
    # sql_injection false positives. Toggle on every triple-quote.
    in_docstring = False
    for line_num, line in enumerate(lines, 1):
        # 跳过注释行（整行都是注释才跳；trailing `# security-scanner: allow` 仍被处理）
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        # docstring scope: skip pattern matching inside docstrings
        triple_dq = line.count('"""')
        triple_sq = line.count("'''")
        if in_docstring:
            # Currently inside docstring; check for closing triple-quote
            if (triple_dq % 2) == 1 or (triple_sq % 2) == 1:
                in_docstring = False
            continue
        # Not in docstring; check if this line opens one
        if (triple_dq % 2) == 1 or (triple_sq % 2) == 1:
            in_docstring = True
            # Still scan THIS line (it may have a false-positive opener),
            # but flip state so next lines are skipped until close.
            # Skip-this-line is safer to avoid scanning the docstring opener.
            continue

        for category, severity, pattern, description in RULES:
            if re.search(pattern, line, re.IGNORECASE):
                # 排除已知安全的模式
                if _is_false_positive(line, category):
                    continue
                # plan_v1 TU-B4 inline-allow (precedence 1)
                if _is_inline_allowed(line, category):
                    continue
                # plan_v1 TU-B4 file-level allowlist (precedence 2, requires HMAC)
                if _is_file_allowlisted(str(filepath), line_num, category,
                                        validated_allowlist):
                    continue
                findings.append({
                    "category": category,
                    "severity": severity,
                    "file": str(filepath),
                    "line": line_num,
                    "code": stripped[:120],
                    "description": description,
                })
    return findings


def _is_false_positive(line: str, category: str) -> bool:
    """过滤已知的误报"""
    line_lower = line.lower().strip()

    # 注释中的匹配不算
    if line_lower.startswith("#") or line_lower.startswith("//") or line_lower.startswith('"""'):
        return True

    # TU-9 (plan_v1, 2026-04-25): universal noqa annotation. Previously the
    # noqa check existed only inside the command_injection branch, leaving
    # path_traversal / sql_injection / others without an opt-out. Promote
    # to top-level so any line marked `# noqa: <category>` (or just
    # `# noqa`) is universally honored.
    if "noqa" in line_lower:
        return True

    # 字符串中的模式定义 (如 regex pattern 字符串) 不算
    # 检测: 行中匹配项被引号包裹 (r'..eval..' 或 "..eval..")
    if category == "command_injection":
        if any(x in line for x in ["ast.parse", "ast.literal_eval", "r'\\b", 'r"\\b', "r'.*", 'r".*']):
            return True
        # literal_eval is safe
        if "literal_eval" in line:
            return True
        # 在字符串常量中定义的模式规则
        if re.search(r"""['"]\s*\(?\s*r['"].*(?:eval|exec|os\.system)""", line):
            return True
        # Function/method names containing eval/exec as substrings
        if re.search(r"def\s+\w*(?:eval|exec)\w*\(", line):
            return True
        if re.search(r"self\.\w*(?:eval|exec)\w*\(", line):
            return True
        # Quoted blocker strings in lists (e.g. "eval(", "exec(")
        if line.strip().startswith('"') and line.strip().rstrip().endswith('",'):
            return True
        # noqa-annotated intentional usage
        if "noqa" in line:
            return True
        # Documentation strings mentioning these patterns
        if "No eval" in line or "No exec" in line:
            return True

    # api_key_leak: 占位符和 DEPRECATED 标记
    if category == "api_key_leak":
        if any(x in line_lower for x in ["deprecated", "placeholder", "example", "xxx", "your_key_here", "test"]):
            return True

    # path_traversal: Path(__file__).parent.parent 不算
    if category == "path_traversal" and "__file__" in line:
        return True
    # path_traversal: JSON 模板中的 "..." 不算
    if category == "path_traversal":
        if '".."' in line or "'..'" in line:
            return True
        # 省略号模式 (...) 在字符串中
        if '\\"...\\"' in line or "'..'..." in line:
            return True
        # prompt 字符串中的 JSON 示例
        if "severity" in line and "description" in line:
            return True

    # 通用: 如果行本身是字符串/regex 定义 (以引号或 r' 开头)
    stripped = line.strip()
    if stripped.startswith("(r'") or stripped.startswith('(r"') or stripped.startswith("r'") or stripped.startswith('r"'):
        return True

    return False


def scan_directory(directory: Path) -> List[dict]:
    """扫描目录下所有 .py 文件"""
    findings = []
    for pyfile in sorted(directory.rglob("*.py")):
        # 跳过排除的目录和文件
        if any(ex in pyfile.parts for ex in EXCLUDE_DIRS):
            continue
        if pyfile.name in EXCLUDE_FILES:
            continue
        findings.extend(scan_file(pyfile))
    return findings


# TU-R9 (2026-04-23): expand default scope beyond memex/core/.
# Reality check: _safe_fs.py HIGH sat 3 days undetected by daily push scan
# because push hook hardcoded memex/core/ only. tests/ and scripts/ carry
# real attack surface (test fixtures that execute, CLI entry points).
_DEFAULT_SCOPE_DIRS = ("memex/core", "memex/tests", "memex/scripts")


def scan_default_scope(memex_root: Optional[Path] = None) -> List[dict]:
    """Scan the default multi-dir scope. Env MEMEX_SCANNER_SCOPE can
    override (comma-separated relative paths).

    Returns merged findings from all scoped dirs.
    """
    root = memex_root or _MEMEX
    scope_env = os.environ.get("MEMEX_SCANNER_SCOPE", "").strip()
    if scope_env:
        dirs = [d.strip() for d in scope_env.split(",") if d.strip()]
    else:
        dirs = list(_DEFAULT_SCOPE_DIRS)
    findings: List[dict] = []
    for rel in dirs:
        d = root / rel
        if d.exists() and d.is_dir():
            findings.extend(scan_directory(d))
    return findings


def format_report(findings: List[dict], scan_dir: str,
                  scan_mode: str = "regex") -> str:
    """格式化报告

    scan_mode: "regex" | "ast" | "combined"
    """
    lines = [f"Security Audit: {scan_dir} [mode={scan_mode}]"]

    if not findings:
        lines.append("Result: PASS (no issues found)")
        return "\n".join(lines)

    by_severity = {}
    for f in findings:
        by_severity.setdefault(f["severity"], []).append(f)

    critical = len(by_severity.get("CRITICAL", []))
    high = len(by_severity.get("HIGH", []))
    medium = len(by_severity.get("MEDIUM", []))
    low = len(by_severity.get("LOW", []))

    verdict = "FAIL" if (critical + high) > 0 else "PASS"
    lines.append(f"Result: {verdict} ({critical} critical, {high} high, {medium} medium, {low} low)")

    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
        items = by_severity.get(sev, [])
        if items:
            lines.append(f"\n[{sev}]")
            for f in items[:10]:  # 每级最多 10 条
                src = f.get("source", "regex")
                lines.append(f"  [{src}] {f['file']}:{f['line']} - {f['description']}")
                lines.append(f"    {f['code']}")

    return "\n".join(lines)


def _dedup_findings(findings: List[dict]) -> List[dict]:
    """
    Deduplicate findings from combined regex + AST scans.

    Dedup key: (file, line, category).
    When duplicates exist, prefer the AST finding as more precise.
    """
    seen: dict = {}
    # Process AST findings first so they take priority
    ast_findings = [f for f in findings if f.get("source") == "ast"]
    regex_findings = [f for f in findings if f.get("source") != "ast"]

    result: List[dict] = []
    for f in ast_findings:
        key = (f["file"], f["line"], f["category"])
        if key not in seen:
            seen[key] = True
            result.append(f)
    for f in regex_findings:
        key = (f["file"], f["line"], f["category"])
        if key not in seen:
            seen[key] = True
            result.append(f)
    return result


def scan_file_combined(filepath: Path) -> List[dict]:
    """
    Run BOTH regex and AST scan on a single file.

    Deduplicates results: same (file, line, category) keeps AST version.
    """
    from src.core.ast_security import scan_file_ast

    regex_findings = scan_file(filepath)
    # Mark regex findings with source tag
    for f in regex_findings:
        f.setdefault("source", "regex")

    ast_findings = scan_file_ast(filepath)

    return _dedup_findings(regex_findings + ast_findings)


def scan_directory_combined(directory: Path) -> List[dict]:
    """
    Run BOTH regex and AST scan on all .py files in directory.

    Deduplicates results across both scan modes.
    """
    from src.core.ast_security import scan_file_ast

    findings: List[dict] = []
    for pyfile in sorted(directory.rglob("*.py")):
        # Apply same exclusion rules as scan_directory
        if any(ex in pyfile.parts for ex in EXCLUDE_DIRS):
            continue
        if pyfile.name in EXCLUDE_FILES:
            continue
        regex_findings = scan_file(pyfile)
        for f in regex_findings:
            f.setdefault("source", "regex")
        ast_findings = scan_file_ast(pyfile)
        findings.extend(regex_findings + ast_findings)

    return _dedup_findings(findings)


def _cmd_scan_allow(argv: List[str]) -> int:
    """scan-allow CLI: compute HMAC for a (file, line, category, reason) tuple
    and append to .scanner-allowlist.json. Refuses if env key unset."""
    import argparse
    p = argparse.ArgumentParser(prog="security_scanner scan-allow")
    p.add_argument("--file", required=True, help="workspace-relative path")
    p.add_argument("--line", required=True, type=int)
    p.add_argument("--category", required=True)
    p.add_argument("--reason", required=True)
    args = p.parse_args(argv)

    # S2 fix: refuse inside pytest scope. A compromised test body could
    # otherwise invoke scan-allow using the monkeypatched fixture key and
    # self-whitelist its own findings.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        print(
            "error: scan-allow refused inside pytest scope "
            "(PYTEST_CURRENT_TEST is set). Run from an operator shell.",
            file=sys.stderr,
        )
        return 2
    key = os.environ.get(_ALLOWLIST_KEY_ENV, "")
    if not key:
        print(
            f"error: {_ALLOWLIST_KEY_ENV} env var not set. "
            f"Run `setx {_ALLOWLIST_KEY_ENV} \"<32-hex-random>\"` (new terminal required)",
            file=sys.stderr,
        )
        return 2
    sig = _sign_allowlist_entry(args.file, args.line, args.category, args.reason)
    entry = {
        "file": args.file, "line": args.line,
        "category": args.category, "reason": args.reason,
        "hmac_sha256": sig,
    }
    _ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _ALLOWLIST_PATH.exists():
        try:
            data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {"version": 1, "entries": []}
    else:
        data = {"version": 1, "entries": []}
    data.setdefault("entries", []).append(entry)
    _ALLOWLIST_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[scan-allow] added entry: {args.file}:{args.line} {args.category}")
    return 0


def main():
    # Subcommand dispatch: scan (default) | scan-allow
    if len(sys.argv) >= 2 and sys.argv[1] == "scan-allow":
        sys.exit(_cmd_scan_allow(sys.argv[2:]))

    scan_dir = sys.argv[1] if len(sys.argv) > 1 else str(_MEMEX / "memex")
    directory = Path(scan_dir)

    if not directory.exists():
        print(f"Directory not found: {scan_dir}", file=sys.stderr)
        sys.exit(1)

    findings = scan_directory(directory)
    report = format_report(findings, scan_dir)
    print(report)

    # Exit 2 = FAIL (blocks git push via hook)
    critical_high = [f for f in findings if f["severity"] in ("CRITICAL", "HIGH")]
    if critical_high:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
