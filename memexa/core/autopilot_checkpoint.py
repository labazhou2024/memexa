"""Mid-session checkpoint + /autopilot --resume (U11 long_term_plan_v2 Phase 3).

Provides crash-recoverable state snapshots for 15-50h autopilot runs.

Triggers
--------
- Every persistent_mode.refresh_stage() -> trigger="stage_transition"
- Every 5th task_unit_scheduler.mark_done() -> trigger="tu_milestone"

Storage
-------
<task_dir>/checkpoints/checkpoint_<stage>_<utc_ts>.json (~few KB; capped <50KB)

Resume
------
python -m memexa.core.autopilot_checkpoint resume <tid>
  -> restores persistent_mode_state + scheduler.state.mark_done_count
  -> emits autopilot_resumed trace
  -> prints git_stash_ref for CEO to manually `git stash apply <ref>` if needed

Schema (v1)
-----------
{
  "schema_version": 1,
  "task_id": "<id>",
  "stage": "<stage_name>",
  "trigger": "stage_transition|tu_milestone",
  "created_at": <epoch_float>,
  "active_tu_id": "TU-N|null",
  "git_stash_ref": "<40hex>|null",
  "persistent_mode_state": {...},
  "scheduler_state": {
    "units": [...],
    "current_unit_idx": int,
    "mark_done_count": int,
    "last_updated": float,
  },
  "env_snapshot": {<filtered>},  # secrets denylisted
}

Security
--------
- task_id charset enforced ^[A-Za-z0-9_]{1,80}$ (path-traversal guard)
- target dir resolve()+is_relative_to() guard
- NTFS reparse-point check on Windows before mkdir
- env_snapshot denylist: drop keys matching (KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API)
- subprocess git: args=list (no shell=True) + timeout=10s
- copied prior git_stash_ref validated ^[0-9a-f]{40}$ before reuse
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_SCHEMA_VERSION = 1
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_GIT_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_SECRET_KEY_RE = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API|"
    r"AUTHORIZATION|BEARER|JWT|PASSPHRASE|PRIVATE)",
    re.IGNORECASE)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_DEFAULT_GIT_TIMEOUT_SEC = 10.0
_MAX_CHECKPOINT_BYTES = 50 * 1024


def _validate_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        raise ValueError(
            f"invalid task_id (must match {_TASK_ID_RE.pattern}): "
            f"{task_id!r}"
        )
    return task_id


def _checkpoints_dir(task_id: str) -> Path:
    """Return <task_dir>/checkpoints/, with traversal + reparse-point guards.

    Per security-iter2-2: resolve()+is_relative_to() asserted.
    Per security-iter2-3: Windows reparse-point bit checked.
    Per security-iter2 charset: task_id pre-validated.
    """
    _validate_task_id(task_id)
    from memexa.core.task_dir_layout import task_dir
    base = task_dir(task_id).resolve()
    if not base.exists():
        raise FileNotFoundError(f"task_dir does not exist: {base}")

    if sys.platform == "win32":
        # security-code-iter1-2 fix: fail-CLOSED on stat error (was fail-open).
        # An attacker who can race the stat call could cause silent bypass
        # of the reparse-point guard. Treat stat failure as untrustworthy.
        try:
            attrs = getattr(os.stat(base), "st_file_attributes", 0)
        except OSError as e:
            _emit_trace(task_id, "gate_infra_error", {
                "where": "autopilot_checkpoint._checkpoints_dir",
                "reason": f"stat_failed_fail_closed: {e}"[:200],
                "path": str(base),
            })
            raise PermissionError(
                f"refusing checkpoints because stat() failed: {base}: {e}"
            )
        if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
            _emit_trace(task_id, "gate_infra_error", {
                "where": "autopilot_checkpoint._checkpoints_dir",
                "reason": "reparse_point_detected",
                "path": str(base),
            })
            raise PermissionError(
                f"refusing checkpoints under reparse-point: {base}"
            )

    target = (base / "checkpoints").resolve()
    if not _is_relative_to(target, base):
        raise ValueError(
            f"checkpoints dir escapes task_dir: target={target} base={base}"
        )
    return target


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _emit_trace(task_id: str, event: str, payload: Dict[str, Any]) -> None:
    """Write to per-task trace.jsonl + global trace_sink. Fail-soft."""
    try:
        from memexa.core.task_dir_layout import append_trace
        append_trace(task_id, event, payload)
    except Exception:
        pass
    try:
        from memexa.core.trace_sink import write_trace_event
        write_trace_event(event, {"task_id": task_id, **payload})
    except Exception:
        pass


def _filter_env() -> Dict[str, str]:
    """Snapshot os.environ minus secret-suspicious keys (denylist regex)."""
    out = {}
    for k, v in os.environ.items():
        if _SECRET_KEY_RE.search(k):
            continue
        if not isinstance(v, str):
            continue
        if len(v) > 4096:
            continue
        out[k] = v
    return out


def _create_git_stash_ref(cwd: Optional[Path] = None) -> Optional[str]:
    """Run `git stash create` (does NOT push to stash list). Returns SHA40 or None."""
    if cwd is None:
        cwd = Path.cwd()
    try:
        proc = subprocess.run(
            ["git", "stash", "create"],
            capture_output=True, text=True,
            timeout=_DEFAULT_GIT_TIMEOUT_SEC, cwd=str(cwd),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    if not sha:
        return None
    if not _GIT_HEX40_RE.match(sha):
        return None
    return sha


def _validate_stash_ref(ref: Any) -> Optional[str]:
    if not isinstance(ref, str):
        return None
    if _GIT_HEX40_RE.match(ref):
        return ref
    return None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_stage_label(stage: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", stage)[:60] or "unknown"


def _load_persistent_mode_state() -> Optional[Dict[str, Any]]:
    try:
        from memexa.core.persistent_mode import _load_state
        return _load_state()
    except Exception:
        return None


def _load_scheduler_state(task_id: str) -> Optional[Dict[str, Any]]:
    try:
        from memexa.core.task_dir_layout import load_state
        s = load_state(task_id) or {}
        return {
            "units": s.get("units", []),
            "current_unit_idx": s.get("current_unit_idx", -1),
            "mark_done_count": s.get("mark_done_count", 0),
            "last_updated": s.get("last_updated", 0.0),
        }
    except Exception:
        return None


def _active_tu_id(scheduler_state: Optional[Dict[str, Any]]) -> Optional[str]:
    if not scheduler_state:
        return None
    units = scheduler_state.get("units") or []
    idx = scheduler_state.get("current_unit_idx", -1)
    if 0 <= idx < len(units):
        u = units[idx]
        return u.get("id") if isinstance(u, dict) else None
    return None


def write_checkpoint(
    task_id: str,
    stage: str,
    trigger: str,
    cwd: Optional[Path] = None,
) -> Optional[Path]:
    """Snapshot current state to <task_dir>/checkpoints/checkpoint_<stage>_<ts>.json.

    Returns path on success, None on failure (fail-soft for hot-path callers).
    """
    try:
        ck_dir = _checkpoints_dir(task_id)
    except (ValueError, FileNotFoundError, PermissionError) as e:
        _emit_trace(task_id, "gate_infra_error", {
            "where": "write_checkpoint",
            "reason": str(e)[:200],
        })
        return None

    ck_dir.mkdir(parents=True, exist_ok=True)

    if trigger == "stage_transition":
        stash_ref = _create_git_stash_ref(cwd=cwd)
    elif trigger == "tu_milestone":
        prev = latest_checkpoint(task_id)
        stash_ref = None
        if prev is not None:
            try:
                with prev.open("r", encoding="utf-8") as f:
                    prev_data = json.load(f)
                stash_ref = _validate_stash_ref(prev_data.get("git_stash_ref"))
            except (OSError, json.JSONDecodeError, ValueError):
                stash_ref = None
    else:
        stash_ref = None

    pm_state = _load_persistent_mode_state()
    sch_state = _load_scheduler_state(task_id)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "task_id": task_id,
        "stage": stage,
        "trigger": trigger,
        "created_at": time.time(),
        "active_tu_id": _active_tu_id(sch_state),
        "git_stash_ref": stash_ref,
        "persistent_mode_state": pm_state,
        "scheduler_state": sch_state,
        "env_snapshot": _filter_env(),
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(raw.encode("utf-8")) > _MAX_CHECKPOINT_BYTES:
        # Truncate env_snapshot if too large.
        payload["env_snapshot"] = {"_truncated": True}
        raw = json.dumps(payload, ensure_ascii=False, indent=2)

    fname = f"checkpoint_{_safe_stage_label(stage)}_{_utc_stamp()}.json"
    target = ck_dir / fname
    if target.exists():
        target = ck_dir / f"checkpoint_{_safe_stage_label(stage)}_{_utc_stamp()}_{os.getpid()}.json"
    tmp = target.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(raw)
        os.replace(str(tmp), str(target))
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        _emit_trace(task_id, "gate_infra_error", {
            "where": "write_checkpoint",
            "reason": f"write_failure: {e}"[:200],
        })
        return None

    _emit_trace(task_id, "checkpoint_written", {
        "stage": stage,
        "trigger": trigger,
        "checkpoint_path": str(target),
        "git_stash_ref": stash_ref,
        "active_tu_id": payload["active_tu_id"],
    })
    return target


def list_checkpoints(task_id: str) -> List[Path]:
    """Return checkpoint paths in chronological order (oldest first)."""
    try:
        ck_dir = _checkpoints_dir(task_id)
    except (ValueError, FileNotFoundError, PermissionError):
        return []
    if not ck_dir.is_dir():
        return []
    paths = sorted(ck_dir.glob("checkpoint_*.json"))
    return [p for p in paths if p.is_file()]


def latest_checkpoint(task_id: str) -> Optional[Path]:
    paths = list_checkpoints(task_id)
    return paths[-1] if paths else None


def read_checkpoint(path: Path) -> Dict[str, Any]:
    """Load and return checkpoint dict. Raises on parse failure."""
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"checkpoint not a dict: {path}")
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version {data.get('schema_version')!r} in {path}"
        )
    return data


def resume(task_id: str, ckpt_path: Optional[Path] = None) -> Dict[str, Any]:
    """Restore state from checkpoint and return a summary dict.

    Restores persistent_mode_state + scheduler_state.mark_done_count to disk.
    Does NOT auto-pop git stash; ref is returned for CEO to apply manually.
    """
    if ckpt_path is None:
        ckpt_path = latest_checkpoint(task_id)
    if ckpt_path is None:
        return {"ok": False, "reason": "no_checkpoint_found", "task_id": task_id}

    try:
        data = read_checkpoint(Path(ckpt_path))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return {"ok": False, "reason": f"read_failure: {e}", "task_id": task_id}

    pm_state = data.get("persistent_mode_state")
    if isinstance(pm_state, dict):
        try:
            from memexa.core.persistent_mode import _save_state
            _save_state(pm_state)
        except Exception as e:
            _emit_trace(task_id, "gate_infra_error", {
                "where": "resume.restore_persistent",
                "reason": str(e)[:200],
            })

    sch_state = data.get("scheduler_state")
    restored_count = None
    sch_restore_ok = True
    if isinstance(sch_state, dict):
        try:
            from memexa.core.task_dir_layout import update_state
            target_count = sch_state.get("mark_done_count", 0)
            target_idx = sch_state.get("current_unit_idx", -1)

            def _set(state):
                state["mark_done_count"] = target_count
                if target_idx >= 0:
                    state["current_unit_idx"] = target_idx
                state["last_updated"] = time.time()
                state["resumed_at"] = time.time()
                return state
            # logic-code-iter1-2 fix: respect update_state return value;
            # silent failure (e.g. task_dir gone) must not falsely report ok.
            ok_write = update_state(task_id, _set)
            if ok_write:
                restored_count = target_count
            else:
                sch_restore_ok = False
                _emit_trace(task_id, "gate_infra_error", {
                    "where": "resume.restore_scheduler",
                    "reason": "update_state_returned_false",
                })
        except Exception as e:
            sch_restore_ok = False
            _emit_trace(task_id, "gate_infra_error", {
                "where": "resume.restore_scheduler",
                "reason": str(e)[:200],
            })

    summary = {
        "ok": sch_restore_ok,
        "task_id": task_id,
        "from_path": str(ckpt_path),
        "restored_stage": data.get("stage"),
        "restored_tu": data.get("active_tu_id"),
        "git_stash_ref": data.get("git_stash_ref"),
        "mark_done_count": restored_count,
    }
    if not sch_restore_ok:
        summary["reason"] = "scheduler_restore_failed"
    _emit_trace(task_id, "autopilot_resumed", summary)
    return summary


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="autopilot_checkpoint")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_w = sub.add_parser("write", help="Write a checkpoint now")
    sp_w.add_argument("task_id")
    sp_w.add_argument("stage")
    sp_w.add_argument("--trigger", default="manual")

    sp_r = sub.add_parser("resume", help="Resume from latest (or --from)")
    sp_r.add_argument("task_id")
    sp_r.add_argument("--from", dest="from_path", default=None)

    sp_l = sub.add_parser("list", help="List checkpoint paths for task")
    sp_l.add_argument("task_id")

    sp_s = sub.add_parser("show", help="Show parsed checkpoint JSON")
    sp_s.add_argument("path")

    args = parser.parse_args(argv)

    if args.cmd == "write":
        p = write_checkpoint(args.task_id, args.stage, args.trigger)
        if p is None:
            print(json.dumps({"ok": False, "reason": "write_failed"}))
            return 1
        print(json.dumps({"ok": True, "path": str(p)}))
        return 0

    if args.cmd == "resume":
        ck_path = Path(args.from_path) if args.from_path else None
        summary = resume(args.task_id, ck_path)
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if summary.get("ok") else 1

    if args.cmd == "list":
        paths = list_checkpoints(args.task_id)
        print(json.dumps({"ok": True, "count": len(paths),
                          "paths": [str(p) for p in paths]}))
        return 0

    if args.cmd == "show":
        try:
            data = read_checkpoint(Path(args.path))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(json.dumps({"ok": False, "reason": str(e)}))
            return 1
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
