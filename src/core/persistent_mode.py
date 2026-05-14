"""
Persistent Mode -- oh-my-claudecode Ralph 模式的 Python 实现

核心机制: 拦截 Stop 事件，防止长任务提前返回。

工作流:
1. keyword_router 检测到 "做完为止"/"persistent"/"ralph" 关键词
2. 激活 persistent mode，写入状态文件
3. Stop hook 调用本模块检查是否应阻止退出
4. 如果任务未完成，输出继续指令到 stdout (Claude Code 读取)
5. 断路器: 最多拦截 max_reinforcements 次，超过后强制允许退出

状态文件: memex/data/persistent_mode_state.json
Hook 集成: Stop hook 调用 python memex/memex/core/persistent_mode.py check

安全机制 (借鉴 oh-my-claudecode):
- 状态过期: > 2 小时自动失效
- 断路器: 拦截次数超限后强制退出
- 用户取消: "取消"/"cancel" 立即解除
- Context 耗尽: 不拦截 (防死锁)
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Script-mode safety: when invoked as `python memex/core/persistent_mode.py ...`
# (not `python -m src.core.persistent_mode`), the `memex` package is not on
# sys.path. Internal imports below (and via call into other modules) need it.
# Library imports (`from src.core import persistent_mode`) make this a harmless
# no-op duplicate. NOT cargo-cult: this file is dual-invocation by design
# (autopilot skill uses script form; tests use module form).
_pkg_root = Path(__file__).resolve().parents[2]
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

# [Env var override for test isolation] Respect MEMEX_DATA_DIR
_env_data = os.environ.get("MEMEX_DATA_DIR")
if _env_data and Path(_env_data).is_dir():
    _DATA_DIR = Path(_env_data)
else:
    _DATA_DIR = Path(__file__).parent.parent / "data"
_STATE_FILE = _DATA_DIR / "persistent_mode_state.json"

# 配置 (P0-1, P0-2 2026-04-21 autopilot 10h+ uplift, plan v2)
# STALE_THRESHOLD_SEC: default 12h per verifier R2 #7 (Graphiti 9h+30% tail); env override for 24h supertasks
# MAX_REINFORCEMENTS: default 12 (old 5 calibrated for 2h wall; 10h+refresh_stage needs ~24 effective intercepts)
_DEFAULT_MAX_HOURS = 12
# SEC-R2 #4: clamp both envs to safe ranges; invalid values fall back to defaults
# (0/negative/huge values would disable the guard or starve legitimate sessions)
_MAX_HOURS_LO, _MAX_HOURS_HI = 1, 48
_MAX_REINFORCE_LO, _MAX_REINFORCE_HI = 1, 200


def _clamp_int(raw: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return default
    if v < lo or v > hi:
        return default
    return v


MAX_REINFORCEMENTS = _clamp_int(
    os.environ.get("MEMEX_MAX_REINFORCEMENTS", "12"),
    default=12, lo=_MAX_REINFORCE_LO, hi=_MAX_REINFORCE_HI,
)
STALE_THRESHOLD_SEC = _clamp_int(
    os.environ.get("MEMEX_PERSISTENT_MAX_H", str(_DEFAULT_MAX_HOURS)),
    default=_DEFAULT_MAX_HOURS, lo=_MAX_HOURS_LO, hi=_MAX_HOURS_HI,
) * 3600


def _load_state() -> Optional[dict]:
    """加载持续模式状态"""
    if not _STATE_FILE.exists():
        return None
    try:
        state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return state
    except (json.JSONDecodeError, OSError):
        return None


def _save_state(state: dict) -> None:
    """保存持续模式状态 — atomic tmp+os.replace.

    Cluster 4 migration (2026-04-20 autopilot): previous direct write_text
    left a window where a crash mid-write produced truncated JSON that
    blocked next session start. Inline tmp+replace (no memex package
    imports so CLI entry `python memex/core/persistent_mode.py` works).
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(_STATE_FILE.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(_STATE_FILE))


def _is_spec_stale(spec_file) -> bool:
    """Return True if the existing spec should be regenerated.

    A spec is stale when:
    - status is ``None`` or ``"completed"`` (task is done; new run needs fresh spec)
    - file is older than 24 h AND status is not ``"in_progress"``
    - file is unreadable / corrupt (treat as stale so we regenerate cleanly)
    """
    import json as _json_stale
    from src.core._thresholds import SPEC_STALE_REGEN_SEC
    try:
        d = _json_stale.loads(spec_file.read_text(encoding="utf-8"))
        status = d.get("status")
        if status in (None, "completed"):
            return True
        # Age check: > regen-horizon AND not in_progress → stale
        mtime_age = time.time() - spec_file.stat().st_mtime
        if mtime_age > SPEC_STALE_REGEN_SEC and status != "in_progress":
            return True
        return False
    except (OSError, _json_stale.JSONDecodeError):
        return True  # Corrupted / unreadable = stale


def activate(
    task_description: str = "",
    mode: str = "persistent",
    max_reinforcements: int = MAX_REINFORCEMENTS,
    task_id: Optional[str] = None,
) -> dict:
    """
    激活持续模式。

    Args:
        task_description: 当前任务描述
        mode: 模式名 (persistent, autopilot, parallel)
        max_reinforcements: 最大拦截次数
        task_id: explicit task_id override (TU-1, plan_v1).
            For autopilot mode, resolution order is:
              1. explicit ``task_id`` arg
              2. ``task_binding.get_active_task_id()`` (env channel)
              3. cold-session: ``task_dir_layout.create_task_dir + set_current``
            This guarantees ``set_flag`` receives a canonical tid (never empty,
            never 'unknown_session') — fixes the LIVE bug where 12/17
            ``gate_skipped`` events had reason=no_task_binding because flag
            stored 'unknown_session'.

    Returns:
        激活后的状态 dict
    """
    state = {
        "active": True,
        "mode": mode,
        "task_description": task_description,
        "activated_at": time.time(),
        "activated_at_human": datetime.now().isoformat(),
        "reinforcement_count": 0,
        "max_reinforcements": max_reinforcements,
        "completed": False,
    }
    _save_state(state)

    # 2026-04-24 plan_v3 TU-persistent: for autopilot mode, write the
    # durable flag that session_gate uses to switch to fail-closed
    # behavior on missing evidence, AND auto-generate a minimal
    # task_spec.json so REVIEW_GATE + RELEASE_GATE can enforce the
    # criteria chain.
    #
    # 2026-04-25 plan_v1 TU-1: resolve canonical tid BEFORE set_flag.
    # set_flag is now strict (raises on empty/unknown_session).
    if mode == "autopilot":
        try:
            from src.core._autopilot_flag import set_flag
            from src.core.task_binding import get_active_task_id
            from src.core import task_dir_layout

            # Tier 1: explicit arg
            tid = (task_id or "").strip()
            # Tier 2: env channel
            if not tid:
                tid = (get_active_task_id() or "").strip()
            # Tier 3: cold-session — create a task_dir + set_current.
            # Use a 30-char-or-less slug derived from task_description so the
            # tid is human-readable in flag_info() output.
            if not tid:
                slug = task_description or "autopilot_task"
                tid = task_dir_layout.create_task_dir(slug[:120])
                task_dir_layout.set_current(tid)

            state["task_id"] = tid
            _save_state(state)
            set_flag(tid)
        except (ImportError, OSError, ValueError) as e:
            try:
                from src.core.trace_sink import write_trace_event
                write_trace_event("gate_infra_error", {
                    "where": "persistent_mode.activate",
                    "stage": "set_flag",
                    "error": str(e)[:200],
                })
            except Exception:
                pass  # trace fail-soft
            from src.core._errors import GateInfraError
            raise GateInfraError(
                "activate(autopilot): set_flag failed",
                where="persistent_mode.activate",
            ) from e

        # Generate minimal task_spec.json if absent or stale
        try:
            import json as _json
            spec_file = _DATA_DIR / "task_spec.json"
            if not spec_file.exists() or _is_spec_stale(spec_file):
                spec_payload = {
                    "complexity": "complex",
                    "status": "in_progress",
                    "task_description": task_description,
                    "mode": mode,
                    # U12-B (long_term_plan_v2 §623-648): per-task cost cap
                    # consumed by cost_meter.check_budget(). Default $50.
                    # Override via direct task_spec.json edit before/during run.
                    "cost_budget_usd": 50.0,
                    "created_at": time.time(),
                    "acceptance_criteria": [
                        {"id": "plan_verified", "verified": False},
                        {"id": "review_approved", "verified": False},
                        {"id": "state_synced", "verified": False},
                        {"id": "report_completed", "verified": False},
                    ],
                }
                spec_file.parent.mkdir(parents=True, exist_ok=True)
                spec_file.write_text(
                    _json.dumps(spec_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except (ImportError, OSError, ValueError) as e:
            try:
                from src.core.trace_sink import write_trace_event
                write_trace_event("gate_infra_error", {
                    "where": "persistent_mode.activate",
                    "stage": "task_spec_regen",
                    "error": str(e)[:200],
                })
            except Exception:
                pass  # trace fail-soft
            from src.core._errors import GateInfraError
            raise GateInfraError(
                "activate(autopilot): task_spec regen failed",
                where="persistent_mode.activate",
            ) from e

    return state


def deactivate(reason: str = "manual") -> None:
    """
    解除持续模式。

    Non-emergency reasons ("manual", "completed") require criteria check in
    autopilot mode. Only "expired" and "circuit_breaker" bypass criteria.

    "cancel" is treated as non-emergency when called programmatically.
    Only the CLI entry point (main()) sets the MEMEX_HOOK_CALLER env var
    to mark user-initiated cancels. The LLM cannot forge environment
    variables of the running hook process.

    Args:
        reason: 解除原因 (manual, completed, expired, circuit_breaker, cancel)
    """
    state = _load_state()
    if not state:
        if _STATE_FILE.exists():
            _STATE_FILE.unlink()
        return

    # Emergency reasons always bypass criteria (only infra-level reasons)
    emergency_reasons = {"expired", "circuit_breaker"}

    # "cancel" is emergency ONLY when called from hook CLI process
    # (env var set by main() -- LLM cannot forge env of a running process)
    if reason == "cancel" and os.environ.get("MEMEX_HOOK_CALLER") == "cli":
        emergency_reasons.add("cancel")
    elif reason == "cancel":
        # Programmatic cancel -> treat as manual (requires criteria check)
        reason = "manual"

    if reason not in emergency_reasons and state.get("mode") == "autopilot":
        try:
            from src.core.task_router import check_all_criteria_met
            all_met, summary = check_all_criteria_met()
            if not all_met:
                print(
                    f"[persistent-mode] deactivate('{reason}') BLOCKED: {summary}",
                    file=sys.stderr,
                )
                return  # refuse to deactivate
        except ImportError:
            pass  # fail-open only on import errors (module not found)
        except Exception as e:
            # Fail-CLOSED on data errors (HIGH-2 fix: corrupt task_spec etc.)
            print(
                f"[persistent-mode] deactivate('{reason}') BLOCKED: "
                f"criteria check failed ({type(e).__name__}: {e})",
                file=sys.stderr,
            )
            return

    state["active"] = False
    state["deactivated_at"] = time.time()
    state["deactivate_reason"] = reason
    _save_state(state)


def _check_p2a_guard():
    """RP-SEC-11: prevent premature mark_completed when phase_b_pending=true.
    Closure B P2a writes grayscale_state.json with phase_b_pending=true; mark_completed only at P2b Day-30."""
    import json as _js
    from pathlib import Path as _P
    state_path = _P(__file__).resolve().parents[1] / 'data' / 'grayscale_state.json'
    if not state_path.exists():
        return
    try:
        s = _js.loads(state_path.read_text(encoding='utf-8'))
        if s.get('phase_b_pending') is True:
            raise RuntimeError(
                "PrematureCompletion: closure_b grayscale_state.json has phase_b_pending=true. "
                "P2b manual /autopilot resume required first (Day-30)."
            )
    except (OSError, ValueError):
        pass  # corrupt state → no guard


def mark_completed(force: bool = False) -> bool:
    """
    标记任务已完成，允许正常退出。

    Hard gate: 如果 task_spec.json 存在且有未满足的 acceptance criteria,
    拒绝标记完成 (返回 False)。仅 force=True 时跳过检查 (断路器用)。

    Returns:
        True if marked completed, False if blocked by unmet criteria.
    """
    _check_p2a_guard()
    # PRD 驱动验证: 检查 task_spec 的 acceptance criteria
    if not force:
        try:
            from src.core.task_router import check_all_criteria_met
            all_met, summary = check_all_criteria_met()
            if not all_met:
                print(f"[persistent-mode] BLOCKED: {summary}", file=sys.stderr)
                print(
                    "[persistent-mode] Cannot mark completed. "
                    "Verify all criteria first, or use force=True for emergency exit.",
                    file=sys.stderr,
                )
                return False
        except ImportError as ie:
            # Only fail-open if the entire memex package is missing (infra issue).
            # A specific module missing (e.g., task_router deleted) should fail-closed.
            if "memex" in str(ie) and "task_router" in str(ie):
                print(
                    f"[persistent-mode] BLOCKED: task_router module missing ({ie})",
                    file=sys.stderr,
                )
                return False
            pass  # memex package itself not installed -- fail-open
        except Exception as e:
            # Fail-CLOSED on data errors (corrupt task_spec etc.)
            print(
                f"[persistent-mode] BLOCKED: criteria check failed "
                f"({type(e).__name__}: {e})",
                file=sys.stderr,
            )
            return False

    state = _load_state()
    if state:
        state["completed"] = True
        state["active"] = False
        state["deactivate_reason"] = "completed" if not force else "force_completed"
        _save_state(state)

        # Post-completion learning: extract patterns + update harness
        if state.get("mode") == "autopilot":
            # [Feedback loop Gate 3] Write tests_passed marker on normal completion.
            # autopilot 的 Stage 3 QC 已跑过 pytest, 若代码走到 mark_completed
            # (criteria 都满足), 可以安全标记 tests_passed=True.
            # force=True 是断路器路径, 不写 (tests_passed 状态未知).
            if not force:
                try:
                    _DATA_DIR.mkdir(parents=True, exist_ok=True)
                    test_result_file = _DATA_DIR / "last_session_test_result.json"
                    test_result_file.write_text(json.dumps({
                        "tests_passed": True,
                        "source": "autopilot_completion",
                        "summary": "autopilot mark_completed: all acceptance criteria met",
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass  # non-blocking

            # Step 1: Extract learnings (only on normal completion, not forced exit)
            if not force:
                try:
                    from src.core.pattern_extractor import extract_from_session
                    added = extract_from_session(max_patterns=5)
                    if added > 0:
                        print(
                            f"[persistent-mode] Learned {added} patterns from this session",
                            file=sys.stderr,
                        )
                except Exception:
                    pass  # non-blocking

            # Step 2: Record completion in harness
            try:
                from src.core.auto_trigger import record_autopilot_completion
                task_desc = state.get("task_description", "")
                record_autopilot_completion(
                    task_type="autopilot",
                    summary=task_desc[:300],
                )
            except Exception:
                pass  # non-blocking

            # 2026-04-24 plan_v3 TU-persistent: clear the autopilot flag
            # on completion so the next session starts fresh (no
            # stale-flag lock-out) + mark task_spec criteria verified
            # so RELEASE_GATE is satisfied.
            try:
                from src.core._autopilot_flag import clear_flag
                clear_flag()
            except Exception:
                pass
            try:
                import json as _json
                spec_file = _DATA_DIR / "task_spec.json"
                if spec_file.exists():
                    data = _json.loads(spec_file.read_text(encoding="utf-8"))
                    if "acceptance_criteria" in data:
                        for c in data["acceptance_criteria"]:
                            c["verified"] = True
                        data["status"] = "completed"
                        spec_file.write_text(
                            _json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
            except Exception:
                pass

            # Step 3 [P3 2026-04-18]: Credit primed patterns as helpful.
            # Signal: autopilot completed without force → every pattern primed
            # in last 12h (capped at 10) gets helpful_count += 1.
            try:
                from src.core.pattern_extractor import credit_session_helpful
                n_credited = credit_session_helpful()
                if n_credited > 0:
                    print(
                        f"[persistent-mode] Credited {n_credited} patterns as helpful",
                        file=sys.stderr,
                    )
            except Exception:
                pass  # non-blocking

    return True


# Plan v1 M3: per-stage soft walltime budgets. Exceeding these emits a
# `_stage_overbudget` trace event but never blocks — it's a warn signal.
# Per-stage budget is reset on each refresh call (L2 canonical).
STAGE_SOFT_BUDGETS_SEC: dict[str, int] = {
    "stage_1_research_design": 1800,   # 30 min
    "stage_2_implementation": 3600,    # 60 min
    "stage_3_qc": 600,                 # 10 min
    "stage_4_review": 1500,            # 25 min
    "stage_4_5_plan_retro": 300,       # 5 min
    "stage_5_live": 900,               # 15 min
    "stage_6_live_verify": 900,        # 15 min
}


def check_stage_budget(stage: str, now: Optional[float] = None) -> dict:
    """Read-only: return {status, stage, elapsed_s, budget_s, hint} for
    the CURRENTLY-active stage (as recorded in state). Does not mutate.

    status in {"ok","overbudget","warn_clock_skew","warn_unknown_stage",
               "not_active"}.
    """
    now = now if now is not None else time.time()
    state = _load_state() or {}
    if not state.get("active"):
        return {"status": "not_active", "stage": stage, "elapsed_s": 0,
                "budget_s": 0, "hint": ""}
    stage_start = state.get("stage_start_ts")
    current_stage = state.get("current_stage") or state.get("last_stage") or ""
    # Unknown stage: no budget known → warn but don't block
    if stage not in STAGE_SOFT_BUDGETS_SEC:
        return {"status": "warn_unknown_stage", "stage": stage,
                "elapsed_s": 0, "budget_s": 0,
                "hint": f"no budget entry for stage {stage!r}"}
    if not stage_start:
        return {"status": "ok", "stage": stage, "elapsed_s": 0,
                "budget_s": STAGE_SOFT_BUDGETS_SEC[stage], "hint": ""}
    if now < stage_start:
        # Backward clock jump (system time changed)
        return {"status": "warn_clock_skew", "stage": stage,
                "elapsed_s": 0,
                "budget_s": STAGE_SOFT_BUDGETS_SEC[stage],
                "hint": "system clock moved backward since stage start"}
    elapsed = int(now - stage_start)
    budget = STAGE_SOFT_BUDGETS_SEC[stage]
    if elapsed > budget:
        return {"status": "overbudget", "stage": stage,
                "elapsed_s": elapsed, "budget_s": budget,
                "hint": f"stage ran {elapsed}s > budget {budget}s; consider switching path"}
    return {"status": "ok", "stage": stage, "elapsed_s": elapsed,
            "budget_s": budget, "hint": ""}


def _emit_stage_overbudget_trace(warn: dict) -> None:
    """Write a trace_sink event if available; fail-soft."""
    try:
        from src.core import trace_sink
        trace_sink.write("_stage_overbudget", warn)
    except Exception:
        pass


def refresh_stage(stage_name: str) -> Optional[dict]:
    """
    刷新 reinforcement 计数器（大循环每个阶段完成后调用）。

    防止长任务因 Stop 拦截耗尽 reinforcement 配额。
    每完成一个有意义的阶段，重置计数器，延长运行时间。

    plan_v1 M3: Before resetting timer, check whether the PREVIOUS stage
    (recorded in state.current_stage) exceeded its budget; if yes, emit a
    trace event and return a warn dict so callers/Stop hooks can surface it.
    Per-stage timer is RESET on each refresh call (not accumulated).

    Returns:
      None if persistent mode not active
      dict with status/elapsed/budget/hint otherwise (or empty dict on ok)
    """
    state = _load_state()
    warn: dict = {}
    if state and state.get("active"):
        # 2026-04-24 plan_v3: touch autopilot flag's mtime so TTL is
        # a sliding window, not absolute from activate time. Allows
        # long (10h+) autopilot sessions to stay flag-active.
        try:
            from src.core._autopilot_flag import refresh_flag
            refresh_flag()
        except Exception:
            pass
        now = time.time()
        # Budget check on the stage we're leaving (the previously current one)
        prev_stage = state.get("current_stage") or state.get("last_stage") or ""
        prev_start = state.get("stage_start_ts", 0) or 0
        if prev_stage and prev_stage in STAGE_SOFT_BUDGETS_SEC and prev_start:
            elapsed = max(0, now - prev_start)
            budget = STAGE_SOFT_BUDGETS_SEC[prev_stage]
            if elapsed > budget:
                warn = {
                    "status": "overbudget",
                    "stage": prev_stage,
                    "elapsed_s": int(elapsed),
                    "budget_s": budget,
                    "hint": f"previous stage {prev_stage} ran {int(elapsed)}s "
                            f"> budget {budget}s",
                }
                _emit_stage_overbudget_trace(warn)
                print(
                    f"[persistent-mode] stage budget WARN: {warn['hint']}",
                    file=sys.stderr,
                )
        old_count = state.get("reinforcement_count", 0)
        state["reinforcement_count"] = max(0, old_count - 2)  # 每阶段恢复 2 次配额
        state["last_stage"] = stage_name
        state["last_stage_time"] = now
        # M3 additions: track stage timer for the incoming stage
        state["current_stage"] = stage_name
        state["stage_start_ts"] = now  # RESET per refresh (L2 canonical)

        # Plan-retro gate invocation (bug-3 fix, 2026-04-21)
        if stage_name == "stage_4_5_plan_retro":
            tid = state.get("task_id") or ""
            if tid:
                try:
                    from src.core.plan_retro_gate import (
                        check_gate as _prg_check,
                        env_skip_flag as _prg_skip,
                    )
                    if _prg_skip():
                        state["plan_retro_block_reason"] = (
                            "SKIPPED: MEMEX_SKIP_PLAN_RETRO=1 override"
                        )
                        print(
                            "[persistent-mode] plan_retro_gate: "
                            "operator-set skip flag honored (logged)",
                            file=sys.stderr,
                        )
                    else:
                        allow, reason = _prg_check(tid)
                        if allow:
                            state.pop("plan_retro_block_reason", None)
                            print(
                                f"[persistent-mode] plan_retro_gate: "
                                f"{reason[:120]}",
                                file=sys.stderr,
                            )
                        else:
                            state["plan_retro_block_reason"] = reason
                            print(
                                f"[persistent-mode] plan_retro_gate: "
                                f"BLOCK — {reason[:240]}",
                                file=sys.stderr,
                            )
                except Exception as e:
                    # fail-open on import/logic error; record for debug
                    state["plan_retro_error"] = str(e)[:200]

        _save_state(state)

        # U11 (long_term_plan_v2 Phase 3): write a mid-session checkpoint
        # on every stage transition when in autopilot mode. logic-iter1-5:
        # use in-memory state.mode, not a fresh file read (TOCTOU-free).
        if state.get("mode") == "autopilot":
            tid = (state.get("task_id") or "").strip()
            if tid:
                try:
                    from src.core.autopilot_checkpoint import write_checkpoint
                    write_checkpoint(tid, stage_name,
                                     trigger="stage_transition")
                except Exception as e:
                    try:
                        from src.core.trace_sink import write_trace_event
                        write_trace_event("gate_infra_error", {
                            "where": "persistent_mode.refresh_stage.checkpoint",
                            "reason": str(e)[:200],
                        })
                    except Exception:
                        pass
    return warn if warn else None


def check_should_block_stop() -> tuple:
    """
    检查是否应阻止 Stop 事件。

    Returns:
        (should_block: bool, message: str)
        - should_block=True: 输出 message 到 stdout，Claude 继续工作
        - should_block=False: 允许正常退出
    """
    state = _load_state()

    # 无状态或未激活
    if not state or not state.get("active", False):
        return False, ""

    # 任务已标记完成 -- BUT independently re-verify criteria.
    # The LLM could have written completed=True directly to the state file.
    # Don't trust the flag; re-check artifacts independently.
    if state.get("completed", False):
        try:
            from src.core.task_router import check_all_criteria_met
            all_met, summary = check_all_criteria_met()
            if all_met:
                deactivate("completed")
                return False, ""
            else:
                # completed flag is set but criteria are NOT met.
                # This means either: (a) LLM tampered with state file, or
                # (b) artifact files expired. Reset completed flag.
                state["completed"] = False
                state["active"] = True
                _save_state(state)
                return True, (
                    f"[persistent-mode] TAMPER DETECTED: completed=True but criteria unmet. "
                    f"Resetting. {summary}"
                )
        except ImportError:
            # Module not found -- infra issue, fail-open.
            # Direct state manipulation (not deactivate()) to avoid re-entrant
            # import chain: deactivate("completed") -> check_all_criteria_met
            # -> ImportError -> except ImportError -> pass (double fail-open)
            state["completed"] = True
            state["active"] = False
            state["deactivate_reason"] = "completed_infra_fallback"
            _save_state(state)
            return False, ""
        except OSError as e:
            # Transient IO error (antivirus lock, disk full) -- fail-open.
            # Don't treat transient IO issues as tampering.
            return False, (
                f"[persistent-mode] IO error during tamper check ({e}), "
                f"fail-open (transient)."
            )
        except Exception as e:
            # Data error (corrupt JSON, logic error) -- fail-CLOSED.
            state["completed"] = False
            state["active"] = True
            _save_state(state)
            return True, (
                f"[persistent-mode] Tamper check error ({type(e).__name__}), "
                f"fail-closed. Resetting completed flag."
            )

    # 过期检查 (default 12h, env MEMEX_PERSISTENT_MAX_H 可覆盖)
    elapsed = time.time() - state.get("activated_at", 0)
    if elapsed > STALE_THRESHOLD_SEC:
        deactivate("expired")
        return False, (
            f"[persistent-mode] Expired after {elapsed/3600:.1f}h "
            f"(limit {STALE_THRESHOLD_SEC/3600:.0f}h), allowing exit."
        )

    # 断路器检查
    count = state.get("reinforcement_count", 0)
    max_r = state.get("max_reinforcements", MAX_REINFORCEMENTS)
    if count >= max_r:
        deactivate("circuit_breaker")
        return False, f"[persistent-mode] Circuit breaker triggered ({count}/{max_r}), allowing exit."

    # RELEASE GATE: check task_spec for unmet phase criteria
    criteria_msg = ""
    deepening_question = ""
    try:
        from src.core.task_router import check_all_criteria_met
        all_met, summary = check_all_criteria_met()
        if not all_met:
            criteria_msg = f"\nUNMET CRITERIA: {summary}."

            # Phase-aware deepening questions (not generic "keep going")
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            spec_file = _DATA_DIR / "task_spec.json"
            if spec_file.exists():
                import json as _j
                spec = _j.loads(spec_file.read_text(encoding="utf-8"))
                unmet = [c["id"] for c in spec.get("acceptance_criteria", [])
                         if not c.get("verified")]

                if "plan_approved" in unmet:
                    deepening_question = (
                        "\nACTION REQUIRED: Spawn the architect agent to create a plan. "
                        "You cannot write code until plan_approved is verified."
                    )
                elif "review_approved" in unmet:
                    deepening_question = (
                        "\nACTION REQUIRED: Spawn the verifier agent for independent review. "
                        "You cannot commit until review_approved is verified."
                    )
                elif "report_completed" in unmet:
                    deepening_question = (
                        "\nACTION REQUIRED: Spawn the briefing agent to generate session report. "
                        "You cannot exit until report_completed is verified."
                    )
                else:
                    # Other unmet criteria: ask specific deepening questions
                    deepening_question = (
                        f"\nUNMET: {', '.join(unmet)}. "
                        "Verify each criterion with evidence before calling mark_completed()."
                    )
    except Exception:
        pass

    # 阻止退出，递增计数器
    state["reinforcement_count"] = count + 1
    _save_state(state)

    task = state.get("task_description", "current task")
    mode = state.get("mode", "persistent")

    message = (
        f"[persistent-mode] {mode} mode active. "
        f"Task not yet completed: {task[:200]}\n"
        f"Reinforcement {count + 1}/{max_r}."
        f"{criteria_msg}"
        f"{deepening_question}"
    )

    return True, message


def get_status() -> dict:
    """获取当前持续模式状态"""
    state = _load_state()
    if not state:
        return {"active": False}
    return {
        "active": state.get("active", False),
        "mode": state.get("mode", ""),
        "task": state.get("task_description", ""),
        "reinforcements": f"{state.get('reinforcement_count', 0)}/{state.get('max_reinforcements', MAX_REINFORCEMENTS)}",
        "elapsed_min": round((time.time() - state.get("activated_at", time.time())) / 60, 1),
    }


def main():
    """
    CLI 入口。

    用法:
      python persistent_mode.py activate "task description"
      python persistent_mode.py check          # Stop hook 调用
      python persistent_mode.py deactivate
      python persistent_mode.py complete
      python persistent_mode.py status
    """
    if len(sys.argv) < 2:
        print("Usage: persistent_mode.py [activate|check|deactivate|complete|status]")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "activate":
        task = sys.argv[2] if len(sys.argv) > 2 else ""
        mode = sys.argv[3] if len(sys.argv) > 3 else "persistent"
        state = activate(task, mode)
        print(f"[persistent-mode] Activated: {mode} mode")
        sys.exit(0)

    elif cmd == "check":
        should_block, message = check_should_block_stop()
        # [Item #2] Emit gate_decision event
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent.parent))
            from src.core._hook_utils import log_gate_decision
            log_gate_decision(
                gate="persistent_mode",
                rule="exit_check",
                decision="block" if should_block else "allow",
                target="stop_hook",
                reason=(message or "")[:200],
            )
        except Exception:
            pass
        if should_block:
            # 输出到 stderr，Claude Code 会显示
            print(message, file=sys.stderr)
            # 关键: exit code 2 = 阻止 Stop (不是 1!)
            # exit 0 = 允许, exit 1 = 错误但仍允许, exit 2 = 阻止
            sys.exit(2)
        else:
            if message:
                print(message)
            sys.exit(0)

    elif cmd == "deactivate":
        reason = sys.argv[2] if len(sys.argv) > 2 else "manual"
        try:
            os.environ["MEMEX_HOOK_CALLER"] = "cli"
            deactivate(reason)
        finally:
            os.environ.pop("MEMEX_HOOK_CALLER", None)
        print(f"[persistent-mode] Deactivated: {reason}")
        sys.exit(0)

    elif cmd == "complete":
        force = "--force" in sys.argv
        success = mark_completed(force=force)
        if success:
            print("[persistent-mode] Task marked completed")
            sys.exit(0)
        else:
            print("[persistent-mode] BLOCKED: unmet criteria. Use --force to override.", file=sys.stderr)
            sys.exit(2)

    elif cmd == "status":
        status = get_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        sys.exit(0)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
