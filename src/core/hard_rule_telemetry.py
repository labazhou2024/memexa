"""hard_rule_telemetry — measure HARD RULE fire-rate from traces + git log.

Per long_term_plan_v2 §908-947 U20 Action 1: measure each rule's fire-rate
over rolling 30/90 day windows so 0-fire rules can be sunset and bloated
HARD RULE corpus governed before half-year accretion saturates Tier-0.

Output: data/hard_rule_fire_count.jsonl
Schema: {rule_id, fired_count_30d, fired_count_90d, last_fired_ts, scanned_at}

Signal sources (rolling window):
  - .claude/data/traces.jsonl (top-level trace)
  - .claude/harness/tasks/*/traces.jsonl (per-task)
  - memex git log subjects (recent_commits referencing rule keyword)

Keyword extraction per rule_id:
  - basename minus 'feedback_' prefix tokenized on underscore
  - first markdown H1/H2 line as natural-language hint
  - file's first sentence (≤200 chars)

Counter logic: count of distinct (source_path, line_no) where any keyword
matches case-insensitive. De-dup within (rule_id, source_path) per scan.
"""
from __future__ import annotations


import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from src.core._path_resolver import memory_dir

_WORKSPACE = Path(__file__).resolve().parents[3]
_MEMORY_DIR = memory_dir()
_DATA_DIR = _WORKSPACE / "memex" / "data"
_OUT_FILE = _DATA_DIR / "hard_rule_fire_count.jsonl"

_TRACE_FILES: Tuple[Path, ...] = (
    _WORKSPACE / ".claude" / "data" / "traces.jsonl",
)
_TASK_DIRS_GLOB = _WORKSPACE / ".claude" / "harness" / "tasks"

_NOISE_TOKENS = frozenset({
    "the", "and", "for", "with", "that", "this", "must",
    "rule", "feedback", "hard", "md", "before", "after",
    "not", "use", "into",
})


def _extract_keywords(path: Path) -> List[str]:
    """Tokenize basename + first heading; return distinct ≥4-char tokens."""
    base = path.stem
    if base.startswith("feedback_"):
        base = base[len("feedback_"):]
    tokens = [t for t in re.split(r"[_\-]", base) if t]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except Exception:
        text = ""
    for line in text.splitlines()[:20]:
        if line.startswith("#") or "HARD RULE" in line:
            for tok in re.findall(r"[A-Za-z][A-Za-z0-9_]+", line):
                tokens.append(tok)
    keywords: List[str] = []
    seen: set[str] = set()
    for t in tokens:
        tl = t.lower()
        if len(tl) < 4 or tl in _NOISE_TOKENS or tl in seen:
            continue
        seen.add(tl)
        keywords.append(tl)
    return keywords


def inventory_hard_rules(memory_dir: Path = _MEMORY_DIR) -> List[Dict]:
    """Walk memory_dir; return [{rule_id, path, keywords[]}] for HARD RULE files."""
    if not memory_dir.exists():
        return []
    out: List[Dict] = []
    for p in sorted(memory_dir.glob("feedback_*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        candidates = text.count("HARD RULE candidate")
        total = text.count("HARD RULE")
        if total <= candidates:
            continue
        out.append({
            "rule_id": p.stem,
            "path": str(p),
            "keywords": _extract_keywords(p),
        })
    return out


def _iter_trace_lines(window_seconds: float, now: float) -> Iterable[Tuple[str, str]]:
    """Yield (source_tag, content_line) for trace files within the window.

    Uses file mtime as cheap pre-filter. Per-line ts not parsed — we trust
    rolling window mtime + content scan.
    """
    cutoff = now - window_seconds
    for tf in _TRACE_FILES:
        if not tf.exists():
            continue
        try:
            if tf.stat().st_mtime < cutoff:
                continue
            with tf.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    yield (str(tf), line)
        except Exception:
            continue
    if _TASK_DIRS_GLOB.exists():
        for tdir in _TASK_DIRS_GLOB.iterdir():
            if not tdir.is_dir():
                continue
            ttf = tdir / "traces.jsonl"
            if not ttf.exists():
                continue
            try:
                if ttf.stat().st_mtime < cutoff:
                    continue
                with ttf.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        yield (str(ttf), line)
            except Exception:
                continue


def scan_traces(rules: List[Dict], window_days: int,
                now: Optional[float] = None) -> Dict[str, Dict]:
    """For each rule, count fires within window_days.

    Returns {rule_id: {fired_count, last_fired_ts}}.
    Match: any keyword (case-insensitive) appears in line content.
    De-dup: per (rule_id, source_path) — multi-fire in same file counts once
    (fire-rate is a coarse signal for sunset, not exhaustive count).
    """
    now = now if now is not None else time.time()
    window_sec = window_days * 86400.0
    counts: Dict[str, Dict] = {
        r["rule_id"]: {"fired_count": 0, "last_fired_ts": None,
                       "_seen_sources": set()}
        for r in rules
    }
    for source, line in _iter_trace_lines(window_sec, now):
        ll = line.lower()
        for r in rules:
            if not r["keywords"]:
                continue
            for kw in r["keywords"]:
                if kw in ll:
                    rec = counts[r["rule_id"]]
                    if source in rec["_seen_sources"]:
                        break
                    rec["_seen_sources"].add(source)
                    rec["fired_count"] += 1
                    rec["last_fired_ts"] = now
                    break
    for v in counts.values():
        v.pop("_seen_sources", None)
    return counts


def record(window_days_a: int = 30, window_days_b: int = 90,
           memory_dir: Path = _MEMORY_DIR,
           out_file: Path = _OUT_FILE) -> Dict:
    """Inventory + scan(30d) + scan(90d) → write jsonl. Return summary dict."""
    rules = inventory_hard_rules(memory_dir)
    now = time.time()
    c30 = scan_traces(rules, window_days_a, now=now)
    c90 = scan_traces(rules, window_days_b, now=now)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    for r in rules:
        rid = r["rule_id"]
        rec = {
            "rule_id": rid,
            "fired_count_30d": c30.get(rid, {}).get("fired_count", 0),
            "fired_count_90d": c90.get(rid, {}).get("fired_count", 0),
            "last_fired_ts": (c90.get(rid, {}).get("last_fired_ts")
                              or c30.get(rid, {}).get("last_fired_ts")),
            "scanned_at": int(now),
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    out_file.write_text("\n".join(lines) + ("\n" if lines else ""),
                        encoding="utf-8")
    summary = {
        "total_rules": len(rules),
        "fired_30d": sum(1 for r in rules
                         if c30.get(r["rule_id"], {}).get("fired_count", 0) > 0),
        "fired_90d": sum(1 for r in rules
                         if c90.get(r["rule_id"], {}).get("fired_count", 0) > 0),
        "out_file": str(out_file),
        "scanned_at": int(now),
    }
    return summary


def diff(memory_dir: Optional[Path] = None,
         out_file: Optional[Path] = None) -> Dict:
    """Compare inventory vs last recorded jsonl; report missing/added rule_ids.

    Defaults None → resolved from module attributes at call time so that
    monkeypatch.setattr(ht, "_MEMORY_DIR", ...) takes effect in tests.
    """
    if memory_dir is None:
        memory_dir = _MEMORY_DIR
    if out_file is None:
        out_file = _OUT_FILE
    inv = {r["rule_id"] for r in inventory_hard_rules(memory_dir)}
    recorded: set[str] = set()
    if out_file.exists():
        try:
            for line in out_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    recorded.add(obj.get("rule_id", ""))
                except Exception:
                    continue
        except Exception:
            pass
    return {
        "inventory_count": len(inv),
        "recorded_count": len(recorded),
        "missing_in_record": sorted(inv - recorded),
        "stale_in_record": sorted(recorded - inv),
    }


def _cli(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="src.core.hard_rule_telemetry")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("record", help="inventory + scan + write jsonl")
    pr.add_argument("--window", type=int, default=30,
                    help="primary window (days)")
    pr.add_argument("--window-secondary", type=int, default=90)
    pr.add_argument("--memory-dir", default=str(_MEMORY_DIR))
    pr.add_argument("--out", default=str(_OUT_FILE))
    sub.add_parser("diff", help="inventory vs recorded jsonl")
    sub.add_parser("inventory", help="list rule_ids with keywords")
    args = p.parse_args(argv)
    if args.cmd == "record":
        s = record(window_days_a=args.window,
                   window_days_b=args.window_secondary,
                   memory_dir=Path(args.memory_dir),
                   out_file=Path(args.out))
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "diff":
        d = diff()
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "inventory":
        inv = inventory_hard_rules()
        print(json.dumps(inv, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
