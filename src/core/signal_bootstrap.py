"""
Signal Bootstrap (v1, 2026-04-20)

CEO bulk-rating bootstrap for the improvement-patterns knowledge base.
Provides a structured way for the CEO to rate 30 candidate patterns
(20 ranker-surfaced + 10 uniform-random) to bootstrap the reward signal.

Design (AC-15c paired condition):
  natural_rating_fraction >= 0.50 AND natural_rating_count >= 10
  Both must be met — count floor guards against a tiny-sample fraction fluke.

Storage:
  Ratings written to memexa/memexa/data/ratings.jsonl (JSONL per line):
  {
    "ts":         ISO-8601 naive UTC,
    "pattern_id": str,
    "score":      int (1-5),
    "source":     "bootstrap" | "natural",
    "bootstrap_round": "YYYY-MM-DD" (only present for bootstrap source)
  }

  Also written via ceo_feedback.record_feedback() so the trace_sink
  reward-signal pipeline sees the ratings.

CLI:
  python -m src.core.signal_bootstrap export     # JSON to stdout
  python -m src.core.signal_bootstrap grid       # interactive rating
  python -m src.core.signal_bootstrap readiness  # AC-15 status
"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Literal, Optional


# ---------------------------------------------------------------------------
# Data directory resolution (mirrors pattern_extractor logic)
# ---------------------------------------------------------------------------

def _resolve_data_dir() -> Path:
    """Resolve data dir. Respects MEMEXA_DATA_DIR env var for test isolation."""
    default = Path(__file__).parent.parent / "data"
    env_override = os.environ.get("MEMEXA_DATA_DIR")
    if env_override:
        try:
            p = Path(env_override).resolve()
            if not p.is_dir():
                return default
            workspace_root = Path(__file__).parent.parent.parent.parent.resolve()
            import tempfile as _t
            temp_root = Path(_t.gettempdir()).resolve()
            try:
                p.relative_to(workspace_root)
                return p
            except ValueError:
                pass
            try:
                p.relative_to(temp_root)
                return p
            except ValueError:
                pass
            return default
        except (OSError, ValueError):
            return default
    return default


_DATA_DIR = _resolve_data_dir()
_RATINGS_FILE = _DATA_DIR / "ratings.jsonl"


# ---------------------------------------------------------------------------
# export_candidates
# ---------------------------------------------------------------------------

def export_candidates(
    n_ranker: int = 20,
    n_random: int = 10,
    patterns_file: Optional[Path] = None,
) -> List[dict]:
    """Export candidates for CEO bulk rating.

    Returns list of dicts with keys:
      id, content_preview, source, last_hit_ts, rank_reason

    n_ranker: top-N ranker-surfaced by (usage_count desc, age asc)
    n_random: uniform-random from the remaining pool; seed is fixed
              to date (YYYY-MM-DD) for reproducibility within a day.
    """
    from src.core.pattern_extractor import load_all_patterns

    data_dir = _DATA_DIR if patterns_file is None else patterns_file.parent
    # Honour test overrides: if patterns_file given, patch loader path temporarily
    all_patterns = _load_patterns_from(patterns_file)

    if not all_patterns:
        return []

    # Build sortable representation
    def _entry_age_ts(e) -> float:
        """Parse created_at -> float epoch for sort (lower = older)."""
        try:
            return datetime.fromisoformat(
                e.created_at.replace("Z", "")
            ).timestamp()
        except Exception:
            return 0.0

    # Sort: usage_count desc, then created_at asc (older = higher priority)
    sorted_all = sorted(
        all_patterns,
        key=lambda e: (-e.usage_count, _entry_age_ts(e)),
    )

    # Ranker-surfaced: top n_ranker
    ranker_slice = sorted_all[:n_ranker]
    ranker_ids = {e.id for e in ranker_slice}

    # Remaining pool for random sampling
    remaining = [e for e in sorted_all[n_ranker:] if e.id not in ranker_ids]

    # Fixed seed = today's date string for reproducibility within a day
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    rng = random.Random(today_str)
    random_slice = rng.sample(remaining, min(n_random, len(remaining)))

    candidates = []
    for entry in ranker_slice:
        candidates.append(_to_candidate(entry, rank_reason="top_usage"))
    for entry in random_slice:
        candidates.append(_to_candidate(entry, rank_reason="uniform_random"))

    return candidates


def _load_patterns_from(patterns_file: Optional[Path]):
    """Load patterns, optionally from an explicit file path (for tests)."""
    if patterns_file is None:
        from src.core.pattern_extractor import load_all_patterns
        return load_all_patterns()

    # Load directly from the given file (test isolation)
    if not patterns_file.exists():
        return []
    from src.core.pattern_extractor import PatternEntry
    entries = []
    for line in patterns_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            entry = PatternEntry(**{
                k: v for k, v in data.items()
                if k in PatternEntry.__dataclass_fields__
            })
            entries.append(entry)
        except Exception:
            pass
    return entries


def _to_candidate(entry, rank_reason: str) -> dict:
    """Convert a PatternEntry to a candidate dict for export."""
    preview_src = (entry.fact or "") + " " + (entry.recommendation or "")
    preview = preview_src[:70].strip()
    return {
        "id": entry.id,
        "content_preview": preview,
        "source": getattr(entry, "source", "auto_extracted"),
        "last_hit_ts": getattr(entry, "last_hit_ts", None),
        "rank_reason": rank_reason,
    }


# ---------------------------------------------------------------------------
# record_bootstrap_rating
# ---------------------------------------------------------------------------

def record_bootstrap_rating(
    pattern_id: str,
    score: int,
    source: Literal["bootstrap", "natural"] = "bootstrap",
) -> bool:
    """Record a CEO rating for a pattern.

    Writes to ratings.jsonl (JSONL, one record per line).
    Also calls ceo_feedback.record_feedback() to feed the trace_sink pipeline.

    Returns True on success.
    """
    if not pattern_id or not (1 <= score <= 5):
        return False

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    record: dict = {
        "ts": datetime.utcnow().isoformat(timespec="microseconds"),
        "pattern_id": pattern_id,
        "score": score,
        "source": source,
    }
    if source == "bootstrap":
        record["bootstrap_round"] = today_str

    # Write to ratings.jsonl
    ratings_file = _get_ratings_file()
    try:
        ratings_file.parent.mkdir(parents=True, exist_ok=True)
        with open(ratings_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return False

    # Mirror to ceo_feedback / trace_sink
    try:
        from src.core.ceo_feedback import record_feedback
        verdict = "positive" if score >= 4 else "negative" if score <= 2 else "neutral"
        conf = 1.0  # explicit CEO rating is always high-confidence
        record_feedback(
            verdict=verdict,
            confidence=conf,
            reason=f"bootstrap_rating pattern={pattern_id} score={score}",
            source="bootstrap",
            rating_1_5=score,
        )
    except Exception:
        pass  # non-blocking

    return True


def _get_ratings_file() -> Path:
    """Return ratings.jsonl path, respecting env override."""
    env_override = os.environ.get("MEMEXA_RATINGS_FILE")
    if env_override:
        try:
            p = Path(env_override).resolve()
            workspace_root = Path(__file__).parent.parent.parent.parent.resolve()
            import tempfile as _t
            temp_root = Path(_t.gettempdir()).resolve()
            for allowed in (workspace_root, temp_root):
                try:
                    p.relative_to(allowed)
                    return p
                except ValueError:
                    pass
        except Exception:
            pass
    return _RATINGS_FILE


# ---------------------------------------------------------------------------
# natural_rating_fraction / natural_rating_count
# ---------------------------------------------------------------------------

def natural_rating_count(window_days: int = 30) -> int:
    """Count natural (non-bootstrap) ratings within the rolling window.

    M2 fix (2026-04-20): the semantic here is 'rating is natural iff
    source != "bootstrap"'. Previously we filtered on source == "natural",
    but the rest of the codebase (ceo_feedback.record_feedback, implicit
    UserPromptSubmit feedback hooks) never writes 'natural' as a label;
    the default is simply 'no source field' or a non-bootstrap label.
    Counting everything that is NOT bootstrap is backward-compatible
    (old 'natural'-labelled records still count) and correctly includes
    the ceo_feedback-ingested ratings.
    """
    return _count_ratings_not_bootstrap(window_days=window_days)


def natural_rating_fraction(window_days: int = 30) -> float:
    """Fraction of ratings within the window that are natural (= NOT bootstrap).

    M2 fix (2026-04-20): see natural_rating_count for the rationale.
    Returns 0.0 if there are no ratings at all in the window.
    """
    ratings_file = _get_ratings_file()
    if not ratings_file.exists():
        return 0.0

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    total = 0
    natural = 0
    try:
        for line in ratings_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("ts", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", ""))
                        if ts < cutoff:
                            continue
                    except ValueError:
                        pass  # malformed ts: include it
                total += 1
                # M2: natural iff source != "bootstrap". Backward-compat
                # with any existing source == "natural" records.
                if rec.get("source") != "bootstrap":
                    natural += 1
            except json.JSONDecodeError:
                continue
    except OSError:
        return 0.0

    if total == 0:
        return 0.0
    return natural / total


def _count_ratings_not_bootstrap(window_days: int) -> int:
    """Count ratings in the rolling window that are NOT source='bootstrap'."""
    ratings_file = _get_ratings_file()
    if not ratings_file.exists():
        return 0

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    count = 0
    try:
        for line in ratings_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("ts", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", ""))
                        if ts < cutoff:
                            continue
                    except ValueError:
                        pass
                if rec.get("source") != "bootstrap":
                    count += 1
            except json.JSONDecodeError:
                continue
    except OSError:
        return 0
    return count


def _count_ratings(source_filter: Optional[str], window_days: int) -> int:
    """Count ratings in the rolling window, optionally filtered by source.

    Retained for callers that need exact-source-match counts (not used by
    natural_rating_count after M2 fix).
    """
    ratings_file = _get_ratings_file()
    if not ratings_file.exists():
        return 0

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    count = 0
    try:
        for line in ratings_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("ts", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", ""))
                        if ts < cutoff:
                            continue
                    except ValueError:
                        pass
                if source_filter is None or rec.get("source") == source_filter:
                    count += 1
            except json.JSONDecodeError:
                continue
    except OSError:
        return 0
    return count


# ---------------------------------------------------------------------------
# ac_15_readiness_check
# ---------------------------------------------------------------------------

def ac_15_readiness_check(window_days: int = 30) -> dict:
    """Returns AC-15 readiness status.

    AC-15c paired condition:
      natural_rating_fraction >= 0.50 AND natural_rating_count >= 10

    Both must be true for met=True (count floor guards small-sample fluke).

    Returns:
      {met: bool, fraction: float, count: int,
       fraction_target: 0.50, count_target: 10}
    """
    fraction = natural_rating_fraction(window_days=window_days)
    count = natural_rating_count(window_days=window_days)
    fraction_target = 0.50
    count_target = 10
    met = (fraction >= fraction_target) and (count >= count_target)
    return {
        "met": met,
        "fraction": round(fraction, 4),
        "count": count,
        "fraction_target": fraction_target,
        "count_target": count_target,
    }


# ---------------------------------------------------------------------------
# CLI grid (interactive rating)
# ---------------------------------------------------------------------------

def _cmd_export(patterns_file: Optional[Path] = None) -> int:
    """Dump 30 candidates as JSON to stdout."""
    candidates = export_candidates(patterns_file=patterns_file)
    print(json.dumps(candidates, indent=2, ensure_ascii=False))
    return 0


def _cmd_grid(patterns_file: Optional[Path] = None) -> int:
    """Interactive CLI grid: rate 30 candidates one by one."""
    candidates = export_candidates(patterns_file=patterns_file)
    if not candidates:
        print("No candidates to rate. Knowledge base may be empty.", file=sys.stderr)
        return 1

    print(f"\n=== CEO Bootstrap Rating ({len(candidates)} candidates) ===")
    print("Rate each pattern 1-5 (s=skip, q=quit)\n")

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    ratings_batch: List[tuple] = []  # (pattern_id, score)

    for i, cand in enumerate(candidates, 1):
        pid = cand["id"]
        preview = cand["content_preview"][:70]
        rank_reason = cand["rank_reason"]
        source_label = cand["source"]

        prompt_line = (
            f"[{i:02d}/{len(candidates):02d}] id={pid} "
            f"[{rank_reason}] [{source_label}]\n"
            f"  {preview}\n"
            f"  Rate 1-5 (s=skip, q=quit): "
        )
        try:
            raw = input(prompt_line).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            break

        if raw == "q":
            print("Quit early. Ratings recorded so far will be saved.")
            break
        if raw == "s" or raw == "":
            continue
        try:
            score = int(raw)
            if not 1 <= score <= 5:
                print("  (invalid, skipped)")
                continue
        except ValueError:
            print("  (invalid, skipped)")
            continue

        ratings_batch.append((pid, score))

    # Batch-write all ratings
    saved = 0
    for pid, score in ratings_batch:
        if record_bootstrap_rating(pid, score, source="bootstrap"):
            saved += 1

    print(f"\nSaved {saved}/{len(ratings_batch)} ratings (bootstrap_round={today_str})")
    return 0


def _cmd_readiness() -> int:
    """Print AC-15 readiness status."""
    status = ac_15_readiness_check()
    met_str = "YES" if status["met"] else "NO"
    print(f"AC-15 readiness: {met_str}")
    print(f"  natural_rating_fraction : {status['fraction']:.4f} "
          f"(target >= {status['fraction_target']})")
    print(f"  natural_rating_count    : {status['count']} "
          f"(target >= {status['count_target']})")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: python -m src.core.signal_bootstrap "
            "<export|grid|readiness>",
            file=sys.stderr,
        )
        return 1

    cmd = sys.argv[1]
    if cmd == "export":
        return _cmd_export()
    if cmd == "grid":
        return _cmd_grid()
    if cmd == "readiness":
        return _cmd_readiness()

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
