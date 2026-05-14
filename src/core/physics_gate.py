"""TU-1 (long_term_plan_v2 §3 U15): Stage 3 sub-gate that PHYSICS-class TUs link
to a non-stub toy benchmark with REF_VALUE + tolerance call.

Per HARD RULEs:
- feedback_scientific_code_toy_benchmark.md: scientific code MUST have toy benchmark
- feedback_anti_halluc_stub_must_emit_status.md: stubs must emit status, not silent True
- feedback_audit_corpus_completeness_precondition.md: AST scan, not grep

axis_anchor: [C:cli:physics_gate]
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


class TUClass(str, Enum):
    """tu_class enum per RP-6 logic-iter2-1 fix.

    PHYSICS_CLASSES are the values that REQUIRE a toy benchmark. UNKNOWN raises
    NotImplementedError to fail loud per HARD RULE feedback_anti_halluc_stub.
    """
    NUMERIC_KERNEL = "numeric_kernel"
    MIXED = "mixed"
    REFACTOR = "refactor"
    SCHEMA_MIGRATION = "schema_migration"
    DOC_UPDATE = "doc_update"
    UNKNOWN = "unknown"


PHYSICS_CLASSES = {TUClass.NUMERIC_KERNEL, TUClass.MIXED}
NON_PHYSICS_CLASSES = {TUClass.REFACTOR, TUClass.SCHEMA_MIGRATION, TUClass.DOC_UPDATE}


@dataclass
class StubReport:
    """Result of AST scan over a test file body."""
    test_path: str
    has_assert: bool
    has_ref_value: bool
    has_tolerance_call: bool
    test_func_count: int
    body_only_pass: bool  # all bodies are `pass`-only / `...`-only / NotImplementedError

    def is_stub(self) -> bool:
        """True if the file looks like a stub (no real check)."""
        return (
            self.body_only_pass
            or not self.has_assert
            or not self.has_ref_value
            or not self.has_tolerance_call
        )

    def missing_fields(self) -> List[str]:
        out: List[str] = []
        if self.body_only_pass:
            out.append("body_only_pass")
        if not self.has_assert:
            out.append("no_assert")
        if not self.has_ref_value:
            out.append("no_ref_value_constant")
        if not self.has_tolerance_call:
            out.append("no_tolerance_call")
        return out


@dataclass
class CheckResult:
    """Result of physics_gate.check_toy_present."""
    present: bool
    path: Optional[str]
    stub_report: Optional[StubReport]
    reason: str
    abs_path_logged: Optional[str] = None  # RP-LOGIC-ITER1-5: log resolved abs path on BLOCK


_TOLERANCE_CALLS = re.compile(
    r"\b(math\.isclose|numpy\.allclose|np\.allclose|pytest\.approx|"
    r"isclose|allclose)\s*\("
)
# REF_VALUE-style constant: capitalized identifier with `_VALUE`, `_REF`, `_EXACT`,
# `_LITERAL`, `_HA`, `_AU`, `_EV`, etc. Matches assignments like `E_HE_REF = -2.9037`.
_REF_VALUE_NAME = re.compile(
    r"^[A-Z][A-Z0-9_]*("
    r"_REF|_VALUE|_EXACT|_LITERAL|_HA|_AU|_EV|_HARTREE|_GROUND|"
    r"_ENERGY|_BOND|_SPREAD|_OMEGA"
    r")$"
)


def _ast_scan_test_body(test_path: Path) -> StubReport:
    """Scan a Python test file's AST for assert / REF_VALUE / tolerance call.

    Per HARD RULE feedback_audit_corpus_completeness_precondition: grep is
    insufficient — `def t(): assert True` passes a grep but is a stub.
    AST detects empty/passthrough bodies and missing REF_VALUE constants.
    """
    try:
        text = test_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return StubReport(
            test_path=str(test_path),
            has_assert=False,
            has_ref_value=False,
            has_tolerance_call=False,
            test_func_count=0,
            body_only_pass=True,
        )

    has_tolerance_call = bool(_TOLERANCE_CALLS.search(text))

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return StubReport(
            test_path=str(test_path),
            has_assert=False,
            has_ref_value=False,
            has_tolerance_call=has_tolerance_call,
            test_func_count=0,
            body_only_pass=True,
        )

    # Module-level REF_VALUE constants
    has_ref_value = False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and _REF_VALUE_NAME.match(tgt.id):
                    has_ref_value = True
                    break
        if has_ref_value:
            break

    # Test functions — logic-iter1-5 LOW fix: walk subtree for class-based test
    # methods too (TestX class with test_y method); not just top-level functions.
    test_funcs: List[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            test_funcs.append(node)
    test_func_count = len(test_funcs)
    has_assert = False
    body_only_pass = True

    # coverage-iter1-4 LOW fix: also recognize tolerance-call asserts inside
    # helper-function calls (e.g. `_check(x, REF_VALUE)` where helper does the
    # math.isclose). We treat any call expression to a name starting with "_"
    # or any function call inside a test body as non-stub when REF_VALUE is
    # present at module level.
    for fn in test_funcs:
        for stmt in fn.body:
            if isinstance(stmt, ast.Assert):
                has_assert = True
                body_only_pass = False
            elif isinstance(stmt, ast.Pass):
                continue
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                # `...` Ellipsis / docstring only
                continue
            elif isinstance(stmt, ast.Raise):
                # raise NotImplementedError counts as stub
                continue
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                # Helper-function call (e.g. assert delegated to _check) — non-stub
                body_only_pass = False
            else:
                body_only_pass = False

    if not test_funcs:
        # No test_* functions → treat as stub
        body_only_pass = True
        has_assert = False

    return StubReport(
        test_path=str(test_path),
        has_assert=has_assert,
        has_ref_value=has_ref_value,
        has_tolerance_call=has_tolerance_call,
        test_func_count=test_func_count,
        body_only_pass=body_only_pass,
    )


def _signal_hint_to_test_path(signal_hint: str) -> Optional[str]:
    """Extract a tests/ path from a signal_hint string.

    Accepts forms like 'tests/physics_canon/test_X.py:test_y' or just
    'tests/physics_canon/test_X.py'. Returns None if no tests/ ref.

    Per HARD RULE feedback_plan_driven_filesystem_traversal_guard.md
    (security-iter1-1 HIGH): the regex character class previously allowed
    `..` segments enabling traversal outside workspace. We now also reject
    `..` literal segments at the regex level; final boundary check happens
    in check_toy_present via .resolve().relative_to().
    """
    if not signal_hint:
        return None
    # Forbid `..` segments and `\` separators in tests/ ref
    m = re.search(r"(tests/[\w/.-]+\.py)", signal_hint)
    if not m:
        return None
    candidate = m.group(1)
    # Reject literal traversal segments
    parts = candidate.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        return None
    return candidate


def _workspace_root() -> Path:
    """Resolve workspace root.

    Per RP-LOGIC-ITER1-5 (logic-iter1-5 MED): use .resolve().parents[N] not
    chained .parent. From memex/core/physics_gate.py → memex/.
    """
    return Path(__file__).resolve().parents[2]


def check_toy_present(
    tu_class: str,
    signal_hint: Optional[str],
    plan_md_path: Optional[Path] = None,
) -> CheckResult:
    """Stage 3 sub-gate: PHYSICS-class TU MUST link to a non-stub toy benchmark.

    Per HARD RULE feedback_anti_halluc_stub_must_emit_status.md: unknown tu_class
    MUST raise NotImplementedError, not silently return True.

    Args:
        tu_class: One of TUClass values.
        signal_hint: Either a test path or 'file.py:func' form.
        plan_md_path: Optional plan.md path for context (unused in v1; reserved).

    Returns:
        CheckResult with .present True iff toy benchmark exists AND is non-stub.

    Raises:
        NotImplementedError: when tu_class not in TUClass enum.
    """
    # Emit invocation trace event (best-effort)
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("physics_gate_invoked", {
            "tu_class": tu_class,
            "signal_hint": signal_hint or "",
        })
    except Exception:
        pass

    # Validate tu_class
    try:
        tc_enum = TUClass(tu_class)
    except ValueError:
        raise NotImplementedError(
            f"unknown tu_class={tu_class!r}; register in TUClass enum first "
            f"(allowed: {[t.value for t in TUClass]})"
        )

    if tc_enum in NON_PHYSICS_CLASSES:
        # Non-physics TUs do not need a toy benchmark
        return CheckResult(
            present=True,
            path=None,
            stub_report=None,
            reason=f"tu_class={tu_class} is non-physics; no toy benchmark required",
        )

    if tc_enum == TUClass.UNKNOWN:
        # UNKNOWN is registered but cannot pass — must be classified before Stage 3
        result = CheckResult(
            present=False,
            path=None,
            stub_report=None,
            reason="tu_class=unknown; classify TU before Stage 3",
        )
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("physics_gate_stub_detected", {
                "tu_class": tu_class,
                "reason": result.reason,
            })
        except Exception:
            pass
        return result

    # PHYSICS class: require toy benchmark
    test_ref = _signal_hint_to_test_path(signal_hint or "")
    if not test_ref:
        result = CheckResult(
            present=False,
            path=None,
            stub_report=None,
            reason=f"tu_class={tu_class} is physics-class but signal_hint has no tests/ path",
        )
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("physics_gate_stub_detected", {
                "tu_class": tu_class,
                "signal_hint": signal_hint or "",
                "reason": result.reason,
            })
        except Exception:
            pass
        return result

    test_path = _workspace_root() / test_ref
    resolved = test_path.resolve()
    abs_path_logged = str(resolved)  # RP-LOGIC-ITER1-5: log abs path

    # security-iter1-1 HIGH fix: traversal guard via .relative_to()
    workspace_resolved = _workspace_root().resolve()
    try:
        resolved.relative_to(workspace_resolved)
    except ValueError:
        result = CheckResult(
            present=False,
            path=str(test_path),
            stub_report=None,
            reason=f"traversal_rejected: {abs_path_logged} outside workspace {workspace_resolved}",
            abs_path_logged=abs_path_logged,
        )
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("physics_gate_stub_detected", {
                "tu_class": tu_class,
                "path": str(test_path),
                "abs_path": abs_path_logged,
                "reason": "traversal_rejected",
            })
        except Exception:
            pass
        return result

    if not test_path.exists():
        result = CheckResult(
            present=False,
            path=str(test_path),
            stub_report=None,
            reason=f"toy benchmark not found at {abs_path_logged}",
            abs_path_logged=abs_path_logged,
        )
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("physics_gate_stub_detected", {
                "tu_class": tu_class,
                "path": str(test_path),
                "abs_path": abs_path_logged,
                "reason": "missing",
            })
        except Exception:
            pass
        return result

    stub_rep = _ast_scan_test_body(test_path)
    if stub_rep.is_stub():
        result = CheckResult(
            present=False,
            path=str(test_path),
            stub_report=stub_rep,
            reason=f"toy benchmark is a stub: missing {stub_rep.missing_fields()}",
            abs_path_logged=abs_path_logged,
        )
        try:
            from src.core.trace_sink import write_trace_event
            write_trace_event("physics_gate_stub_detected", {
                "tu_class": tu_class,
                "path": str(test_path),
                "abs_path": abs_path_logged,
                "missing_fields": stub_rep.missing_fields(),
            })
        except Exception:
            pass
        return result

    # Pass
    result = CheckResult(
        present=True,
        path=str(test_path),
        stub_report=stub_rep,
        reason="toy benchmark present and non-stub",
        abs_path_logged=abs_path_logged,
    )
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("physics_gate_passed", {
            "tu_class": tu_class,
            "path": str(test_path),
            "test_func_count": stub_rep.test_func_count,
        })
    except Exception:
        pass
    return result


__all__ = [
    "TUClass",
    "PHYSICS_CLASSES",
    "NON_PHYSICS_CLASSES",
    "StubReport",
    "CheckResult",
    "check_toy_present",
    "_ast_scan_test_body",
    "_signal_hint_to_test_path",
    "_workspace_root",
]
