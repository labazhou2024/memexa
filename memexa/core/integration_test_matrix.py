"""Integration test matrix — declarative cross-TU integration validator.

U17 (long_term_plan_v2 §3 U17, BL-8). Backs plan_uniformity_check R17.

Public API:
    parse_plan_for_matrix(text)  -> dict[tu_id, list[ref]]
    cluster_tus(text, window=5)  -> list[list[tu_id]]   (non-overlapping ceil(N/5))
    validate_matrix(plan_text)   -> ValidationReport
    generate_test_stubs(plan_text, out_dir) -> list[Path]
    emit_validated(tid, report)  -> bool   (trace event integration_matrix_validated)

CLI:
    python -m memexa.core.integration_test_matrix validate <plan_path>
    python -m memexa.core.integration_test_matrix cluster  <plan_path>
    python -m memexa.core.integration_test_matrix generate <plan_path> --out-dir <dir>
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "ValidationReport",
    "parse_plan_for_matrix",
    "cluster_tus",
    "validate_matrix",
    "generate_test_stubs",
    "emit_validated",
]

_TU_BLOCK_RE = re.compile(r"### TU-(\d+)[\s\S]*?(?=### TU-|\n## |\Z)")
_FIELD_RE = re.compile(r"\*\*integration_matrix\*\*\s*:\s*([^\n]+)")
_EXEMPT_RE = re.compile(r"enforces_via\s*:\s*not_yet")
_REF_SPLIT_RE = re.compile(r"[;,]")
_TU_REF_RE = re.compile(r"TU-\d+")
_NA_RE = re.compile(r"\bn/?a\b", re.IGNORECASE)
_MAX_PLAN_BYTES = 5 * 1024 * 1024  # 5 MB hard cap; defends against ReDoS


@dataclass
class ValidationReport:
    ok: bool = True
    tu_count: int = 0
    cluster_count: int = 0
    missing_field_tus: List[str] = field(default_factory=list)
    clusters_without_test: List[int] = field(default_factory=list)
    matrix: Dict[str, List[str]] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "tu_count": self.tu_count,
            "clusters": self.cluster_count,
            "missing_fields": len(self.missing_field_tus),
            "clusters_without_test": len(self.clusters_without_test),
            "ok": self.ok,
        }


def _iter_tu_blocks(text: str):
    for m in _TU_BLOCK_RE.finditer(text):
        yield f"TU-{m.group(1)}", m.group(0)


def parse_plan_for_matrix(text: str) -> Dict[str, List[str]]:
    """Extract integration_matrix declarations per TU.

    Returns mapping tu_id -> list[ref]. Absent field -> tu_id maps to [].
    Exempt blocks (enforces_via: not_yet) are excluded from output.
    Special: a value containing 'n/a' is encoded as ['__N_A__'] so callers
    can treat it as field-PRESENT (counts as has-refs) per plan_template guidance.
    """
    if len(text) > _MAX_PLAN_BYTES:
        text = text[:_MAX_PLAN_BYTES]
    out: Dict[str, List[str]] = {}
    for tu_id, block in _iter_tu_blocks(text):
        if _EXEMPT_RE.search(block):
            continue
        m = _FIELD_RE.search(block)
        if not m:
            out[tu_id] = []
            continue
        raw = m.group(1).strip()
        if _NA_RE.search(raw):
            out[tu_id] = ["__N_A__"]
            continue
        refs = []
        for piece in _REF_SPLIT_RE.split(raw):
            for ref_m in _TU_REF_RE.finditer(piece):
                ref = ref_m.group(0)
                # logic-iter1-4: self-ref doesn't count as cross-TU integration
                if ref != tu_id:
                    refs.append(ref)
        out[tu_id] = refs
    return out


def cluster_tus(text: str, window: int = 5) -> List[List[str]]:
    """Non-overlapping chunks of size `window`. ceil(N/window) clusters.

    Logic-reviewer iter1-7: explicit non-overlapping; matches AC-2 (80 TU / 5 = 16).
    """
    if window <= 0:
        raise ValueError("window must be positive")
    tus = [tu_id for tu_id, _ in _iter_tu_blocks(text)]
    if not tus:
        return []
    return [tus[i:i + window] for i in range(0, len(tus), window)]


def _has_integration_field(block: str) -> bool:
    return bool(_FIELD_RE.search(block))


def validate_matrix(plan_text: str) -> ValidationReport:
    """Validate plan has integration_matrix per TU and >=1 cross-TU ref per cluster.

    Distinguishes two failure modes (logic-iter1-1 + coverage-iter1-2 fix):
      - missing_field_tus: TU lacks the **integration_matrix**: line entirely
      - clusters_without_test: every TU in cluster has empty cross-TU refs
        (e.g., field present but only self-refs which were filtered)
    For total TU block count <= 2: skip BOTH checks (R17 small-plan exemption).
    """
    if len(plan_text) > _MAX_PLAN_BYTES:
        plan_text = plan_text[:_MAX_PLAN_BYTES]
    matrix = parse_plan_for_matrix(plan_text)
    clusters = cluster_tus(plan_text)
    field_present: Dict[str, bool] = {}
    total_blocks = 0
    for tu_id, block in _iter_tu_blocks(plan_text):
        total_blocks += 1
        if _EXEMPT_RE.search(block):
            continue
        field_present[tu_id] = _has_integration_field(block)
    rep = ValidationReport(
        tu_count=total_blocks,
        cluster_count=len(clusters),
        matrix=matrix,
    )
    if total_blocks <= 2:
        rep.ok = True
        return rep
    rep.missing_field_tus = [tu for tu, present in field_present.items() if not present]
    for idx, cluster in enumerate(clusters):
        if not any(matrix.get(tu) for tu in cluster):
            rep.clusters_without_test.append(idx)
    rep.ok = (not rep.missing_field_tus) and (not rep.clusters_without_test)
    return rep


def _safe_resolve_out_dir(out_dir: Path, allowed_roots: Optional[List[Path]] = None) -> Path:
    """Path-traversal guard per HARD RULE feedback_plan_driven_filesystem_traversal_guard.

    out_dir.resolve() must be a descendant of one of allowed_roots
    (default: cwd, system tempdir). Raises ValueError on escape.
    """
    import tempfile
    resolved = Path(out_dir).resolve()
    if allowed_roots is None:
        allowed_roots = [Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve()]
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ValueError(
        f"out_dir {resolved} escapes allowed roots: {[str(r) for r in allowed_roots]}"
    )


def generate_test_stubs(plan_text: str, out_dir: Path,
                        allowed_roots: Optional[List[Path]] = None) -> List[Path]:
    """Emit one stub file per cluster: test_cluster_<N>.py with a placeholder test.

    Idempotent: existing stubs overwritten. Returns sorted list of paths.
    Path-traversal guard via _safe_resolve_out_dir (HARD RULE).
    """
    safe_out = _safe_resolve_out_dir(out_dir, allowed_roots)
    safe_out.mkdir(parents=True, exist_ok=True)
    clusters = cluster_tus(plan_text)
    paths: List[Path] = []
    for idx, cluster in enumerate(clusters):
        path = safe_out / f"test_cluster_{idx}.py"
        body = (
            "# AUTO-GENERATED by integration_test_matrix.generate_test_stubs\n"
            f"# Cluster {idx}: {', '.join(cluster)}\n\n"
            f"def test_cluster_{idx}_placeholder() -> None:\n"
            f"    \"\"\"TODO: integration test for {', '.join(cluster)}.\"\"\"\n"
            f"    assert True  # stub\n"
        )
        path.write_text(body, encoding="utf-8")
        paths.append(path)
    return sorted(paths)


def emit_validated(task_id: str, report: ValidationReport) -> bool:
    """Append integration_matrix_validated trace event. Soft-fail if sink absent."""
    payload = report.to_payload()
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event("integration_matrix_validated", payload)
        return True
    except Exception:
        try:
            from memexa.core.task_dir_layout import append_trace
            return append_trace(task_id, "integration_matrix_validated", payload)
        except Exception:
            return False


def _cli_main(argv: List[str]) -> int:
    if len(argv) < 3:
        print("Usage: integration_test_matrix {validate|cluster|generate} <plan_path> [--out-dir <dir>]",
              file=sys.stderr)
        return 64
    cmd = argv[1]
    plan_path = Path(argv[2])
    if not plan_path.is_file():
        print(f"plan file not found: {plan_path}", file=sys.stderr)
        return 2
    text = plan_path.read_text(encoding="utf-8")
    if cmd == "validate":
        rep = validate_matrix(text)
        print(f"validate: ok={rep.ok} tu_count={rep.tu_count} clusters={rep.cluster_count} "
              f"missing_fields={len(rep.missing_field_tus)} clusters_without_test={len(rep.clusters_without_test)}")
        if rep.missing_field_tus:
            print(f"  missing: {rep.missing_field_tus}", file=sys.stderr)
        return 0 if rep.ok else 1
    if cmd == "cluster":
        clusters = cluster_tus(text)
        for i, c in enumerate(clusters):
            print(f"cluster_{i}: {','.join(c)}")
        return 0
    if cmd == "generate":
        out_dir = Path("tests/integration")
        if "--out-dir" in argv:
            i = argv.index("--out-dir")
            if i + 1 < len(argv):
                out_dir = Path(argv[i + 1])
        try:
            paths = generate_test_stubs(text, out_dir)
        except ValueError as e:
            print(f"path traversal blocked: {e}", file=sys.stderr)
            return 2
        for p in paths:
            print(p)
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(_cli_main(sys.argv))
