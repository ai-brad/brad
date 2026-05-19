"""Worker-side execution liveness tracking and graceful shutdown helpers."""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from brad import db
from brad.logging_config import get_logger


@dataclass
class ActiveExecution:
    execution_id: int
    issue_key: str
    phase: str = ""
    detail: str = ""
    started_monotonic: float = 0.0


class ExecutionLivenessTracker:
    """Tracks active executions for heartbeat, shutdown marking, and diagnostics."""

    def __init__(
        self,
        *,
        heartbeat_interval_seconds: int = 30,
        memory_log_interval_seconds: int = 300,
        worker_id: Optional[str] = None,
        worker_pid: Optional[int] = None,
    ):
        self.logger = get_logger(__name__)
        self.heartbeat_interval_seconds = max(5, int(heartbeat_interval_seconds))
        self.memory_log_interval_seconds = max(30, int(memory_log_interval_seconds))
        self.worker_pid = int(worker_pid or os.getpid())
        self.worker_id = worker_id or f"{socket.gethostname()}:{self.worker_pid}:{int(time.time())}"

        self._lock = threading.RLock()
        self._active: Dict[int, ActiveExecution] = {}
        self._active_order: list[int] = []
        self._peak_memory_rss_bytes = 0
        self._last_memory_log_monotonic = 0.0
        self._stop_event = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="brad-execution-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    @classmethod
    def from_env(cls) -> "ExecutionLivenessTracker":
        return cls(
            heartbeat_interval_seconds=int(os.environ.get("EXECUTION_HEARTBEAT_INTERVAL_SECONDS", "30")),
            memory_log_interval_seconds=int(os.environ.get("EXECUTION_MEMORY_LOG_INTERVAL_SECONDS", "300")),
        )

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigint)

    def close(self) -> None:
        self._stop_event.set()
        self._heartbeat_thread.join(timeout=2)

    def on_execution_created(self, execution_id: int, issue_key: str) -> None:
        with self._lock:
            self._active[execution_id] = ActiveExecution(
                execution_id=execution_id,
                issue_key=issue_key,
                started_monotonic=time.monotonic(),
            )
            if execution_id in self._active_order:
                self._active_order.remove(execution_id)
            self._active_order.append(execution_id)

        self._touch_execution(execution_id, progress=True)
        self.logger.info(
            "Execution #%s (%s) claimed by worker %s",
            execution_id,
            issue_key,
            self.worker_id,
        )

    def on_execution_phase_changed(self, execution_id: int, phase: str, detail: str) -> None:
        with self._lock:
            active = self._active.get(execution_id)
            if active:
                active.phase = phase
                active.detail = detail

    def on_execution_finished(self, execution_id: int, status: str, error_message: Optional[str]) -> None:
        with self._lock:
            self._active.pop(execution_id, None)
            if execution_id in self._active_order:
                self._active_order.remove(execution_id)
        self.logger.info(
            "Execution #%s left active set with status=%s%s",
            execution_id,
            status,
            f" error={error_message[:120]!r}" if error_message else "",
        )

    def mark_active_failed(self, reason: str) -> int:
        return self.mark_active_terminated("failed", reason)

    def mark_active_stopped(self, reason: str) -> int:
        return self.mark_active_terminated("stopped", reason)

    def mark_active_terminated(self, status: str, reason: str) -> int:
        with self._lock:
            execution_ids = list(self._active_order)

        failed = 0
        for execution_id in execution_ids:
            try:
                phase = "stopped" if status == "stopped" else "interrupted"
                db.update_execution_phase(execution_id, phase, reason[:200])
                db.finish_execution(execution_id, status=status, error_message=reason[:500])
                failed += 1
            except Exception as exc:
                self.logger.error(
                    "Could not mark execution #%s %s during shutdown: %s",
                    execution_id,
                    status,
                    exc,
                )
        return failed

    def _handle_sigterm(self, signum, frame) -> None:
        reason = "Worker interrupted by service stop signal"
        failed = self.mark_active_stopped(reason)
        self.logger.warning(
            "Received SIGTERM; marked %d active execution(s) stopped before exit",
            failed,
        )
        raise KeyboardInterrupt()

    def _handle_sigint(self, signum, frame) -> None:
        reason = "Worker interrupted by operator"
        failed = self.mark_active_stopped(reason)
        self.logger.warning(
            "Received SIGINT; marked %d active execution(s) stopped before exit",
            failed,
        )
        raise KeyboardInterrupt()

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_interval_seconds):
            with self._lock:
                execution_ids = list(self._active_order)

            for execution_id in execution_ids:
                try:
                    self._touch_execution(execution_id, progress=False)
                except Exception as exc:
                    self.logger.warning(
                        "Heartbeat update failed for execution #%s: %s",
                        execution_id,
                        exc,
                    )

    def _touch_execution(self, execution_id: int, *, progress: bool) -> None:
        current_rss = _read_process_rss_bytes()
        if current_rss is not None:
            self._peak_memory_rss_bytes = max(self._peak_memory_rss_bytes, current_rss)
        db.touch_execution_liveness(
            execution_id,
            worker_id=self.worker_id,
            worker_pid=self.worker_pid,
            current_memory_rss_bytes=current_rss,
            peak_memory_rss_bytes=self._peak_memory_rss_bytes or current_rss,
            progress=progress,
        )

        now = time.monotonic()
        if current_rss is not None and (
            self._last_memory_log_monotonic == 0.0
            or now - self._last_memory_log_monotonic >= self.memory_log_interval_seconds
        ):
            self._last_memory_log_monotonic = now
            self.logger.info(
                "Worker memory snapshot: rss=%s peak=%s active_executions=%s",
                _format_bytes(current_rss),
                _format_bytes(self._peak_memory_rss_bytes),
                len(self._active_order),
            )


def _read_process_rss_bytes() -> Optional[int]:
    """Return current process RSS in bytes, if available."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except FileNotFoundError:
        pass
    except Exception:
        return None

    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if rss <= 0:
            return None
        if os.uname().sysname == "Darwin":
            return int(rss)
        return int(rss) * 1024
    except Exception:
        return None


def _format_bytes(value: Optional[int]) -> str:
    if not value:
        return "n/a"
    units = ["B", "KiB", "MiB", "GiB"]
    size = float(value)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024 or candidate == units[-1]:
            break
        size /= 1024.0
    return f"{size:.1f}{unit}"
