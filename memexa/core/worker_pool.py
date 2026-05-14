"""Worker Pool — parallel KAIROS project execution via asyncio.to_thread.

Wraps blocking subprocess.run("claude -p ...") calls in asyncio.to_thread(),
enabling multiple projects to execute concurrently within the KAIROS daemon.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class WorkerState(Enum):
    IDLE = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()


@dataclass
class WorkerSlot:
    slot_id: int
    state: WorkerState = WorkerState.IDLE
    project_id: Optional[str] = None
    project_title: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    cost_usd: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.name.lower()
        return d


@dataclass
class PoolStatus:
    active_workers: int
    idle_workers: int
    total_completed: int
    total_failed: int
    total_cost_usd: float
    workers: List[dict]


class WorkerPool:
    """Parallel KAIROS execution pool.

    Wraps blocking execute_project() calls in asyncio.to_thread(),
    enabling multiple projects to execute concurrently.

    Usage:
        pool = WorkerPool(max_workers=2, status_file=Path("status.json"))
        slot_id = await pool.submit(project_dict, execute_project)
        await pool.drain()  # wait for all to finish
    """

    def __init__(self, max_workers: int = 2,
                 status_file: Optional[Path] = None,
                 on_status_change: Optional[Callable] = None):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._max_workers = max_workers
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._slots: Dict[int, WorkerSlot] = {
            i: WorkerSlot(slot_id=i) for i in range(max_workers)
        }
        self._completed_count = 0
        self._failed_count = 0
        self._total_cost = 0.0
        self._lock: Optional[asyncio.Lock] = None
        self._tasks: Dict[int, asyncio.Task] = {}
        self._status_file = status_file
        self._on_status_change = on_status_change
        self._paused = False
        self._history: List[dict] = []  # Recent completion history

    def _ensure_async_primitives(self):
        """Lazily create asyncio primitives (requires running event loop in 3.9)."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_workers)
        if self._lock is None:
            self._lock = asyncio.Lock()

    @property
    def max_workers(self) -> int:
        return self._max_workers

    async def submit(self, project: dict, execute_fn: Callable) -> int:
        """Submit a project for execution. Returns slot_id.

        Blocks (awaits) if all worker slots are currently busy.
        The execute_fn should be a sync function — it will be run
        in a thread via asyncio.to_thread().

        Sets slot to RUNNING synchronously before returning, so that
        get_status() immediately reflects the active worker count.
        """
        self._ensure_async_primitives()
        slot_id = await self._acquire_slot()

        # Mark RUNNING before creating the task — prevents race where
        # daemon loop sees active_workers==0 between submit() and _run_in_slot()
        slot = self._slots[slot_id]
        slot.state = WorkerState.RUNNING
        slot.project_id = project.get("id", "")
        slot.project_title = (project.get("title") or "")[:80]
        slot.started_at = datetime.now(timezone.utc).isoformat()
        slot.completed_at = None
        slot.error = None
        slot.cost_usd = 0.0

        task = asyncio.create_task(
            self._run_in_slot(slot_id, project, execute_fn)
        )
        self._tasks[slot_id] = task
        self._write_status()
        return slot_id

    async def _acquire_slot(self) -> int:
        """Wait for an available slot and return its id."""
        await self._semaphore.acquire()
        async with self._lock:
            for sid, slot in self._slots.items():
                if slot.state in (WorkerState.IDLE, WorkerState.COMPLETED,
                                  WorkerState.FAILED):
                    return sid
        # Fallback — should not happen if semaphore is correct
        return 0

    async def _run_in_slot(self, slot_id: int, project: dict,
                           execute_fn: Callable) -> dict:
        """Execute a project in the given worker slot.

        Note: slot is already marked RUNNING by submit() before this starts.
        """
        slot = self._slots[slot_id]

        final_result = {"success": False}
        try:
            final_result = await asyncio.to_thread(execute_fn, project)

            slot.cost_usd = final_result.get("cost_usd", 0)
            async with self._lock:
                self._total_cost += slot.cost_usd
                if final_result.get("success"):
                    slot.state = WorkerState.COMPLETED
                    self._completed_count += 1
                else:
                    slot.state = WorkerState.FAILED
                    slot.error = (final_result.get("error") or "")[:200]
                    self._failed_count += 1

            return final_result

        except Exception as e:
            slot.state = WorkerState.FAILED
            slot.error = str(e)[:200]
            final_result = {"success": False, "error": str(e)}
            async with self._lock:
                self._failed_count += 1
            return final_result

        finally:
            slot.completed_at = datetime.now(timezone.utc).isoformat()
            self._record_history(slot, final_result)
            self._semaphore.release()
            self._write_status()

    def get_status(self) -> PoolStatus:
        """Return current pool status snapshot."""
        active = sum(
            1 for s in self._slots.values()
            if s.state == WorkerState.RUNNING
        )
        idle = self._max_workers - active
        return PoolStatus(
            active_workers=active,
            idle_workers=idle,
            total_completed=self._completed_count,
            total_failed=self._failed_count,
            total_cost_usd=round(self._total_cost, 4),
            workers=[s.to_dict() for s in self._slots.values()],
        )

    def _write_status(self):
        """Persist pool status to JSON file and notify callback."""
        status = self.get_status()

        # Notify callback (e.g., WebSocket broadcast)
        if self._on_status_change:
            try:
                self._on_status_change(status)
            except Exception:
                pass

        if not self._status_file:
            return
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pool": {
                    "active_workers": status.active_workers,
                    "idle_workers": status.idle_workers,
                    "total_completed": status.total_completed,
                    "total_failed": status.total_failed,
                    "total_cost_usd": status.total_cost_usd,
                },
                "workers": status.workers,
            }
            self._status_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _record_history(self, slot: WorkerSlot, result: dict):
        """Record completed worker to history (for /api/workers/history)."""
        entry = {
            "slot_id": slot.slot_id,
            "project_id": slot.project_id,
            "project_title": slot.project_title,
            "state": slot.state.name.lower(),
            "started_at": slot.started_at,
            "completed_at": slot.completed_at,
            "cost_usd": slot.cost_usd,
            "success": result.get("success", False),
            "error": slot.error,
        }
        self._history.append(entry)
        # Keep only last 50
        if len(self._history) > 50:
            self._history = self._history[-50:]

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool):
        self._paused = value
        self._write_status()

    def get_history(self, last_n: int = 20) -> List[dict]:
        """Return recent completion history."""
        return self._history[-last_n:]

    async def drain(self, timeout: float = 1200):
        """Wait for all active tasks to complete (with timeout)."""
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def cancel_worker(self, slot_id: int) -> bool:
        """Cancel a specific worker's task. Returns True if cancelled."""
        task = self._tasks.get(slot_id)
        if task and not task.done():
            task.cancel()
            slot = self._slots.get(slot_id)
            if slot:
                slot.state = WorkerState.FAILED
                slot.error = "Cancelled by user"
                slot.completed_at = datetime.now(timezone.utc).isoformat()
            self._write_status()
            return True
        return False

    async def cancel_all(self):
        """Cancel all running tasks."""
        for t in self._tasks.values():
            if not t.done():
                t.cancel()
