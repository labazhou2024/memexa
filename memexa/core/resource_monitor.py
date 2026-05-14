"""
Resource Monitoring for Long-Running Tasks

Monitors: API quota, memory, disk, network, CPU
Alerts when resources are low
"""
import os
import sys
import psutil
import json
import time
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, Callable, List
from dataclasses import dataclass, field, asdict
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


@dataclass
class ResourceSnapshot:
    """Snapshot of system resources at a point in time"""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    # Memory (MB)
    memory_total_mb: float = 0.0
    memory_available_mb: float = 0.0
    memory_used_mb: float = 0.0
    memory_percent: float = 0.0
    
    # Disk (MB)
    disk_total_mb: float = 0.0
    disk_free_mb: float = 0.0
    disk_used_mb: float = 0.0
    disk_percent: float = 0.0
    
    # CPU
    cpu_percent: float = 0.0
    cpu_count: int = 0
    
    # API usage (tracked externally)
    api_calls: int = 0
    api_tokens_used: int = 0
    api_quota_remaining: Optional[int] = None
    
    # Process info
    process_memory_mb: float = 0.0
    process_cpu_percent: float = 0.0
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ResourceThresholds:
    """Thresholds for resource alerts"""
    memory_warning: float = 80.0  # %
    memory_critical: float = 90.0  # %
    disk_warning: float = 85.0  # %
    disk_critical: float = 95.0  # %
    api_calls_warning: int = 1000  # calls
    api_calls_critical: int = 1500  # calls
    
    def check(self, snapshot: ResourceSnapshot) -> List[Dict]:
        """Check snapshot against thresholds, return alerts"""
        alerts = []
        
        # Memory
        if snapshot.memory_percent >= self.memory_critical:
            alerts.append({
                "level": "CRITICAL",
                "resource": "memory",
                "value": snapshot.memory_percent,
                "threshold": self.memory_critical,
                "message": f"Memory usage critical: {snapshot.memory_percent:.1f}%"
            })
        elif snapshot.memory_percent >= self.memory_warning:
            alerts.append({
                "level": "WARNING",
                "resource": "memory",
                "value": snapshot.memory_percent,
                "threshold": self.memory_warning,
                "message": f"Memory usage high: {snapshot.memory_percent:.1f}%"
            })
        
        # Disk
        if snapshot.disk_percent >= self.disk_critical:
            alerts.append({
                "level": "CRITICAL",
                "resource": "disk",
                "value": snapshot.disk_percent,
                "threshold": self.disk_critical,
                "message": f"Disk usage critical: {snapshot.disk_percent:.1f}%"
            })
        elif snapshot.disk_percent >= self.disk_warning:
            alerts.append({
                "level": "WARNING",
                "resource": "disk",
                "value": snapshot.disk_percent,
                "threshold": self.disk_warning,
                "message": f"Disk usage high: {snapshot.disk_percent:.1f}%"
            })
        
        # API calls
        if snapshot.api_calls >= self.api_calls_critical:
            alerts.append({
                "level": "CRITICAL",
                "resource": "api_calls",
                "value": snapshot.api_calls,
                "threshold": self.api_calls_critical,
                "message": f"API calls near limit: {snapshot.api_calls}"
            })
        elif snapshot.api_calls >= self.api_calls_warning:
            alerts.append({
                "level": "WARNING",
                "resource": "api_calls",
                "value": snapshot.api_calls,
                "threshold": self.api_calls_warning,
                "message": f"API calls warning: {snapshot.api_calls}"
            })
        
        return alerts


class ResourceMonitor:
    """
    Monitor system resources and API usage.
    
    Usage:
        monitor = ResourceMonitor()
        monitor.start()  # Start background monitoring
        
        # In your task
        monitor.record_api_call(tokens=150)
        
        # Check status
        if monitor.has_critical_alert():
            pause_execution()
    """
    
    def __init__(self, 
                 log_dir: Optional[Path] = None,
                 check_interval: int = 30,
                 thresholds: Optional[ResourceThresholds] = None):
        
        self.log_dir = log_dir or Path(".claude/harness/logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.check_interval = check_interval
        self.thresholds = thresholds or ResourceThresholds()
        
        # State
        self.snapshots: List[ResourceSnapshot] = []
        self.max_snapshots = 1000  # Keep last N snapshots
        self.current_snapshot: Optional[ResourceSnapshot] = None
        self.alerts: List[Dict] = []
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        
        # API tracking
        self._api_calls = 0
        self._api_tokens = 0
        
        # Callbacks
        self._alert_callbacks: List[Callable[[Dict], None]] = []
        
        # Get process info
        self._process = psutil.Process()
    
    def capture_snapshot(self) -> ResourceSnapshot:
        """Capture current resource state"""
        # System memory
        mem = psutil.virtual_memory()
        
        # Disk (current working directory)
        disk = psutil.disk_usage(".")
        
        # CPU
        cpu_percent = psutil.cpu_percent(interval=0.1)
        
        # Process info
        try:
            proc_mem = self._process.memory_info()
            proc_cpu = self._process.cpu_percent()
            process_memory_mb = proc_mem.rss / 1024 / 1024
        except Exception:
            process_memory_mb = 0.0
            proc_cpu = 0.0
        
        snapshot = ResourceSnapshot(
            memory_total_mb=mem.total / 1024 / 1024,
            memory_available_mb=mem.available / 1024 / 1024,
            memory_used_mb=mem.used / 1024 / 1024,
            memory_percent=mem.percent,
            disk_total_mb=disk.total / 1024 / 1024,
            disk_free_mb=disk.free / 1024 / 1024,
            disk_used_mb=disk.used / 1024 / 1024,
            disk_percent=(disk.used / disk.total) * 100,
            cpu_percent=cpu_percent,
            cpu_count=psutil.cpu_count(),
            api_calls=self._api_calls,
            api_tokens_used=self._api_tokens,
            process_memory_mb=process_memory_mb,
            process_cpu_percent=proc_cpu,
        )
        
        self.current_snapshot = snapshot
        
        # Store snapshot
        self.snapshots.append(snapshot)
        if len(self.snapshots) > self.max_snapshots:
            self.snapshots.pop(0)
        
        # Check thresholds
        new_alerts = self.thresholds.check(snapshot)
        for alert in new_alerts:
            if alert not in self.alerts:
                self.alerts.append(alert)
                # Trigger callbacks
                for callback in self._alert_callbacks:
                    try:
                        callback(alert)
                    except Exception as e:
                        logger.error(f"Alert callback error: {e}")
        
        return snapshot
    
    async def _monitor_loop(self):
        """Background monitoring loop"""
        while self._running:
            try:
                snapshot = self.capture_snapshot()
                
                # Log to file periodically
                if len(self.snapshots) % 10 == 0:
                    self._write_log()
                
                # Check for critical conditions
                critical_alerts = [a for a in self.alerts if a["level"] == "CRITICAL"]
                if critical_alerts:
                    logger.critical(f"Resource critical: {critical_alerts}")
                
            except Exception as e:
                logger.error(f"Monitor error: {e}")
            
            await asyncio.sleep(self.check_interval)
    
    def _write_log(self):
        """Write resource log to file"""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        log_file = self.log_dir / f"resource_{timestamp}.jsonl"
        
        with open(log_file, 'a', encoding='utf-8') as f:
            for snapshot in self.snapshots[-10:]:  # Last 10 snapshots
                f.write(json.dumps(snapshot.to_dict(), ensure_ascii=False) + "\n")
    
    def start(self):
        """Start background monitoring"""
        if self._running:
            return
        
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Resource monitor started")
    
    def stop(self):
        """Stop background monitoring"""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        logger.info("Resource monitor stopped")
    
    def record_api_call(self, tokens: int = 0):
        """Record an API call"""
        self._api_calls += 1
        self._api_tokens += tokens
        
        if self.current_snapshot:
            self.current_snapshot.api_calls = self._api_calls
            self.current_snapshot.api_tokens_used = self._api_tokens
    
    def update_api_quota(self, remaining: int):
        """Update API quota info"""
        if self.current_snapshot:
            self.current_snapshot.api_quota_remaining = remaining
    
    def on_alert(self, callback: Callable[[Dict], None]):
        """Register alert callback"""
        self._alert_callbacks.append(callback)
    
    def has_critical_alert(self) -> bool:
        """Check if there are any critical alerts"""
        return any(a["level"] == "CRITICAL" for a in self.alerts)
    
    def has_warning_alert(self) -> bool:
        """Check if there are any warning alerts"""
        return any(a["level"] == "WARNING" for a in self.alerts)
    
    def get_latest_snapshot(self) -> Optional[ResourceSnapshot]:
        """Get most recent snapshot"""
        return self.current_snapshot
    
    def get_summary(self) -> Dict:
        """Get resource usage summary"""
        if not self.snapshots:
            return {}
        
        latest = self.snapshots[-1]
        
        # Calculate trends (last 10 snapshots)
        recent = self.snapshots[-10:]
        memory_trend = recent[-1].memory_percent - recent[0].memory_percent if len(recent) > 1 else 0
        
        return {
            "current": latest.to_dict(),
            "alerts": self.alerts,
            "alert_count": len(self.alerts),
            "critical_count": sum(1 for a in self.alerts if a["level"] == "CRITICAL"),
            "monitoring_duration_minutes": len(self.snapshots) * self.check_interval / 60,
            "memory_trend_percent": memory_trend,
        }
    
    def export_report(self, path: Optional[Path] = None) -> Path:
        """Export full resource report"""
        path = path or self.log_dir / f"resource_report_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
        
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": self.get_summary(),
            "thresholds": asdict(self.thresholds),
            "snapshots": [s.to_dict() for s in self.snapshots],
        }
        
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        
        return path


# Convenience functions

def get_quick_snapshot() -> Dict:
    """Get quick resource snapshot without starting monitor"""
    monitor = ResourceMonitor()
    snapshot = monitor.capture_snapshot()
    return snapshot.to_dict()


def check_resources() -> bool:
    """Quick check if resources are OK"""
    monitor = ResourceMonitor()
    monitor.capture_snapshot()
    return not monitor.has_critical_alert()


# Example usage
if __name__ == "__main__":
    async def example():
        monitor = ResourceMonitor(check_interval=5)
        
        # Add alert handler
        def on_alert(alert):
            print(f"ALERT: {alert['level']} - {alert['message']}")
        
        monitor.on_alert(on_alert)
        
        # Start monitoring
        monitor.start()
        
        # Simulate work
        for i in range(3):
            monitor.record_api_call(tokens=100)
            print(f"Snapshot {i+1}: {monitor.get_latest_snapshot().memory_percent:.1f}% memory")
            await asyncio.sleep(6)
        
        # Stop and report
        monitor.stop()
        print("\nSummary:")
        print(json.dumps(monitor.get_summary(), indent=2))
    
    asyncio.run(example())
