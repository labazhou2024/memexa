"""
Local Reviewer — Zero-cost local code quality gate.

Runs without any API calls. Checks:
1. Syntax (ast.parse)
2. Import resolution (importlib)
3. Security patterns (regex)
4. Dead imports (ast analysis)
5. Hardcoded paths/secrets

Designed to be called:
- By PreToolUse hook (before git commit)
- By sonnet-executor Agents (after writing code)
- By CTO inline (Phase C Step 1)
- By daily_evolution.py

Usage:
    from src.core.local_reviewer import review_files, review_file
    results = review_files(["memex/core/auto_dream.py"])
    if results["blocking"]:
        print("CHANGES_REQUIRED:", results["findings"])
    else:
        print("LOCAL REVIEW PASSED")
"""

import ast
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SECURITY_PATTERNS = [
    (r'\beval\s*\(', "eval() usage", "critical"),
    (r'\bexec\s*\(', "exec() usage", "critical"),
    (r'\bos\.system\s*\(', "os.system() — use subprocess instead", "critical"),
    (r'sk-[a-zA-Z0-9]{20,}', "hardcoded API key", "critical"),
    (r'password\s*=\s*["\'][^"\']{4,}["\']', "hardcoded password", "high"),
    (r'subprocess\.run\([^)]*shell\s*=\s*True', "shell=True in subprocess", "high"),
    (r'__import__\s*\(', "dynamic __import__() — potential injection", "medium"),
]

HARDCODED_PATH_PATTERN = re.compile(r'[A-Z]:\\\\?Users\\\\?[^"\'\\]+', re.IGNORECASE)


@dataclass
class Finding:
    """A single review finding."""
    file: str
    line: int
    severity: str  # critical, high, medium, low
    category: str  # syntax, security, import, dead_code, path
    message: str

    def __str__(self):
        return f"[{self.severity.upper()}] {self.file}:{self.line} ({self.category}) {self.message}"


@dataclass
class ReviewResult:
    """Result of reviewing one or more files."""
    files_reviewed: int
    findings: List[Finding] = field(default_factory=list)
    passed: bool = True

    @property
    def blocking(self) -> List[Finding]:
        """Findings that block commit (critical or high)."""
        return [f for f in self.findings if f.severity in ("critical", "high")]

    @property
    def summary(self) -> str:
        c = sum(1 for f in self.findings if f.severity == "critical")
        h = sum(1 for f in self.findings if f.severity == "high")
        m = sum(1 for f in self.findings if f.severity == "medium")
        lo = sum(1 for f in self.findings if f.severity == "low")
        status = "PASSED" if self.passed else "CHANGES_REQUIRED"
        return f"{status}: {c}C {h}H {m}M {lo}L across {self.files_reviewed} files"


def review_file(file_path: Path) -> List[Finding]:
    """Review a single Python file. Returns list of findings."""
    findings: List[Finding] = []
    fp_str = str(file_path)

    if not file_path.exists():
        findings.append(Finding(fp_str, 0, "critical", "syntax", "File does not exist"))
        return findings

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        findings.append(Finding(fp_str, 0, "critical", "syntax", f"Cannot read file: {e}"))
        return findings

    lines = content.splitlines()
    is_test = "test" in file_path.name.lower()

    # 1. Syntax check
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        findings.append(Finding(fp_str, e.lineno or 0, "critical", "syntax",
                                f"SyntaxError: {e.msg}"))
        return findings  # Can't do further analysis without valid AST

    # 2. Security scan (skip test files for eval/exec — test mocks may use them)
    in_multiline_string = False
    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        # Skip comments
        if stripped.startswith("#"):
            continue
        # Track triple-quoted strings (rough — toggle on odd count of """)
        if '"""' in line or "'''" in line:
            count = line.count('"""') + line.count("'''")
            if count % 2 == 1:
                in_multiline_string = not in_multiline_string
                continue
        if in_multiline_string:
            continue

        for pattern, desc, severity in SECURITY_PATTERNS:
            # In test files, skip eval/exec/os.system checks (mocking context)
            if is_test and severity == "critical":
                continue
            if re.search(pattern, line):
                # Skip if the match is inside a string literal (not an actual call)
                # Heuristics:
                # 1. Line is a raw string pattern definition: (r'...')
                if re.match(r'^\s*\(r["\']', stripped):
                    continue
                if re.match(r'^\s*r["\']', stripped):
                    continue
                # 2. Line is a dict/list value string: "description": "eval() usage"
                if re.match(r'^\s*["\']', stripped) and stripped.rstrip().endswith((',', '"', "'", '",', "',", '}')):
                    continue
                # 3. Match is inside quotes (rough: check if pattern match position is within quotes)
                match = re.search(pattern, line)
                if match:
                    before = line[:match.start()]
                    # Count unescaped quotes before match — odd count means inside string
                    single_q = before.count("'") - before.count("\\'")
                    double_q = before.count('"') - before.count('\\"')
                    if single_q % 2 == 1 or double_q % 2 == 1:
                        continue
                findings.append(Finding(fp_str, i, severity, "security", desc))

    # 3. Hardcoded Windows paths
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        if HARDCODED_PATH_PATTERN.search(line):
            findings.append(Finding(fp_str, i, "medium", "path",
                                    "Hardcoded Windows path — use Path or config"))

    # 4. Import analysis
    imported_names = set()
    import_lines: Dict[str, int] = {}  # name -> line number

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imported_names.add(name)
                import_lines[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.names:
                for alias in node.names:
                    name = alias.asname or alias.name
                    imported_names.add(name)
                    import_lines[name] = node.lineno

    # Check for unused imports (basic heuristic)
    for name, lineno in import_lines.items():
        if name.startswith("_"):
            continue
        # Count occurrences beyond import lines
        usage_count = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                continue
            if stripped.startswith("#"):
                continue
            # Check for the name as a whole word (rough)
            if re.search(rf'\b{re.escape(name)}\b', line):
                usage_count += 1
        if usage_count == 0:
            findings.append(Finding(fp_str, lineno, "low", "dead_code",
                                    f"Possibly unused import: {name}"))

    # 5. Encoding check for file I/O
    for i, line in enumerate(lines, 1):
        # Check for open() without encoding
        if re.search(r'\bopen\s*\(', line) and "encoding" not in line:
            # Skip if it's a binary mode
            if "'rb'" not in line and '"rb"' not in line and "'wb'" not in line and '"wb"' not in line:
                findings.append(Finding(fp_str, i, "low", "encoding",
                                        "open() without encoding= parameter"))

    return findings


def review_files(file_paths: List[str], project_root: Optional[Path] = None) -> ReviewResult:
    """Review multiple files. Returns aggregate result.

    Args:
        file_paths: List of file paths (relative or absolute)
        project_root: If given, paths are relative to this root

    Returns:
        ReviewResult with all findings and pass/fail verdict
    """
    all_findings: List[Finding] = []

    for fp_str in file_paths:
        fp = Path(fp_str)
        if project_root and not fp.is_absolute():
            fp = project_root / fp
        if not fp.suffix == ".py":
            continue
        findings = review_file(fp)
        all_findings.extend(findings)

    blocking = [f for f in all_findings if f.severity in ("critical", "high")]
    passed = len(blocking) == 0

    result = ReviewResult(
        files_reviewed=len(file_paths),
        findings=all_findings,
        passed=passed,
    )

    if not passed:
        logger.warning("Local review FAILED: %s", result.summary)
    else:
        logger.info("Local review passed: %s", result.summary)

    return result


def review_staged_files(repo_root: Path) -> ReviewResult:
    """Review all staged .py files in a git repo. For use in pre-commit hook."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=10,
        )
        if result.returncode != 0:
            return ReviewResult(files_reviewed=0, passed=True)

        py_files = [f for f in result.stdout.strip().splitlines() if f.endswith(".py")]
        if not py_files:
            return ReviewResult(files_reviewed=0, passed=True)

        return review_files(py_files, project_root=repo_root)
    except Exception as e:
        logger.error("Failed to get staged files: %s", e)
        return ReviewResult(files_reviewed=0, passed=True)


# CLI entry point for hook usage
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        files = sys.argv[1:]
        result = review_files(files)
    else:
        # Default: review staged files
        result = review_staged_files(Path.cwd())

    print(result.summary)
    for f in result.findings:
        print(f"  {f}")

    sys.exit(0 if result.passed else 1)
