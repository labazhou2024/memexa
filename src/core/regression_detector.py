"""
Multi-dimensional Regression Detector

4 dimensions of regression detection:
1. Test regression: existing Q4 logic (test pass->fail)
2. Performance regression: tracks test duration and flags slowdowns > 20%
3. API contract regression: tracks public function signatures and flags changes
4. Type safety regression: runs pyright/mypy if available, tracks error count

Each dimension produces findings with severity levels (HIGH/MEDIUM/LOW).
"""

import ast
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dimension 2: Performance Tracking
# ---------------------------------------------------------------------------

def run_pytest_with_timing(project_root: Path, timeout: int = 180) -> Dict[str, float]:
    """Run pytest with --durations=0 to collect per-test timings.

    Returns dict mapping test node id to duration in seconds.
    Returns empty dict on failure.
    """
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", "tests/",
                "--durations=0", "-q", "--tb=no", "--no-header",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=timeout,
        )
        return _parse_pytest_durations(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning("run_pytest_with_timing: timed out after %ds", timeout)
        return {}
    except Exception as exc:
        logger.debug("run_pytest_with_timing failed: %s", exc)
        return {}


def _parse_pytest_durations(output: str) -> Dict[str, float]:
    """Parse --durations=0 output into {test_id: seconds} mapping.

    pytest --durations=0 emits lines like:
        0.42s call     tests/test_foo.py::TestBar::test_baz
        0.01s setup    tests/test_foo.py::TestBar::test_baz
    We capture 'call' lines only (actual test body duration).
    """
    timings: Dict[str, float] = {}
    # Match lines produced by pytest durations report
    pattern = re.compile(r"^\s*([\d.]+)s\s+call\s+(\S+)", re.MULTILINE)
    for match in pattern.finditer(output):
        duration = float(match.group(1))
        test_id = match.group(2)
        timings[test_id] = duration
    return timings


def detect_performance_regression(
    baseline: Dict[str, float],
    current: Dict[str, float],
    threshold: float = 0.2,
) -> List[Dict[str, Any]]:
    """Compare per-test durations; flag tests that slowed by more than threshold.

    Args:
        baseline: {test_id: seconds} from Q1 baseline run.
        current: {test_id: seconds} from Q4 current run.
        threshold: fractional slowdown threshold (0.2 = 20%).

    Returns:
        List of finding dicts with category="performance_regression".
    """
    findings: List[Dict[str, Any]] = []

    for test_id, base_dur in baseline.items():
        curr_dur = current.get(test_id)
        if curr_dur is None:
            continue  # test may have been removed or not run
        if base_dur <= 0:
            continue  # avoid division by zero

        slowdown = (curr_dur - base_dur) / base_dur
        if slowdown > threshold:
            findings.append({
                "category": "performance_regression",
                "severity": "MEDIUM",
                "test": test_id,
                "baseline_seconds": round(base_dur, 4),
                "current_seconds": round(curr_dur, 4),
                "slowdown_pct": round(slowdown * 100, 1),
                "message": (
                    f"{test_id} slowed by {slowdown * 100:.1f}% "
                    f"({base_dur:.3f}s -> {curr_dur:.3f}s)"
                ),
            })

    return findings


# ---------------------------------------------------------------------------
# Dimension 3: API Contract Tracking
# ---------------------------------------------------------------------------

def extract_public_api(project_root: Path, package: str = "memex") -> Dict[str, Any]:
    """Extract all public function/method signatures from .py files.

    Traverses project_root/<package>/**/*.py, parses with ast, and collects:
    - module-level functions (no underscore prefix)
    - class methods (no underscore prefix, keyed as module.ClassName.method)

    Returns:
        {
            "module.function": {
                "params": [{"name": str, "annotation": str}, ...],
                "returns": str,
                "lineno": int,
            },
            ...
        }
    """
    api: Dict[str, Any] = {}
    package_dir = project_root / package

    if not package_dir.is_dir():
        logger.debug("extract_public_api: package dir not found: %s", package_dir)
        return api

    for py_file in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in str(py_file):
            continue
        module_key = _file_to_module_key(py_file, project_root)
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (OSError, SyntaxError) as exc:
            logger.debug("extract_public_api: skipping %s: %s", py_file, exc)
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                # Module-level or nested — only capture top-level here
                # We'll handle class methods separately below
                pass

        # Walk top-level and class nodes explicitly
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                key = f"{module_key}.{node.name}"
                api[key] = _extract_func_signature(node)

            elif isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
                key = f"{module_key}.{node.name}"
                sig = _extract_func_signature(node)
                sig["async"] = True
                api[key] = sig

            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                for child in ast.iter_child_nodes(node):
                    if (
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and not child.name.startswith("_")
                    ):
                        key = f"{module_key}.{node.name}.{child.name}"
                        sig = _extract_func_signature(child)
                        if isinstance(child, ast.AsyncFunctionDef):
                            sig["async"] = True
                        api[key] = sig

    return api


def _file_to_module_key(py_file: Path, project_root: Path) -> str:
    """Convert a file path to a dotted module key relative to project_root."""
    try:
        rel = py_file.relative_to(project_root)
        parts = list(rel.with_suffix("").parts)
        return ".".join(parts)
    except ValueError:
        return py_file.stem


def _extract_func_signature(node: ast.FunctionDef) -> Dict[str, Any]:
    """Extract params and return annotation from an ast FunctionDef node."""
    params: List[Dict[str, str]] = []

    # args: positional, *args, keyword-only, **kwargs
    all_args = list(node.args.args)
    defaults_offset = len(all_args) - len(node.args.defaults)

    for i, arg in enumerate(all_args):
        annotation = _annotation_to_str(arg.annotation)
        has_default = i >= defaults_offset
        params.append({
            "name": arg.arg,
            "annotation": annotation,
            "has_default": has_default,
        })

    if node.args.vararg:
        params.append({
            "name": f"*{node.args.vararg.arg}",
            "annotation": _annotation_to_str(node.args.vararg.annotation),
            "has_default": False,
        })

    for kwarg in node.args.kwonlyargs:
        params.append({
            "name": kwarg.arg,
            "annotation": _annotation_to_str(kwarg.annotation),
            "has_default": True,
        })

    if node.args.kwarg:
        params.append({
            "name": f"**{node.args.kwarg.arg}",
            "annotation": _annotation_to_str(node.args.kwarg.annotation),
            "has_default": False,
        })

    returns = _annotation_to_str(node.returns)
    return {
        "params": params,
        "returns": returns,
        "lineno": node.lineno,
    }


def _annotation_to_str(annotation: Optional[ast.expr]) -> str:
    """Convert an AST annotation node to a string representation."""
    if annotation is None:
        return ""
    try:
        return ast.unparse(annotation)
    except Exception:
        return ""


def detect_api_regression(
    baseline_api: Dict[str, Any],
    current_api: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Compare two API snapshots and produce regression findings.

    Detects:
    - Removed public functions (HIGH)
    - Changed parameter count (MEDIUM)
    - Changed parameter names (MEDIUM)
    - Changed return type annotation (LOW)

    Returns:
        List of finding dicts with category="api_regression".
    """
    findings: List[Dict[str, Any]] = []

    for func_key, base_sig in baseline_api.items():
        curr_sig = current_api.get(func_key)

        if curr_sig is None:
            findings.append({
                "category": "api_regression",
                "severity": "HIGH",
                "function": func_key,
                "change": "removed",
                "message": f"Public function removed: {func_key}",
            })
            continue

        base_params = base_sig.get("params", [])
        curr_params = curr_sig.get("params", [])

        # Check parameter count change
        if len(base_params) != len(curr_params):
            findings.append({
                "category": "api_regression",
                "severity": "MEDIUM",
                "function": func_key,
                "change": "param_count_changed",
                "baseline_count": len(base_params),
                "current_count": len(curr_params),
                "message": (
                    f"{func_key}: parameter count changed "
                    f"({len(base_params)} -> {len(curr_params)})"
                ),
            })
        else:
            # Check parameter names (positional order matters)
            base_names = [p["name"] for p in base_params]
            curr_names = [p["name"] for p in curr_params]
            if base_names != curr_names:
                findings.append({
                    "category": "api_regression",
                    "severity": "MEDIUM",
                    "function": func_key,
                    "change": "param_names_changed",
                    "baseline_params": base_names,
                    "current_params": curr_names,
                    "message": (
                        f"{func_key}: parameter names changed "
                        f"({base_names} -> {curr_names})"
                    ),
                })

        # Check return annotation change
        base_returns = base_sig.get("returns", "")
        curr_returns = curr_sig.get("returns", "")
        if base_returns != curr_returns:
            findings.append({
                "category": "api_regression",
                "severity": "LOW",
                "function": func_key,
                "change": "return_annotation_changed",
                "baseline_returns": base_returns,
                "current_returns": curr_returns,
                "message": (
                    f"{func_key}: return annotation changed "
                    f"('{base_returns}' -> '{curr_returns}')"
                ),
            })

    return findings


# ---------------------------------------------------------------------------
# Dimension 4: Type Safety Tracking
# ---------------------------------------------------------------------------

def run_type_check(project_root: Path, timeout: int = 60) -> Dict[str, Any]:
    """Run pyright or mypy on the project; return error/warning counts.

    Tries pyright first, falls back to mypy, falls back to skipping.
    Never crashes even if neither tool is installed.

    Returns:
        {
            "tool": "pyright" | "mypy" | "none",
            "error_count": int,
            "warning_count": int,
            "details": [str, ...],
            "skipped": bool,
        }
    """
    # Try pyright
    pyright_result = _try_pyright(project_root, timeout)
    if pyright_result is not None:
        return pyright_result

    # Try mypy
    mypy_result = _try_mypy(project_root, timeout)
    if mypy_result is not None:
        return mypy_result

    logger.debug("run_type_check: neither pyright nor mypy available, skipping")
    return {
        "tool": "none",
        "error_count": 0,
        "warning_count": 0,
        "details": [],
        "skipped": True,
    }


def _try_pyright(project_root: Path, timeout: int) -> Optional[Dict[str, Any]]:
    """Attempt to run pyright. Returns result dict or None if unavailable."""
    try:
        result = subprocess.run(
            ["pyright", "--outputjson"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=timeout,
        )
        output = result.stdout.strip()
        if not output:
            return None

        data = json.loads(output)
        summary = data.get("summary", {})
        errors = summary.get("errorCount", 0)
        warnings = summary.get("warningCount", 0)

        # Collect a few sample messages
        details: List[str] = []
        for diag in data.get("generalDiagnostics", [])[:10]:
            msg = diag.get("message", "")
            rule = diag.get("rule", "")
            severity = diag.get("severity", "")
            details.append(f"[{severity}] {rule}: {msg}")

        return {
            "tool": "pyright",
            "error_count": errors,
            "warning_count": warnings,
            "details": details,
            "skipped": False,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception as exc:
        logger.debug("_try_pyright failed: %s", exc)
        return None


def _try_mypy(project_root: Path, timeout: int) -> Optional[Dict[str, Any]]:
    """Attempt to run mypy. Returns result dict or None if unavailable."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "memex/", "--ignore-missing-imports", "--no-error-summary"],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=timeout,
        )
        lines = result.stdout.splitlines()
        error_count = 0
        warning_count = 0
        details: List[str] = []

        for line in lines:
            if ": error:" in line:
                error_count += 1
                if len(details) < 10:
                    details.append(line.strip())
            elif ": warning:" in line or ": note:" in line:
                warning_count += 1

        # mypy returns exit code 1 even for type errors, but 127/2 means not found
        if result.returncode in (126, 127):
            return None

        return {
            "tool": "mypy",
            "error_count": error_count,
            "warning_count": warning_count,
            "details": details,
            "skipped": False,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    except Exception as exc:
        logger.debug("_try_mypy failed: %s", exc)
        return None


def detect_type_regression(
    baseline: Dict[str, Any],
    current: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Flag regression if type error count increased.

    Returns:
        List of finding dicts with category="type_regression".
    """
    findings: List[Dict[str, Any]] = []

    # If either side skipped/used different tool, skip comparison
    if baseline.get("skipped") or current.get("skipped"):
        return findings
    if baseline.get("tool") != current.get("tool"):
        return findings

    base_errors = baseline.get("error_count", 0)
    curr_errors = current.get("error_count", 0)

    if curr_errors > base_errors:
        increase = curr_errors - base_errors
        findings.append({
            "category": "type_regression",
            "severity": "MEDIUM",
            "tool": current.get("tool", "unknown"),
            "baseline_errors": base_errors,
            "current_errors": curr_errors,
            "increase": increase,
            "message": (
                f"Type errors increased by {increase} "
                f"({base_errors} -> {curr_errors}) via {current.get('tool', 'unknown')}"
            ),
        })

    return findings


# ---------------------------------------------------------------------------
# Unified Regression Check
# ---------------------------------------------------------------------------

def run_full_regression(
    project_root: Path,
    baseline_dir: Path,
    perf_threshold: float = 0.2,
) -> Dict[str, Any]:
    """Run all 4 regression dimensions and return combined results.

    Loads previously saved baselines from baseline_dir/.
    Saves current measurements as updated baselines (useful for next run).

    Args:
        project_root: Root of the project (where pytest is run).
        baseline_dir: Directory storing baseline JSON files.
        perf_threshold: Fractional slowdown threshold for performance regression.

    Returns:
        {
            "test_regressions": int,
            "performance_regressions": int,
            "api_regressions": int,
            "type_regressions": int,
            "total_regressions": int,
            "findings": [finding_dict, ...],
            "dimensions": {
                "performance": {...},
                "api": {...},
                "type": {...},
            },
            "passed": bool,
        }
    """
    baseline_dir = Path(baseline_dir)
    baseline_dir.mkdir(parents=True, exist_ok=True)

    all_findings: List[Dict[str, Any]] = []
    dimensions: Dict[str, Any] = {}

    # --- Dimension 2: Performance ---
    perf_baseline_file = baseline_dir / "performance_baseline.json"
    perf_baseline: Dict[str, float] = {}
    if perf_baseline_file.exists():
        try:
            perf_baseline = json.loads(perf_baseline_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to load performance baseline: %s", exc)

    current_timings = run_pytest_with_timing(project_root)

    perf_findings: List[Dict[str, Any]] = []
    if perf_baseline:
        perf_findings = detect_performance_regression(
            perf_baseline, current_timings, threshold=perf_threshold
        )
        all_findings.extend(perf_findings)

    # Save current timings as new performance baseline
    if current_timings:
        try:
            perf_baseline_file.write_text(
                json.dumps(current_timings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.debug("Failed to save performance baseline: %s", exc)

    dimensions["performance"] = {
        "regressions": len(perf_findings),
        "tests_timed": len(current_timings),
        "findings": perf_findings,
    }

    # --- Dimension 3: API contract ---
    api_baseline_file = baseline_dir / "api_baseline.json"
    api_baseline: Dict[str, Any] = {}
    if api_baseline_file.exists():
        try:
            api_baseline = json.loads(api_baseline_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to load API baseline: %s", exc)

    current_api = extract_public_api(project_root)

    api_findings: List[Dict[str, Any]] = []
    if api_baseline:
        api_findings = detect_api_regression(api_baseline, current_api)
        all_findings.extend(api_findings)

    # Save current API as new baseline
    try:
        api_baseline_file.write_text(
            json.dumps(current_api, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.debug("Failed to save API baseline: %s", exc)

    dimensions["api"] = {
        "regressions": len(api_findings),
        "functions_tracked": len(current_api),
        "findings": api_findings,
    }

    # --- Dimension 4: Type safety ---
    type_baseline_file = baseline_dir / "type_baseline.json"
    type_baseline: Dict[str, Any] = {}
    if type_baseline_file.exists():
        try:
            type_baseline = json.loads(type_baseline_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Failed to load type baseline: %s", exc)

    current_type = run_type_check(project_root)

    type_findings: List[Dict[str, Any]] = []
    if type_baseline:
        type_findings = detect_type_regression(type_baseline, current_type)
        all_findings.extend(type_findings)

    # Save current type result as new baseline
    try:
        type_baseline_file.write_text(
            json.dumps(current_type, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.debug("Failed to save type baseline: %s", exc)

    dimensions["type"] = {
        "regressions": len(type_findings),
        "tool": current_type.get("tool", "none"),
        "error_count": current_type.get("error_count", 0),
        "findings": type_findings,
    }

    total = len(all_findings)
    return {
        "test_regressions": 0,  # filled in by big_loop from test comparison
        "performance_regressions": len(perf_findings),
        "api_regressions": len(api_findings),
        "type_regressions": len(type_findings),
        "total_regressions": total,
        "findings": all_findings,
        "dimensions": dimensions,
        "passed": total == 0,
    }


def save_baseline(project_root: Path, baseline_dir: Path) -> Dict[str, Any]:
    """Create a fresh baseline snapshot for all dimensions.

    Call this at Q1 so Q4 can compare against it.

    Returns:
        {
            "performance_tests": int,
            "api_functions": int,
            "type_tool": str,
            "type_errors": int,
            "baseline_dir": str,
        }
    """
    baseline_dir = Path(baseline_dir)
    baseline_dir.mkdir(parents=True, exist_ok=True)

    # Performance baseline
    timings = run_pytest_with_timing(project_root)
    perf_file = baseline_dir / "performance_baseline.json"
    try:
        perf_file.write_text(
            json.dumps(timings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.debug("save_baseline: failed to write performance baseline: %s", exc)

    # API baseline
    api = extract_public_api(project_root)
    api_file = baseline_dir / "api_baseline.json"
    try:
        api_file.write_text(
            json.dumps(api, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.debug("save_baseline: failed to write API baseline: %s", exc)

    # Type baseline
    type_result = run_type_check(project_root)
    type_file = baseline_dir / "type_baseline.json"
    try:
        type_file.write_text(
            json.dumps(type_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.debug("save_baseline: failed to write type baseline: %s", exc)

    return {
        "performance_tests": len(timings),
        "api_functions": len(api),
        "type_tool": type_result.get("tool", "none"),
        "type_errors": type_result.get("error_count", 0),
        "baseline_dir": str(baseline_dir),
    }
