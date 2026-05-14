"""
Async Execution Framework for Long-Running Tasks

Features:
- Background task execution
- Progress reporting
- Cancellation support
- Result callbacks
"""
import asyncio
import functools
from typing import Callable, Any, Optional, Dict, List
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
import uuid


class TaskState(Enum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()


@dataclass
class BackgroundTask:
    """Represents a background task"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = "unnamed"
    state: TaskState = TaskState.PENDING
    result: Any = None
    error: Optional[Exception] = None
    progress: float = 0.0  # 0-100
    message: str = ""
    
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    
    _asyncio_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _cancelled: bool = False


class AsyncExecutor:
    """
    Execute tasks in background with progress tracking.
    
    Usage:
        executor = AsyncExecutor()
        
        # Start background task
        task = await executor.submit(my_async_func, args=(data,))
        
        # Check progress
        print(f"Progress: {task.progress}%")
        
        # Wait for completion
        result = await executor.wait(task.id)
    """
    
    def __init__(self, max_concurrent: int = 5):
        self.max_concurrent = max_concurrent
        self.tasks: Dict[str, BackgroundTask] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._progress_callbacks: Dict[str, List[Callable]] = {}
    
    async def submit(self, 
                     func: Callable,
                     args: tuple = (),
                     kwargs: Optional[Dict] = None,
                     name: Optional[str] = None) -> BackgroundTask:
        """Submit a function for background execution"""
        kwargs = kwargs or {}
        
        task = BackgroundTask(name=name or func.__name__)
        self.tasks[task.id] = task
        
        # Create async task
        async def wrapper():
            async with self._semaphore:
                if task._cancelled:
                    task.state = TaskState.CANCELLED
                    return
                
                task.state = TaskState.RUNNING
                task.started_at = datetime.now(timezone.utc).isoformat()
                
                try:
                    if asyncio.iscoroutinefunction(func):
                        result = await func(*args, **kwargs)
                    else:
                        loop = asyncio.get_event_loop()
                        result = await loop.run_in_executor(None, 
                            functools.partial(func, *args, **kwargs))
                    
                    task.result = result
                    task.state = TaskState.COMPLETED
                    task.progress = 100.0
                    
                except asyncio.CancelledError:
                    task.state = TaskState.CANCELLED
                    raise
                except Exception as e:
                    task.error = e
                    task.state = TaskState.FAILED
                finally:
                    task.completed_at = datetime.now(timezone.utc).isoformat()
        
        task._asyncio_task = asyncio.create_task(wrapper())
        return task
    
    async def wait(self, task_id: str, timeout: Optional[float] = None) -> Any:
        """Wait for task completion and return result"""
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        if task._asyncio_task:
            try:
                await asyncio.wait_for(task._asyncio_task, timeout=timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Task {task_id} timed out")
        
        if task.state == TaskState.FAILED and task.error:
            raise task.error
        
        if task.state == TaskState.CANCELLED:
            raise asyncio.CancelledError(f"Task {task_id} was cancelled")
        
        return task.result
    
    def cancel(self, task_id: str) -> bool:
        """Cancel a running task"""
        task = self.tasks.get(task_id)
        if not task or not task._asyncio_task:
            return False
        
        task._cancelled = True
        task._asyncio_task.cancel()
        return True
    
    def update_progress(self, task_id: str, progress: float, message: str = ""):
        """Update task progress (call from within task)"""
        task = self.tasks.get(task_id)
        if task:
            task.progress = min(100.0, max(0.0, progress))
            if message:
                task.message = message
            
            # Trigger callbacks
            for callback in self._progress_callbacks.get(task_id, []):
                try:
                    callback(task.progress, task.message)
                except Exception:
                    pass
    
    def on_progress(self, task_id: str, callback: Callable[[float, str], None]):
        """Register progress callback"""
        if task_id not in self._progress_callbacks:
            self._progress_callbacks[task_id] = []
        self._progress_callbacks[task_id].append(callback)
    
    def get_status(self, task_id: Optional[str] = None) -> Dict:
        """Get task status"""
        if task_id:
            task = self.tasks.get(task_id)
            if not task:
                return {"error": "Task not found"}
            return {
                "id": task.id,
                "name": task.name,
                "state": task.state.name,
                "progress": task.progress,
                "message": task.message,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "completed_at": task.completed_at,
            }
        else:
            return {
                "total": len(self.tasks),
                "pending": sum(1 for t in self.tasks.values() if t.state == TaskState.PENDING),
                "running": sum(1 for t in self.tasks.values() if t.state == TaskState.RUNNING),
                "completed": sum(1 for t in self.tasks.values() if t.state == TaskState.COMPLETED),
                "failed": sum(1 for t in self.tasks.values() if t.state == TaskState.FAILED),
            }


# Decorator for progress-aware functions
def with_progress(func: Callable) -> Callable:
    """Decorator to make function report progress"""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Get executor from kwargs or context
        executor = kwargs.pop('_executor', None)
        task_id = kwargs.pop('_task_id', None)
        
        # Create progress reporter
        def report_progress(pct: float, msg: str = ""):
            if executor and task_id:
                executor.update_progress(task_id, pct, msg)
        
        # Add progress reporter to kwargs
        kwargs['_progress'] = report_progress
        
        return await func(*args, **kwargs)
    
    return wrapper


# Convenience function for fire-and-forget
async def run_in_background(func: Callable, 
                            *args, 
                            executor: Optional[AsyncExecutor] = None,
                            **kwargs) -> str:
    """
    Run function in background, return task ID.
    
    Usage:
        task_id = await run_in_background(long_running_func, data)
        # ... do other work ...
        result = await executor.wait(task_id)
    """
    exec = executor or AsyncExecutor()
    task = await exec.submit(func, args=args, kwargs=kwargs)
    return task.id


# Example usage
if __name__ == "__main__":
    async def example():
        executor = AsyncExecutor()
        
        # Define a long-running task
        async def process_items(items: list, _progress=None):
            results = []
            for i, item in enumerate(items):
                await asyncio.sleep(0.1)  # Simulate work
                results.append(item * 2)
                
                if _progress:
                    _progress((i + 1) / len(items) * 100, f"Processed {i+1}/{len(items)}")
            
            return results
        
        # Submit task
        task = await executor.submit(
            process_items, 
            args=([1, 2, 3, 4, 5],),
            kwargs={"_progress": lambda p, m: print(f"{p:.0f}%: {m}")},
            name="process_items"
        )
        
        print(f"Task {task.id} started")
        
        # Poll status
        for _ in range(10):
            await asyncio.sleep(0.15)
            status = executor.get_status(task.id)
            print(f"Status: {status['state']}, Progress: {status['progress']:.0f}%")
            
            if status['state'] in ('COMPLETED', 'FAILED'):
                break
        
        # Get result
        result = await executor.wait(task.id)
        print(f"Result: {result}")
    
    asyncio.run(example())
