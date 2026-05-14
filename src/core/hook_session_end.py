"""SessionEnd hook: clean shutdown + autoDream trigger.

Per CEO-approved hybrid design (R7):
- SessionEnd writes clean_shutdown=true
- SessionStart (existing) checks: if last shutdown was unclean, run autoDream catchup
- This way autoDream runs on EITHER SessionEnd or next SessionStart, never lost

Hook input:
    {
      "hook_event_name": "SessionEnd",
      "reason": "clear|resume|logout|prompt_input_exit|bypass_permissions_disabled|other",
      "session_id": "...",
      "transcript_path": "..."
    }

autoDream trigger conditions (only fires if):
- reason in {clear, logout} (not on resume - that's normal context restore)
- sessions_since_last_dream >= threshold (5 by default)

Cannot block (session already ending).
"""

import hashlib as _hashlib
import json as _json
import os
import sys
import uuid as _uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core._hook_utils import (  # noqa: E402
    read_hook_input,
    emit_decision,
    log_hook_event,
    atomic_json_write,
    safe_load_json,
    get_workspace_paths,
)


_HOOK_NAME = "session_end"
_PATHS = get_workspace_paths()
_HARNESS = _PATHS["harness"]

# Reasons that should trigger autoDream + credit.
# [Fix A] `resume` added 2026-04-18: session ends because user initiated a
# resume action. Previous comment "resume = context restore" was factually
# wrong — SessionEnd hook with reason=resume is a legitimate session end.
# Empirical evidence: 2026-04-18 production showed hundreds of resume
# session_end events, all fully-working sessions being silently skipped.
_TRIGGERING_REASONS = {"clear", "logout", "prompt_input_exit", "resume"}


def _mark_clean_shutdown(reason: str) -> None:
    """Write clean_shutdown=true with reason + timestamp to harness.

    L-H4 fix: Only reset recovery_attempted if it was previously set
    (means we successfully completed a recovery cycle). Don't reset on
    every clean shutdown - that breaks cycle isolation when consecutive
    unclean shutdowns happen.
    """
    harness = safe_load_json(_HARNESS)
    if "auto_dream" not in harness:
        harness["auto_dream"] = {}
    harness["auto_dream"]["clean_shutdown"] = True
    # Only clear recovery_attempted if a recovery was actually attempted+completed
    # (we reached this clean shutdown handler, so the cycle is done)
    if harness["auto_dream"].get("recovery_attempted"):
        harness["auto_dream"]["recovery_attempted"] = False
    harness["auto_dream"]["last_shutdown_reason"] = reason
    harness["auto_dream"]["last_shutdown_time"] = datetime.utcnow().isoformat() + "Z"
    harness["updated_at"] = datetime.utcnow().isoformat() + "Z"
    atomic_json_write(_HARNESS, harness)


def _should_run_autodream(reason: str) -> bool:
    """Check if autoDream should run on this SessionEnd."""
    if reason not in _TRIGGERING_REASONS:
        return False
    harness = safe_load_json(_HARNESS)
    dream = harness.get("auto_dream", {})
    sessions = dream.get("sessions_since_last_dream", 0)
    threshold = dream.get("dream_threshold", 5)
    return sessions >= threshold


def _sid_digest(sid: str) -> str:
    """[LOG-R1-005 2026-04-20] Length-independent collision-resistant sid key.

    Prior implementation sliced ``sid[:32]``; two sids that share the first 32
    characters but differ later would collide on the same lock/sentinel file,
    causing one session to block or clobber another. Use sha1 of the full sid
    (16 hex chars) so lock names depend on the full identity regardless of sid
    length. SHA1 is fine here — no security claim, just dispersion.
    """
    if not isinstance(sid, str):
        sid = str(sid)
    # [LOG-R2-002 2026-04-20] usedforsecurity=False so FIPS-mode Linux
    # does not refuse SHA1; we use the digest only for collision-free
    # lock filenames, no security claim.
    try:
        return _hashlib.sha1(sid.encode("utf-8", errors="replace"),
                             usedforsecurity=False).hexdigest()[:16]
    except TypeError:
        # Python < 3.9 does not support usedforsecurity kwarg.
        return _hashlib.sha1(sid.encode("utf-8", errors="replace")).hexdigest()[:16]


def _credit_lock(data_dir, sid: str):
    """[AC-1] Cross-process filelock scoped to a single sid's credit operation.

    Returns a filelock context manager, or a no-op contextmanager if the
    library is unavailable. The lock path is derived from the sid so two
    different sessions never block each other.

    [LOG-R1-005 2026-04-20] Lock name uses sha1(sid)[:16] rather than sid[:32]
    to avoid collisions between long sids sharing the same 32-char prefix.

    LOCK-ORDER: 2 (credit critical section). Must be acquired AFTER _sid_lock
    (if both are needed) and BEFORE the _PATTERNS_FILE lock inside
    _atomic_rewrite_patterns. See the lock-order docstring at the top of
    pattern_extractor.py.
    """
    try:
        import filelock
    except ImportError:
        from contextlib import nullcontext
        return nullcontext()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lock_path = data_dir / f".credit_lock_{_sid_digest(sid)}.lock"
    return filelock.FileLock(str(lock_path), timeout=10)


def _check_and_claim_session(data_dir, sid: str, credited_placeholder: int = -1) -> bool:
    """[MED-1 race fix / AC-1] Atomic check-and-claim.

    Returns True if sid was ALREADY claimed (should abort credit).
    Returns False if we successfully claimed it (safe to proceed).

    Layered defense:
      1. Caller wraps the whole credit region in _credit_lock() (filelock).
      2. This fn uses O_CREAT|O_EXCL as a second defence line — idempotent on
         recovery paths and race-safe even across non-Python processes.
    """
    if not sid:
        return False
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # [LOG-R1-005 2026-04-20] Use collision-resistant sid digest (was sid[:32]).
        sentinel = data_dir / f".credit_sentinel_{_sid_digest(sid)}"
        try:
            # O_CREAT | O_EXCL: atomic "create if not exists" — fails if exists
            fd = os.open(str(sentinel), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, _json.dumps({
                "session_id": sid,
                "claimed_at": datetime.utcnow().isoformat() + "Z",
            }).encode("utf-8"))
            os.close(fd)
            return False  # Successfully claimed
        except FileExistsError:
            return True  # Already claimed by another process
    except OSError:
        return False  # Infra failure, allow proceed (fail-open for observability)


def _mark_session_credited(data_dir, sid: str, credited: int) -> None:
    """Append sid to credited_sessions.jsonl for audit log (not for dedup)."""
    if not sid:
        return
    log_file = data_dir / "credited_sessions.jsonl"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(_json.dumps({
                "session_id": sid,
                "credited": credited,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }) + "\n")
    except OSError:
        pass


# Backwards-compat alias for the old API signature
def _is_session_already_credited(data_dir, sid: str) -> bool:
    """[Deprecated — kept for test compat] Non-atomic check against audit log."""
    if not sid:
        return False
    log_file = data_dir / "credited_sessions.jsonl"
    if not log_file.exists():
        return False
    try:
        for line in log_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = _json.loads(line)
                if entry.get("session_id") == sid:
                    return True
            except _json.JSONDecodeError:
                continue
    except OSError:
        pass
    return False


def _credit_helpful_patterns(reason: str) -> dict:
    """Apply helpful_count credit to patterns primed this session.

    Three gates must ALL pass (hard-wired):
      1. Reason in triggering set (clean shutdown / resume)
      2. harness last_session.status == complete OR persistent_mode.completed
      3. last_session_test_result.json exists AND tests_passed == True

    [Fix A] `resume` 加入触发集.
    [Fix A idempotency] 同 sid 不会二次 credit.
    [Fix B] 过滤 KB 已不存在的 primed ID, 区分 success / success_partial / all_vanished.
    [Fix C] 成功 credit 后清 current_session_id.txt (防止跨 session 污染).
    [V-add-2] credit 路径所有写操作包裹异常日志, 失败记 credit_failed 事件.

    Returns:
        dict with status + counts + primed_total + missing_count
    """
    result = {"status": "skipped", "reason": "gate_not_met", "credited": 0}
    try:
        if reason not in _TRIGGERING_REASONS:
            result["reason"] = "non_triggering_reason"
            return result

        harness = safe_load_json(_HARNESS)
        last_session = harness.get("last_session", {})
        status_ok = last_session.get("status") == "complete"

        from src.core._hook_utils import get_workspace_paths as _gwp
        data_dir = _gwp()["data"]
        pm_state = safe_load_json(data_dir / "persistent_mode_state.json")
        pm_ok = pm_state.get("completed", False)

        if not (status_ok or pm_ok):
            result["reason"] = "task_not_complete"
            return result

        # Gate 3: test-passed check (TU-A2 self_evolution_reconnect: 3-state)
        # State A: file exists + tests_passed=true → credit_increment=1.0 (full)
        # State B: file exists + tests_passed=false → skip (test failed)
        # State C: file missing → soft pass with degraded_mode + increment=0.5
        test_result_file = data_dir / "last_session_test_result.json"
        if not test_result_file.exists():
            # State C: degraded mode — file never written this session, soft pass
            credit_increment = 0.5
            credit_mode = "degraded_no_test_result_file"
            try:
                from src.core.trace_sink import write_trace_event
                write_trace_event("credit_degraded_mode", {
                    "reason": "no_test_result_file",
                    "credit_increment": credit_increment,
                })
            except Exception:  # pragma: no cover
                pass
        else:
            test_res = safe_load_json(test_result_file, default={})
            tests_passed = test_res.get("tests_passed", False)
            if not tests_passed:
                # State B: tests explicitly failed → skip credit
                result["reason"] = "test_result_failed"
                return result
            # State A: full credit
            credit_increment = 1.0
            credit_mode = "full_pass"

        from src.core.pattern_extractor import (
            get_current_session_id,
            _read_primed_for_session,
            _remove_primed_for_session,
            record_pattern_helpful,
            load_all_patterns,
        )
        import src.core.pattern_extractor as _pe
        sid = get_current_session_id()

        # [AC-1] Serialize the entire check-and-credit critical section via
        # cross-process filelock. Prevents race where two hook instances
        # both pass the O_EXCL check in parallel (possible on Windows if
        # sentinel cleanup and second fire interleave) and double-credit.
        try:
            with _credit_lock(data_dir, sid):
                # [MED-1] Atomic claim via O_EXCL sentinel — second defence line
                if _check_and_claim_session(data_dir, sid):
                    result["reason"] = "already_credited"
                    result["session_id"] = sid
                    return result

                primed_ids = _read_primed_for_session(sid)
                if not primed_ids:
                    result["reason"] = "no_primed_patterns"
                    return result

                # [Fix B] Force cache invalidation + filter missing IDs explicitly
                _pe._patterns_cache = (0.0, [])  # Force reload from disk
                try:
                    all_current = {p.id for p in load_all_patterns()}
                except Exception as e:
                    result["reason"] = f"load_patterns_failed: {type(e).__name__}"
                    return result

                existing_ids = [i for i in primed_ids if i in all_current]
                missing_count = len(primed_ids) - len(existing_ids)

                if not existing_ids:
                    result["status"] = "skipped"
                    result["reason"] = "all_primed_vanished"
                    result["primed_total"] = len(primed_ids)
                    result["missing_count"] = missing_count
                    # Clean stale primed entries even in this path
                    try:
                        _remove_primed_for_session(sid)
                    except Exception:
                        pass
                    # [MED-2 / AC-2] Also clear sid file here — otherwise stale
                    # sid leaks into next session within the 1h TTL, recreating
                    # Bug C. Double-wrap the unlink so no path through this
                    # branch returns without attempting cleanup.
                    try:
                        _pe._CURRENT_SESSION_FILE.unlink(missing_ok=True)
                    except FileNotFoundError:
                        pass
                    except Exception:
                        pass
                    return result

                # Try the credit (wrap in its own guard)
                try:
                    credited = record_pattern_helpful(existing_ids, reason="session_completed")
                except Exception as e:
                    result["reason"] = f"credit_failed: {type(e).__name__}"
                    result["primed_total"] = len(primed_ids)
                    return result

                # Cleanup + idempotency mark (best-effort, non-blocking)
                try:
                    _remove_primed_for_session(sid)
                except Exception:
                    pass
                try:
                    _mark_session_credited(data_dir, sid, credited)
                except Exception:
                    pass

                # W1-3 (2026-05-04): write last_credit.json so W2 health
                # check (self_evolution_health._check_w2_credit) sees the
                # credit. Pre-fix: 865 LIVE credits invisible to W2 because
                # writer wrote events.jsonl but reader expected last_credit.json
                # — writer-reader schema contract violation.
                try:
                    last_credit_file = data_dir / "last_credit.json"
                    last_credit_file.write_text(
                        _json.dumps({
                            "count_credited": int(credited),
                            "ts": datetime.utcnow().isoformat() + "Z",
                            "session_id": sid,
                            "credit_mode": credit_mode,
                            "credit_increment": credit_increment,
                        }, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception:
                    pass  # fail-soft; W2 metric is observability only

                # [Fix C] Clear session_id file on successful credit (prevents
                # stale sid leaking into next session's primed log).
                # Use the authoritative path from pattern_extractor module
                # (which respects MEMEX_DATA_DIR env var + monkeypatch for tests).
                try:
                    _pe._CURRENT_SESSION_FILE.unlink(missing_ok=True)
                except Exception:
                    pass

                result["status"] = "success" if credited > 0 else "success_partial"
                result["reason"] = "credited" if credited > 0 else "credit_zero_unexpected"
                result["credited"] = credited
                result["session_id"] = sid
                result["primed_total"] = len(primed_ids)
                result["missing_count"] = missing_count
        except Exception as e:
            # Filelock timeout or other infra failure — fail-open but log reason
            try:
                import filelock as _fl
                if isinstance(e, _fl.Timeout):
                    result["reason"] = "credit_lock_timeout"
                    return result
            except ImportError:
                pass
            result["reason"] = f"credit_lock_failed: {type(e).__name__}"
            return result
    except Exception as e:
        result["reason"] = f"exception: {type(e).__name__}"
    return result


def _run_autodream() -> dict:
    """Run autoDream gather + prune (sync, no LLM needed)."""
    try:
        from src.core.auto_dream import AutoDream
        from src.core.pattern_extractor import prune_expired_patterns
        from src.core.auto_trigger import reset_dream_counter

        ad = AutoDream()
        episodes = ad.gather()
        prune_result = ad.prune()
        pruned_patterns = prune_expired_patterns(ttl_days=30)
        reset_dream_counter()
        return {
            "episodes": len(episodes),
            "patterns_pruned": pruned_patterns,
            "events_rotated": prune_result.get("events_rotated", False),
            "success": True,
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def main() -> int:
    data = read_hook_input()
    if not data:
        return 0

    reason = data.get("reason", "other")
    session_id = data.get("session_id", "")

    # Always mark clean shutdown (regardless of reason)
    _mark_clean_shutdown(reason)

    # [Item #1 / V-1] Credit helpful_count for primed patterns if all gates pass
    credit_result = _credit_helpful_patterns(reason)

    # Conditionally run autoDream
    dream_result = None
    if _should_run_autodream(reason):
        dream_result = _run_autodream()

    log_hook_event(
        event_type="session_end",
        hook_name=_HOOK_NAME,
        details={
            "reason": reason,
            "session_id": session_id,
            "dream_triggered": dream_result is not None,
            "dream_result": dream_result,
            "credit_result": credit_result,
        },
    )

    emit_decision(decision="allow", hook_event_name="SessionEnd")
    return 0


if __name__ == "__main__":
    sys.exit(main())
