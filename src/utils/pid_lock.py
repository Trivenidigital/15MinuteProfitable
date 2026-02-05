"""PID lock file management to prevent multiple bot instances."""

from __future__ import annotations

import os
import signal
from pathlib import Path

from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class PidLock:
    """File-based PID lock to prevent duplicate bot instances.

    Usage::

        lock = PidLock("bot.pid")
        if not lock.acquire():
            sys.exit("Another instance is running")
        try:
            run_bot()
        finally:
            lock.release()

    Or as a context manager::

        with PidLock("bot.pid") as lock:
            run_bot()
    """

    def __init__(self, path: str = "btc15minutebot.pid") -> None:
        self._path = Path(path)
        self._acquired = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> bool:
        """Try to acquire the PID lock.

        If a stale lock file exists (the recorded PID is no longer alive),
        it is automatically cleaned up.

        Returns:
            True if the lock was acquired, False if another instance holds it.
        """
        if self._path.exists():
            try:
                existing_pid = int(self._path.read_text().strip())
            except (ValueError, OSError):
                # Corrupted lock file — remove it
                logger.warning("pid_lock_corrupted", path=str(self._path))
                self._path.unlink(missing_ok=True)
            else:
                if _is_process_alive(existing_pid):
                    logger.error(
                        "pid_lock_held",
                        path=str(self._path),
                        pid=existing_pid,
                    )
                    return False
                # Stale lock — previous process died
                logger.info(
                    "pid_lock_stale_removed",
                    path=str(self._path),
                    stale_pid=existing_pid,
                )
                self._path.unlink(missing_ok=True)

        # Write our PID
        self._path.write_text(str(os.getpid()))
        self._acquired = True
        logger.info("pid_lock_acquired", path=str(self._path), pid=os.getpid())
        return True

    def release(self) -> None:
        """Release the PID lock by removing the lock file."""
        if self._acquired and self._path.exists():
            try:
                self._path.unlink()
                logger.info("pid_lock_released", path=str(self._path))
            except OSError as exc:
                logger.warning("pid_lock_release_failed", error=str(exc))
        self._acquired = False

    def __enter__(self) -> PidLock:
        if not self.acquire():
            raise RuntimeError(
                f"Could not acquire PID lock at {self._path}: "
                "another instance is running"
            )
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.release()
