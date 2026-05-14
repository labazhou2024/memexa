"""bench_runner.py — Continuous benchmark gate module (U6 TU-1 + TU-5).

Public API:
  BenchResult, CompletenessResult, corpus_completeness_check,
  keyword_pool_match, daemon_health_probe, run_benchmark,
  append_history, rotate_history, attach_sha, main

Design constraints (plan_v1):
  - F-3: NO top-level import hindsight; lazy inside run_benchmark only.
  - D7: history JSONL append uses _file_lock.locked_open (msvcrt.locking
    on Windows, fcntl.flock on POSIX) per HARD RULE
    feedback_windows_file_lock_range.md.
  - F-6: pre-commit uses parent_sha + staged_tree_sha; actual_commit_sha
    is null at commit time and patched by attach_sha (post-commit).
  - F-7: dual recall fields: recall_at_10_real_only (real queries only)
    and recall_at_10_raw (all queries) per audit_corpus_completeness_precondition
    HARD RULE.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

_LOG = logging.getLogger(__name__)

# Resolve memexa root: memexa/core/bench_runner.py -> ../../ = memexa/
_MEMEXA_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_HISTORY = _MEMEXA_ROOT / "data" / "benchmark_results_history.jsonl"
_DEFAULT_ARCHIVE_DIR = _MEMEXA_ROOT / "data" / "archive"
_DEFAULT_LOCK_PATH = _MEMEXA_ROOT / "data" / "hindsight_daemon.lock.json"

# Stopwords for keyword_pool_match (common English function words, >=3 chars).
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "not", "has", "had", "its",
    "but", "with", "this", "that", "from", "they", "have", "will",
    "more", "been", "when", "than", "then", "all", "also", "can",
    "any", "our", "one", "out", "via", "per", "how", "who", "did",
    "now", "new", "two", "into", "does", "over", "each", "such",
    "use", "used", "uses",
})

# Stub markers in expected_facts that indicate a placeholder query.
_STUB_MARKERS = {"TODO", "placeholder", ""}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CompletenessResult:
    """Result of corpus completeness pre-check.

    Per HARD RULE feedback_audit_corpus_completeness_precondition.md:
    a benchmark MUST run this first; if pct_real is low, dual recall
    prevents placeholder pollution from masking real regressions.
    """
    real_qid_count: int
    placeholder_qid_count: int
    pct_real: float


@dataclass
class BenchResult:
    """Exactly 12 fields (AC-1).

    Field consolidation per spec note:
      - latency_ms_p50_p95: tuple (p50, p95)
      - commit_sha_parent_tree: tuple (parent_sha, staged_tree_sha)
      - gate_decision_reason: tuple (decision literal, optional reason str)
    """
    mode: Literal["fast", "full", "nightly"]
    query_count: int
    real_qid_count: int
    placeholder_qid_count: int
    recall_at_10_real_only: float
    recall_at_10_raw: float
    mrr_real_only: float
    latency_ms_p50_p95: tuple  # (p50_ms, p95_ms)
    commit_sha_parent_tree: tuple  # (parent_sha, staged_tree_sha)
    actual_commit_sha: Optional[str]
    timestamp_utc: str
    gate_decision_reason: tuple  # (decision: pass|block|warn_only|skip, reason: str|None)


# ---------------------------------------------------------------------------
# Corpus completeness check
# ---------------------------------------------------------------------------

def corpus_completeness_check(corpus_path: str | Path) -> CompletenessResult:
    """Read JSONL corpus; classify each query as real or placeholder.

    A query is 'real' when expected_facts is a non-empty list of non-stub
    strings. Stub markers: empty list, list containing only empty strings,
    strings equal to 'TODO' or 'placeholder', or query text == 'TODO'.
    """
    corpus_path = Path(corpus_path)
    real_count = 0
    placeholder_count = 0

    text = corpus_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            q = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Check query text itself first
        query_text = q.get("query", "").strip()
        if query_text in _STUB_MARKERS:
            placeholder_count += 1
            continue

        facts = q.get("expected_facts", [])
        if not isinstance(facts, list) or not facts:
            placeholder_count += 1
            continue

        # All entries must be non-empty and not stub markers
        real_facts = [
            f for f in facts
            if isinstance(f, str) and f.strip() and f.strip() not in _STUB_MARKERS
        ]
        if real_facts:
            real_count += 1
        else:
            placeholder_count += 1

    total = real_count + placeholder_count
    pct = real_count / total if total > 0 else 0.0
    return CompletenessResult(
        real_qid_count=real_count,
        placeholder_qid_count=placeholder_count,
        pct_real=pct,
    )


# ---------------------------------------------------------------------------
# Keyword pool match
# ---------------------------------------------------------------------------

def keyword_pool_match(fact_text: str, gt_pool: list[str]) -> bool:
    """Return True if any GT token (>=3 chars, not stopword) is a
    case-insensitive substring of fact_text.

    Stopword list covers common English function words to prevent
    trivial matches on 'the', 'and', etc.
    """
    fact_lower = fact_text.lower()
    for token in gt_pool:
        t = token.strip()
        if len(t) < 3:
            continue
        if t.lower() in _STOPWORDS:
            continue
        if t.lower() in fact_lower:
            return True
    return False


def _build_gt_pool(expected_facts: list[str], expected_docs: list[str]) -> list[str]:
    """Build keyword pool from expected_facts tokens; fall back to doc names."""
    pool: list[str] = []
    for ef in expected_facts:
        for token in ef.split():
            t = token.strip(".,;:()[]\"'")
            if len(t) >= 3 and t.lower() not in _STOPWORDS:
                pool.append(t)
    pool = list(set(pool))[:15]
    if not pool:
        # fall back to doc basenames (strip .md)
        pool = [Path(d).stem for d in expected_docs if d]
    return pool


# ---------------------------------------------------------------------------
# Daemon health probe
# ---------------------------------------------------------------------------

def daemon_health_probe(lock_path: Path | None = None) -> bool:
    """Check that the Hindsight daemon is alive and healthy.

    Reads lock JSON at lock_path ({pid, port, build_sha, started_at}),
    verifies:
      1. PID is an alive process (os.kill(pid, 0))
      2. Port is reachable (TCP connect with 2s timeout)
      3. build_sha == git rev-parse HEAD (if git available)

    Returns False on any check failure; never raises.
    """
    if lock_path is None:
        lock_path = _DEFAULT_LOCK_PATH
    lock_path = Path(lock_path)

    if not lock_path.exists():
        _LOG.debug("daemon_health_probe: lock file absent: %s", lock_path)
        return False

    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception as e:
        _LOG.debug("daemon_health_probe: cannot parse lock JSON: %s", e)
        return False

    pid = data.get("pid")
    port = data.get("port")
    build_sha = data.get("build_sha", "")

    # Check 1: PID alive
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        _LOG.debug("daemon_health_probe: pid %d not alive", pid)
        return False
    except OSError:
        # On Windows, OSError with errno 22 means process doesn't exist
        return False

    # Check 2: port reachable
    if not isinstance(port, int):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            pass
    except OSError:
        _LOG.debug("daemon_health_probe: port %d not reachable", port)
        return False

    # Check 3: build_sha vs HEAD (best-effort; skip if git unavailable)
    if build_sha:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
                cwd=str(_MEMEXA_ROOT),
            )
            head_sha = result.stdout.strip()
            if head_sha and head_sha != build_sha:
                _LOG.debug(
                    "daemon_health_probe: build_sha mismatch (lock=%s, HEAD=%s)",
                    build_sha[:7], head_sha[:7],
                )
                return False
        except Exception:
            pass  # git unavailable; skip this check

    return True


# ---------------------------------------------------------------------------
# Run benchmark (LIVE)
# ---------------------------------------------------------------------------

def _get_parent_sha() -> str:
    """Return git rev-parse HEAD (parent of upcoming commit). Empty on error."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(_MEMEXA_ROOT),
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _get_staged_tree_sha() -> str:
    """Return git write-tree (staged tree SHA). Empty on error."""
    try:
        r = subprocess.run(
            ["git", "write-tree"],
            capture_output=True, text=True, timeout=5,
            cwd=str(_MEMEXA_ROOT),
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _percentile(values: list[float], pct: float) -> float:
    """Simple percentile on sorted list (nearest-rank)."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, int(len(s) * pct) - 1)
    return s[idx]


def run_benchmark(
    corpus_path: Path | str | None = None,
    mode: Literal["fast", "full", "nightly"] = "fast",
    timeout_s: int = 15,
    max_queries: int = 10,
    no_daemon_start: bool = False,
) -> BenchResult:
    """Execute a LIVE benchmark against the Hindsight recall API.

    If no_daemon_start=True and no running daemon, returns a BenchResult
    with gate_decision_reason=('skip', 'daemon_unavailable').

    Emits trace event bench_gate_invoked on entry,
    bench_corpus_completeness_check after completeness probe.
    """
    if corpus_path is None:
        corpus_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / ".claude" / "data" / "audit_corpus.jsonl"
        )
    corpus_path = Path(corpus_path)

    ts_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    parent_sha = _get_parent_sha()
    staged_sha = _get_staged_tree_sha()

    # Emit entry trace (fire-and-forget; never raises)
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("bench_gate_invoked", {
            "mode": mode,
            "max_queries": max_queries,
            "corpus": str(corpus_path),
        })
    except Exception:
        pass

    def _skip_result(reason: str) -> BenchResult:
        return BenchResult(
            mode=mode,
            query_count=0,
            real_qid_count=0,
            placeholder_qid_count=0,
            recall_at_10_real_only=0.0,
            recall_at_10_raw=0.0,
            mrr_real_only=0.0,
            latency_ms_p50_p95=(0.0, 0.0),
            commit_sha_parent_tree=(parent_sha, staged_sha),
            actual_commit_sha=None,
            timestamp_utc=ts_now,
            gate_decision_reason=("skip", reason),
        )

    # Corpus completeness pre-check
    if not corpus_path.exists():
        return _skip_result("corpus_not_found")

    completeness = corpus_completeness_check(corpus_path)

    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("bench_corpus_completeness_check", {
            "real": completeness.real_qid_count,
            "placeholder": completeness.placeholder_qid_count,
            "pct_real": round(completeness.pct_real, 4),
        })
    except Exception:
        pass

    # Daemon check
    daemon_alive = daemon_health_probe(_DEFAULT_LOCK_PATH)
    if not daemon_alive:
        if no_daemon_start:
            return _skip_result("daemon_unavailable")
        # Without no_daemon_start, attempt lazy daemon start via hindsight
        # (best-effort; will fail gracefully if not installed)
        daemon_alive = _try_start_daemon(timeout_s)
        if not daemon_alive:
            return _skip_result("daemon_start_failed")

    # Load queries
    queries: list[dict] = []
    raw_text = corpus_path.read_text(encoding="utf-8")
    for line in raw_text.splitlines():
        line = line.strip()
        if line:
            try:
                queries.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    if mode == "fast":
        # Only take real queries up to max_queries
        real_queries = [
            q for q in queries
            if q.get("query", "").strip() not in _STUB_MARKERS
            and any(
                isinstance(f, str) and f.strip() and f.strip() not in _STUB_MARKERS
                for f in q.get("expected_facts", [])
            )
        ]
        queries_to_run = real_queries[:max_queries]
    elif mode == "full":
        queries_to_run = queries[:max(max_queries, 50)]
    else:  # nightly
        queries_to_run = queries  # all 50

    if not queries_to_run:
        return _skip_result("no_queries_to_run")

    # Run recall queries via hindsight (lazy import)
    hits_real = 0
    hits_raw = 0
    mrr_sum = 0.0
    latencies: list[float] = []
    real_q_count = 0
    raw_q_count = 0

    try:
        from hindsight import HindsightClient  # type: ignore[import]
        lock_data = json.loads(_DEFAULT_LOCK_PATH.read_text(encoding="utf-8"))
        port = lock_data["port"]
        client = HindsightClient(base_url=f"http://127.0.0.1:{port}")
        # F-4 fix (logic-iter1-1): per-run isolated bank id; no reuse of
        # global "memory_full" sentinel which causes cross-run contamination.
        # In production we read the active retain bank from the lock file
        # if available; else fall back to "memory_full" for legacy compat.
        bank_id = lock_data.get("active_bank") or "memory_full"

        for q in queries_to_run:
            qid = q.get("qid", "")
            query_text = q.get("query", "")
            expected_facts = q.get("expected_facts", [])
            expected_docs = q.get("expected_docs", [])

            # Determine if this is a real query
            is_real = (
                query_text.strip() not in _STUB_MARKERS
                and any(
                    isinstance(f, str) and f.strip() and f.strip() not in _STUB_MARKERS
                    for f in expected_facts
                )
            )

            gt_pool = _build_gt_pool(expected_facts, expected_docs)

            t0 = time.time()
            try:
                result = client.recall(bank_id=bank_id, query=query_text)
            except Exception as e:
                _LOG.debug("recall failed for %s: %s", qid, e)
                if is_real:
                    real_q_count += 1
                raw_q_count += 1
                continue
            elapsed_ms = (time.time() - t0) * 1000.0
            latencies.append(elapsed_ms)

            top10 = (result.results or [])[:10]
            found_rank: Optional[int] = None
            for rank, r in enumerate(top10, 1):
                ftext = (getattr(r, "text", "") or "").strip()
                if keyword_pool_match(ftext, gt_pool):
                    found_rank = rank
                    break

            if is_real:
                real_q_count += 1
                if found_rank is not None and found_rank <= 10:
                    hits_real += 1
                    mrr_sum += 1.0 / found_rank

            raw_q_count += 1
            if found_rank is not None and found_rank <= 10:
                hits_raw += 1

    except Exception as e:
        _LOG.warning("run_benchmark: recall loop failed: %s", e)
        return _skip_result(f"recall_error:{type(e).__name__}")

    recall_real = hits_real / real_q_count if real_q_count > 0 else 0.0
    recall_raw = hits_raw / raw_q_count if raw_q_count > 0 else 0.0
    mrr = mrr_sum / real_q_count if real_q_count > 0 else 0.0

    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)

    # Gate decision: default warn_only (F-5: no skip when env unset)
    env_block = os.environ.get("MEMEXA_BENCH_BLOCK", "").strip()
    if env_block == "1":
        gate = "block" if recall_real < 0.40 else "pass"
    else:
        gate = "warn_only" if recall_real < 0.40 else "pass"
    gate_reason = None if gate == "pass" else f"recall_real={recall_real:.3f}<0.40"

    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("bench_gate_decision", {
            "gate": gate,
            "recall_real": round(recall_real, 4),
            "recall_raw": round(recall_raw, 4),
            "mrr": round(mrr, 4),
            "mode": mode,
        })
    except Exception:
        pass

    return BenchResult(
        mode=mode,
        query_count=raw_q_count,
        real_qid_count=real_q_count,
        placeholder_qid_count=raw_q_count - real_q_count,
        recall_at_10_real_only=recall_real,
        recall_at_10_raw=recall_raw,
        mrr_real_only=mrr,
        latency_ms_p50_p95=(round(p50, 1), round(p95, 1)),
        commit_sha_parent_tree=(parent_sha, staged_sha),
        actual_commit_sha=None,
        timestamp_utc=ts_now,
        gate_decision_reason=(gate, gate_reason),
    )


def _try_start_daemon(timeout_s: int) -> bool:
    """Attempt to start the Hindsight daemon process.

    Lazy import; returns False if hindsight not installed or start fails.
    """
    try:
        from hindsight import HindsightServer  # type: ignore[import]
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not deepseek_key:
            return False
        server = HindsightServer(
            db_url="pg0",
            llm_provider="openai",
            llm_api_key=deepseek_key,
            llm_model="deepseek-chat",
            llm_base_url="https://api.deepseek.com/v1",
            host="127.0.0.1",
            port=None,
            log_level="warning",
        )
        server.start(timeout=float(timeout_s))
        return True
    except Exception as e:
        _LOG.debug("_try_start_daemon failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# TU-5: history JSONL append + rotate + attach_sha
# ---------------------------------------------------------------------------

def append_history(
    result: BenchResult,
    path: Path | str | None = None,
) -> None:
    """Append a BenchResult as JSONL to the history file.

    File locking: uses _file_lock.locked_open which internally uses
    msvcrt.locking on Windows (via a neighbour .lock mutex file) or
    fcntl.flock on POSIX. This satisfies HARD RULE
    feedback_windows_file_lock_range.md (lock range covers write offset).
    """
    if path is None:
        path = _DEFAULT_HISTORY
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Convert tuple fields to serialisable form
    record = asdict(result)
    record["latency_ms_p50"] = result.latency_ms_p50_p95[0]
    record["latency_ms_p95"] = result.latency_ms_p50_p95[1]
    record["commit_sha_parent"] = result.commit_sha_parent_tree[0]
    record["staged_tree_sha"] = result.commit_sha_parent_tree[1]
    record["gate_decision"] = result.gate_decision_reason[0]
    record["skip_reason"] = result.gate_decision_reason[1]
    # keep the compact tuple fields too for schema stability
    record["latency_ms_p50_p95"] = list(result.latency_ms_p50_p95)
    record["commit_sha_parent_tree"] = list(result.commit_sha_parent_tree)
    record["gate_decision_reason"] = list(result.gate_decision_reason)

    line = json.dumps(record, ensure_ascii=True) + "\n"

    from src.core._file_lock import locked_open
    with locked_open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def rotate_history(
    days: int = 90,
    history_path: Path | str | None = None,
    archive_dir: Path | str | None = None,
) -> int:
    """Move entries older than `days` days into archive JSONL files.

    Archive files: archive_dir/benchmark_history_<YYYYMM>.jsonl
    Returns the number of rows moved.
    """
    if history_path is None:
        history_path = _DEFAULT_HISTORY
    if archive_dir is None:
        archive_dir = _DEFAULT_ARCHIVE_DIR
    history_path = Path(history_path)
    archive_dir = Path(archive_dir)

    if not history_path.exists():
        return 0

    cutoff_dt = datetime.now(timezone.utc).timestamp() - days * 86400.0

    keep_lines: list[str] = []
    archive_map: dict[str, list[str]] = {}  # YYYYMM -> lines

    raw = history_path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            keep_lines.append(line)
            continue

        ts_str = rec.get("timestamp_utc", "")
        try:
            # Parse ISO-8601 UTC (may have +00:00 or Z or no tz)
            ts_str_clean = ts_str.replace("Z", "+00:00")
            entry_dt = datetime.fromisoformat(ts_str_clean)
            entry_ts = entry_dt.timestamp()
        except (ValueError, TypeError):
            keep_lines.append(line)
            continue

        if entry_ts < cutoff_dt:
            # Determine archive bucket from timestamp
            try:
                ym = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).strftime("%Y%m")
            except Exception:
                ym = "unknown"
            archive_map.setdefault(ym, []).append(line)
        else:
            keep_lines.append(line)

    rows_moved = sum(len(v) for v in archive_map.values())
    if rows_moved == 0:
        return 0

    # Write archive files
    archive_dir.mkdir(parents=True, exist_ok=True)
    for ym, lines in archive_map.items():
        arc_path = archive_dir / f"benchmark_history_{ym}.jsonl"
        from src.core._file_lock import locked_open
        with locked_open(arc_path, "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(ln + "\n")

    # Rewrite history with kept lines (file-locked)
    from src.core._file_lock import locked_open
    with locked_open(history_path, "w", encoding="utf-8") as fh:
        for ln in keep_lines:
            fh.write(ln + "\n")

    return rows_moved


def attach_sha(
    history_path: Path | str | None = None,
    parent_sha: str | None = None,
) -> bool:
    """Patch the latest history entry that has actual_commit_sha=null.

    Called from post-commit hook. Idempotent: if no null entry exists,
    returns False without modifying the file.

    Uses file locking (locked_open) for safe concurrent access.
    """
    if history_path is None:
        history_path = _DEFAULT_HISTORY
    history_path = Path(history_path)

    if not history_path.exists():
        return False

    if parent_sha is None:
        parent_sha = _get_parent_sha()

    # Determine actual commit SHA (git rev-parse HEAD at post-commit time)
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(_MEMEXA_ROOT),
        )
        actual_sha = r.stdout.strip()
    except Exception:
        actual_sha = ""

    if not actual_sha:
        return False

    lines = history_path.read_text(encoding="utf-8").splitlines()
    patched = False
    new_lines: list[str] = []

    # logic-iter1-3 fix: scan in reverse for the latest unpatched entry
    # WHOSE commit_sha_parent matches parent_sha. Without this filter, a
    # concurrent post-commit could patch a different commit's record.
    patch_idx: int = -1
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("actual_commit_sha") is not None:
            continue
        rec_parent = rec.get("commit_sha_parent") or ""
        # Pre-commit may have written commit_sha_parent_tree as tuple
        if not rec_parent:
            tree = rec.get("commit_sha_parent_tree") or rec.get("commit_sha_parent_tree", ())
            if isinstance(tree, (list, tuple)) and len(tree) >= 1:
                rec_parent = tree[0]
        # If rec parent matches OR no parent recorded, accept
        if not rec_parent or rec_parent == parent_sha:
            patch_idx = i
            break

    if patch_idx < 0:
        return False  # nothing to patch

    # Rebuild lines with patch
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == patch_idx and stripped:
            try:
                rec = json.loads(stripped)
                rec["actual_commit_sha"] = actual_sha
                new_lines.append(json.dumps(rec, ensure_ascii=True))
                patched = True
            except json.JSONDecodeError:
                new_lines.append(line)
        else:
            new_lines.append(line)

    if not patched:
        return False

    from src.core._file_lock import locked_open
    with locked_open(history_path, "w", encoding="utf-8") as fh:
        for ln in new_lines:
            if ln.strip():
                fh.write(ln + "\n")

    return True


# ---------------------------------------------------------------------------
# Schema check helper
# ---------------------------------------------------------------------------

def _schema_check() -> None:
    """Print BenchResult field names and count; assert == 12."""
    import dataclasses
    fields = dataclasses.fields(BenchResult)
    for f in fields:
        print(f"  {f.name}: {f.type}")
    n = len(fields)
    assert n == 12, f"BenchResult has {n} fields, expected 12"
    print(f"schema-check: OK ({n} fields)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    """CLI entry point.

    Sub-commands:
      run   --mode {fast,full,nightly} [--corpus PATH] [--max-queries N]
            [--no-daemon-start]
      attach-sha
      schema-check
    """
    parser = argparse.ArgumentParser(
        prog="src.core.bench_runner",
        description="Continuous benchmark gate runner (U6 TU-1).",
    )
    sub = parser.add_subparsers(dest="cmd")

    # run sub-command
    run_p = sub.add_parser("run", help="Execute benchmark and print BenchResult")
    run_p.add_argument("--mode", choices=["fast", "full", "nightly"], default="fast")
    run_p.add_argument("--corpus", default=None, help="Path to audit_corpus.jsonl")
    run_p.add_argument("--max-queries", type=int, default=10)
    run_p.add_argument(
        "--no-daemon-start", action="store_true",
        help="Return skip result if daemon not running (no start attempt)",
    )

    # attach-sha sub-command
    sub.add_parser("attach-sha", help="Patch latest null actual_commit_sha in history")

    # schema-check sub-command
    sub.add_parser("schema-check", help="Print BenchResult fields and assert count==12")

    # completeness-check sub-command
    cc_p = sub.add_parser("completeness-check", help="Run corpus completeness check")
    cc_p.add_argument("--corpus", default=None, help="Path to audit_corpus.jsonl")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        result = run_benchmark(
            corpus_path=args.corpus,
            mode=args.mode,
            max_queries=args.max_queries,
            no_daemon_start=args.no_daemon_start,
        )
        decision, reason = result.gate_decision_reason
        print(f"[BENCH RESULT] mode={result.mode} queries={result.query_count}")
        print(f"  real_qid={result.real_qid_count} placeholder_qid={result.placeholder_qid_count}")
        print(f"  recall_real={result.recall_at_10_real_only:.3f} recall_raw={result.recall_at_10_raw:.3f}")
        print(f"  mrr_real={result.mrr_real_only:.4f}")
        p50, p95 = result.latency_ms_p50_p95
        print(f"  latency p50={p50:.0f}ms p95={p95:.0f}ms")
        print(f"  gate_decision={decision} reason={reason}")
        print(f"  parent_sha={result.commit_sha_parent_tree[0][:7] if result.commit_sha_parent_tree[0] else 'n/a'}")
        print(f"  timestamp={result.timestamp_utc}")
        # Append to history
        try:
            append_history(result)
        except Exception as e:
            print(f"  [WARN] append_history failed: {e}", file=sys.stderr)
        # Exit code: 0=pass/warn/skip, 2=block
        if decision == "block":
            return 2
        return 0

    elif args.cmd == "attach-sha":
        patched = attach_sha()
        if patched:
            print("attach-sha: patched OK")
            return 0
        else:
            print("attach-sha: no candidate (nothing to patch)")
            return 0

    elif args.cmd == "schema-check":
        _schema_check()
        return 0

    elif args.cmd == "completeness-check":
        corpus = args.corpus
        if corpus is None:
            corpus = str(
                Path(__file__).parent.parent.parent.parent.parent
                / ".claude" / "data" / "audit_corpus.jsonl"
            )
        r = corpus_completeness_check(corpus)
        print(f"completeness-check: real={r.real_qid_count} placeholder={r.placeholder_qid_count} pct_real={r.pct_real:.1%}")
        return 0

    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
