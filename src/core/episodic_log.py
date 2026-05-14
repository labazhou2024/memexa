"""
Episodic Log - Event Sourcing for Agent Actions
 episodic log - 基于事件溯源的Agent行为记录

Features:
- Append-only log
- Event sourcing pattern
- Async batch writes
"""

import json
import asyncio
import aiosqlite
import atexit
import signal
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """Single log entry"""
    timestamp: datetime
    agent: str
    action: str
    task_id: str
    details: Dict[str, Any]
    
    def to_dict(self) -> Dict:
        return {
            'timestamp': self.timestamp.isoformat(),
            'agent': self.agent,
            'action': self.action,
            'task_id': self.task_id,
            'details': self.details
        }


class EpisodicLog:
    """
    Append-only log for all agent actions.
    Uses event sourcing pattern for auditability.
    """
    
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).parent.parent / "data" / "episodic_log.db"
        self.db_path = db_path
        self._initialized = False
        self._buffer: List[LogEntry] = []
        self._buffer_size = 10
        self._buffer_lock = asyncio.Lock()  # 保护缓冲区的异步锁
        self._flush_task: Optional[asyncio.Task] = None
        
        # 注册关闭处理器
        self._register_shutdown_handlers()
    
    async def initialize(self):
        """Initialize database"""
        if self._initialized:
            return
            
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS episodic_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    agent TEXT NOT NULL,
                    action TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    details TEXT
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_task ON episodic_log(task_id)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent ON episodic_log(agent)
            """)
            await db.commit()
        
        self._initialized = True
        logger.info(f"Episodic Log initialized at {self.db_path}")
    
    async def log(self, agent: str, action: str, task_id: str, details: Dict = None, immediate: bool = False):
        """线程安全的日志记录"""
        entry = LogEntry(
            timestamp=datetime.now(),
            agent=agent,
            action=action,
            task_id=task_id,
            details=details or {}
        )
        
        async with self._buffer_lock:
            self._buffer.append(entry)
            
            # Flush if buffer is full or immediate write requested
            if immediate or len(self._buffer) >= self._buffer_size:
                await self._flush()
    
    async def _flush(self):
        """Flush buffer to database - 必须在buffer_lock保护下调用"""
        if not self._buffer:
            return
        
        # 复制缓冲区内容（最小化锁持有时间）
        entries_to_flush = self._buffer.copy()
        self._buffer = []
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for entry in entries_to_flush:
                    await db.execute("""
                        INSERT INTO episodic_log (timestamp, agent, action, task_id, details)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        entry.timestamp,
                        entry.agent,
                        entry.action,
                        entry.task_id,
                        json.dumps(entry.details)
                    ))
                await db.commit()
        except Exception as e:
            # 刷新失败，将条目恢复至缓冲区（调用者已持有锁，直接操作）
            logger.error(f"Failed to flush episodic log: {e}")
            self._buffer = entries_to_flush + self._buffer
            raise
    
    async def get_task_history(self, task_id: str) -> List[Dict]:
        """Get all logs for a task"""
        async with self._buffer_lock:
            await self._flush()  # Ensure all buffered logs are written
        
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """SELECT timestamp, agent, action, details 
                   FROM episodic_log 
                   WHERE task_id=? 
                   ORDER BY timestamp""",
                (task_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        'timestamp': r[0],
                        'agent': r[1],
                        'action': r[2],
                        'details': json.loads(r[3]) if r[3] else {}
                    }
                    for r in rows
                ]
    
    def _register_shutdown_handlers(self):
        """注册关闭处理器以确保数据刷新"""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, flushing episodic log...")
            try:
                # 尝试同步刷新
                if self._buffer:
                    import sqlite3
                    conn = sqlite3.connect(str(self.db_path))
                    for entry in self._buffer:
                        conn.execute(
                            """INSERT INTO episodic_log (timestamp, agent, action, task_id, details)
                               VALUES (?, ?, ?, ?, ?)""",
                            (entry.timestamp, entry.agent, entry.action, 
                             entry.task_id, json.dumps(entry.details))
                        )
                    conn.commit()
                    conn.close()
                    self._buffer = []
                    logger.info("Episodic log flushed successfully")
            except Exception as e:
                logger.error(f"Failed to flush on shutdown: {e}")
        
        # 注册信号处理器（仅在主线程中）
        try:
            # 检查是否在主线程
            import threading
            if threading.current_thread() is threading.main_thread():
                # 保存原始处理器
                self._orig_sigint = signal.signal(signal.SIGINT, signal_handler)
                self._orig_sigterm = signal.signal(signal.SIGTERM, signal_handler)
        except (ValueError, OSError, RuntimeError) as e:
            # 在非主线程或无法修改信号时静默失败
            logger.debug(f"Could not register signal handlers: {e}")
        
        # 注册atexit处理器
        atexit.register(self._atexit_handler)
    
    def _atexit_handler(self):
        """atexit处理器（尽可能刷新数据）"""
        if self._buffer:
            logger.warning(f"Episodic log has {len(self._buffer)} unflushed entries on exit")
    
    async def close(self):
        """Close and flush remaining logs"""
        async with self._buffer_lock:
            if self._buffer:
                await self._flush()


# Singleton
_episodic_log = None


def get_episodic_log() -> EpisodicLog:
    """Get singleton EpisodicLog instance"""
    global _episodic_log
    if _episodic_log is None:
        _episodic_log = EpisodicLog()
    return _episodic_log
