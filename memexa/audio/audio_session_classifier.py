"""Auto-classify an audio session as active/passive/unknown for the
extraction pipeline. Used by v5_audio_batch_builder when session_speakers.yaml
has no explicit `passive_listener_session` flag.

Goal: in cron-driven autonomous mode (no human supervision), default to the
SAFEST attribution policy. Mis-attributing teacher's '我' to the CEO pollutes
the memory graph, so we err on passive_listener=true when uncertain.

Signals used (all derivable from transcript.jsonl + filename + clock):
  1. Time-of-day: weekday 8:00-18:00 → likely class/work
  2. Duration: >60 min → likely lecture/meeting
  3. Speaker dominance: one spk_local with >60% utts → monologue/lecture
  4. Long monologues: consecutive same-spk stretches >2 min → lecture
  5. Turn-taking ratio: low (>10:1 in any direction) → unilateral

The classifier returns one of:
  - "passive"  → CEO is audience; LLM should NOT auto-attribute '我' to is_self
  - "active"   → CEO is participant; standard known_speakers attribution OK
  - "unknown"  → safest default = treat as "passive"

When session_speakers.yaml has explicit `passive_listener_session: true/false`,
that wins. Auto-classifier only fires for sessions WITHOUT explicit config.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List


_PASSIVE_RATIO_THRESHOLD = 0.55     # top speaker must be ≥55% to count as monologue
_LONG_MONOLOGUE_SEC = 120.0          # 2 min continuous same-spk
_LONG_MONOLOGUE_FRACTION = 0.30      # ≥30% of session in long monologues
_PASSIVE_DURATION_MIN = 30.0         # session >30min is candidate for passive


def classify_session(
    *,
    transcript_path: Path,
    recording_start: dt.datetime | None = None,
    session_duration_s: float | None = None,
) -> Dict[str, Any]:
    """Return classification dict with verdict + signal evidence."""
    if not transcript_path.exists():
        return {"verdict": "unknown", "reason": "no transcript"}

    utts: List[Dict[str, Any]] = []
    with transcript_path.open(encoding="utf-8") as fp:
        for line in fp:
            try:
                utts.append(json.loads(line))
            except Exception:
                continue
    if not utts:
        return {"verdict": "unknown", "reason": "empty transcript"}

    # Compute signals
    spk_count = Counter(u.get("spk_local") or "spk_?" for u in utts)
    total_utts = len(utts)
    top_spk, top_n = spk_count.most_common(1)[0]
    top_ratio = top_n / total_utts

    # Speech duration by speaker
    spk_dur: Dict[str, float] = {}
    for u in utts:
        s = u.get("spk_local") or "spk_?"
        spk_dur[s] = spk_dur.get(s, 0.0) + float(u.get("duration", 0.0))
    total_speech_s = sum(spk_dur.values())
    top_dur_ratio = (spk_dur.get(top_spk, 0.0) / total_speech_s
                     if total_speech_s > 0 else 0.0)

    # Long-monologue detection — consecutive same-spk stretches
    long_mono_s = 0.0
    if len(utts) > 1:
        sorted_utts = sorted(utts, key=lambda u: u.get("ts_start", 0))
        cur_spk = sorted_utts[0].get("spk_local")
        cur_start = sorted_utts[0].get("ts_start", 0)
        cur_end = sorted_utts[0].get("ts_end", 0)
        for u in sorted_utts[1:]:
            if u.get("spk_local") == cur_spk:
                cur_end = u.get("ts_end", cur_end)
            else:
                if cur_end - cur_start >= _LONG_MONOLOGUE_SEC:
                    long_mono_s += cur_end - cur_start
                cur_spk = u.get("spk_local")
                cur_start = u.get("ts_start", 0)
                cur_end = u.get("ts_end", 0)
        if cur_end - cur_start >= _LONG_MONOLOGUE_SEC:
            long_mono_s += cur_end - cur_start
    long_mono_ratio = long_mono_s / total_speech_s if total_speech_s > 0 else 0.0

    # Time-of-day signal
    is_work_hours = False
    is_weekday = False
    if recording_start:
        try:
            is_weekday = recording_start.weekday() < 5
            h = recording_start.hour
            is_work_hours = 8 <= h < 21
        except Exception:
            pass

    # Duration signal
    if session_duration_s is None:
        session_duration_s = max(u.get("ts_end", 0) for u in utts) if utts else 0.0
    duration_min = session_duration_s / 60.0

    signals = {
        "total_utts": total_utts,
        "duration_min": round(duration_min, 1),
        "top_speaker": top_spk,
        "top_speaker_utt_ratio": round(top_ratio, 3),
        "top_speaker_dur_ratio": round(top_dur_ratio, 3),
        "long_monologue_sec": round(long_mono_s, 1),
        "long_monologue_ratio": round(long_mono_ratio, 3),
        "n_speakers_detected": len(spk_count),
        "is_work_hours": is_work_hours,
        "is_weekday": is_weekday,
        "recording_start": recording_start.isoformat() if recording_start else None,
    }

    # Verdict logic
    if duration_min < 5:
        signals["reason"] = "too short to classify (<5min)"
        return {"verdict": "unknown", **signals}

    # PASSIVE = long monologues + duration + (or) work hours
    passive_score = 0
    if duration_min > _PASSIVE_DURATION_MIN:
        passive_score += 1
    if top_dur_ratio >= _PASSIVE_RATIO_THRESHOLD:
        passive_score += 2  # strongest signal
    if long_mono_ratio >= _LONG_MONOLOGUE_FRACTION:
        passive_score += 2
    if is_weekday and is_work_hours:
        passive_score += 1

    # ACTIVE = balanced dialog + short
    active_score = 0
    if len(spk_count) >= 2 and 0.3 < top_ratio < 0.65:
        active_score += 2  # balanced 2-party
    if duration_min < 30:
        active_score += 1
    if long_mono_ratio < 0.15:
        active_score += 1

    if passive_score >= 3 and passive_score > active_score + 1:
        verdict = "passive"
        reason = (f"long session ({duration_min:.0f}min) + "
                  f"top spk {top_dur_ratio:.0%} dominant + "
                  f"long-monologue {long_mono_ratio:.0%}")
    elif active_score >= 3 and active_score > passive_score + 1:
        verdict = "active"
        reason = (f"balanced {top_dur_ratio:.0%} top spk + "
                  f"short {duration_min:.0f}min + low monologue")
    else:
        # Uncertain → safest is passive (conservative attribution)
        verdict = "passive"
        reason = (f"ambiguous (passive={passive_score} active={active_score}); "
                  f"defaulting to passive for safety")

    return {"verdict": verdict, "reason": reason,
            "passive_score": passive_score, "active_score": active_score,
            **signals}


if __name__ == "__main__":
    # CLI: classify a session
    import argparse, sys
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--session-dir", required=True, type=Path)
    p.add_argument("--recording-start", default=None)
    args = p.parse_args()
    rs = (dt.datetime.fromisoformat(args.recording_start).astimezone()
          if args.recording_start else None)
    r = classify_session(transcript_path=args.session_dir / "transcript.jsonl",
                          recording_start=rs)
    print(json.dumps(r, ensure_ascii=False, indent=2))
