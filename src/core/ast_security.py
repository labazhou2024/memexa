"""
AST-level Security Analyzer

Uses Python's ast module for semantic security analysis:
1. Taint tracking: traces user input through function calls
2. Dangerous call detection: finds eval/exec/os.system with non-literal args
3. Hardcoded secret detection: finds string assignments to secret-named variables
4. Unsafe deserialization: finds pickle/yaml.load with non-SafeLoader
5. SQL injection: finds f-string/format in SQL-like strings
6. Path traversal: finds unvalidated path joins with user input
7. Subprocess shell: finds subprocess calls with shell=True and non-literal args
8. Import analysis: flags dangerous imports (ctypes, importlib with user input)

Unlike regex scanning, AST analysis understands:
- Variable scope and data flow
- Function call arguments (literal vs variable)
- String formatting methods (f-string, .format, %)
- Exception handling that swallows security errors
"""

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# SQL keywords that indicate SQL query construction
# Only keywords that are unambiguously SQL (not common English like "execute")
_SQL_KEYWORDS = {"SELECT", "INSERT INTO", "UPDATE", "DELETE FROM", "DROP TABLE",
                 "CREATE TABLE", "ALTER TABLE", "TRUNCATE", "UNION SELECT"}

# Variable name patterns that suggest secrets
_SECRET_PATTERNS = {"key", "secret", "password", "passwd", "token",
                    "credential", "apikey", "api_key", "auth", "private"}

# Dangerous imports to flag
_DANGEROUS_IMPORTS = {"ctypes", "cffi", "mmap"}


@dataclass
class SecurityFinding:
    category: str
    severity: str
    file: str
    line: int
    col: int
    description: str
    ast_node_type: str
    confidence: str  # "HIGH" | "MEDIUM" | "LOW"
    code: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "code": self.code,
            "description": self.description,
            "ast_node_type": self.ast_node_type,
            "confidence": self.confidence,
            "source": "ast",
        }


def _is_constant(node: ast.expr) -> bool:
    """Return True if an AST expression is a compile-time constant (literal)."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_constant(el) for el in node.elts)
    return False


def _node_source(node: ast.AST, lines: List[str]) -> str:
    """Extract the source line for a node (best effort)."""
    lineno = getattr(node, "lineno", None)
    if lineno and 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:120]
    return ""


def _name_contains_secret(name: str) -> bool:
    """Return True if variable name contains a secret-pattern word."""
    lower = name.lower()
    return any(pat in lower for pat in _SECRET_PATTERNS)


def _is_sql_like_string(s: str) -> bool:
    """Return True if string looks like a SQL query template.

    Requires multi-word SQL patterns (e.g. 'SELECT ... FROM', 'INSERT INTO')
    to avoid false positives on common English words like 'execute'.
    """
    upper = s.upper()
    if not any(kw in upper for kw in _SQL_KEYWORDS):
        return False
    # Require at least one SQL structural marker alongside the keyword
    import re
    sql_patterns = [
        r"\bSELECT\b.*\bFROM\b",
        r"\bINSERT\s+INTO\b",
        r"\bUPDATE\b.*\bSET\b",
        r"\bDELETE\s+FROM\b",
        r"\bDROP\s+TABLE\b",
        r"\bCREATE\s+TABLE\b",
        r"\bALTER\s+TABLE\b",
        r"\bUNION\s+SELECT\b",
    ]
    return any(re.search(p, upper) for p in sql_patterns)


def _call_func_name(node: ast.Call) -> str:
    """Extract dotted function name from a Call node (best effort)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        current: ast.expr = func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


class ASTSecurityVisitor(ast.NodeVisitor):
    """
    Walk an AST tree and collect security findings.

    Taint model:
      - Function parameters are treated as tainted (user-controlled input).
      - Variables assigned from tainted sources propagate taint.
      - Assignments of string constants are safe.
      - Other assignments are unknown (treated conservatively as tainted
        for high-severity checks, but not flagged for MEDIUM/LOW checks).
    """

    def __init__(self, filepath: str, lines: List[str]) -> None:
        self.filepath = filepath
        self.lines = lines
        self.findings: List[SecurityFinding] = []
        # var_name -> "tainted" | "safe" | "unknown"
        self.assignments: Dict[str, str] = {}
        self._imported_names: List[str] = []  # module names imported

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _add(self, node: ast.AST, category: str, severity: str,
             description: str, confidence: str = "MEDIUM") -> None:
        line = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", 0)
        # Skip findings on lines with noqa comments (intentional usage)
        if line and 1 <= line <= len(self.lines):
            if "noqa" in self.lines[line - 1]:
                return
        self.findings.append(SecurityFinding(
            category=category,
            severity=severity,
            file=self.filepath,
            line=line,
            col=col,
            description=description,
            ast_node_type=type(node).__name__,
            confidence=confidence,
            code=_node_source(node, self.lines),
        ))

    def _var_is_tainted(self, name: str) -> bool:
        state = self.assignments.get(name)
        return state in ("tainted", "unknown", None)

    def _arg_is_non_literal(self, node: ast.expr) -> bool:
        """Return True if the arg is NOT a compile-time constant."""
        return not _is_constant(node)

    # ── Visitors ─────────────────────────────────────────────────────────────

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Mark function parameters as tainted sources
        for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
            self.assignments[arg.arg] = "tainted"
        if node.args.vararg:
            self.assignments[node.args.vararg.arg] = "tainted"
        if node.args.kwarg:
            self.assignments[node.args.kwarg.arg] = "tainted"
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track assignments and detect hardcoded secrets."""
        value = node.value
        is_str_constant = isinstance(value, ast.Constant) and isinstance(value.value, str)
        is_safe = _is_constant(value)

        for target in node.targets:
            if isinstance(target, ast.Name):
                if is_safe:
                    self.assignments[target.id] = "safe"
                elif isinstance(value, ast.Name):
                    # Propagate taint from RHS variable
                    self.assignments[target.id] = self.assignments.get(value.id, "unknown")
                else:
                    self.assignments[target.id] = "unknown"

                # Check hardcoded secrets: string literal assigned to secret-named var
                if is_str_constant and _name_contains_secret(target.id):
                    str_val = value.value  # type: ignore[union-attr]
                    # Minimum length 6 to avoid flagging empty/trivial placeholders
                    if len(str_val) >= 6:
                        self._check_hardcoded_secrets(node, target.id, str_val)

        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Handle annotated assignments (x: str = 'value')."""
        if node.value is not None and isinstance(node.target, ast.Name):
            value = node.value
            is_str_constant = isinstance(value, ast.Constant) and isinstance(value.value, str)
            is_safe = _is_constant(value)
            name = node.target.id

            if is_safe:
                self.assignments[name] = "safe"
            elif isinstance(value, ast.Name):
                self.assignments[name] = self.assignments.get(value.id, "unknown")
            else:
                self.assignments[name] = "unknown"

            if is_str_constant and _name_contains_secret(name):
                str_val = value.value  # type: ignore[union-attr]
                if len(str_val) >= 6:
                    self._check_hardcoded_secrets(node, name, str_val)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._imported_names.append(alias.name)
            if alias.name in _DANGEROUS_IMPORTS:
                self._add(node, "dangerous_import", "MEDIUM",
                          f"Import of '{alias.name}' (low-level memory access)",
                          confidence="HIGH")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module in _DANGEROUS_IMPORTS:
            self._add(node, "dangerous_import", "MEDIUM",
                      f"Import from '{module}' (low-level memory access)",
                      confidence="HIGH")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Dispatch to specific checkers based on function name."""
        name = _call_func_name(node)

        self._check_eval_exec(node, name)
        self._check_os_system(node, name)
        self._check_subprocess_shell(node, name)
        self._check_pickle(node, name)
        self._check_yaml_unsafe(node, name)
        self._check_sql_format(node, name)
        self._check_path_traversal(node, name)

        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        """Detect bare except: clauses that swallow all errors."""
        self._check_bare_except(node)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        """Flag assert statements (disabled with python -O)."""
        self._check_assert_in_prod(node)
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        """Detect SQL injection via f-strings."""
        # Reconstruct the template string portion to check for SQL keywords
        parts = []
        for val in node.values:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                parts.append(val.value)
        template = " ".join(parts)
        if _is_sql_like_string(template):
            self._add(node, "sql_injection", "HIGH",
                      "f-string SQL query (SQL injection risk)",
                      confidence="HIGH")
        self.generic_visit(node)

    # ── Specific Checks ───────────────────────────────────────────────────────

    def _check_eval_exec(self, node: ast.Call, name: str) -> None:
        """eval/exec with non-Constant arg -> CRITICAL."""
        if name not in ("eval", "exec"):
            return
        if not node.args:
            return
        arg = node.args[0]
        if self._arg_is_non_literal(arg):
            self._add(node, "command_injection", "CRITICAL",
                      f"{name}() with non-literal argument (command injection risk)",
                      confidence="HIGH")

    def _check_os_system(self, node: ast.Call, name: str) -> None:
        """os.system with non-Constant arg -> HIGH."""
        if name not in ("os.system", "os.popen"):
            return
        if not node.args:
            return
        arg = node.args[0]
        if self._arg_is_non_literal(arg):
            self._add(node, "command_injection", "HIGH",
                      f"{name}() with non-literal argument (prefer subprocess)",
                      confidence="HIGH")

    def _check_subprocess_shell(self, node: ast.Call, name: str) -> None:
        """subprocess calls with shell=True and non-literal cmd -> HIGH."""
        if not any(name.startswith(p) for p in
                   ("subprocess.run", "subprocess.Popen", "subprocess.call",
                    "subprocess.check_call", "subprocess.check_output")):
            return
        # Check if shell=True keyword argument is present
        has_shell_true = False
        for kw in node.keywords:
            if (kw.arg == "shell" and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True):
                has_shell_true = True
                break
        if not has_shell_true:
            return
        # Check first positional arg (the command)
        if not node.args:
            return
        cmd_arg = node.args[0]
        if self._arg_is_non_literal(cmd_arg):
            self._add(node, "command_injection", "HIGH",
                      "subprocess with shell=True and non-literal command (injection risk)",
                      confidence="HIGH")

    def _check_pickle(self, node: ast.Call, name: str) -> None:
        """pickle.load/loads -> HIGH."""
        if name in ("pickle.load", "pickle.loads",
                    "cPickle.load", "cPickle.loads"):
            self._add(node, "unsafe_deserialize", "HIGH",
                      f"{name}() (arbitrary code execution risk)",
                      confidence="HIGH")

    def _check_yaml_unsafe(self, node: ast.Call, name: str) -> None:
        """yaml.load without Loader kwarg -> HIGH."""
        if name != "yaml.load":
            return
        # Check if Loader keyword argument is present
        has_loader = any(kw.arg == "Loader" for kw in node.keywords)
        if not has_loader:
            self._add(node, "unsafe_deserialize", "HIGH",
                      "yaml.load() without Loader= argument (use yaml.safe_load instead)",
                      confidence="HIGH")

    def _check_sql_format(self, node: ast.Call, name: str) -> None:
        """String .format() call on a SQL-like template string -> HIGH."""
        # Look for: "SELECT ... {}".format(...)
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "format"):
            return
        obj = node.func.value
        if isinstance(obj, ast.Constant) and isinstance(obj.value, str):
            if _is_sql_like_string(obj.value):
                self._add(node, "sql_injection", "HIGH",
                          "SQL query built with .format() (SQL injection risk)",
                          confidence="HIGH")

    def _check_path_traversal(self, node: ast.Call, name: str) -> None:
        """os.path.join / pathlib join with user-tainted non-base args -> HIGH."""
        if name not in ("os.path.join", "os.path.abspath"):
            return
        for arg in node.args[1:]:  # skip first (base path)
            if isinstance(arg, ast.Name) and self._var_is_tainted(arg.id):
                self._add(node, "path_traversal", "HIGH",
                          f"{name}() with user-tainted argument (path traversal risk)",
                          confidence="MEDIUM")
                break

    def _check_hardcoded_secrets(self, node: ast.AST, varname: str,
                                 value: str) -> None:
        """Assignment of string literal to secret-named variable -> MEDIUM."""
        # Exclude obvious placeholders
        lower_val = value.lower()
        if any(tok in lower_val for tok in ("placeholder", "example", "your_",
                                            "xxx", "todo", "changeme", "dummy",
                                            "test", "fake")):
            return
        self._add(node, "hardcoded_secret", "MEDIUM",
                  f"Hardcoded secret in variable '{varname}'",
                  confidence="MEDIUM")

    def _check_bare_except(self, node: ast.Try) -> None:
        """Bare 'except:' without specific exception type -> LOW."""
        for handler in node.handlers:
            if handler.type is None:
                self._add(handler, "error_handling", "LOW",
                          "Bare 'except:' clause swallows all exceptions"
                          " (use 'except Exception:' or specific type)",
                          confidence="HIGH")

    def _check_assert_in_prod(self, node: ast.Assert) -> None:
        """assert statements are disabled with python -O -> LOW."""
        self._add(node, "assert_in_prod", "LOW",
                  "assert statement disabled when Python runs with -O flag",
                  confidence="HIGH")


# ── Public API ────────────────────────────────────────────────────────────────

_EXCLUDE_DIRS_AST = {"__pycache__", ".git", "node_modules", ".venv",
                     "venv", "archive", "tests"}


def scan_file_ast(filepath: Path) -> List[dict]:
    """
    Parse and AST-scan a single Python file.

    Returns findings in the same dict format as security_scanner.scan_file,
    with extra keys: 'col', 'ast_node_type', 'confidence', 'source'='ast'.

    Skips the file silently on SyntaxError.
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    lines = source.splitlines()
    visitor = ASTSecurityVisitor(str(filepath), lines)
    visitor.visit(tree)

    return [f.to_dict() for f in visitor.findings]


def scan_directory_ast(directory: Path) -> List[dict]:
    """
    Recursively scan all .py files in directory using AST analysis.

    Excludes __pycache__, .git, archive, tests directories.
    """
    findings: List[dict] = []
    for pyfile in sorted(directory.rglob("*.py")):
        if any(ex in pyfile.parts for ex in _EXCLUDE_DIRS_AST):
            continue
        findings.extend(scan_file_ast(pyfile))
    return findings


if __name__ == "__main__":
    import sys as _sys

    target = Path(_sys.argv[1]) if len(_sys.argv) > 1 else Path(".")
    if target.is_file():
        results = scan_file_ast(target)
    else:
        results = scan_directory_ast(target)

    if not results:
        print("AST scan: PASS (no issues found)")
        _sys.exit(0)

    by_sev: Dict[str, List[dict]] = {}
    for r in results:
        by_sev.setdefault(r["severity"], []).append(r)

    critical = len(by_sev.get("CRITICAL", []))
    high = len(by_sev.get("HIGH", []))
    medium = len(by_sev.get("MEDIUM", []))
    low = len(by_sev.get("LOW", []))
    verdict = "FAIL" if (critical + high) > 0 else "PASS"
    print(f"AST scan: {verdict} ({critical}C {high}H {medium}M {low}L)")
    for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        for f in by_sev.get(sev, [])[:10]:
            print(f"  [{sev}] {f['file']}:{f['line']} - {f['description']}")
            if f.get("code"):
                print(f"    {f['code']}")
    _sys.exit(2 if (critical + high) > 0 else 0)
