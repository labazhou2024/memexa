"""U12 (2026-04-27) — LLM cost meter for tier-1 parity.

U12-B (2026-04-28) — extended with per-task budget guard
(cf. long_term_plan_v2 §623-648 plan_v2 spec; commit 12ce3a1 left 4/5 spec
gap; this revision closes it without introducing a parallel cost_telemetry
ledger). check_budget(tid) reads task_spec.cost_budget_usd (default 50.0),
sums est_cost_usd filtered by entry.task_id == tid, returns state dict.
log(... task_id=tid) lazily checks (every 10th call) and emits cost_budget_warn
on 80% crossing (one-shot per tid) + cost_budget_block on 100% crossing
(also writes <task_dir>/cost_budget_blocked flag file for pretool_gate Rule 12).

Append-only JSONL log + daily summary CLI. Hot-path overhead <1ms via raw
append + lazy stat (rotation check every 100 calls). Schema:

  {ts, op, model, prompt_tokens, completion_tokens, est_cost_usd, task_id?}

Pricing rates as of 2026-04-27 (approximate; per-million-token):
  - deepseek-chat:     $0.27 input / $1.10 output
  - gpt-4o:            $5.00 input / $15.00 output
  - claude-sonnet-4-6: $3.00 input / $15.00 output

CLI:
  python -m src.core.cost_meter daily              # text summary
  python -m src.core.cost_meter daily --json       # JSON summary
  python -m src.core.cost_meter summary <tid>      # per-task summary
  python -m src.core.cost_meter summary <tid> --json
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Per-million-token rates (USD)
_RATES: Dict[str, Dict[str, float]] = {
    "deepseek-chat":     {"input": 0.27e-6, "output": 1.10e-6},
    "deepseek-reasoner": {"input": 0.55e-6, "output": 2.19e-6},
    "gpt-4o":            {"input": 5.00e-6, "output": 15.00e-6},
    "gpt-4o-mini":       {"input": 0.15e-6, "output": 0.60e-6},
    "claude-sonnet-4-6": {"input": 3.00e-6, "output": 15.00e-6},
    "claude-opus-4-7":   {"input": 15.00e-6, "output": 75.00e-6},
    "claude-haiku-4-5":  {"input": 0.25e-6, "output": 1.25e-6},
}

_LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "llm_cost.jsonl"
_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MB
_ROTATE_CHECK_EVERY = 100  # lazy: check size every N calls

# U12-B budget guard
# Lazy check every 100 calls. Rationale: avg call cost ~$0.001 → 50k calls
# before $50 budget; checking every 100 gives <0.2% budget overshoot which
# is acceptable. Every-10 was tried but pushed P95 over 2ms on Windows
# OneDrive due to llm_cost.jsonl scan cost (Stage 6 LIVE test verified).
_BUDGET_CHECK_EVERY = 100  # lazy: budget check every N log() calls (RP-7 hot-path)
_DEFAULT_BUDGET_USD = 50.0  # fallback when task_spec.cost_budget_usd missing
_WARN_THRESHOLD = 0.80
_BLOCK_THRESHOLD = 1.00
# In-memory dedup: last-emitted state per task_id (process-local).
# Process-local means a subprocess re-emits warn at restart; acceptable
# because flag-file persistence carries the BLOCK state across processes.
_TASK_BUDGET_STATE: Dict[str, str] = {}

_call_count = 0


def _emit_trace(event: str, payload: Dict[str, Any]) -> None:
    """Best-effort trace emit; swallow errors (hot-path resilience)."""
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


def _compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Compute USD cost. Unknown model → 0.0 + trace warn."""
    rates = _RATES.get(model)
    if rates is None:
        _emit_trace("cost_meter_unknown_model", {"model": model[:80]})
        return 0.0
    return prompt_tokens * rates["input"] + completion_tokens * rates["output"]


def _rotate_if_needed() -> None:
    """Lazy rotation: rename to .YYYY-MM-DD.jsonl when size >= 10 MB.

    Called every _ROTATE_CHECK_EVERY log() invocations. Defends against
    cross-process race via os.replace + FileNotFoundError catch.
    """
    try:
        size = _LOG_PATH.stat().st_size
    except OSError:
        return
    if size < _ROTATE_BYTES:
        return
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    target = _LOG_PATH.with_suffix(f".{today}.jsonl")
    try:
        os.replace(str(_LOG_PATH), str(target))
        _emit_trace("cost_meter_rotated", {"to": target.name, "size_bytes": size})
    except (FileNotFoundError, OSError) as e:
        # cross-process race: another rotator already renamed → no-op
        _emit_trace("cost_meter_rotate_race", {"err": type(e).__name__})


def log(
    op: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    *,
    task_id: Optional[str] = None,
    **extra: Any,
) -> None:
    """Append a cost-meter entry. Append-only; hot-path safe.

    Args:
        op: operation name (e.g. "retain", "recall", "reflect")
        model: model identifier (matches _RATES keys for cost computation)
        prompt_tokens: input token count
        completion_tokens: output token count
        task_id: optional task_id for per-task budget tracking (U12-B). When
            provided, the entry is tagged at top level (not in extra) so
            check_budget(tid) can filter cleanly without nested-dict reads.
        **extra: extra metadata (logged but not part of required schema)
    """
    global _call_count
    _call_count += 1

    entry: Dict[str, Any] = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "op": op,
        "model": model,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "est_cost_usd": _compute_cost(model, int(prompt_tokens), int(completion_tokens)),
    }
    if task_id:
        entry["task_id"] = task_id
    if extra:
        entry["extra"] = extra

    # Lazy rotation check
    if _call_count % _ROTATE_CHECK_EVERY == 0:
        _rotate_if_needed()

    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # NOTE: Skip trace emit on success hot path (the JSONL entry IS the
        # log; emitting `cost_recorded` doubles file I/O at ~4ms / call,
        # blowing AC-5 P95 budget). Only emit on rotation/error/unknown.
    except OSError as e:
        _emit_trace("cost_meter_log_failed", {"err": str(e)[:200]})

    # U12-B lazy budget check (every Nth call). Only when task_id provided.
    if task_id and _call_count % _BUDGET_CHECK_EVERY == 0:
        try:
            _check_budget_after_log(task_id)
        except Exception:
            # hot-path resilience: never let budget check break log()
            pass


# --------------------------------------------------------------------------
# U12-B Budget guard layer
# --------------------------------------------------------------------------

_BUDGET_CACHE: Dict[str, Any] = {"val": None, "ts": 0.0}
_BUDGET_CACHE_TTL = 60.0  # seconds; task_spec.cost_budget_usd rarely changes


def _read_task_spec_budget() -> float:
    """Read cost_budget_usd from canonical task_spec.json. Default 50.0.

    Per HARD RULE feedback_state_file_dual_path_discovery (2026-04-27 U12 lesson):
    only the canonical memexa/memexa/data/task_spec.json path is authoritative.
    The legacy memexa/data/ shadow was deleted 2026-04-28 (tech debt cleanup
    autopilot 20260427_153149_tech_debt_B_) so dual-path drift is no longer
    possible. persistent_mode.py is the sole writer.

    Cached with 60s TTL — task_spec.cost_budget_usd rarely changes, and the
    hot-path budget check (every 10th log() call) cannot afford a disk read
    each time on Windows OneDrive (~5-10ms per stat()). Per HARD RULE
    feedback_hot_path_no_trace_emit, hot-path overhead must stay under P95<2ms.
    """
    import time as _t
    now = _t.time()
    if _BUDGET_CACHE["val"] is not None and (now - _BUDGET_CACHE["ts"]) < _BUDGET_CACHE_TTL:
        return _BUDGET_CACHE["val"]
    spec_path = Path(__file__).resolve().parents[1] / "data" / "task_spec.json"
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _BUDGET_CACHE["val"] = _DEFAULT_BUDGET_USD
        _BUDGET_CACHE["ts"] = now
        return _DEFAULT_BUDGET_USD
    val = data.get("cost_budget_usd", _DEFAULT_BUDGET_USD)
    try:
        out = float(val)
    except (TypeError, ValueError):
        out = _DEFAULT_BUDGET_USD
    _BUDGET_CACHE["val"] = out
    _BUDGET_CACHE["ts"] = now
    return out


def _resolve_task_dir(task_id: str) -> Optional[Path]:
    """Resolve workspace task_dir. Returns None if absent. Reparse-point-safe.

    Lookup order (per HARD RULE feedback_value_resolution_chain_explicit):
    Tier-1: env MEMEXA_TASK_DIR (if set + dir exists)
    Tier-2: <workspace>/.claude/harness/tasks/<task_id>/
    Tier-3: None (skip flag-file write)
    """
    env_path = os.environ.get("MEMEXA_TASK_DIR")
    if env_path:
        p = Path(env_path)
        if p.is_dir():
            return p
    # Workspace anchor: parents[3] of this file = workspace root
    # (memexa/core/cost_meter.py → memexa → workspace)
    workspace = Path(__file__).resolve().parents[3]
    candidate = workspace / ".claude" / "harness" / "tasks" / task_id
    if candidate.is_dir():
        return candidate
    return None


def _spent_for_task(tid: str) -> float:
    """Sum est_cost_usd over llm_cost.jsonl entries with task_id == tid."""
    if not _LOG_PATH.exists():
        return 0.0
    total = 0.0
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("task_id") == tid:
                    total += float(e.get("est_cost_usd", 0.0))
    except OSError:
        return total
    return total


def check_budget(tid: str) -> Dict[str, Any]:
    """Public API: return per-task budget state dict.

    Returns:
        {
          "task_id": tid,
          "spent_usd": float,
          "budget_usd": float (from task_spec or _DEFAULT_BUDGET_USD),
          "pct": float (0.0..),
          "state": "ok" | "warn80" | "block100",
        }

    Schema contract (per HARD RULE feedback_writer_reader_schema_contract):
    callers MUST use these key names exactly; do not rename.
    """
    spent = _spent_for_task(tid)
    budget = _read_task_spec_budget()
    pct = (spent / budget) if budget > 0 else 0.0
    if pct >= _BLOCK_THRESHOLD:
        state = "block100"
    elif pct >= _WARN_THRESHOLD:
        state = "warn80"
    else:
        state = "ok"
    return {
        "task_id": tid,
        "spent_usd": spent,
        "budget_usd": budget,
        "pct": pct,
        "state": state,
    }


def _write_block_flag(tid: str) -> None:
    """Atomically write <task_dir>/cost_budget_blocked. tmp+os.replace.

    Idempotent: re-writing same content is fine; pretool_gate Rule 12 only
    checks for file existence, not contents.
    """
    task_dir = _resolve_task_dir(tid)
    if task_dir is None:
        # Tier-3: silently skip; no flag file. pretool_gate then can't
        # block; budget exceedance still observable via trace events.
        return
    flag = task_dir / "cost_budget_blocked"
    tmp = task_dir / "cost_budget_blocked.tmp"
    ts_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    content = f"{ts_iso}\n{tid}\n"
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(flag))
    except OSError as e:
        _emit_trace("cost_budget_flag_write_failed", {"err": str(e)[:200], "task_id": tid})


def _check_budget_after_log(tid: str) -> None:
    """Internal: called by log() lazy hook. Emits trace events on threshold cross.

    One-shot dedup via _TASK_BUDGET_STATE: only emits when state TRANSITIONS
    upward (ok→warn80 or →block100). Repeat states are silent.
    """
    snap = check_budget(tid)
    new_state = snap["state"]
    prev_state = _TASK_BUDGET_STATE.get(tid, "ok")
    if new_state == prev_state:
        return
    # State changed; record + emit
    _TASK_BUDGET_STATE[tid] = new_state
    payload = {
        "task_id": tid,
        "spent_usd": round(snap["spent_usd"], 6),
        "budget_usd": snap["budget_usd"],
        "pct": round(snap["pct"], 4),
        "prev_state": prev_state,
    }
    if new_state == "warn80":
        _emit_trace("cost_budget_warn", payload)
    elif new_state == "block100":
        _emit_trace("cost_budget_block", payload)
        _write_block_flag(tid)


def summary_by_task(tid: str) -> Dict[str, Any]:
    """Public API: per-task summary (CLI consumer).

    Returns dict with totals + by_model_op breakdown + budget state.
    Empty (no entries) → returns budget snapshot with note='no entries'.
    """
    if not _LOG_PATH.exists():
        return {
            "task_id": tid,
            "totals": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "est_cost_usd": 0.0},
            "by_model_op": {},
            "budget": check_budget(tid),
            "note": "no entries (llm_cost.jsonl absent)",
        }
    by_key: Dict[str, Dict[str, Any]] = {}
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "est_cost_usd": 0.0}
    found = False
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("task_id") != tid:
                    continue
                found = True
                key = f"{e.get('model', 'unknown')}::{e.get('op', 'unknown')}"
                bucket = by_key.setdefault(key, {
                    "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "est_cost_usd": 0.0,
                })
                bucket["calls"] += 1
                bucket["prompt_tokens"] += e.get("prompt_tokens", 0)
                bucket["completion_tokens"] += e.get("completion_tokens", 0)
                bucket["est_cost_usd"] += e.get("est_cost_usd", 0.0)
                totals["calls"] += 1
                totals["prompt_tokens"] += e.get("prompt_tokens", 0)
                totals["completion_tokens"] += e.get("completion_tokens", 0)
                totals["est_cost_usd"] += e.get("est_cost_usd", 0.0)
    except OSError:
        pass
    out: Dict[str, Any] = {
        "task_id": tid,
        "totals": totals,
        "by_model_op": by_key,
        "budget": check_budget(tid),
    }
    if not found:
        out["note"] = "no entries for this task_id"
    return out


def daily(json_out: bool = False) -> Dict[str, Any]:
    """Read JSONL + group by (model, op) + return summary.

    Returns dict {date_range, by_model_op, totals}.
    """
    if not _LOG_PATH.exists():
        summary: Dict[str, Any] = {
            "date_range": None,
            "by_model_op": {},
            "totals": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "est_cost_usd": 0.0},
            "note": "no llm_cost.jsonl yet",
        }
        return summary

    by_key: Dict[str, Dict[str, Any]] = {}
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "est_cost_usd": 0.0}
    min_ts: Optional[str] = None
    max_ts: Optional[str] = None
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = f"{e.get('model', 'unknown')}::{e.get('op', 'unknown')}"
                bucket = by_key.setdefault(key, {
                    "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "est_cost_usd": 0.0,
                })
                bucket["calls"] += 1
                bucket["prompt_tokens"] += e.get("prompt_tokens", 0)
                bucket["completion_tokens"] += e.get("completion_tokens", 0)
                bucket["est_cost_usd"] += e.get("est_cost_usd", 0.0)
                totals["calls"] += 1
                totals["prompt_tokens"] += e.get("prompt_tokens", 0)
                totals["completion_tokens"] += e.get("completion_tokens", 0)
                totals["est_cost_usd"] += e.get("est_cost_usd", 0.0)
                ts = e.get("ts", "")
                if ts:
                    if min_ts is None or ts < min_ts:
                        min_ts = ts
                    if max_ts is None or ts > max_ts:
                        max_ts = ts
    except OSError:
        pass

    return {
        "date_range": {"min_ts": min_ts, "max_ts": max_ts},
        "by_model_op": by_key,
        "totals": totals,
    }


_USAGE = (
    "Usage:\n"
    "  python -m src.core.cost_meter daily [--json]\n"
    "  python -m src.core.cost_meter summary <task_id> [--json]\n"
)


def main() -> int:
    """CLI entry: python -m src.core.cost_meter <subcmd>"""
    args = sys.argv[1:]
    if not args:
        print(_USAGE, file=sys.stderr)
        return 64
    sub = args[0]
    json_mode = "--json" in args

    if sub == "daily":
        summary = daily()
        if json_mode:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        print("LLM Cost Summary (approximate; rates as of 2026-04-27)")
        print(f"  date range: {summary['date_range']}")
        print(f"  total calls: {summary['totals']['calls']}")
        print(f"  prompt tokens: {summary['totals']['prompt_tokens']}")
        print(f"  completion tokens: {summary['totals']['completion_tokens']}")
        print(f"  est cost USD: {summary['totals']['est_cost_usd']:.4f}")
        print("  By model::op:")
        for key, b in sorted(summary["by_model_op"].items()):
            print(f"    {key}: calls={b['calls']} cost=${b['est_cost_usd']:.4f}")
        return 0

    if sub == "summary":
        if len(args) < 2 or args[1].startswith("--"):
            print("Usage: cost_meter summary <task_id> [--json]", file=sys.stderr)
            return 64
        tid = args[1]
        out = summary_by_task(tid)
        if json_mode:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        b = out["budget"]
        print(f"Per-task summary (task_id={tid})")
        print(f"  spent_usd: ${b['spent_usd']:.4f}")
        print(f"  budget_usd: ${b['budget_usd']:.2f}")
        print(f"  pct: {b['pct']*100:.1f}%")
        print(f"  state: {b['state']}")
        print(f"  calls: {out['totals']['calls']}")
        if "note" in out:
            print(f"  note: {out['note']}")
        if out["by_model_op"]:
            print("  By model::op:")
            for key, bb in sorted(out["by_model_op"].items()):
                print(f"    {key}: calls={bb['calls']} cost=${bb['est_cost_usd']:.4f}")
        return 0

    print(_USAGE, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
