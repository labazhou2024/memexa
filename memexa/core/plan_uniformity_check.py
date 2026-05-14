"""TU-3 (U1, plan_v1, 2026-04-26) — plan_uniformity_check.

Lints autopilot plan_v<N>.md against SKILL.md §1.3 mandatory structure.

12 rule checks (composition over reinvention; not reverse-audit which is U8):
  R1: All 12 mandatory sections present
  R2: Each Stage section >=200 chars
  R3: max/min Stage section ratio <=3:1
  R4: Each Stage section has >=2 verify_cmd
  R5: Each Stage section has >=1 trace event named
  R6: Every AC has verify_cmd + signal_hint
  R7: Every TU has axis_anchor
  R8: Every TU has root_cause_when_violated in {plan_gap, code_bug, test_gap}
  R9: Plan provenance section exists
  R10: UTF-8 readable
  R11: Non-empty file
  R12: Markdown heading hierarchy intact (no skipped levels in descent)

Library API:
  check(plan_path: Path, *, strict: bool = True) -> LintResult
  check_by_task_id(task_id: str, *, strict: bool = True) -> LintResult

CLI:
  python -m memexa.core.plan_uniformity_check <task_id>      # via path resolver
  python -m memexa.core.plan_uniformity_check --path <plan>  # direct path
  python -m memexa.core.plan_uniformity_check --self-test    # fixture demo

Exit codes:
  0  pass
  1  section_missing OR R10/R11/R12 fail
  2  density_fail (R2)
  3  ratio_fail (R3)
  4  ac_schema_fail (R6/R7/R8/R9)
  5  trace_event_missing (R4 or R5)
  64 usage_error
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# S6 fix (security-reviewer iter2): sync comment linking to SKILL.md §1.3
# STAGE_SECTIONS list MUST stay in sync with SKILL.md §1.3 mandatory sections
# 5/6/7/8/9 (the Stage Plan sections; not Architecture / Risks / Out-of-scope etc).
STAGE_SECTIONS = [
    "Stage 3 Plan",
    "Stage 4 Plan",
    "Stage 4.5 Plan",
    "Stage 6 Plan",
    "Stage 5 Plan",
]

# 12 mandatory sections per SKILL.md §1.3
MANDATORY_SECTIONS = [
    "Architecture Decision",
    "Forbidden Approaches",
    "TaskUnits",
    "Acceptance Criteria",
    "Stage 3 Plan",
    "Stage 4 Plan",
    "Stage 4.5 Plan",
    "Stage 6 Plan",
    "Stage 5 Plan",
    "Risks",
    "Out-of-scope",
    "Plan provenance",
]

ALLOWED_ROOT_CAUSES = {"plan_gap", "code_bug", "test_gap"}

# U1 (long_term_plan_v2 TU-1) extensions: R13-R19
PHYSICS_TU_CLASSES = {"numeric_kernel", "mixed"}
STUB_KEYWORDS = {"stub_loose", "stub_strict", "placeholder", "inherited"}
ALLOWED_TU_CLASSES = {"refactor", "schema_migration", "doc_update",
                     "numeric_kernel", "mixed", "unknown"}

# R14 dual-language numerical-voice regex (per logic-iter1 MED fix)
_NUMERICAL_VOICE_RE = re.compile(
    r"(?i)(numerically|computed|we find|verified|simulation gives|"
    r"数值上|计算得|我们发现|验证为|模拟显示)"
    r"\s*\S{0,40}\s*\b\d[\d.eE+\-]*\b"
)

# R19 stub keyword detection (limited to §3 TaskUnits / §13 Inherited Lessons; excludes §11 Out-of-scope per architect BLOCKER-A2)
_STUB_KEYWORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in STUB_KEYWORDS) + r")\b"
)
_WRAPPER_MATRIX_RE = re.compile(
    r"(Wrapper Status Matrix|Status Matrix|raise NotImplementedError|"
    r"emit\s+`\w+_stub`|trace\s+event\s+`\w+_stub`)",
    re.IGNORECASE
)

EXIT_PASS = 0
EXIT_SECTION_MISSING = 1  # also R10/R11/R12
EXIT_DENSITY_FAIL = 2
EXIT_RATIO_FAIL = 3
EXIT_AC_SCHEMA_FAIL = 4  # also R6/R7/R8/R9/R13/R14/R15/R16/R17/R18/R19
EXIT_TRACE_EVENT_MISSING = 5
EXIT_USAGE_ERROR = 64

# S1 fix (security-reviewer iter2): strip ANSI escapes before emitting plan_path to trace
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mK]|[\x00-\x1f\x7f]")


@dataclass
class LintResult:
    ok: bool
    exit_code: int
    failed_rules: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def _emit_invocation_trace(plan_path: Path, strict: bool) -> None:
    """LR-3 fix + S1 fix: emit at invocation, ANSI-stripped path."""
    try:
        from memexa.core.trace_sink import write_trace_event
        clean_path = _ANSI_RE.sub("", str(plan_path))[:500]
        write_trace_event("plan_uniformity_check_invoked", {
            "plan_path": clean_path,
            "strict": strict,
        })
    except Exception:  # fail-soft
        pass


def _read_text_or_none(plan_path: Path) -> Optional[str]:
    """S5 fix (TOCTOU mitigation): single-read; return None on read error."""
    try:
        return plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _section_body(text: str, section_name: str) -> Optional[str]:
    """Extract section body. Looks for `## <heading>` ending with section_name.

    logic-iter1-1 fix: heading must END with section_name (not just contain it
    as substring). E.g. "## Stage 5 Plan Summary" no longer matches "Stage 5 Plan".

    CB-PARSER-2 fix (2026-05-02): depth-aware stop. Body extends until the
    NEXT heading at SAME-OR-SHALLOWER depth, not any heading 1-6. Previously
    `## TaskUnits` followed by `### TU-1` returned empty body because `### `
    halted extraction. Fix preserves child subsections (`### TU-N`, `#### x`)
    inside parent body, which matches Markdown semantic intent.

    Returns None if section not found.
    """
    head_pattern = rf"^(#{{1,6}})\s+{re.escape(section_name)}\s*$"
    m = re.search(head_pattern, text, re.MULTILINE)
    if not m:
        return None
    depth = len(m.group(1))
    start = m.end()
    next_pattern = rf"^#{{1,{depth}}}\s"
    m2 = re.search(next_pattern, text[start:], re.MULTILINE)
    end = start + m2.start() if m2 else len(text)
    body = text[start:end]
    if body.startswith("\n"):
        body = body[1:]
    return body


def _check_r1_sections(text: str) -> List[str]:
    """R1: all 12 mandatory sections present. Returns list of missing."""
    missing = []
    for sec in MANDATORY_SECTIONS:
        if _section_body(text, sec) is None:
            missing.append(sec)
    return missing


def _check_r2_density(text: str) -> Dict[str, int]:
    """R2: each Stage section >=200 chars."""
    failures = {}
    for sec in STAGE_SECTIONS:
        body = _section_body(text, sec)
        if body is None:
            continue
        if len(body) < 200:
            failures[sec] = len(body)
    return failures


def _check_r3_ratio(text: str) -> Optional[Dict[str, Any]]:
    """R3: max/min Stage section ratio <=3:1."""
    sizes = {}
    for sec in STAGE_SECTIONS:
        body = _section_body(text, sec)
        if body:
            sizes[sec] = len(body)
    if len(sizes) < 2:
        return None
    max_v = max(sizes.values())
    min_v = min(sizes.values())
    if min_v == 0:
        return {"ratio": "infinite", "sizes": sizes}
    ratio = max_v / min_v
    if ratio > 3.0:
        return {"ratio": round(ratio, 2), "sizes": sizes}
    return None


def _check_r4_verify_cmd(text: str) -> Dict[str, int]:
    """R4: each Stage section has >=1 verify_cmd.

    CB-PARSER-2 fix (2026-05-02): threshold relaxed from 2 to 1 to match
    autopilot v2.0 §1 template which prescribes ONE canonical verify_cmd
    per Stage (Stage 3/4/4.5/5/6). Previous threshold (>=2) generated
    persistent false-positives on conformant plans, driving 5 BootstrapBypass
    incidents (count=4-7 in harness_state.action_items).
    """
    failures = {}
    for sec in STAGE_SECTIONS:
        body = _section_body(text, sec) or ""
        count = len(re.findall(r"verify_cmd\s*[:`]", body))
        if count < 1:
            failures[sec] = count
    return failures


def _check_r5_trace_event(text: str) -> Dict[str, bool]:
    """R5: each Stage section has >=1 trace event named."""
    failures = {}
    for sec in STAGE_SECTIONS:
        body = _section_body(text, sec) or ""
        has = bool(re.search(
            r"trace\s*event[s]?\s*[:`]|emit_[a-z_]+\(|trace_sink|write_trace_event",
            body, re.IGNORECASE))
        if not has:
            failures[sec] = False
    return failures


def _check_r6_ac_schema(text: str) -> List[str]:
    """R6: every AC has verify_cmd + signal_hint."""
    failures = []
    ac_pattern = re.compile(r"\*\*(AC-[A-Z0-9_-]+)\*\*[\s\S]*?(?=\*\*AC-|\n## |\Z)")
    for m in ac_pattern.finditer(text):
        ac_id = m.group(1)
        block = m.group(0)
        has_vc = bool(re.search(r"verify_cmd", block))
        has_sh = bool(re.search(r"signal_hint", block))
        if not (has_vc and has_sh):
            failures.append(ac_id)
    return failures


def _check_r7_tu_axis_anchor(text: str) -> List[str]:
    """R7: every TU has axis_anchor."""
    failures = []
    tu_pattern = re.compile(r"### TU-(\d+)[\s\S]*?(?=### TU-|\n## |\Z)")
    for m in tu_pattern.finditer(text):
        tu_id = f"TU-{m.group(1)}"
        block = m.group(0)
        if not re.search(r"axis_anchor", block):
            failures.append(tu_id)
    return failures


def _check_r8_tu_root_cause(text: str) -> List[str]:
    """R8: every TU has root_cause_when_violated in enum."""
    failures = []
    tu_pattern = re.compile(r"### TU-(\d+)[\s\S]*?(?=### TU-|\n## |\Z)")
    for m in tu_pattern.finditer(text):
        tu_id = f"TU-{m.group(1)}"
        block = m.group(0)
        rc_m = re.search(r"root_cause_when_violated.*?[`:]\s*([a-z_]+)", block)
        if not rc_m or rc_m.group(1) not in ALLOWED_ROOT_CAUSES:
            failures.append(tu_id)
    return failures


def _check_r9_provenance(text: str) -> bool:
    """R9: Plan provenance section exists."""
    return _section_body(text, "Plan provenance") is not None


def _check_r13_physics_toy_benchmark(text: str) -> List[str]:
    """R13 (U1): PHYSICS-class TU (tu_class=numeric_kernel/mixed) MUST reference toy benchmark.

    Triggered only when TU has tu_class field matching PHYSICS_TU_CLASSES.
    Detects toy ref via: "tests/physics_canon/", "toy_*", "selftest", "reference value", "analytic".
    Returns list of TU IDs that violate.
    """
    failures = []
    tu_pattern = re.compile(r"### TU-(\d+)[\s\S]*?(?=### TU-|\n## |\Z)")
    for m in tu_pattern.finditer(text):
        tu_id = f"TU-{m.group(1)}"
        block = m.group(0)
        # Extract tu_class
        tc_m = re.search(r"\*\*tu_class\*\*:\s*([a-z_]+)", block)
        if not tc_m or tc_m.group(1) not in PHYSICS_TU_CLASSES:
            continue
        # Physics class — require toy benchmark ref
        has_toy = bool(re.search(
            r"tests/physics_canon/|toy_\w+|--selftest|reference value|"
            r"analytic|FCI|CCSD|Hooke|He atom|Hubbard.*FCI",
            block, re.IGNORECASE))
        if not has_toy:
            failures.append(tu_id)
    return failures


def _check_r14_numerical_claim_script(text: str) -> List[str]:
    """R14 (U1): numerical-voice claim + number MUST cite a runnable script.

    Per HARD RULE feedback_numerical_claim_evidence. Limited to §3 TaskUnits
    region (including all ### TU subheading bodies) and §4 Acceptance Criteria
    (per architect BLOCKER-A1 scope limit). Returns list of "claim_excerpt"
    violations.
    """
    failures = []
    scan_regions: List[str] = []

    # §3 TaskUnits region: span from "## TaskUnits" to next ## heading,
    # including all ### TU subheadings (unlike _section_body which stops at any heading).
    m_tu = re.search(
        r"^##\s+TaskUnits\s*$\n([\s\S]*?)(?=^##\s+[^#]|\Z)",
        text, re.MULTILINE,
    )
    if m_tu:
        scan_regions.append(m_tu.group(1))

    # §4 Acceptance Criteria
    body4 = _section_body(text, "Acceptance Criteria")
    if body4:
        scan_regions.append(body4)

    for region in scan_regions:
        for m in _NUMERICAL_VOICE_RE.finditer(region):
            excerpt = region[max(0, m.start() - 20):min(len(region), m.end() + 60)]
            # Check if same paragraph has script ref
            para_start = region.rfind("\n\n", 0, m.start())
            if para_start < 0:
                para_start = 0
            para_end = region.find("\n\n", m.end())
            if para_end == -1:
                para_end = len(region)
            paragraph = region[para_start:para_end]
            has_script = bool(re.search(
                r"script[s]?[/:`]|\.py\b|\.sh\b|verify_cmd|scripts/",
                paragraph))
            if not has_script:
                failures.append(excerpt[:80].replace("\n", " "))
    return failures


def _check_r15_primary_metric_table(text: str) -> bool:
    """R15 (U1): plan §4 Acceptance Criteria MUST declare a primary metric.

    Per HARD RULE feedback_primary_metric_declare_upfront. Looks for
    'Primary metric' or 'PRIMARY' keyword in §4 body.
    Returns True if missing (failure).
    """
    body = _section_body(text, "Acceptance Criteria")
    if body is None:
        return False  # missing §4 caught by R1
    has_primary = bool(re.search(
        r"\*\*Primary metric\*\*|\*\*PRIMARY\*\*|Primary metric:|PRIMARY:",
        body, re.IGNORECASE))
    return not has_primary


def _check_r16_corpus_completeness(text: str) -> List[str]:
    """R16 (U1): plan-referenced tests/ paths MUST not contain TODO/PLACEHOLDER stubs.

    Per HARD RULE feedback_audit_corpus_completeness_precondition. Scans plan
    for tests/ refs (tests/X.py, tests/dir/, etc.) and runs grep on each
    existing path. Returns list of "<path>: N stub_lines".
    """
    failures = []
    test_refs = set(re.findall(
        r"(?:^|[\s`(])(tests/[\w/.-]+\.py)\b", text, re.MULTILINE))
    test_dirs = set(re.findall(
        r"(?:^|[\s`(])(tests/[\w/.-]+/)\b", text, re.MULTILINE))
    workspace_root = Path(__file__).resolve().parents[2]  # memexa/
    workspace_root_resolved = workspace_root.resolve()
    paths_to_check: List[Path] = []

    def _safe_join(ref_str: str) -> Optional[Path]:
        """security-iter1-1 fix: traversal guard. Reject if resolves outside workspace_root."""
        p = (workspace_root / ref_str).resolve()
        try:
            p.relative_to(workspace_root_resolved)
        except ValueError:
            return None  # outside workspace
        return p

    for ref in test_refs:
        p = _safe_join(ref)
        if p is not None and p.exists() and p.is_file():
            paths_to_check.append(p)
    for ref in test_dirs:
        p = _safe_join(ref)
        if p is not None and p.exists() and p.is_dir():
            paths_to_check.extend(p.glob("*.py"))
    for path in paths_to_check[:20]:  # cap to 20 to bound runtime
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Heuristic: stub-only test files have empty function bodies or TODO/NotImplementedError
        stub_count = len(re.findall(r"^\s*(TODO|PLACEHOLDER)\b", content, re.MULTILINE))
        # Not counting bare `pass` since `class X: pass` is legit Python
        if stub_count > 0:
            failures.append(f"{path.name}: {stub_count} TODO/PLACEHOLDER lines")
    return failures


def _check_r17_cross_cluster_integration(text: str) -> Optional[Dict[str, Any]]:
    """R17 (U1): every 5-TU cluster expects ≥1 integration test ref.

    Per BL-8 council blocker. Uses ceil(TU_count / 5) per logic-iter1 HIGH fix.
    TU_count <= 2 expects 0 integration tests (skip rule).
    Returns dict with details if violation.
    """
    import math
    tu_count = len(re.findall(r"### TU-(\d+)", text))
    if tu_count <= 2:
        return None  # too small to require integration tests
    expected = math.ceil(tu_count / 5)
    # Count integration test refs in plan §3 (TaskUnits) or §8 (Stage 6)
    body = _section_body(text, "TaskUnits") or ""
    body6 = _section_body(text, "Stage 6 Plan") or ""
    combined = body + "\n" + body6
    actual = len(re.findall(
        r"integration[\s_-]+test|integration_matrix|cross[\s_-]+TU\s+integration",
        combined, re.IGNORECASE))
    if actual < expected:
        return {"tu_count": tu_count, "expected": expected, "actual": actual}
    return None


def _check_r17_integration_field_present(text: str) -> List[str]:
    """R17 (U17): every TU MUST have **integration_matrix** field.

    Returns list of TU IDs that lack the field. Backward compat:
    TUs with `enforces_via:not_yet` are exempt (mirrors R18 logic).
    Small plans (TU_count <= 2) skip the check (mirrors R17 cluster rule).
    """
    failures: List[str] = []
    tu_pattern = re.compile(r"### TU-(\d+)[\s\S]*?(?=### TU-|\n## |\Z)")
    blocks = list(tu_pattern.finditer(text))
    if len(blocks) <= 2:
        return failures
    for m in blocks:
        tu_id = f"TU-{m.group(1)}"
        block = m.group(0)
        if re.search(r"enforces_via\s*:\s*not_yet", block):
            continue
        if not re.search(r"\*\*integration_matrix\*\*\s*:", block):
            failures.append(tu_id)
    return failures


def _check_r18_tu_class_required(text: str) -> List[str]:
    """R18 (U1, per RP-6 logic-iter2-1 fix): every TU header MUST contain tu_class.

    Detection: each `### TU-N` block must have `**tu_class**: <value>` in
    the 6-field metadata line. Backward compat: legacy plans with
    `enforces_via:not_yet` element are exempted.
    """
    failures = []
    tu_pattern = re.compile(r"### TU-(\d+)[\s\S]*?(?=### TU-|\n## |\Z)")
    for m in tu_pattern.finditer(text):
        tu_id = f"TU-{m.group(1)}"
        block = m.group(0)
        # Backward compat exemption
        if re.search(r"enforces_via:\s*not_yet", block):
            continue
        tc_m = re.search(r"\*\*tu_class\*\*:\s*([a-z_]+)", block)
        if not tc_m:
            failures.append(f"{tu_id} (missing tu_class field)")
            continue
        if tc_m.group(1) not in ALLOWED_TU_CLASSES:
            failures.append(f"{tu_id} (invalid tu_class: {tc_m.group(1)})")
    return failures


def _check_r19_stub_status_matrix(text: str) -> List[str]:
    """R19 (U1, per RP-1 security-iter1-1 fix + logic-iter1 fix): §3/§13 with stub keyword MUST have status matrix.

    Limited to §TaskUnits (full span including ### TU subheadings) and
    §Inherited Lessons; explicitly excludes §Out-of-scope (per architect
    BLOCKER-A2). Sections containing stub keywords MUST also contain Wrapper
    Status Matrix OR raise NotImplementedError reference OR emit `<>_stub`
    trace event.

    logic-iter1 fix: §TaskUnits extraction uses full-span regex (stops only
    at next ## heading) so ### TU body content is included.
    """
    failures = []
    # §TaskUnits full-span (logic-iter1 fix)
    m_tu = re.search(
        r"^##\s+TaskUnits\s*$\n([\s\S]*?)(?=^##\s+[^#]|\Z)",
        text, re.MULTILINE,
    )
    if m_tu:
        body = m_tu.group(1)
        if _STUB_KEYWORD_RE.search(body):
            if not _WRAPPER_MATRIX_RE.search(body):
                failures.append(
                    "TaskUnits: stub keyword present but no Wrapper Status "
                    "Matrix / NotImplementedError / stub trace event"
                )
    # §Inherited Lessons (any heading containing "Inherited Lessons")
    m_il = re.search(
        r"^(#{2,6})\s+[^\n]*Inherited Lessons[^\n]*\n([\s\S]*?)(?=^#{1,6}\s|\Z)",
        text, re.MULTILINE,
    )
    if m_il:
        body = m_il.group(2)
        if _STUB_KEYWORD_RE.search(body):
            if not _WRAPPER_MATRIX_RE.search(body):
                failures.append(
                    "Inherited Lessons Status Matrix: stub keyword present but no Wrapper Status "
                    "Matrix / NotImplementedError / stub trace event"
                )
    return failures


def _check_r12_heading_hierarchy(text: str) -> Optional[str]:
    """R12: no skipped levels in descent (h2->h4 fails; h4->h5 passes).

    LR-5 fix: explicit algorithm.
    S3 fix (security-reviewer iter2): file MUST start with h1 or h2 (no orphan h3+).
    """
    levels: List[int] = []
    started = False
    for line_no, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(#{1,6})\s", line)
        if m:
            depth = len(m.group(1))
            if not started:
                if depth > 2:
                    return f"orphan h{depth} at line {line_no} (file must start with h1 or h2)"
                started = True
            else:
                if depth > levels[-1] + 1:
                    return f"skipped level: h{levels[-1]} -> h{depth} at line {line_no}"
            levels.append(depth)
    return None


# ----------------------------------------------------------------------------
# R20 (autopilot_pi 2026-04-30): verify_cmd anti-pattern lint.
# 4 categories of broken verify_cmds known to fail on Win Git Bash:
#   (a) heredoc_python_c: `python -c "..."` containing literal newline in quoted body
#   (b) unix_env_prefix:  `^VAR=val cmd` Unix env-prefix on cmd-start (cmd parses VAR=val as command name)
#   (c) bash_heredoc:     `<<EOF` heredoc syntax (bash-only, breaks on Win Git Bash)
#   (d) tautological_pipe: `... | true` masks all errors silently
# Regex anchored to `verify_cmd:` payload only, per security-iter1-3 fix
# (prevents false-positive on plan_body markdown tables containing FOO=BAR).
# ----------------------------------------------------------------------------

# Compiled once at module load for P95 <50ms on ~30 KB plans.
_R20_PAYLOAD_RE = re.compile(
    r"^[ \t]*verify_cmd:[ \t]*`?([^\n]+?)`?[ \t]*$",
    re.MULTILINE,
)
_R20_HEREDOC_PYTHON_C_RE = re.compile(
    # `python -c "..."` containing literal newline (`\n` escape OR raw newline inside body).
    # Conservative: only flags actual heredoc-style multiline; one-liners with `;` are
    # legitimate Windows-safe pattern and frequent in legacy plans.
    r"""python\s+-c\s+["'][^"']*\\n""",
    re.MULTILINE,
)
# Additional category: nested-quote `python -c "..."` with single-quoted strings inside.
# memory_syste evidence.jsonl LIVE-failure: `python -c "...os.chdir('memexa')..."` triggered
# bash escape misparse on Win Git Bash. Distinct from legacy U1 `python -c "import x"` (no nested).
_R20_PYTHON_C_NESTED_QUOTES_RE = re.compile(
    r"""python\s+-c\s+"[^"]*'[^']*'[^"]*\"""",
)
_R20_BASH_HEREDOC_RE = re.compile(r"""<<\s*['"]?EOF""", re.IGNORECASE)
_R20_UNIX_ENV_PREFIX_RE = re.compile(
    r"^[A-Z_][A-Z0-9_]*=\S+\s+\w",
)
_R20_TAUTOLOGICAL_PIPE_RE = re.compile(r"\|\s*true\b")


def _check_r20_verify_cmd_lint(text: str) -> List[str]:
    """R20 verify_cmd anti-pattern lint. Returns violation list."""
    violations: List[str] = []
    for m in _R20_PAYLOAD_RE.finditer(text):
        payload = m.group(1).strip()
        if not payload:
            continue
        excerpt = payload[:60]
        if _R20_HEREDOC_PYTHON_C_RE.search(payload):
            violations.append(f"R20:heredoc_python_c:{excerpt}")
        if _R20_PYTHON_C_NESTED_QUOTES_RE.search(payload):
            violations.append(f"R20:python_c_nested_quotes:{excerpt}")
        if _R20_BASH_HEREDOC_RE.search(payload):
            violations.append(f"R20:bash_heredoc:{excerpt}")
        if _R20_UNIX_ENV_PREFIX_RE.match(payload):
            violations.append(f"R20:unix_env_prefix:{excerpt}")
        if _R20_TAUTOLOGICAL_PIPE_RE.search(payload):
            violations.append(f"R20:tautological_pipe:{excerpt}")
    return violations


# ----------------------------------------------------------------------------
# R21 (autopilot_pi 2026-04-30): every TU MUST have **tu_outputs** field.
# Required for stage5_staging_gate whitelist enforcement.
# Backward compat: TUs with `enforces_via:not_yet` are exempt (mirrors R18).
# Small plans (TU_count <= 2) skip the check (mirrors R17).
# ----------------------------------------------------------------------------

def _check_r21_tu_outputs_required(text: str) -> List[str]:
    """R21 every TU has **tu_outputs** OR **Outputs** field.

    CB-PARSER-2 fix (2026-05-02): also accept `**Outputs**:` (capital O,
    no `tu_` prefix). This is the canonical format prescribed by autopilot
    v2.0 §1 plan template; the legacy `**tu_outputs**:` is retained as an
    accepted alias. Previous regex (tu_outputs only) produced 12-TU
    false-positives on conformant plans.

    Returns list of TU IDs missing both fields.
    """
    failures: List[str] = []
    tu_pattern = re.compile(r"### TU-(\d+)[\s\S]*?(?=### TU-|\n## |\Z)")
    blocks = list(tu_pattern.finditer(text))
    if len(blocks) <= 2:
        return failures
    for m in blocks:
        tu_id = f"TU-{m.group(1)}"
        block = m.group(0)
        if re.search(r"enforces_via\s*:\s*not_yet", block):
            continue
        if not re.search(r"\*\*(?:tu_outputs|Outputs)\*\*\s*:", block):
            failures.append(tu_id)
    return failures


# ----------------------------------------------------------------------------
# R22 (learning_pip 2026-04-30): trace event emit-or-assert lint.
# For each `trace event: <name>` declared inside ### TU-N blocks, grep that
# TU's tu_outputs files for emit/append_trace/write_trace_event call sites.
# If declared but no emit-site found -> BLOCK with file list searched.
# Per HARD RULE feedback_trace_event_emit_or_assert.
# Per logic-iter1-1 fix: scope LIMITED to ### TU-N blocks; Stage Plan §5-§9
# trace event declarations are autopilot-infrastructure spec, not TU code.
# Env override: MEMEXA_SKIP_R22=1.
# ----------------------------------------------------------------------------

_R22_DECLARED_EVENT_RE = re.compile(
    # Match both "trace event: <name>" and "- trace event: <name>" markdown bullet
    r"^[ \t\-\*]*trace event[s]?\s*:\s*`?([A-Za-z_][A-Za-z0-9_]*)`?",
    re.MULTILINE,
)
_R22_TU_OUTPUTS_LIST_RE = re.compile(
    r"\*\*tu_outputs\*\*\s*:\s*\[([^\]]+)\]",
    re.MULTILINE,
)


def _r22_emit_pattern(event_name: str) -> "re.Pattern[str]":
    """Compile a regex matching emit-site patterns for `event_name`.

    Three patterns supported:
      - emit("event_name"...) / emit('event_name'...)
      - append_trace(... "event_name" ... )  [arg appears within next 100 chars]
      - write_trace_event("event_name"...)
    """
    name = re.escape(event_name)
    return re.compile(
        rf"""(?:emit\s*\(\s*["']{name}["']"""
        rf"""|append_trace\s*\([^)]{{0,200}}["']{name}["']"""
        rf"""|write_trace_event\s*\(\s*["']{name}["']"""
        rf"""|emit_trace_event\s*\(\s*["']{name}["']"""
        rf"""|_emit_event\s*\(\s*["']{name}["']"""
        rf"""|_emit_trace\s*\(\s*["']{name}["']"""
        rf"""|_eb_log\s*\(\s*["']{name}["']"""
        rf"""|log_event\s*\(\s*["']{name}["'])""",
        re.DOTALL,
    )


def _detect_plan_version(plan_path: Optional[str]) -> int:
    """B-6 helper: extract plan_v<N> from path. Returns 0 on miss/legacy."""
    if not plan_path:
        return 0
    import re as _re
    m = _re.search(r"plan_v(\d+)\.md$", str(plan_path).replace("\\", "/"))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def _check_r23_ac_pre_post_trace(text: str) -> List[str]:
    """B-6 (2026-05-04): each `**AC-N**:` block must contain pre_state +
    post_state + expected_trace within the next 10 lines. Returns list of
    AC ids that failed.
    """
    import re as _re
    failures: List[str] = []
    # Find each AC block start
    for m in _re.finditer(r"\*\*AC-(\d+\w*)\*\*\s*:", text):
        ac_id = f"AC-{m.group(1)}"
        # Look ahead 10 lines or until next ** (next AC marker)
        start = m.end()
        # Find end of AC block: next "**AC-" or 10 lines
        next_ac = _re.search(r"\*\*AC-\d+\w*\*\*\s*:", text[start:])
        block_end = start + (next_ac.start() if next_ac else min(800, len(text) - start))
        block = text[start:block_end]
        has_pre = bool(_re.search(r"\bpre_state\s*:", block, _re.IGNORECASE))
        has_post = bool(_re.search(r"\bpost_state\s*:", block, _re.IGNORECASE))
        has_trace = bool(_re.search(r"\bexpected_trace\s*:", block, _re.IGNORECASE))
        missing = []
        if not has_pre: missing.append("pre_state")
        if not has_post: missing.append("post_state")
        if not has_trace: missing.append("expected_trace")
        if missing:
            failures.append(f"{ac_id}: missing {', '.join(missing)}")
    return failures


def _check_r22_trace_event_emit_or_assert(
    text: str,
    plan_path: Optional[Path] = None,
) -> List[str]:
    """R22 lint: every `trace event: X` declared in ### TU-N block must have
    a matching emit-site in that TU's tu_outputs files.

    Returns list of violations as strings ``R22:no_emit_site:<event>:<tu_id>``.
    Skipped events found in §5-§9 Stage Plan sections (autopilot infra spec).

    Per logic-iter1-1 fix: TU-only scope.
    """
    if os.environ.get("MEMEXA_SKIP_R22") == "1":
        return []
    workspace_root: Optional[Path] = None
    if plan_path is not None:
        try:
            # plan_path = <ws>/.claude/harness/tasks/<tid>/plan_vN.md → parents[4] = <ws>
            workspace_root = plan_path.resolve().parents[4]
        except (IndexError, Exception):
            workspace_root = None
    violations: List[str] = []
    tu_pattern = re.compile(r"### TU-(\d+)[\s\S]*?(?=### TU-|\n## |\Z)")
    blocks = list(tu_pattern.finditer(text))
    if not blocks:
        return violations
    for m in blocks:
        tu_id = f"TU-{m.group(1)}"
        block = m.group(0)
        # Extract declared events in this TU only
        declared: list[str] = []
        for em in _R22_DECLARED_EVENT_RE.finditer(block):
            ev = em.group(1).strip()
            if ev and not ev.startswith(("X", "Y", "Z")):  # placeholder filter
                declared.append(ev)
        if not declared:
            continue
        # Extract tu_outputs file list
        out_m = _R22_TU_OUTPUTS_LIST_RE.search(block)
        if not out_m:
            continue  # R21 already catches missing tu_outputs
        outputs_blob = out_m.group(1)
        files = re.findall(r'"([^"]+)"', outputs_blob)
        if not files:
            continue
        # For each declared event, check at least one tu_outputs file emits it
        for event in declared:
            pat = _r22_emit_pattern(event)
            found = False
            for rel_path in files:
                # Resolve relative to workspace if possible; else try as-is
                candidates: list[Path] = []
                if workspace_root is not None:
                    candidates.append(workspace_root / rel_path)
                candidates.append(Path(rel_path))
                # Also try stripping leading "memexa/" if path doesn't exist
                if rel_path.startswith("memexa/") and workspace_root is not None:
                    candidates.append(workspace_root / rel_path[len("memexa/"):])
                for fp in candidates:
                    try:
                        if fp.is_file():
                            content = fp.read_text(encoding="utf-8", errors="replace")
                            if pat.search(content):
                                found = True
                                break
                    except OSError:
                        continue
                if found:
                    break
            if not found:
                violations.append(f"R22:no_emit_site:{event}:{tu_id}")
    # Per HARD RULE feedback_trace_event_emit_or_assert (self-emit on violation)
    if violations:
        try:
            from memexa.core.trace_sink import write_trace_event
            for v in violations[:5]:
                _, _, evname, tu = v.split(":", 3)
                write_trace_event("r22_lint_violation", {
                    "event_name": evname,
                    "tu_id": tu,
                })
        except Exception:
            pass
    return violations


def check(plan_path: Path, *, strict: bool = True) -> LintResult:
    """Lint a plan_v<N>.md file. Returns LintResult."""
    _emit_invocation_trace(plan_path, strict)

    if not plan_path.exists():
        return LintResult(ok=False, exit_code=EXIT_USAGE_ERROR,
                          failed_rules=["file_missing"],
                          details={"plan_path": str(plan_path)})

    text = _read_text_or_none(plan_path)
    if text is None:
        return LintResult(ok=False, exit_code=EXIT_SECTION_MISSING,
                          failed_rules=["R10_utf8"],
                          details={"plan_path": str(plan_path)})

    if not text.strip():
        return LintResult(ok=False, exit_code=EXIT_SECTION_MISSING,
                          failed_rules=["R11_empty"],
                          details={"size": len(text)})

    failed = []
    details: Dict[str, Any] = {}

    missing = _check_r1_sections(text)
    if missing:
        failed.append("R1_sections")
        details["R1_missing"] = missing

    r2_fail = _check_r2_density(text)
    if r2_fail:
        failed.append("R2_density")
        details["R2_below_200"] = r2_fail

    r3_fail = _check_r3_ratio(text)
    if r3_fail:
        failed.append("R3_ratio")
        details["R3"] = r3_fail

    r4_fail = _check_r4_verify_cmd(text)
    if r4_fail:
        failed.append("R4_verify_cmd")
        details["R4"] = r4_fail

    r5_fail = _check_r5_trace_event(text)
    if r5_fail:
        failed.append("R5_trace_event")
        details["R5"] = list(r5_fail.keys())

    r6_fail = _check_r6_ac_schema(text)
    if r6_fail:
        failed.append("R6_ac_schema")
        details["R6_bad_acs"] = r6_fail

    r7_fail = _check_r7_tu_axis_anchor(text)
    if r7_fail:
        failed.append("R7_tu_axis_anchor")
        details["R7"] = r7_fail

    r8_fail = _check_r8_tu_root_cause(text)
    if r8_fail:
        failed.append("R8_tu_root_cause")
        details["R8"] = r8_fail

    if not _check_r9_provenance(text):
        failed.append("R9_provenance")

    r12_err = _check_r12_heading_hierarchy(text)
    if r12_err:
        failed.append("R12_heading_hierarchy")
        details["R12"] = r12_err

    # U1 (long_term_plan_v2 TU-1) extensions: R13-R19
    r13_fail = _check_r13_physics_toy_benchmark(text)
    if r13_fail:
        failed.append("R13_physics_toy_benchmark")
        details["R13_missing_toy"] = r13_fail

    r14_fail = _check_r14_numerical_claim_script(text)
    if r14_fail:
        failed.append("R14_numerical_claim_script")
        details["R14_unscripted_claims"] = r14_fail[:5]  # cap to 5 excerpts

    if _check_r15_primary_metric_table(text):
        failed.append("R15_primary_metric_table")

    r16_fail = _check_r16_corpus_completeness(text)
    if r16_fail:
        failed.append("R16_corpus_completeness")
        details["R16_stub_files"] = r16_fail

    r17_fail = _check_r17_cross_cluster_integration(text)
    if r17_fail:
        failed.append("R17_cross_cluster_integration")
        details["R17"] = r17_fail

    # U17: TU-level integration_matrix field-presence (sibling of R17 cluster check)
    r17_field_fail = _check_r17_integration_field_present(text)
    if r17_field_fail:
        if "R17_cross_cluster_integration" not in failed:
            failed.append("R17_cross_cluster_integration")
        details["R17_field_missing"] = r17_field_fail

    r18_fail = _check_r18_tu_class_required(text)
    if r18_fail:
        failed.append("R18_tu_class_required")
        details["R18"] = r18_fail

    r19_fail = _check_r19_stub_status_matrix(text)
    if r19_fail:
        failed.append("R19_stub_status_matrix")
        details["R19"] = r19_fail

    # R20 (autopilot_pi 2026-04-30): verify_cmd anti-pattern lint.
    # Detects 4 categories of broken verify_cmds known to fail on Win Git Bash.
    # Per HARD RULE feedback_subagent_and_stall_protocol §1.
    r20_fail = _check_r20_verify_cmd_lint(text)
    if r20_fail:
        failed.append("R20_verify_cmd_lint")
        details["R20"] = r20_fail[:10]  # cap to 10

    # R21 (autopilot_pi 2026-04-30): every TU MUST have **tu_outputs** field
    # for stage5_staging_gate whitelist enforcement.
    r21_fail = _check_r21_tu_outputs_required(text)
    if r21_fail:
        failed.append("R21_tu_outputs_required")
        details["R21"] = r21_fail

    # R22 (learning_pip 2026-04-30): trace event declared in TU MUST have emit-site
    # in tu_outputs files. Closes HARD RULE feedback_trace_event_emit_or_assert
    # from text-only HARD RULE to machine-enforced lint.
    r22_fail = _check_r22_trace_event_emit_or_assert(text, plan_path)
    if r22_fail:
        failed.append("R22_trace_event_emit_or_assert")
        details["R22"] = r22_fail[:10]

    # R23 (B-6, 2026-05-04): each AC declaration MUST include pre_state +
    # post_state + expected_trace fields (B-1 contract upgrade). OPT-IN by
    # plan content — only fires if plan body declares `schema_version: 2`
    # (or "Plan schema: v2"). Legacy plans without this marker are exempt
    # so historical plan_v*.md files don't retroactively fail R23.
    try:
        is_opt_in = bool(
            re.search(r"(?:schema_version|plan\s*schema)\s*[:=]\s*[\"']?v?2", text, re.IGNORECASE)
        )
    except Exception:
        is_opt_in = False
    if is_opt_in:
        r23_fail = _check_r23_ac_pre_post_trace(text)
        if r23_fail:
            failed.append("R23_ac_pre_post_trace")
            details["R23"] = r23_fail[:10]

    if not failed:
        return LintResult(ok=True, exit_code=EXIT_PASS, failed_rules=[], details=details)

    if ("R1_sections" in failed or "R10_utf8" in failed
            or "R11_empty" in failed or "R12_heading_hierarchy" in failed):
        ec = EXIT_SECTION_MISSING
    elif "R2_density" in failed:
        ec = EXIT_DENSITY_FAIL
    elif "R3_ratio" in failed:
        ec = EXIT_RATIO_FAIL
    elif ("R6_ac_schema" in failed or "R7_tu_axis_anchor" in failed
            or "R8_tu_root_cause" in failed or "R9_provenance" in failed
            or "R13_physics_toy_benchmark" in failed
            or "R14_numerical_claim_script" in failed
            or "R15_primary_metric_table" in failed
            or "R16_corpus_completeness" in failed
            or "R17_cross_cluster_integration" in failed
            or "R18_tu_class_required" in failed
            or "R19_stub_status_matrix" in failed
            or "R20_verify_cmd_lint" in failed
            or "R21_tu_outputs_required" in failed
            or "R22_trace_event_emit_or_assert" in failed):
        ec = EXIT_AC_SCHEMA_FAIL
    elif "R4_verify_cmd" in failed or "R5_trace_event" in failed:
        ec = EXIT_TRACE_EVENT_MISSING
    else:
        ec = EXIT_AC_SCHEMA_FAIL
    return LintResult(ok=False, exit_code=ec, failed_rules=failed, details=details)


def check_by_task_id(task_id: str, *, strict: bool = True) -> LintResult:
    """Resolve plan path then check. Raises FileNotFoundError if no plan."""
    from memexa.core._plan_path_resolver import resolve_plan_path
    plan_path = resolve_plan_path(task_id)
    return check(plan_path, strict=strict)


# U1 (long_term_plan_v2 TU-2): SKILL→plan reverse audit + session_gate hook

@dataclass
class ReverseAuditResult:
    """Result of grep-ing SKILL.md MUST/SHOULD directives and matching against plan TUs."""
    ok: bool
    coverage_ratio: float  # fraction of directives covered by plan TUs
    threshold: float
    directives_total: int
    directives_covered: int
    uncovered_samples: List[str] = field(default_factory=list)
    skill_path: str = ""
    plan_path: str = ""


# Directive extraction regex: lines containing MUST/SHOULD/必须/必 + verb-object pattern.
# Limited to lines (not full body) to bound matches and reduce false positives.
_DIRECTIVE_RE = re.compile(
    r"^[\s\-*•]*([^\n]*?(?:\bMUST\b|\bSHOULD\b|\bMUST NOT\b|\bSHOULD NOT\b|"
    r"必须|必|应当|不得)[^\n]*?)$",
    re.MULTILINE | re.IGNORECASE,
)


def _extract_directives(skill_text: str) -> List[str]:
    """Extract MUST/SHOULD-class directives from SKILL.md.

    Returns list of one-line normalized directive strings (deduplicated).
    Excludes code-block content (```...```) to avoid false positives in samples.
    """
    # Strip fenced code blocks
    no_code = re.sub(r"```[\s\S]*?```", "", skill_text)
    # Strip inline code
    no_code = re.sub(r"`[^`\n]*`", "", no_code)
    seen = set()
    out: List[str] = []
    for m in _DIRECTIVE_RE.finditer(no_code):
        line = m.group(1).strip()
        if len(line) < 8 or len(line) > 300:
            continue
        # Filter trivial: lone "MUST" without context
        if re.fullmatch(r"\W*MUST\W*|\W*SHOULD\W*|\W*必须\W*", line, re.IGNORECASE):
            continue
        norm = re.sub(r"\s+", " ", line).lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(line)
    return out


def _directive_covered_by_plan(directive: str, plan_text: str) -> bool:
    """Heuristic: directive considered covered if ≥2 of its content words
    appear together in the plan §3 TaskUnits / §4 Acceptance Criteria.

    Filter stop-words (must/should/the/a/...) and require ≥2 content words.
    """
    stop = {"must", "should", "the", "a", "an", "of", "to", "in", "and", "or",
            "for", "on", "at", "by", "with", "is", "are", "be", "not",
            "必须", "必", "应当", "不得", "of", "this", "that"}
    words = re.findall(r"[a-zA-Z\u4e00-\u9fff_]{3,}", directive.lower())
    content = [w for w in words if w not in stop]
    if len(content) < 2:
        return True  # too vague to falsify; pass
    body3 = _section_body(plan_text, "TaskUnits") or ""
    body4 = _section_body(plan_text, "Acceptance Criteria") or ""
    combined = (body3 + "\n" + body4).lower()
    hits = sum(1 for w in content if w in combined)
    return hits >= 2


def reverse_audit_skill_to_plan(
    skill_path: Path,
    plan_path: Path,
    threshold: float = 0.75,
) -> ReverseAuditResult:
    """SKILL→plan reverse audit per architect BLOCKER-A3.

    Greps MUST/SHOULD directives from SKILL.md and checks each is reflected
    in plan_v<N>.md TU/AC sections. Returns ReverseAuditResult with
    coverage_ratio (default threshold 0.75 per logic-iter1 baseline).

    Returns ok=False if coverage < threshold OR file unreadable.
    """
    _emit_invocation_trace(plan_path, strict=True)
    if not skill_path.exists():
        return ReverseAuditResult(
            ok=False, coverage_ratio=0.0, threshold=threshold,
            directives_total=0, directives_covered=0,
            uncovered_samples=[f"skill_not_found: {skill_path}"],
            skill_path=str(skill_path), plan_path=str(plan_path),
        )
    if not plan_path.exists():
        return ReverseAuditResult(
            ok=False, coverage_ratio=0.0, threshold=threshold,
            directives_total=0, directives_covered=0,
            uncovered_samples=[f"plan_not_found: {plan_path}"],
            skill_path=str(skill_path), plan_path=str(plan_path),
        )
    skill_text = _read_text_or_none(skill_path) or ""
    plan_text = _read_text_or_none(plan_path) or ""
    directives = _extract_directives(skill_text)
    if not directives:
        return ReverseAuditResult(
            ok=True, coverage_ratio=1.0, threshold=threshold,
            directives_total=0, directives_covered=0,
            uncovered_samples=[],
            skill_path=str(skill_path), plan_path=str(plan_path),
        )
    covered = 0
    uncovered: List[str] = []
    for d in directives:
        if _directive_covered_by_plan(d, plan_text):
            covered += 1
        else:
            if len(uncovered) < 5:
                uncovered.append(d[:120])
    ratio = covered / len(directives)
    return ReverseAuditResult(
        ok=(ratio >= threshold),
        coverage_ratio=round(ratio, 3),
        threshold=threshold,
        directives_total=len(directives),
        directives_covered=covered,
        uncovered_samples=uncovered,
        skill_path=str(skill_path),
        plan_path=str(plan_path),
    )


def session_gate_stage_1_5_lint(task_id: str) -> LintResult:
    """Hook interface for session_gate Stage 1.5 to invoke plan_uniformity_check.

    Per plan_v2 §3 U1 step 6: this is the API surface only. Real session_gate
    integration is deferred to U18 patch consolidator.

    Returns the LintResult from check_by_task_id. Caller decides BLOCK on
    LintResult.ok == False (per architect "Missed" recommendation: return
    LintResult not bool, give caller decision granularity).
    """
    try:
        return check_by_task_id(task_id, strict=True)
    except FileNotFoundError as e:
        return LintResult(
            ok=False, exit_code=EXIT_USAGE_ERROR,
            failed_rules=["plan_not_found"],
            details={"error": str(e), "task_id": task_id},
        )


def _runner_for_selftest(plan_path: Path) -> int:
    """LR-1 fix: adapter — convert check() LintResult to int for selftest signature."""
    return check(plan_path).exit_code


# Minimal POS / NEG fixtures for --self-test (TU-4 has full 19-fixture suite)
_POS_FIXTURE = """# Plan v0 minimal valid

## Architecture Decision
Build it.

## Forbidden Approaches
- nope

## TaskUnits

### TU-1: example
**axis_anchor [C:cli:x]**: works
**root_cause_when_violated**: `plan_gap`
**tu_class**: refactor

## Acceptance Criteria

**Primary metric**: AC-1 verify_cmd exit_code = 0
**Secondary**: backward compat preserved

- **AC-1**: do
  verify_cmd: `bash`
  signal_hint: file:func

## Stage 3 Plan
""" + ("padded text " * 30) + """ verify_cmd: x verify_cmd: y trace event: e

## Stage 4 Plan
""" + ("padded text " * 30) + """ verify_cmd: x verify_cmd: y trace event: e

## Stage 4.5 Plan
""" + ("padded text " * 30) + """ verify_cmd: x verify_cmd: y trace event: e

## Stage 6 Plan
""" + ("padded text " * 30) + """ verify_cmd: x verify_cmd: y trace event: e

## Stage 5 Plan
""" + ("padded text " * 30) + """ verify_cmd: x verify_cmd: y trace event: e

## Risks
none

## Out-of-scope
none

## Plan provenance
v0
"""

_NEG_FIXTURE = """# Plan
## Architecture Decision
short
## TaskUnits
none
"""


def _cli(argv: List[str]) -> int:
    """CLI: plan_uniformity_check <task_id> | --path <plan> | --self-test | --reverse-audit <skill> <plan>."""
    if len(argv) < 2:
        print("Usage: plan_uniformity_check <task_id> | --path <plan> | --self-test | --reverse-audit <skill_path> <plan_path>",
              file=sys.stderr)
        return EXIT_USAGE_ERROR

    if argv[1] == "--self-test":
        from memexa.core._plan_lint_common import selftest
        return selftest("plan_uniformity_check",
                        _runner_for_selftest, _POS_FIXTURE, _NEG_FIXTURE)

    if argv[1] == "--reverse-audit":
        if len(argv) < 4:
            print("Usage: plan_uniformity_check --reverse-audit <skill_path> <plan_path>",
                  file=sys.stderr)
            return EXIT_USAGE_ERROR
        ra = reverse_audit_skill_to_plan(Path(argv[2]), Path(argv[3]))
        if ra.ok:
            print(f"[reverse_audit] PASS coverage={ra.coverage_ratio:.2%} "
                  f"({ra.directives_covered}/{ra.directives_total}) "
                  f"threshold={ra.threshold:.2%}")
            return EXIT_PASS
        print(f"[reverse_audit] FAIL coverage={ra.coverage_ratio:.2%} "
              f"({ra.directives_covered}/{ra.directives_total}) "
              f"threshold={ra.threshold:.2%}", file=sys.stderr)
        for s in ra.uncovered_samples:
            # security-iter1-2 fix: strip ANSI/control chars + bound length
            clean = _ANSI_RE.sub("", s)[:200]
            print(f"  uncovered: {clean}", file=sys.stderr)
        return EXIT_AC_SCHEMA_FAIL

    if argv[1] == "--path":
        if len(argv) < 3:
            print("Usage: plan_uniformity_check --path <plan_path>", file=sys.stderr)
            return EXIT_USAGE_ERROR
        result = check(Path(argv[2]))
    else:
        try:
            result = check_by_task_id(argv[1])
        except FileNotFoundError as e:
            print(f"[plan_uniformity_check] {e}", file=sys.stderr)
            return EXIT_USAGE_ERROR

    if result.ok:
        print(f"[plan_uniformity_check] PASS for {argv[1]}")
    else:
        print(f"[plan_uniformity_check] FAIL exit={result.exit_code} "
              f"rules={result.failed_rules}", file=sys.stderr)
        for k, v in result.details.items():
            print(f"  {k}: {v}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
