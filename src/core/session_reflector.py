"""
L3 End-of-Session Reflector (Phase 2, 2026-04-18)

Stop hook dispatcher. Fires async subprocess immediately and returns so the
Stop hook never blocks session exit (< 100ms budget).

Two modes:
- dispatch(): called from Stop hook. Does NOT wait for Haiku. Starts a detached
  python subprocess that runs reflect_worker() in the background.
- reflect_worker(): runs in the dispatched subprocess. Reads recent transcript,
  calls Haiku, writes distilled PatternEntry to knowledge base.

Design (per verifier R2):
- `start_new_session=True` on Popen (POSIX) / `CREATE_NEW_PROCESS_GROUP` (Windows)
- Env whitelist via soft_signal_classifier._safe_env()
- PII scrub before Haiku
- Budget cap shared with L2 classifier
- Only triggers when recent signals warrant reflection (error / review reject / long session)
- ENV flag: MEMEXA_L3_REFLECTION=1 default on
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

__all__ = [
    "dispatch",
    "reflect_worker",
    "is_enabled",
    "classify_session_state",
]

# [AC-3 2026-04-19] Resume semantic labels (10 scenarios, plan AC-3 ≥ 8).
SessionStateLabel = Literal[
    "fresh",
    "resumed",
    "stale_1h",
    "stale_24h",
    "aborted",
    "clock_skew",
    "two_terminal",
    "os_sleep",
    "mutated_state",
    "partial_write",
]


_DATA_DIR = Path(__file__).parent.parent / "data"
_LOGS_DIR = _DATA_DIR / "logs"
_REFLECTION_LOG_FMT = "reflection_{sid}.log"


def is_enabled() -> bool:
    """ENV flag. Default on."""
    return os.environ.get("MEMEXA_L3_REFLECTION", "1") == "1"


def _safe_env() -> dict:
    """Whitelist env. Do NOT inherit full os.environ to avoid leaking secrets."""
    whitelist = [
        "PATH", "PYTHONPATH", "ANTHROPIC_API_KEY",
        "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE",
        "MEMEXA_DATA_DIR", "MEMEXA_L2_SOFT_SIGNAL", "MEMEXA_L3_REFLECTION",
        "MEMEXA_HAIKU_DAILY_USD",
    ]
    return {k: v for k, v in os.environ.items() if k in whitelist}


def _worth_reflecting(session_signals: dict) -> bool:
    """Skip reflection if nothing interesting happened (save money).

    Triggers:
    - any tool error in session
    - CHANGES_REQUIRED from any reviewer
    - retry count > 1 on same file
    - session > 20 turns
    """
    if session_signals.get("had_tool_error"):
        return True
    if session_signals.get("review_changes_required"):
        return True
    if session_signals.get("retry_count", 0) > 1:
        return True
    if session_signals.get("turn_count", 0) > 20:
        return True
    return False


def _gather_session_signals() -> dict:
    """Best-effort: read recent events.jsonl to detect reflection triggers."""
    signals = {
        "had_tool_error": False,
        "review_changes_required": False,
        "retry_count": 0,
        "turn_count": 0,
    }
    try:
        events_file = _DATA_DIR / "events.jsonl"
        if not events_file.exists():
            return signals
        # Read last 100 events only (keep fast)
        with open(events_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-100:]
        for line in lines:
            try:
                ev = json.loads(line)
                t = ev.get("type", "")
                if "error" in t.lower() or "failure" in t.lower():
                    signals["had_tool_error"] = True
                if "changes_required" in str(ev.get("details", {})).lower():
                    signals["review_changes_required"] = True
                if t == "retry_triggered":
                    signals["retry_count"] = signals.get("retry_count", 0) + 1
                if t == "user_prompt_submit":
                    signals["turn_count"] = signals.get("turn_count", 0) + 1
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception:
        pass
    return signals


_WORKSPACE_ROOT = Path(__file__).parent.parent.parent.parent
_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _under(candidate: Path, root: Path) -> bool:
    """Return True iff realpath(candidate) is inside realpath(root)."""
    try:
        cr = Path(os.path.realpath(str(candidate)))
        rr = Path(os.path.realpath(str(root)))
        cr.relative_to(rr)
        return True
    except (ValueError, OSError):
        return False


def _validate_transcript_path(path_str: str) -> str:
    """[SEC-HIGH 2026-04-18] Validate transcript_path.

    Accept path iff:
      (a) under workspace root, OR
      (b) under ~/.claude/projects/<uuid>/ with .jsonl suffix
          (verifier HIGH fix: Windows junction hardening via realpath + uuid regex)

    Always require:
      - is_file() (reject directories)
      - realpath() re-check after first resolve (catches junction chains)

    Returns normalized path string on success, empty string on failure.
    """
    if not path_str:
        return ""
    try:
        p = Path(path_str).resolve()
        if not p.is_file():
            return ""

        # Branch A: inside workspace (legacy)
        if _under(p, _WORKSPACE_ROOT):
            return str(p)

        # Branch B: Claude Code transcripts under ~/.claude/projects/<uuid>/<sid>.jsonl
        if p.suffix.lower() != ".jsonl":
            return ""
        # parent must be a uuid dir (rejects junctions like projects/evil/)
        if not _UUID_RE.match(p.parent.name):
            return ""
        if not _under(p, _CLAUDE_PROJECTS_ROOT):
            return ""
        return str(p)
    except (ValueError, OSError):
        return ""


# ---------------------------------------------------------------------------
# [AC-3 2026-04-19] Resume-semantic classifier (10 scenarios)
#
# Verifier R2 flagged: "旧会话 sid 可能复用 → helpful_count 虚增". Classifier
# tags every reflector entry so downstream counters can skip resumed/stale/
# aborted sids. The dispatcher itself still runs (no behavior change) — this
# adds a DIAGNOSTIC label, not a short-circuit. Future wiring can use the
# label to gate save_patterns() (planned in v3.2).
# ---------------------------------------------------------------------------


def _load_traces_tail(trace_file: Path, n: int = 500) -> List[dict]:
    """Load last n jsonl records from trace_file. Malformed lines skipped."""
    out: List[dict] = []
    if not trace_file.exists():
        return out
    try:
        with open(trace_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _trace_file_for_classify() -> Path:
    """Path resolution mirrors trace_sink._trace_file() without importing it
    (keep classify_session_state self-contained so tests can inject paths).
    """
    override = os.environ.get("MEMEXA_TRACE_FILE")
    if override:
        try:
            p = Path(override).resolve()
            if p.exists() or p.parent.exists():
                return p
        except (OSError, ValueError):
            pass
    return _WORKSPACE_ROOT / ".claude" / "data" / "traces.jsonl"


def _pid_file_for(sid: str) -> Path:
    """Per-sid active-process registry for two-terminal detection."""
    sid_safe = re.sub(r"[^A-Za-z0-9_\-]", "_", sid)[:64]
    return _DATA_DIR / f".session_pids_{sid_safe}.json"


def _count_active_pids(sid: str) -> int:
    """Best-effort: count live PIDs registered for this sid.

    Returns 0 when no registry exists. Stale PIDs (process gone) are pruned
    on read so we don't falsely trip two_terminal on a crashed prior shell.
    """
    pf = _pid_file_for(sid)
    if not pf.exists():
        return 0
    try:
        data = json.loads(pf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(data, list):
        return 0
    alive = 0
    for pid in data:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            continue
        if _pid_alive(pid_int):
            alive += 1
    return alive


def _pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check. Conservative: unknown → dead."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            # signal 0 not supported on Windows; use OpenProcess probe
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError):
        # PermissionError on POSIX means the process exists (owned by another user)
        return isinstance(sys.exc_info()[1], PermissionError)
    except Exception:
        return False


def classify_session_state(
    sid: str,
    current_ts: Optional[float] = None,
    harness_state: Optional[dict] = None,
    trace_file: Optional[Path] = None,
) -> SessionStateLabel:
    """[AC-3] Classify the resume semantic of a session id.

    Ordered detection — first match wins, so earlier checks shadow later ones.
    The order encodes criticality: integrity problems (partial_write,
    mutated_state, clock_skew) beat temporal labels, and two_terminal beats
    plain resumed because concurrent writers are a bigger deal than elapsed
    time.

    Args:
        sid: session id to classify.
        current_ts: posix timestamp to treat as "now" (test injection).
            Defaults to datetime.now(timezone.utc).timestamp().
        harness_state: optional dict loaded from harness_state.json. If
            provided, its top-level 'session_id' field is compared against
            sid for the mutated_state check.
        trace_file: override traces.jsonl path (test injection).

    Returns:
        One of the SessionStateLabel literals. Never raises.

    Detection order:
      1. partial_write    — traces.jsonl last line is not valid JSON
      2. mutated_state    — harness_state['session_id'] != sid
      3. clock_skew       — current_ts < latest recorded ts (NTP rewind)
      4. os_sleep         — gap since last ts > 6h (device suspend)
      5. two_terminal     — ≥ 2 alive PIDs registered for this sid
      6. aborted          — session_start seen but no session_end
      7. fresh            — no trace records mention this sid
      8. stale_24h        — last activity > 24h ago
      9. stale_1h         — last activity 1-24h ago
     10. resumed          — last activity within 1h
    """
    tf = trace_file or _trace_file_for_classify()
    now = current_ts if current_ts is not None else datetime.now(timezone.utc).timestamp()

    # 1. partial_write — only a tail check; must precede anything that parses the file
    try:
        if tf.exists():
            with open(tf, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-1:] if os.path.getsize(str(tf)) > 0 else []
            if tail:
                last = tail[-1].rstrip("\n")
                if last:
                    try:
                        json.loads(last)
                    except json.JSONDecodeError:
                        return "partial_write"
    except OSError:
        pass

    # 2. mutated_state — harness_state.session_id mismatch
    if harness_state is not None:
        hs_sid = harness_state.get("session_id") if isinstance(harness_state, dict) else None
        if hs_sid is not None and str(hs_sid) != str(sid):
            return "mutated_state"

    # Load trace records (skips malformed lines; partial_write already returned).
    records = _load_traces_tail(tf, n=500)
    sid_records = [r for r in records if r.get("session_id") == sid]

    if not sid_records:
        # Before declaring fresh, still check two_terminal (could be racing
        # terminal that hasn't written its first event yet).
        if _count_active_pids(sid) >= 2:
            return "two_terminal"
        return "fresh"

    # Extract timestamps (ISO strings) for this sid.
    ts_values: List[float] = []
    for r in sid_records:
        ts_raw = r.get("ts", "")
        parsed = _parse_iso_ts(ts_raw)
        if parsed is not None:
            ts_values.append(parsed)
    if not ts_values:
        # We have records but no parseable ts — treat conservatively as resumed.
        return "resumed"

    latest = max(ts_values)
    delta = now - latest

    # 3. clock_skew — NTP reverse jump
    if delta < -60.0:  # >60s in the past; tolerate small NTP drift
        return "clock_skew"

    # 4. os_sleep — abrupt intra-record gap > 6h signals device suspend.
    #    [LOG-R1-001 2026-04-20] Gate removed: previously guarded by
    #    `delta <= 24*3600`, which meant a 30h-stale session that also
    #    experienced an OS sleep never got the os_sleep label (it fell
    #    through to stale_24h). Device-suspend is orthogonal to idleness,
    #    so check the intra-record gap regardless of overall age.
    ts_sorted = sorted(ts_values)
    if len(ts_sorted) >= 2:
        recent_gap = ts_sorted[-1] - ts_sorted[-2]
        if recent_gap > 6 * 3600.0:
            return "os_sleep"

    # 5. two_terminal — concurrent writers (after temporal classification
    #    but before aborted/resumed — two live procs outrank an "aborted"
    #    label because aborted just means missing session_end)
    if _count_active_pids(sid) >= 2:
        return "two_terminal"

    # 6. aborted — session_start observed, session_end absent
    #    [LOG-R1-002 2026-04-20] Use >= for 1h threshold so boundary is
    #    consistent with the time-bucket rule below (delta == 3600.0
    #    belongs to stale_1h, not resumed).
    events = [r.get("event", "") for r in sid_records]
    if "session_start" in events and "session_end" not in events:
        # [LOG-R2-001 2026-04-20] Only call it aborted in the [1h, 24h)
        # window. Beyond 24h, the session is more accurately classified
        # as stale_24h — calling a 30h-old start "aborted" hides the
        # signal that drives sunset/cleanup paths consuming stale_24h.
        if 3600.0 <= delta < 24 * 3600.0:
            return "aborted"

    # 7-10. Time-bucket the healthy cases.
    # [LOG-R1-002 2026-04-20] Half-open boundaries:
    #   delta >= 86400  -> stale_24h
    #   3600 <= delta < 86400 -> stale_1h
    #   delta < 3600    -> resumed
    # Prior code used strict `>`, which left delta==3600 in the resumed
    # bucket (inconsistent with the aborted check and human intuition).
    if delta >= 24 * 3600.0:
        return "stale_24h"
    if delta >= 3600.0:
        return "stale_1h"
    return "resumed"


def _parse_iso_ts(ts_raw: str) -> Optional[float]:
    """Parse ISO-8601 → posix timestamp. Accept trailing 'Z'. Return None on failure."""
    if not ts_raw or not isinstance(ts_raw, str):
        return None
    s = ts_raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Treat naive as UTC (matches trace_sink which writes utcnow().isoformat()).
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def dispatch(transcript_path: Optional[str] = None) -> dict:
    """Called from Stop hook. Fires async worker and returns immediately.

    Returns dict with 'dispatched' bool and 'sid' string.
    [SEC-HIGH 2026-04-18]: path traversal guard + file handle leak fix.
    """
    if not is_enabled():
        return {"dispatched": False, "reason": "disabled"}

    # [AC-3 2026-04-19] Resume classification runs BEFORE worth-reflecting
    # check so that even skipped dispatches carry a diagnostic label in the
    # returned dict (observability, not enforcement).
    resume_label: SessionStateLabel = "fresh"
    try:
        sid_for_classify = os.environ.get("CLAUDE_SESSION_ID", "").strip() or "unknown"
        resume_label = classify_session_state(sid_for_classify)
    except Exception:
        resume_label = "fresh"  # defensive fallback — never block dispatch

    signals = _gather_session_signals()
    if not _worth_reflecting(signals):
        return {
            "dispatched": False,
            "reason": "no_triggers",
            "signals": signals,
            "resume_label": resume_label,
        }

    # [SEC-HIGH] Validate transcript_path is inside workspace
    safe_transcript = _validate_transcript_path(transcript_path or "")

    sid = uuid.uuid4().hex[:12]
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _LOGS_DIR / _REFLECTION_LOG_FMT.format(sid=sid)

    # [SEC-MED 2026-04-18] File handle leak fix: open in try/finally
    log_fh = None
    started = False
    try:
        log_fh = open(log_path, "w", encoding="utf-8")
        popen_kwargs = {
            "stdout": log_fh,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": _safe_env(),
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            )
        else:
            popen_kwargs["start_new_session"] = True

        subprocess.Popen(
            [sys.executable, "-m", "src.core.session_reflector",
             "worker", sid, safe_transcript],
            cwd=str(_WORKSPACE_ROOT),
            **popen_kwargs,
        )
        started = True
        return {
            "dispatched": True,
            "sid": sid,
            "log": str(log_path),
            "resume_label": resume_label,
        }
    except Exception as e:
        return {"dispatched": False, "reason": f"popen_error:{e}"}
    finally:
        # Close the handle ONLY if Popen didn't start (it owns the fd on success)
        if log_fh is not None and not started:
            try:
                log_fh.close()
            except Exception:
                pass


def reflect_worker(sid: str, transcript_path: str) -> int:
    """Runs in detached subprocess. Calls Haiku, writes distilled patterns."""
    print(f"[reflect-worker] sid={sid} starting @ {datetime.now(timezone.utc).isoformat()}")

    try:
        # [SEC-MED Round2 fix 2026-04-18] Atomic budget check + record.
        # Replaces non-atomic _check_budget + _record_budget pair.
        from src.core.soft_signal_classifier import _check_and_record_budget
        if not _check_and_record_budget(0.003):  # estimate $0.003 for reflection
            print("[reflect-worker] budget exhausted, skip")
            return 0

        # Read transcript tail (last 20 turns ~= 40 lines)
        # [SEC-HIGH] validate transcript path even inside worker (defense in depth)
        transcript = ""
        safe_tp = _validate_transcript_path(transcript_path)
        if safe_tp and Path(safe_tp).exists():
            try:
                with open(safe_tp, "r", encoding="utf-8", errors="replace") as f:
                    transcript = "".join(f.readlines()[-40:])
            except Exception:
                pass

        # Fallback: read recent events.jsonl user_prompt_submit events
        # [LOGIC-MED] per-line try/except prevents JSONDecodeError from killing reflection
        # [P1 2026-04-18] window 30 -> 500 lines: tool-heavy sessions had
        # zero user prompts in last 30 events (cause of all past L3 failures)
        if not transcript:
            events_file = _DATA_DIR / "events.jsonl"
            if events_file.exists():
                try:
                    with open(events_file, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()[-500:]
                    prompts = []
                    for l in lines:
                        if not (l.strip() and "user_prompt_submit" in l):
                            continue
                        try:
                            parsed = json.loads(l)
                            p = parsed.get("details", {}).get("prompt", "")
                            if p:
                                prompts.append(p)
                        except Exception:
                            continue  # skip corrupt line, keep going
                    # cap to last 20 user prompts to keep Haiku input <4000 chars
                    transcript = "\n".join(prompts[-20:])
                except Exception:
                    pass

        if not transcript.strip():
            print("[reflect-worker] no transcript available")
            return 0

        # [SEC-HIGH] PII scrub + injection-delim strip (via _redact_pii)
        from src.core.soft_signal_classifier import _redact_pii, _safe_env
        redacted = _redact_pii(transcript)[:4000]

        prompt = f"""You are a post-session reflector. From this conversation tail, distill at most 3 \
durable rules the assistant should remember for FUTURE sessions. Output ONLY JSON array, no prose.

Session tail (PII redacted, delimiters stripped):
<<<
{redacted}
>>>

Output schema:
[{{"type":"prohibition"|"future_rule"|"gotcha"|"pattern",
   "fact":"<observation, <=120 chars>",
   "recommendation":"<what to do, <=120 chars>",
   "confidence":<0.0-1.0>}}]

Rules:
- Return [] if nothing durable learned.
- Only include rules with confidence >= 0.7.
- Avoid restating known facts. Focus on what went wrong/right NEW this session.
"""

        from src.core.subprocess_launcher import claude_argv
        cmd = claude_argv(["-p", "--model", "claude-haiku-4-5",
               "--output-format", "text", prompt])
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30.0, env=_safe_env(),
        )
        # Budget already recorded atomically via _check_and_record_budget above

        if result.returncode != 0:
            print(f"[reflect-worker] claude -p failed rc={result.returncode}")
            return 0

        # Extract JSON array
        raw = result.stdout.strip()
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            print("[reflect-worker] no JSON array found in output")
            return 0

        try:
            rules = json.loads(m.group(0))
        except json.JSONDecodeError:
            print("[reflect-worker] json parse failed")
            return 0

        if not isinstance(rules, list):
            return 0

        # Write to knowledge base
        from src.core.pattern_extractor import PatternEntry, save_patterns
        entries = []
        for r in rules[:3]:
            try:
                conf = float(r.get("confidence", 0.0))
                if conf < 0.7:
                    continue
                entry = PatternEntry(
                    type=r.get("type", "pattern"),
                    fact=str(r.get("fact", ""))[:300],
                    recommendation=str(r.get("recommendation", ""))[:300],
                    confidence="high" if conf >= 0.85 else "medium",
                    tags=["session_reflection", "auto_captured", f"sid_{sid}"],
                    auto_generated=True,
                    provenance=[{
                        "source": "session_reflector_L3",
                        "reference": f"sid={sid}",
                        "date": datetime.now(timezone.utc).isoformat(),
                    }],
                )
                entries.append(entry)
            except Exception as ee:
                print(f"[reflect-worker] entry skip: {ee}")

        if entries:
            added = save_patterns(entries)
            print(f"[reflect-worker] saved {added} patterns")
            # [TU7 2026-04-19] Also add_episode to Graphiti when enabled.
            # Shadow-mode: writes to graph but smart_prime still reads
            # flat-file. Never raises — graph unavailable is fine.
            try:
                from src.core.graphiti_client import add_episode, is_enabled
                if is_enabled():
                    payload = {"pattern_count": added, "sid": sid}
                    add_episode(
                        text=f"Session reflection sid={sid}: {added} patterns distilled",
                        source=f"session_reflector:{sid}",
                        payload=payload,
                    )
            except Exception as e:
                print(f"[reflect-worker] graphiti add_episode skip: {e}")
            # [AC-4 2026-04-19] Incremental memory snapshot: after a reflection
            # cycle, mark the memory files that were "modified" as seen so the
            # next Stop hook only re-processes files that change after this
            # reflection. Prior wiring gap: reflector ran but never updated
            # flat_memory_state.json, so every session re-inspected all
            # modified files (equivalent to watcher disabled). Safe to call
            # even when no memory files are affected (no-op).
            try:
                from src.core.flat_memory_watcher import (
                    changed_files,
                    marked_seen,
                )
                added_m, modified_m, _removed_m = changed_files()
                processed = list(added_m) + list(modified_m)
                if processed:
                    marked_seen(processed)
                    print(f"[reflect-worker] marked_seen: {len(processed)} memory files")
            except Exception as e:
                print(f"[reflect-worker] marked_seen skip: {e}")
            return added
        print("[reflect-worker] no rules passed threshold")
        return 0

    except Exception as e:
        print(f"[reflect-worker] fatal: {e}")
        return 0


def main():
    """CLI: python -m src.core.session_reflector [worker|dispatch] [args...]"""
    if len(sys.argv) < 2:
        print("Usage: session_reflector [worker|dispatch] [args]", file=sys.stderr)
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "worker":
        sid = sys.argv[2] if len(sys.argv) > 2 else "unknown"
        transcript_path = sys.argv[3] if len(sys.argv) > 3 else ""
        reflect_worker(sid, transcript_path)
    elif mode == "dispatch":
        # Read hook JSON stdin; extract transcript_path
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            payload = {}
        transcript_path = payload.get("transcript_path", "")
        result = dispatch(transcript_path)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
